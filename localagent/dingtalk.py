import asyncio
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .db import now

CST = timezone(timedelta(hours=8))


def _iso(dt):
    return dt.astimezone(CST).isoformat(timespec="seconds")


def _fmt_list_time(dt):
    return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


class MockDingTalk:
    """mock 模式：不连真实钉钉；回复写入审计与 conn_state 供管理页面展示。"""

    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db

    async def start(self):
        self.db.set_state("dingtalk_conn", "mock")
        self.db.audit("dingtalk", "conn_started", "", "mock mode")

    def reply(self, group, markdown):
        self.db.audit("dingtalk", "reply_sent", group, markdown[:300])
        self.db.set_state("last_reply", f"{group}: {markdown[:120]}")
        return True

    def reply_private(self, user_id, markdown):
        self.db.audit("dingtalk", "reply_sent_private", f"private:{user_id}", markdown[:300])
        self.db.set_state("last_reply", f"私聊:{user_id}: {markdown[:120]}")
        return True


class DwsDingTalk:
    """dws CLI 模式：list-mentions/list 轮询接收，chat message send 发送。"""

    def __init__(self, cfg, db, on_message):
        self.cfg = cfg
        self.db = db
        self.on_message = on_message
        dt_cfg = cfg.dingtalk
        self.poll_seconds = int(dt_cfg.get("poll_seconds", 20))
        self.backfill_hours = int(dt_cfg.get("backfill_hours", 1))
        self.send_channel = dt_cfg.get("send", "dws")
        self.groups = {g.get("name"): g for g in cfg.groups}

    async def start(self):
        self.db.set_state("dingtalk_conn", "dws_polling")
        self.db.audit("dingtalk", "conn_started", "", "dws polling mode")
        last = self.db.get_state("dws_last_poll")
        err_since = None
        down_alert_at = None
        while True:
            ok = False
            # 失败快速重试：瞬时网络错误 10s 退避重试，避免干等下一轮扩大采集空窗
            for attempt in range(3):
                try:
                    await self.poll_once(last)
                    # dws 接口吐消息延迟可达 15 分钟以上：游标回退 30 分钟安全余量，
                    # 避免迟到的消息因 createTime 早于游标被永久漏采；
                    # msg_id 去重保证重叠窗口不会重复处理
                    last = (datetime.now(CST) - timedelta(minutes=30)).isoformat(timespec="seconds")
                    self.db.set_state("dws_last_poll", last)
                    self.db.set_state("dingtalk_conn", "dws_polling")
                    ok = True
                    break
                except Exception as e:
                    self.db.set_state("dingtalk_conn", "dws_error")
                    self.db.audit("dingtalk", "poll_failed", "", str(e)[:200])
                    if attempt < 2:
                        await asyncio.sleep(10)
            now_dt = datetime.now(CST)
            if ok:
                err_since = None
                down_alert_at = None
            else:
                # 断采提醒：持续失败超 5 分钟显式提醒，之后每 10 分钟提醒一次
                if err_since is None:
                    err_since = now_dt
                if (now_dt - err_since) >= timedelta(minutes=5) and (
                        down_alert_at is None
                        or (now_dt - down_alert_at) >= timedelta(minutes=10)):
                    mins = int((now_dt - err_since).total_seconds() // 60)
                    self.db.set_state("dingtalk_conn", "dws_down")
                    self.db.set_state("pet_toast", f"⚠ 钉钉采集中断已 {mins} 分钟，请检查网络/dws")
                    self.db.set_state("pet_toast_ts", now())
                    self.db.audit("dingtalk", "poll_down_alert", "", f"down {mins}min")
                    down_alert_at = now_dt
            await asyncio.sleep(self.poll_seconds)

    async def _dws(self, argv):
        proc = await asyncio.create_subprocess_exec(
            "dws", *argv, "--format", "json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"dws {argv[1:3]} rc={proc.returncode}: {err.decode()[:200]}")
        return json.loads(out.decode() or "{}")

    async def poll_once(self, last_iso):
        end = datetime.now(CST)
        start = (datetime.fromisoformat(last_iso) if last_iso
                 else end - timedelta(hours=self.backfill_hours))
        hits = 0
        data = await self._dws(["chat", "message", "list-mentions",
                                "--start", _iso(start), "--end", _iso(end),
                                "--limit", "50"])
        for conv in (data.get("result", {}).get("conversationMessagesList") or []):
            title = conv.get("title") or "unknown"
            for m in conv.get("messages") or []:
                if await self._dispatch(title, m, at_me=True):
                    hits += 1
        for name, g in self.groups.items():
            if not g.get("id") or not g.get("enabled", True) or g.get("mode") not in ("alert", "both"):
                continue
            data = await self._dws(["chat", "message", "list", "--group", g["id"],
                                    "--time", _fmt_list_time(start),
                                    "--direction", "newer", "--limit", "50"])
            for m in (data.get("result", {}).get("messages") or
                      data.get("result", {}).get("conversationMessagesList", [{}])[0].get("messages") or []):
                if await self._dispatch(name, m, at_me=False):
                    hits += 1
        if self.cfg.dingtalk.get("private_collect", False):
            hits += await self._poll_private(start, end)
        if hits:
            self.db.set_state("dws_last_hit", f"{hits} @ {now()}")
        return hits

    async def _poll_private(self, start, end):
        """私聊采集（spike 已验证 dws list-all 可拉全量会话消息）：
        跳过已配置群聊会话与自己发送的消息，其余单聊消息按私聊分发。"""
        hits = 0
        try:
            data = await self._dws(["chat", "message", "list-all",
                                    "--start", _fmt_list_time(start),
                                    "--end", _fmt_list_time(end),
                                    "--limit", "50"])
        except Exception as e:
            self.db.audit("dingtalk", "private_poll_failed", "", str(e)[:200])
            return 0
        group_conv_ids = {g.get("id") for g in self.groups.values() if g.get("id")}
        self_name = self.cfg.dingtalk.get("owner_name", "")
        for conv in (data.get("result", {}).get("conversationMessagesList") or []):
            conv_id = conv.get("openConversationId") or conv.get("conversationId") or ""
            if conv_id and conv_id in group_conv_ids:
                continue  # 群聊由群轮询处理
            for m in conv.get("messages") or []:
                sender = m.get("sender") or ""
                if self_name and sender == self_name:
                    continue  # 自己发的消息不采集
                if not (m.get("senderOpenDingTalkId") or m.get("senderId")):
                    continue  # 无发送者 ID 无法作为回复目标
                msg = {"msg_id": m.get("openMessageId") or m.get("messageId") or "",
                       "group": f"私聊:{sender}", "sender": sender,
                       "text": m.get("content") or "",
                       "msg_time": m.get("createTime") or "",
                       "at_me": True, "conv_type": "private",
                       "conv_id": conv_id,
                       "reply_target": m.get("senderOpenDingTalkId") or m.get("senderId") or ""}
                if not msg["msg_id"] or self.db.one(
                        "SELECT 1 FROM messages WHERE msg_id=?", msg["msg_id"]):
                    continue
                if await self.on_message(msg):
                    hits += 1
        return hits

    async def _dispatch(self, group, m, at_me):
        msg = {"msg_id": m.get("openMessageId") or m.get("messageId") or "",
               "group": group,
               "sender": m.get("sender") or "unknown",
               "text": m.get("content") or "",
               "msg_time": m.get("createTime") or "",
               "at_me": at_me}
        if not msg["msg_id"] or self.db.one("SELECT 1 FROM messages WHERE msg_id=?", msg["msg_id"]):
            return False
        await self.on_message(msg)
        return True

    def _group_id(self, group):
        g = self.groups.get(group)
        return g.get("id") if g else None

    def reply(self, group, markdown):
        """同步发送，返回是否投递成功；失败/超时必留 reply_failed 审计。"""
        text = markdown[:4000]
        ok = False
        if self.send_channel == "dws":
            gid = self._group_id(group)
            if not gid:
                self.db.audit("dingtalk", "reply_skipped", group, "no conversationId configured")
                self.db.set_state("last_reply", f"{group}: {text[:120]}")
                return False
            import subprocess
            err = ""
            for attempt in (1, 2):
                try:
                    r = subprocess.run(["dws", "chat", "message", "send", "--group", gid,
                                        "--title", "LocalAgent", "--text", text, "-y"],
                                       capture_output=True, timeout=30)
                    if r.returncode == 0:
                        ok = True
                        err = (r.stdout or b"").decode()[:200]
                        break
                    err = f"rc={r.returncode}: " + (r.stderr or r.stdout or b"").decode()[:160]
                except subprocess.TimeoutExpired:
                    err = "dws send 超时(30s)"
                    break
                except Exception as e:
                    err = f"dws send 异常: {e}"[:200]
                    break
            self.db.audit("dingtalk", "reply_sent" if ok else "reply_failed", group, err)
        else:
            ok = self._reply_webhook(group, text)
        self.db.set_state("last_reply", f"{group}: {text[:120]}")
        return ok

    def reply_private(self, user_id, markdown):
        """私聊发送（spike 已验证 dws chat message send --user 可用）。失败/超时必留审计。"""
        import subprocess
        text = markdown[:4000]
        if not user_id:
            self.db.audit("dingtalk", "reply_skipped", "private", "no reply_target userId")
            return False
        if self.send_channel != "dws":
            self.db.audit("dingtalk", "reply_skipped", "private", "webhook 不支持私聊")
            return False
        ok, err = False, ""
        for attempt in (1, 2):
            try:
                r = subprocess.run(["dws", "chat", "message", "send", "--user", user_id,
                                    "--title", "LocalAgent", "--text", text, "-y"],
                                   capture_output=True, timeout=30)
                if r.returncode == 0:
                    ok = True
                    err = (r.stdout or b"").decode()[:200]
                    break
                err = f"rc={r.returncode}: {(r.stderr or r.stdout or b'').decode()[:160]}"
            except subprocess.TimeoutExpired:
                err = "dws 私聊发送超时(30s)"
                break
            except Exception as e:
                err = f"dws 私聊发送异常: {e}"[:200]
                break
        self.db.audit("dingtalk", "reply_sent_private" if ok else "reply_failed",
                      f"private:{user_id}", err)
        self.db.set_state("last_reply", f"私聊:{user_id}: {text[:100]}")
        return ok

    def _reply_webhook(self, group, text):
        url = self.cfg.dingtalk.get("webhook_url", "")
        secret = self.cfg.dingtalk.get("webhook_secret", "")
        if not url:
            self.db.audit("dingtalk", "reply_skipped", group, "no webhook_url configured")
            return False
        if secret:
            ts = str(round(time.time() * 1000))
            sign = urllib.parse.quote_plus(base64.b64encode(hmac.new(
                secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()))
            url += f"&timestamp={ts}&sign={sign}"
        body = json.dumps({"msgtype": "markdown",
                           "markdown": {"title": "LocalAgent", "text": text}}).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    self.db.audit("dingtalk", "reply_sent", group, resp.read().decode()[:200])
                return True
            except Exception as e:
                if attempt == 2:
                    self.db.audit("dingtalk", "reply_failed", group, str(e)[:200])
        return False


def build(cfg, db, on_message=None):
    if cfg.mock or cfg.dingtalk.get("mode", "dws") == "mock":
        return MockDingTalk(cfg, db)
    return DwsDingTalk(cfg, db, on_message)
