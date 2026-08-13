"""V5 回归：同类报警归类分析（10 分钟窗口归组 / 单订单 vs 多订单研判 / 批量定级升级 / 无同类不变）。
运行：./.venv/bin/python tests/test_v5_correlation.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from localagent import correlate
from localagent.db import DB, now

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


# ---------- 1. 归组键与订单提取 ----------
check("验价关键词归组", correlate.family_key("国际验价失败报警") == "kw:验价")
check("验价成功率下跌同家族", correlate.family_key("验价成功率下跌") == correlate.family_key("验价失败量持续上升"))
check("无关键词退化告警码", correlate.family_key("xx异常", ["TRP_001"]) == "code:TRP_001")
check("无归组键返回None", correlate.family_key("hello") is None)
check("订单号提取去重", correlate.extract_orders("订单9626543917565和9626543917565及1234567890123") == ["1234567890123", "9626543917565"])

# ---------- 2. 窗口归组研判 ----------
ws = tempfile.mkdtemp()
db = DB(os.path.join(ws, "t.sqlite"))


def add_run(rid, text, minutes_ago):
    ts = (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    db.insert("runs", run_id=rid, task_id="t", trigger_type="dingtalk_alert", source="改签监控群",
              status="success", engine="x", engine_version="x", started_at=ts,
              finished_at=ts, report_path=None, error_msg=None, source_text=text)


# 同一订单 3 条验价报警（10 分钟内）
add_run("r1", "验价失败 订单9626543917565 traceId=abc", 8)
add_run("r2", "验价失败 订单9626543917565 重试", 5)
c = correlate.build_context(db, "验价失败 订单9626543917565 再次重试", [], run_id="r3")
check("同类≥2触发关联", c is not None and c["count"] == 3)
check("同一订单判定", c["same_order"] and not c["batch"] and c["orders"] == ["9626543917565"])
check("单订单渲染含单用户影响面", "单一用户" in correlate.render_context(c))

# 多订单批量
add_run("r4", "验价失败 订单1111111111111", 6)
c2 = correlate.build_context(db, "验价失败 订单2222222222222", [], run_id="r5")
check("多订单批量判定", c2["batch"] and len(c2["orders"]) >= 2)
rendered = correlate.render_context(c2)
check("批量渲染要求sunfire+变更取证", "sunfire" in rendered and "发布" in rendered)

# 超窗不关联
add_run("r6", "验价失败 订单3333333333333", 30)
c3 = correlate.build_context(db, "询价失败 订单4444444444444", [], run_id="r7")
check("无窗口内同类返回None", c3 is None)

# ---------- 3. 定级升级兜底 ----------
res_single = {"normal": False, "anomalies": [{"severity": "P3", "summary": "验价失败"}]}
check("单订单不升级", correlate.apply_batch_escalation(res_single, c) is False
      and res_single["anomalies"][0]["severity"] == "P3")
res_batch = {"normal": False, "anomalies": [{"severity": "P3", "summary": "验价失败"}]}
check("多订单升级P2", correlate.apply_batch_escalation(res_batch, c2) is True
      and res_batch["anomalies"][0]["severity"] == "P2")
res_p1 = {"normal": False, "anomalies": [{"severity": "P1", "summary": "批量失败疑似发布"}]}
check("已P1不重复升级", correlate.apply_batch_escalation(res_p1, c2) is False)
res_ok = {"normal": True, "anomalies": []}
check("正常结论不升级", correlate.apply_batch_escalation(res_ok, c2) is False)

# ---------- 4. 提示词含归类规则 ----------
from localagent.engine import PROMPT_TEMPLATE
check("提示词含同类关联规则", "同类报警关联分析" in PROMPT_TEMPLATE)
check("提示词含定级矩阵", "多订单批量失败" in PROMPT_TEMPLATE and "P1" in PROMPT_TEMPLATE)
check("提示词格式仍合法", bool(PROMPT_TEMPLATE.format(context="x")))

print(f"\n全部 {len(PASS)} 项断言通过")
