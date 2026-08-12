"""V3 回归：引擎可用性治理（模型级降级 / 资源不可用解耦 / 一键重跑 / 探针 / 后台呈现）。
运行：./.venv/bin/python tests/test_v3_engine_availability.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from localagent import engine
from localagent.db import DB, now
from localagent.pipeline import Pipeline

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


QUOTA_TXT = ("You've reached your credit usage limit. Please upgrade your subscription plan "
             "to get more resources.")
OK_JSON = '{"normal": true, "conclusion": "ok", "evidence": [{"action": "a", "finding": "f"}]}'


class Cfg:
    """引擎链 qodercli(默认→M1→M2) → codex；命令用 /bin/echo 桩，模型名决定输出。"""
    mock = False
    workspace = tempfile.mkdtemp()
    agent = {"writes_disabled": False}
    notify = {"cooldown_seconds": 0}
    dingtalk = {"listen_all": True}
    solutions = []
    groups = [{"name": "改签监控群", "mode": "alert"}]
    auth_entries = [{"id": "read-g", "app": "dingtalk", "scope": "read",
                     "feature": "读取群消息", "enabled": True,
                     "constraints": {"groups": ["改签监控群"]},
                     "alertRules": [{"type": "keyword", "keywords": ["报警"]}]}]

    def __init__(self, outputs):
        """outputs: {模型名或 'default' 或 'codex': 该模型的 stdout}"""
        self.outputs = outputs
        self.engines = {"default": "qodercli", "fallback": ["codex"], "list": [
            {"name": "qodercli", "cmd": ["/bin/echo", "{prompt}"],
             "model_fallback": ["M1", "M2"]},
            {"name": "codex", "cmd": ["/bin/echo", "{prompt}"]}]}

    def engine_cmd(self, name):
        return next((e["cmd"] for e in self.engines["list"] if e["name"] == name), None)


async def fake_run(cfg, name, prompt, db=None, run_id=None, model=None, timeout=900,
                   stats=None):
    """替换真实子进程：按 引擎/模型 返回预设输出，走真实的 JSON 提取与资源不可用判定。"""
    key = "codex" if name == "codex" else (model or "default")
    raw = cfg.outputs.get(key, "")
    if stats is not None and getattr(cfg, "stderr_warns", None) is not None:
        w = engine.parse_skill_warnings(cfg.stderr_warns)
        if w is not None:
            stats["skill_warnings"] = w
    result = engine._extract_json(raw)
    if result is None:
        tag = f"{name}/{model or 'default'}"
        if db:
            db.audit("engine", "raw_output", tag, raw[:200], run_id)
        if any(h in raw.lower() for h in engine.RESOURCE_HINTS):
            raise engine.EngineUnavailable(f"{tag} 资源不可用（额度/限流/鉴权）：{raw[:80]}")
        raise RuntimeError(f"{tag} 输出无有效 JSON")
    return result


engine._run_engine = fake_run  # 全局桩，避免真实调用 CLI

# ---------- 1. 资源不可用判定与模型链 ----------
check("额度文案判为资源不可用", any(h in QUOTA_TXT.lower() for h in engine.RESOURCE_HINTS))
check("鉴权文案判为资源不可用", any(h in "Unauthorized: please login".lower()
                                for h in engine.RESOURCE_HINTS))
c0 = Cfg({})
check("模型链默认模型在首位", engine.engine_models(c0, "qodercli") == [None, "M1", "M2"],
      str(engine.engine_models(c0, "qodercli")))
check("未配置模型的引擎返回 [None]", engine.engine_models(c0, "codex") == [None])
check("引擎链 default 优先", engine.engine_chain(c0) == ["qodercli", "codex"])


def new_db():
    return DB(os.path.join(tempfile.mkdtemp(), "t.sqlite"))


def audits(db, action):
    return db.q("SELECT * FROM audit_logs WHERE action=?", action)


# ---------- 2. 默认模型额度耗尽 → 同引擎换模型成功 ----------
db = new_db()
cfg = Cfg({"default": QUOTA_TXT, "M1": OK_JSON})
res, eng, ver = asyncio.run(engine.analyze(cfg, db, {"text": "报警", "extra": {}}, "run-a"))
check("默认模型不可用后换模型成功", res["normal"] is True and eng == "qodercli" and ver == "M1",
      f"{eng}/{ver}")
check("写 model_downgraded 审计", len(audits(db, "model_downgraded")) >= 1)
check("写 engine_resource_unavailable 审计", len(audits(db, "engine_resource_unavailable")) == 1)

# ---------- 3. 全模型耗尽 → 切 codex 引擎 ----------
db = new_db()
cfg = Cfg({"default": QUOTA_TXT, "M1": QUOTA_TXT, "M2": QUOTA_TXT, "codex": OK_JSON})
res, eng, ver = asyncio.run(engine.analyze(cfg, db, {"text": "报警", "extra": {}}, "run-b"))
check("同引擎模型全耗尽后切 codex", eng == "codex", f"{eng}/{ver}")
check("记录模型耗尽", len(audits(db, "engine_models_exhausted")) >= 1)
check("记录引擎降级", len(audits(db, "engine_downgraded")) >= 1)

# ---------- 4. 全部资源不可用 → EngineUnavailable（非分析失败） ----------
db = new_db()
cfg = Cfg({"default": QUOTA_TXT, "M1": QUOTA_TXT, "M2": QUOTA_TXT, "codex": QUOTA_TXT})
try:
    asyncio.run(engine.analyze(cfg, db, {"text": "报警", "extra": {}}, "run-c"))
    check("全不可用抛 EngineUnavailable", False)
except engine.EngineUnavailable as e:
    check("全不可用抛 EngineUnavailable", "资源不可用" in str(e) or "不可用" in str(e), str(e)[:80])
except Exception as e:
    check("全不可用抛 EngineUnavailable", False, f"got {type(e).__name__}: {e}")

# ---------- 5. 真实分析失败仍抛普通错误（不被误判为资源不可用） ----------
db = new_db()
cfg = Cfg({"default": "垃圾输出没有 JSON", "M1": "还是垃圾", "M2": "垃圾", "codex": "垃圾"})
try:
    asyncio.run(engine.analyze(cfg, db, {"text": "报警", "extra": {}}, "run-d"))
    check("非资源类失败抛普通异常", False)
except engine.EngineUnavailable:
    check("非资源类失败抛普通异常", False, "误判为资源不可用")
except Exception as e:
    check("非资源类失败抛普通异常", "无有效 JSON" in str(e), str(e)[:80])

# ---------- 6. pipeline：资源不可用不写报告、不产报警、状态可重试 ----------
class Notif:
    def __init__(self):
        self.toasts = []
        self.alerts = []

    def toast(self, t):
        self.toasts.append(t)

    def raise_alerts(self, run_id, group, anomalies):
        self.alerts.append((run_id, anomalies))


class Ding:
    def reply(self, *a, **k):
        pass


db = new_db()
cfg = Cfg({"default": QUOTA_TXT, "M1": QUOTA_TXT, "M2": QUOTA_TXT, "codex": QUOTA_TXT})
n = Notif()
p = Pipeline(cfg, db, n, Ding())
ALERT = "【报警】change-flight-tp 改签 成功率下跌 P2"
r = asyncio.run(p.process({"msg_id": "v3-1", "group": "改签监控群", "sender": "sunfire",
                           "text": ALERT, "at_me": False}))
check("返回 engine_unavailable", r.get("reason") == "engine_unavailable" and r.get("run_id"), str(r))
run = db.one("SELECT * FROM runs WHERE run_id=?", r["run_id"])
check("run 状态 engine_unavailable", run["status"] == "engine_unavailable", run["status"])
check("不写报告", run["report_path"] is None and db.one("SELECT COUNT(*) c FROM reports_meta")["c"] == 0)
check("不产生 P3 假报警", n.alerts == [] and db.one("SELECT COUNT(*) c FROM alerts")["c"] == 0,
      str(n.alerts))
check("提示为可重试而非失败", any("可重试" in t for t in n.toasts), str(n.toasts))
check("消息保留可供重跑",
      db.one("SELECT source_text FROM messages WHERE run_id=?", r["run_id"])["source_text"] == ALERT)
check("写 engine_unavailable_run 审计", len(audits(db, "engine_unavailable_run")) == 1)

# ---------- 7. 一键重跑：额度恢复后按原始消息重新分析成功 ----------
cfg.outputs["M1"] = OK_JSON  # 模拟 dogfooding 模型可用
r2 = asyncio.run(p.rerun(r["run_id"]))
check("重跑成功产出分析", r2 and r2.get("handled") and r2.get("run_id") != r["run_id"], str(r2))
run2 = db.one("SELECT * FROM runs WHERE run_id=?", r2["run_id"])
check("重跑 run 成功且记录命中模型", run2["status"] == "success"
      and run2["engine_version"] == "M1", f"{run2['status']}/{run2['engine_version']}")
check("重跑写报告", run2["report_path"] is not None)
check("写 rerun_submitted 审计", len(audits(db, "rerun_submitted")) == 1)
check("重跑不存在时返回 None", asyncio.run(p.rerun("run-not-exist")) is None)

# ---------- 8. 探针：逐引擎逐模型记录可用性 ----------
db = new_db()
cfg = Cfg({"default": QUOTA_TXT, "M1": OK_JSON, "M2": OK_JSON, "codex": "垃圾"})
out = asyncio.run(engine.probe_engines(cfg, db))
check("探针覆盖引擎×模型", set(out) == {"qodercli/default", "qodercli/M1", "qodercli/M2",
                                    "codex/default"}, str(out))
check("探针区分资源不可用与失败",
      out["qodercli/default"].startswith("资源不可用") and out["qodercli/M1"] == "ok"
      and out["codex/default"].startswith("失败"), str(out))
saved = json.loads(db.get_state("engine_probe", "{}"))
check("探针结果落 conn_state", saved == out and db.get_state("engine_probe_at"))

# ---------- 8b. skill 配置告警：解析 / 落库 / 状态页 ----------
check("解析 skill 告警数",
      engine.parse_skill_warnings("2 warnings loading skill configs. Use /skills to see details.") == 2)
check("单数形式也可解析", engine.parse_skill_warnings("1 warning loading skill configs") == 1)
check("无告警返回 None", engine.parse_skill_warnings("all good") is None
      and engine.parse_skill_warnings("") is None)
db_w = new_db()
cfg_w = Cfg({"default": OK_JSON, "M1": OK_JSON, "M2": OK_JSON, "codex": OK_JSON})
cfg_w.stderr_warns = "2 warnings loading skill configs. Use /skills to see details."
asyncio.run(engine.probe_engines(cfg_w, db_w))
check("探针落库 skill 告警数", db_w.get_state("skill_config_warnings") == "2",
      str(db_w.get_state("skill_config_warnings")))
check("有告警时写审计", len(audits(db_w, "skill_config_warnings")) == 1)
db_ok = new_db()
cfg_ok = Cfg({"default": OK_JSON, "M1": OK_JSON, "M2": OK_JSON, "codex": OK_JSON})
cfg_ok.stderr_warns = "no warnings here"
asyncio.run(engine.probe_engines(cfg_ok, db_ok))
check("无告警提示时不落库", db_ok.get_state("skill_config_warnings") is None)

# ---------- 9. 后台呈现：状态页与历史页 ----------
from localagent.webapp import build_app


class Ctx:
    pass


ctx = Ctx()
ctx.db = db
ctx.cfg = cfg
ctx.cfg.web = {"host": "127.0.0.1", "port": 8765}
ctx.pipeline = p
ctx.ding = Ding()


class N2(Notif):
    def pending(self):
        return []


ctx.notifier = N2()
db.insert("runs", run_id="run-unavail", task_id="t", trigger_type="dingtalk_alert",
          source="改签监控群", status="engine_unavailable", engine=None, engine_version=None,
          started_at=now(), finished_at=now(), report_path=None,
          error_msg="qodercli 资源不可用", source_text=ALERT)
app = build_app(ctx)
ep = {}
for rt in app.routes:
    if hasattr(rt, "endpoint"):
        ep.setdefault(getattr(rt, "path", ""), rt.endpoint)
st = ep["/"]().body.decode()
check("状态页展示引擎/模型探针", "引擎/模型探针" in st and "qodercli/M1=ok" in st)
check("状态页展示 Skill 配置告警行", "Skill 配置告警" in st)
check("状态页展示可重试计数与入口", "引擎资源不可用（可重试）" in st
      and "status_f=engine_unavailable" in st)
hi = ep["/history"](days=7, status_f="engine_unavailable").body.decode()
check("历史页可筛 engine_unavailable", "run-unavail" in hi)
check("历史页提供重跑按钮", "rerun('run-unavail')" in hi and "/api/rerun/" in hi)
check("重跑提示区别于失败重试", "引擎资源不可用" in hi)

print(f"\n全部 {len(PASS)} 项断言通过")
