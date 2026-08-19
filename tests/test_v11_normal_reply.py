"""V11 回归：无问题结论自动回群简短确认（reply_on_normal）。
运行：./.venv/bin/python tests/test_v11_normal_reply.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from localagent.db import DB
from localagent.notify import Notifier
from localagent.pipeline import Pipeline

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


TZ8 = timezone(timedelta(hours=8))
read_entry = {"id": "read-g1", "app": "dingtalk", "scope": "read", "enabled": True,
              "feature": "读取群消息", "constraints": {"groups": ["g1"]},
              "alertRules": [{"type": "keyword", "keywords": ["报警"]}]}
reply_entry = {"id": "dt-reply", "app": "dingtalk", "scope": "write",
               "feature": "回复分析结论到值班群",
               "constraints": {"groups": ["g1"]}, "enabled": True}


def alert_text(ts, order):
    return (f"{ts.strftime('%Y/%m/%d %H:%M')}\n华扬\nchange-flight-tp\n"
            "改签底座-验价指标\n失败量\n共有1条数据触发[warning]报警，摘要：\n"
            f"* [国内,3487] 失败量 [当前值为: 3]\n订单{order}验价失败，已值机业务拦截\n")


class FakeDing:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def reply(self, g, t):
        self.calls.append((g, t))
        return self.ok


def make_pipe(ws, ding, ding_cfg_extra=None):
    db = DB(os.path.join(ws, "t.sqlite"))

    class Cfg:
        workspace = ws
        agent = {}
        notify = {}
        dingtalk = {"reply_enabled": True, "listen_all": True}
        auth_entries = [read_entry, reply_entry]
        groups = []
        solutions = []
        mock = True

    if ding_cfg_extra:
        Cfg.dingtalk.update(ding_cfg_extra)
    cfg = Cfg()
    return cfg, db, Pipeline(cfg, db, Notifier(cfg, db), ding)


ts = datetime.now(TZ8).replace(second=0, microsecond=0) - timedelta(minutes=2)
msg = {"msg_id": "n1", "group": "g1", "sender": "sunfire",
       "text": alert_text(ts, "9985903664559"), "at_me": False}

# ---------- 1. 默认开启：无问题结论自动回群确认 ----------
ws1 = tempfile.mkdtemp()
ding1 = FakeDing(ok=True)
_, db1, p1 = make_pipe(ws1, ding1)
r = asyncio.run(p1.process(dict(msg)))
check("无问题结论正常完成", r.get("handled") and r.get("normal") is True, str(r))
check("无问题自动回群 1 条", len(ding1.calls) == 1, str(len(ding1.calls)))
md = ding1.calls[0][1]
check("确认回复含「已确认…无需处理」", "已确认：" in md and "无需处理" in md, md)
check("确认回复带报警身份头", "> 报警：改签底座-验价指标 · 失败量｜change-flight-tp" in md, md)
check("确认回复无空行", "\n\n" not in md, repr(md[:100]))
check("留 replied 记录",
      db1.one("SELECT exec_result FROM auth_exec WHERE entry_id='dt-reply'")["exec_result"] == "replied")
check("留 reply_auto_normal 审计",
      db1.one("SELECT 1 FROM audit_logs WHERE action='reply_auto_normal'") is not None)

# ---------- 2. 开关关闭：不回群 ----------
ws2 = tempfile.mkdtemp()
ding2 = FakeDing(ok=True)
_, db2, p2 = make_pipe(ws2, ding2, {"reply_on_normal": False})
asyncio.run(p2.process(dict(msg, msg_id="n2")))
check("reply_on_normal=false 不回群", not ding2.calls, str(ding2.calls))
check("关闭时留 reply_normal_skipped 审计",
      db2.one("SELECT 1 FROM audit_logs WHERE action='reply_normal_skipped'") is not None)

# ---------- 3. 发送失败：转 pending_reply 供人工补发 ----------
ws3 = tempfile.mkdtemp()
ding3 = FakeDing(ok=False)
_, db3, p3 = make_pipe(ws3, ding3)
asyncio.run(p3.process(dict(msg, msg_id="n3")))
row = db3.one("SELECT exec_result, payload FROM auth_exec WHERE entry_id='dt-reply'")
check("发送失败转 pending_reply", row["exec_result"] == "pending_reply", str(row["exec_result"]))
check("留 reply_normal_failed 审计",
      db3.one("SELECT 1 FROM audit_logs WHERE action='reply_normal_failed'") is not None)

print(f"\n{sum(1 for _, ok in PASS if ok)}/{len(PASS)} passed")
