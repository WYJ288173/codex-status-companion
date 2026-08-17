"""V6 回归：发送回复链路加固 + 报警类型自动回复白名单。
覆盖：DB 并发安全、dws 发送超时/失败如实流转、白名单门控矩阵、自动失败转人工、前端错误处理。
运行：./.venv/bin/python tests/test_v6_reply_gate.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from localagent import dingtalk as dingmod
from localagent.db import DB, now
from localagent.notify import Notifier
from localagent.pipeline import Pipeline

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


ws = tempfile.mkdtemp()
db = DB(os.path.join(ws, "t.sqlite"))


class FakeCfg:
    workspace = ws
    agent = {}
    notify = {}
    dingtalk = {"reply_enabled": True}
    auth_entries = []
    groups = []
    mock = True


notifier = Notifier(FakeCfg(), db)

# ---------- 1. DB 并发安全（共享连接多线程读写不得抛异常） ----------
errors = []


def worker(tid):
    try:
        for i in range(40):
            db.audit("t", f"op{tid}", str(i), f"thread-{tid}-{i}")
            db.q("SELECT * FROM audit_logs ORDER BY log_id DESC LIMIT 5")
            db.one("SELECT COUNT(*) c FROM audit_logs")
            db.set_state(f"k{tid}", str(i))
            [dict(r) for r in db.q("SELECT * FROM audit_logs LIMIT 3")]
    except Exception as e:
        errors.append(f"{tid}: {type(e).__name__} {e}")


threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
[t.start() for t in threads]
[t.join() for t in threads]
check("DB 8线程×40轮并发读写无异常", not errors, "; ".join(errors[:3]))

# ---------- 2. DwsDingTalk.reply 失败路径 ----------
class FakeDB:
    def __init__(self):
        self.audits = []
        self.states = {}
    def audit(self, category, action, target="", detail="", run_id=None):
        self.audits.append((action, target, detail))
    def set_state(self, key, value):
        self.states[key] = value


class TimeoutCfg:
    dingtalk = {"send": "dws"}
    groups = [{"name": "g1", "id": "cid123"}]


fd = FakeDB()
dt = dingmod.DwsDingTalk(TimeoutCfg(), fd, on_message=None)
orig_run = subprocess.run


def run_timeout(*a, **kw):
    raise subprocess.TimeoutExpired(cmd="dws", timeout=30)


subprocess.run = run_timeout
try:
    ok = dt.reply("g1", "测试超时")
finally:
    subprocess.run = orig_run
check("dws 超时返回 False", ok is False, str(ok))
check("dws 超时留 reply_failed 审计",
      any(a[0] == "reply_failed" and "超时" in a[2] for a in fd.audits), str(fd.audits))

fd2 = FakeDB()
dt2 = dingmod.DwsDingTalk(TimeoutCfg(), fd2, on_message=None)
ok2 = dt2.reply("未配置群", "测试无gid")
check("无会话ID返回 False", ok2 is False, str(ok2))
check("无会话ID留 reply_skipped 审计",
      any(a[0] == "reply_skipped" for a in fd2.audits), str(fd2.audits))


class RcFail:
    returncode = 1
    stdout = b""
    stderr = b"boom"


fd3 = FakeDB()
dt3 = dingmod.DwsDingTalk(TimeoutCfg(), fd3, on_message=None)
calls = []
subprocess.run = lambda *a, **kw: (calls.append(1), RcFail())[1]
try:
    ok3 = dt3.reply("g1", "测试rc失败")
finally:
    subprocess.run = orig_run
check("rc!=0 重试一次后返回 False", ok3 is False and len(calls) == 2, f"ok={ok3} calls={len(calls)}")
check("rc!=0 留 reply_failed 审计", any(a[0] == "reply_failed" for a in fd3.audits), str(fd3.audits))

# ---------- 3. 白名单门控矩阵 ----------
reply_entry = {"id": "dt-reply", "app": "dingtalk", "scope": "write",
               "feature": "回复分析结论到值班群",
               "constraints": {"groups": ["g1"]}, "enabled": True}


class GateCfg:
    workspace = ws
    agent = {"writes_disabled": False}
    notify = {}
    dingtalk = {"reply_enabled": True}
    auth_entries = [reply_entry]
    groups = []
    mock = True


class OkDing:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []
    def reply(self, g, t):
        self.calls.append((g, t))
        return self.ok


result = {"summary": "s", "anomalies": [{"severity": "P2", "summary": "x"}]}

GateCfg.groups = [{"name": "g1", "auto_reply_types": ["验价", "验座"]}]
p = Pipeline(GateCfg(), db, notifier, OkDing())
check("白名单命中放行", p._group_auto_reply("g1", "验价") is True)
check("白名单未命中拦截", p._group_auto_reply("g1", "生单") is False)
check("unclassified 永不自动", p._group_auto_reply("g1", None) is False)
GateCfg.groups[0]["auto_reply"] = True
GateCfg.groups[0]["auto_reply_types"] = []
check("遗留 auto_reply=true 不再直通", p._group_auto_reply("g1", "验价") is False)
del GateCfg.groups[0]["auto_reply"]
check("未配置类型不自动", p._group_auto_reply("g1", "验价") is False)
check("未配置群不自动", p._group_auto_reply("gX", "验价") is False)

# ---------- 4. 自动回复失败转人工 + payload 带 alert_type ----------
GateCfg.groups[0]["auto_reply_types"] = ["验价"]
p_fail = Pipeline(GateCfg(), db, notifier, OkDing(ok=False))
p_fail._reply_if_allowed("g1", result, "run-v6-auto-fail", source_text="验价失败报警")
row_af = db.one("SELECT * FROM auth_exec WHERE run_id='run-v6-auto-fail'")
check("自动发送失败转 pending_reply", row_af["exec_result"] == "pending_reply", row_af["exec_result"])
check("自动失败留 reply_auto_failed 审计",
      db.one("SELECT 1 FROM audit_logs WHERE action='reply_auto_failed' AND run_id='run-v6-auto-fail'") is not None)
pl_af = json.loads(row_af["payload"])
check("payload 带 alert_type", pl_af.get("alert_type") == "验价", str(pl_af.get("alert_type")))

GateCfg.groups[0]["auto_reply_types"] = ["验价", "验座"]
p_ok = Pipeline(GateCfg(), db, notifier, OkDing(ok=True))
p_ok._reply_if_allowed("g1", result, "run-v6-auto-ok", source_text="验座失败报警")
row_ao = db.one("SELECT * FROM auth_exec WHERE run_id='run-v6-auto-ok'")
check("白名单命中且发送成功 → replied", row_ao["exec_result"] == "replied", row_ao["exec_result"])
check("自动成功留 reply_auto 审计",
      db.one("SELECT 1 FROM audit_logs WHERE action='reply_auto' AND run_id='run-v6-auto-ok'") is not None)

# ---------- 5. 前端与页面源码断言 ----------
src = open(os.path.join(os.path.dirname(__file__), "..", "localagent", "webapp.py"),
           encoding="utf-8").read()
check("前端 sendReply 有 r.ok 检查", "async function jfetch" in src and "if(!r.ok)" in src)
check("前端 sendReply 有 try/catch", "catch(e){alert('发送失败" in src)
check("groups 页 checkbox 编辑器", "gsaveTypes" in src and "input type='checkbox'" in src)
check("groups 页已移除全类型放行", "全类型放行" not in src)
check("groups 页已移除 prompt 式设置", "gsetTypes" not in src)
check("端点 send_reply 异常兜底", "send_reply_error" in src)
check("卡片展示 alert_type 徽标", "pl.get('alert_type')" in src)

print(f"\n全部 {len(PASS)} 项断言通过")
