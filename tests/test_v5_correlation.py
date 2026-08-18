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

# 多订单批量（2 单，近 10 分钟内；用验座家族避免与上面验价 run 混组）
add_run("r4", "验座失败 订单1111111111111", 6)
c2 = correlate.build_context(db, "验座失败 订单2222222222222", [], run_id="r5")
check("多订单批量判定", c2["batch"] and len(c2["orders"]) >= 2)
rendered = correlate.render_context(c2)
check("低风险渲染含风险等级与偶发标注", "风险等级" in rendered and "偶发" in rendered)

# 超窗不关联（30 分钟窗口内无同家族）
add_run("r6", "验价失败 订单3333333333333", 30)
c3 = correlate.build_context(db, "询价失败 订单4444444444444", [], run_id="r7")
check("无窗口内同类返回None", c3 is None)

# ---------- 3. 风险分级与定级兜底 ----------
res_single = {"normal": False, "anomalies": [{"severity": "P3", "summary": "验价失败"}]}
check("单订单不升级", correlate.apply_batch_escalation(res_single, c) is False
      and res_single["anomalies"][0]["severity"] == "P3")
check("2单无持续判定为低风险", c2["risk_level"] == "low" and c2["sustained"] is True)
res_low = {"normal": False, "anomalies": [{"severity": "P3", "summary": "验价失败"}]}
check("低风险批量不升级", correlate.apply_batch_escalation(res_low, c2) is False
      and res_low["anomalies"][0]["severity"] == "P3")

ws2 = tempfile.mkdtemp()
db2 = DB(os.path.join(ws2, "t.sqlite"))


def add_run2(rid, text, minutes_ago):
    ts = (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    db2.insert("runs", run_id=rid, task_id="t", trigger_type="dingtalk_alert", source="改签监控群",
               status="success", engine="x", engine_version="x", started_at=ts,
               finished_at=ts, report_path=None, error_msg=None, source_text=text)


# 中风险：3 个订单、预警跨度 10-30 分钟（≤10 分钟密集 3 单按新门槛为高风险）
add_run2("m1", "验价失败 订单1111111111111", 20)
add_run2("m2", "验价失败 订单2222222222222", 10)
c_med = correlate.build_context(db2, "验价失败 订单3333333333333", [], run_id="m3")
check("3单判定为中风险", c_med["risk_level"] == "medium")
rendered_med = correlate.render_context(c_med)
check("中风险渲染要求sunfire+变更取证", "sunfire" in rendered_med and "发布" in rendered_med)
res_med = {"normal": False, "anomalies": [{"severity": "P3", "summary": "验价失败"}]}
check("中风险升级P2", correlate.apply_batch_escalation(res_med, c_med) is True
      and res_med["anomalies"][0]["severity"] == "P2")

# 高风险①：10 分钟内 ≥5 个失败订单
add_run2("h1", "预订失败 订单4444444444441", 9)
add_run2("h2", "预订失败 订单4444444444442", 8)
add_run2("h3", "预订失败 订单4444444444443", 7)
add_run2("h4", "预订失败 订单4444444444444", 6)
c_high = correlate.build_context(db2, "预订失败 订单4444444444445", [], run_id="h5")
check("10分钟内5单判定为高风险", c_high["risk_level"] == "high")
res_high = {"normal": False, "anomalies": [{"severity": "P3", "summary": "预订失败"}]}
check("高风险升级P1", correlate.apply_batch_escalation(res_high, c_high) is True
      and res_high["anomalies"][0]["severity"] == "P1")

# 高风险②：≥2 条独立报警、跨度 ≤10 分钟且 ≥3 订单持续失败
add_run2("s1", "生单失败 订单5555555555551", 9)
add_run2("s2", "生单失败 订单5555555555552", 7)
add_run2("s3", "生单失败 订单5555555555553", 5)
add_run2("s3b", "生单失败 订单5555555555550", 3)
c_sus = correlate.build_context(db2, "生单失败 订单5555555555554", [], run_id="s4")
check("密集多单持续失败判定为高风险",
      c_sus["risk_level"] == "high" and c_sus["sustained"] and len(c_sus["orders"]) >= 5)
res_sus = {"normal": False, "anomalies": [{"severity": "P3", "summary": "生单失败"}]}
check("持续型高风险升级P1", correlate.apply_batch_escalation(res_sus, c_sus) is True
      and res_sus["anomalies"][0]["severity"] == "P1")

res_p1 = {"normal": False, "anomalies": [{"severity": "P1", "summary": "批量失败疑似发布"}]}
check("已P1不重复升级", correlate.apply_batch_escalation(res_p1, c_high) is False)
res_ok = {"normal": True, "anomalies": []}
check("正常结论不升级", correlate.apply_batch_escalation(res_ok, c_high) is False)

# ---------- 4. 提示词含归类规则 ----------
from localagent.engine import PROMPT_TEMPLATE
check("提示词含同类关联规则", "同类报警关联分析" in PROMPT_TEMPLATE)
check("提示词含定级矩阵", "批量失败风险分级" in PROMPT_TEMPLATE and "P1" in PROMPT_TEMPLATE)
check("提示词要求单订单结论发出订单号", "订单号必须同时写进 conclusion 和 summary" in PROMPT_TEMPLATE)
check("提示词要求批量失败明确提醒人工介入",
      "批量失败可能代表系统出现问题，需人工介入排查" in PROMPT_TEMPLATE)
check("提示词含生单超时轮询机制先验",
      "改签域生单超时机制先验知识" in PROMPT_TEMPLATE
      and "禁止判定为\"超时诱发/放大用户重试\"" in PROMPT_TEMPLATE)
check("提示词禁止以报警停止作为已恢复依据",
      "终态判定先验" in PROMPT_TEMPLATE
      and "禁止以\"报警停止/成功率回升\"作为\"已恢复\"依据" in PROMPT_TEMPLATE)
check("提示词格式仍合法", bool(PROMPT_TEMPLATE.format(context="x")))

print(f"\n全部 {len(PASS)} 项断言通过")
