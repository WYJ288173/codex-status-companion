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


class DwsDingTalk:
    """dws CLI 模式：list-mentions/list 轮询接收，chat message send 发送。"""

    def __init__(self, cfg, db, on_message):
        self.cfg = cfg
        self.db = db
        self.on_message = on_message
        dt_cfg = cfg.dingtalk
        self.poll_seconds = int(dt_cfg.get("poll_seconds", 60))
        self.backfill_hours = int(dt_cfg.get("backfill_hours", 1))
        self.send_channel = dt_cfg.get("send", "dws")
        self.groups = {g.get("name"): g for g in cfg.groups}

    async def start(self):
        self.db.set_state("dingtalk_conn", "dws_polling")
        self.db.audit("dingtalk", "conn_started", "", "dws polling mode")
        last = self.db.get_state("dws_last_poll")
        while True:
            try:
                await self.poll_once(last)
                last = now()
                self.db.set_state("dws_last_poll", last)
                self.db.set_state("dingtalk_conn", "dws_polling")
            except Exception as e:
                self.db.set_state("dingtalk_conn", "dws_error")
                self.db.audit("dingtalk", "poll_failed", "", str(e)[:200])
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
        if hits:
            self.db.set_state("dws_last_hit", f"{hits} @ {now()}")
        return hits

    async def _dispatch(self, group, m, at_me):
        msg = {"msg_id": m.get("openMessageId") or m.get("messageId") or "",
               "group": group,
               "sender": m.get("sender") or "unknown",
               "text": m.get("content") or "",
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
