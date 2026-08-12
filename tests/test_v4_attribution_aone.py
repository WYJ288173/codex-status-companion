"""V4 回归：归因深度（判定矩阵进提示词）+ 一键创建 Aone 技术需求闭环。
运行：./.venv/bin/python tests/test_v4_attribution_aone.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from localagent import engine, aone, reports
from localagent.db import DB, now

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


# ---------- 1. 归因判定矩阵进入提示词 ----------
T = engine.PROMPT_TEMPLATE
check("归因以【外部域问题】/【域内问题】开头", "conclusion 必须以归因开头" in T)
check("第一步取证外部返回原文", "外部返回原文摘录" in T)
check("外部失败分支：不应返回失败 vs 数据缺失", '"不应返回的失败"' in T and "返回数据缺失" in T)
check("外部成功分支：兼容性缺陷 vs 逻辑性漏洞", "兼容性缺陷" in T and "逻辑性漏洞" in T)
check("取证缺口规则：归因待定", "归因待定" in T)
check("建议不替外部域做决策", "不替外部域做决策" in T)
check("tech_requirement 要求类/模块级修复方向", "类/模块级" in T)

# ---------- 2. 测试环境：带 tech_requirement 建议的报告 ----------
ws = tempfile.mkdtemp()


class Cfg:
    mock = True
    workspace = ws
    agent = {"aone": {"project": "退改统一域需求空间"}}
    engines = {"default": "qodercli", "fallback": [], "list": []}

    def engine_cmd(self, name):
        return None


cfg = Cfg()
db = DB(os.path.join(ws, "t.sqlite"))
run_id = "run-test-v4"
db.insert("runs", run_id=run_id, task_id="t1", trigger_type="dingtalk_at_me",
          source="改签监控群", status="success", engine="mock", engine_version="x",
          started_at=now(), finished_at=now(), report_path=None, error_msg=None,
          source_text="验价失败报警")
result = {
    "normal": False,
    "summary": "外部验价返回失败",
    "conclusion": "【外部域问题】验价上游返回 no fare，底座透传 C-2-0107",
    "evidence": [{"action": "flyeye-log-query 查询", "finding": "PricingRS success:false rawErrCode=no fare"}],
    "anomalies": [{"severity": "P2", "summary": "外部验价返回 no fare"},
                  {"severity": "P3", "summary": "iacs jar 不兼容致打点错误"}],
    "suggestions": [
        {"app": "验价上游", "feature": "rePricing返回no fare", "action_type": "notify_external",
         "action": "通知验价上游排查返回no fare原因", "params": {}},
        {"app": "change-flight-tp", "feature": "iacs jar兼容", "action_type": "tech_requirement",
         "action": "提技术需求修复：升级iacs jar消除抽象方法错误", "params": {}},
    ],
}
reports.write_report(cfg, db, run_id, result, "改签监控群", "mock")
db.insert("alerts", alert_id="al-v4", run_id=run_id, source_group="改签监控群",
          severity="P2", summary="外部验价返回 no fare", detail="",
          status="pending", created_at=now(), acked_at=None,
          ignore_until=None, reopen_at=None)

# ---------- 3. draft ----------
d = aone.draft(cfg, db, run_id, 1)
check("draft 返回标题含归因摘要", not d.get("error") and "[LocalAgent]" in d["title"], str(d)[:120])
check("draft 描述含结论/证据/修复方向",
      "问题结论" in d["desc"] and "关键证据" in d["desc"] and "建议修复方向" in d["desc"])
check("draft project 取配置默认", d["project"] == "退改统一域需求空间")
check("draft 拒绝 notify_external 建议", "error" in aone.draft(cfg, db, run_id, 0))
check("draft 拒绝无效序号", "error" in aone.draft(cfg, db, run_id, 9))
check("draft 拒绝不存在的 run", "error" in aone.draft(cfg, db, "run-none", 0))

# ---------- 4. execute（mock 引擎）回写闭环 ----------
r = asyncio.run(aone.execute(cfg, db, run_id, 1, d["title"], d["desc"], d["project"]))
check("execute 成功返回需求链接", r.get("ok") and r.get("url"), str(r))
js_path = os.path.join(ws, "reports")
side = None
for root, _, files in os.walk(js_path):
    for f in files:
        if f.endswith(".json"):
            side = json.load(open(os.path.join(root, f), encoding="utf-8"))
check("侧车回写 aone_req_url", side and side["suggestions"][1].get("aone_req_url"))
al = db.one("SELECT detail FROM alerts WHERE alert_id='al-v4'")
check("报警记录回写需求链接", al and "Aone需求" in (al["detail"] or ""))
audits = db.q("SELECT action FROM audit_logs WHERE category='aone_req'")
check("审计记录创建动作", any(a["action"] in ("create_requested", "created") for a in audits))

# ---------- 5. 幂等防护 ----------
d2 = aone.draft(cfg, db, run_id, 1)
check("已创建后 draft 拒绝重复", "error" in d2 and "已创建" in d2["error"])
r2 = asyncio.run(aone.execute(cfg, db, run_id, 1, d["title"], d["desc"], d["project"]))
check("已创建后 execute 拒绝重复", "error" in r2)

print(f"\n全部 {len(PASS)} 项断言通过")
