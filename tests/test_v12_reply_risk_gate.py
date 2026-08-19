"""V12 回归：Reply Risk Gate（方案 §8.9 全用例 + Pipeline 接入）。
运行：./.venv/bin/python tests/test_v12_reply_risk_gate.py
"""
import copy
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from localagent import reply_policy as rp
from localagent.db import DB
from localagent.notify import Notifier
from localagent.pipeline import Pipeline

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


POLICY_ON = rp.default_policy()
POLICY_ON["auto_reply"]["enabled"] = True

A_CODE_EV = [{"action": "本机源码核实",
              "finding": "change-flight-tp/src/main/java/com/x/ConsultationService.java:35 "
                         "已值机按设计拦截，非缺陷"}]


def qa_result(**kw):
    r = {"summary": "已值机乘机人不可改，属业务规则拦截",
         "conclusion": "【域内问题】业务规则拦截，非缺陷",
         "evidence": copy.deepcopy(A_CODE_EV), "anomalies": []}
    r.update(kw)
    return r


CTX_QA = {"at_me": True, "trigger": "dingtalk_at_me"}
QA_TEXT = "@LocalAgent 已值机为什么不能改签？"

# ---------- 1. 场景分类与证据分级 ----------
check("QA 场景分类 low_risk_qa",
      rp.classify_scenario(qa_result(), QA_TEXT, CTX_QA) == "low_risk_qa")
check("监控恢复场景分类 monitor_recovered",
      rp.classify_scenario({"normal": True, "summary": "s"},
                           "change-flight-tp 改签底座 报警 已恢复，当前值正常", {}) == "monitor_recovered")
graded = rp.grade_evidence(A_CODE_EV)
check("代码路径证据推导为 A 档", graded[0]["strength"] == "A"
      and graded[0]["source_ref"].endswith("ConsultationService.java:35"), str(graded[0]))
check("无来源证据至多 B 档",
      rp.grade_evidence([{"action": "看一眼", "finding": "像是没问题"}])[0]["strength"] == "B")

# ---------- 2. §8.9 门禁决策矩阵（policy enabled=true） ----------
d = rp.decide(POLICY_ON, qa_result(), QA_TEXT, ctx=CTX_QA, group_auto=True)
check("low_risk_qa + A档代码证据 → auto_reply", d["reply_decision"] == "auto_reply",
      d["reply_reason"])
check("自动回复正文含 A 档 source_ref",
      d["reply_markdown"] and graded[0]["source_ref"] in d["reply_markdown"],
      d["reply_markdown"][:120])

d = rp.decide(POLICY_ON, qa_result(evidence=[]), QA_TEXT, ctx=CTX_QA, group_auto=True)
check("无 evidence → pending_confirm", d["reply_decision"] == "pending_confirm"
      and "evidence_missing" in d["risk_markers"], d["reply_reason"])

d = rp.decide(POLICY_ON, qa_result(), "traceId=213e01b217870657846782958 帮我看下",
              ctx=CTX_QA, group_auto=True)
check("traceId → pending_confirm", d["reply_decision"] == "pending_confirm"
      and "has_trace_id" in d["risk_markers"], d["reply_reason"])

d = rp.decide(POLICY_ON, qa_result(), "订单 9985903664559 为什么失败", ctx=CTX_QA, group_auto=True)
check("订单号 → pending_confirm", d["reply_decision"] == "pending_confirm"
      and "has_order_id" in d["risk_markers"], d["reply_reason"])

d = rp.decide(POLICY_ON, qa_result(summary="退票费差异 45 元"), "这个退票费差异怎么回事",
              ctx=CTX_QA, group_auto=True)
check("金额 → pending_confirm", d["reply_decision"] == "pending_confirm"
      and "has_amount" in d["risk_markers"], d["reply_reason"])

rec = {"normal": True, "summary": "成功率恢复至 95%，无异常",
       "conclusion": "监控恢复", "evidence": [
           {"action": "核对监控原文", "finding": "报警已恢复原文：成功率 95 > 90"}], "anomalies": []}
d = rp.decide(POLICY_ON, rec, "change-flight-tp 改签底座 报警 已恢复，当前值正常",
              ctx={}, group_auto=True)
check("monitor_recovered + 原始指标证据 → auto_reply", d["reply_decision"] == "auto_reply",
      d["reply_reason"])

audit_res = qa_result(suggestions=[{"app": "ateye", "feature": "订正", "action_type": "data_correction"}])
d = rp.decide(POLICY_ON, audit_res, "### 改签履约-审计汇总-告警定时播报",
              ctx={"audit_parsed": {"items": [{"code": "TRP_X"}]}}, group_auto=True)
check("audit_solution + 写操作建议 → pending_confirm",
      d["reply_decision"] == "pending_confirm" and d["scenario_type"] == "audit_solution"
      and "needs_write" in d["risk_markers"], d["reply_reason"])

d = rp.decide(POLICY_ON, qa_result(correlation={"batch": True, "risk_level": "medium"}),
              QA_TEXT, ctx=CTX_QA, group_auto=True)
check("batch_impact → pending_confirm", d["reply_decision"] == "pending_confirm"
      and "batch_impact" in d["risk_markers"], d["reply_reason"])

d = rp.decide(POLICY_ON, qa_result(evidence_warning="⚠️ 取证工具失败"), QA_TEXT,
              ctx=CTX_QA, group_auto=True)
check("tool_failed → pending_confirm", d["reply_decision"] == "pending_confirm"
      and "tool_failed" in d["risk_markers"], d["reply_reason"])

d = rp.decide(POLICY_ON, qa_result(), QA_TEXT, ctx=CTX_QA, group_auto=True, reply_enabled=False)
check("reply_enabled=false → no_reply", d["reply_decision"] == "no_reply", d["reply_reason"])

d = rp.decide(POLICY_ON, qa_result(), QA_TEXT, ctx=CTX_QA, group_auto=False)
check("群级未放行 → pending_confirm", d["reply_decision"] == "pending_confirm", d["reply_reason"])

no_ref = qa_result(evidence=[{"action": "x", "finding": "y", "strength": "A"}])
d = rp.decide(POLICY_ON, no_ref, QA_TEXT, ctx=CTX_QA, group_auto=True)
check("A 档但无可展示 source_ref → pending_confirm",
      d["reply_decision"] == "pending_confirm", d["reply_reason"])

check("灰度关闭（enabled=false）一律 pending",
      rp.decide(rp.default_policy(), qa_result(), QA_TEXT, ctx=CTX_QA,
                group_auto=True)["reply_decision"] == "pending_confirm")

# ---------- 3. Pipeline 接入：群级白名单不能绕过门禁 ----------
ws = tempfile.mkdtemp()
db = DB(os.path.join(ws, "t.sqlite"))
read_entry = {"id": "read-g1", "app": "dingtalk", "scope": "read", "enabled": True,
              "feature": "读取群消息", "constraints": {"groups": ["g1"]},
              "alertRules": [{"type": "keyword", "keywords": ["报警"]}]}
reply_entry = {"id": "dt-reply", "app": "dingtalk", "scope": "write",
               "feature": "回复分析结论到值班群",
               "constraints": {"groups": ["g1"]}, "enabled": True}


class GateCfg:
    workspace = ws
    agent = {}
    notify = {}
    dingtalk = {"reply_enabled": True}
    auth_entries = [read_entry, reply_entry]
    groups = [{"name": "g1", "auto_reply_types": ["验价"]}]
    solutions = []
    mock = True
    reply_policy = POLICY_ON


class OkDing:
    def __init__(self):
        self.calls = []

    def reply(self, g, t):
        self.calls.append((g, t))
        return True


ding = OkDing()
p = Pipeline(GateCfg(), db, Notifier(GateCfg(), db), ding)

# 门禁通过：QA + A 档代码证据 + 确定性结论 → 自动发群且正文带依据
QA_TEXT_HIT = "@LocalAgent 验价失败，已值机为什么不能改签？"
p._reply_if_allowed("g1", qa_result(), "run-g12-auto", source_text=QA_TEXT_HIT,
                    received_at="2026-08-19T10:00:00+08:00", ctx=CTX_QA)
check("门禁通过自动发群", len(ding.calls) == 1, str(len(ding.calls)))
check("群发正文含依据段", "依据：" in ding.calls[0][1]
      and "ConsultationService.java:35" in ding.calls[0][1], ding.calls[0][1][:150])
check("自动回复留 replied 记录",
      db.one("SELECT exec_result FROM auth_exec WHERE run_id='run-g12-auto'")["exec_result"] == "replied")

# 门禁拦截：白名单命中但带订单号 → 待确认且 payload 带 gate
p._reply_if_allowed("g1", qa_result(), "run-g12-block", source_text="验价失败 订单9985903664559",
                    ctx={"trigger": "dingtalk_alert"})
row = db.one("SELECT * FROM auth_exec WHERE run_id='run-g12-block'")
check("白名单命中但门禁拦截转待确认", row["exec_result"] == "pending_reply", row["exec_result"])
pl = json.loads(row["payload"])
check("待确认 payload 带门禁结果与原因",
      pl.get("gate", {}).get("reply_decision") == "pending_confirm"
      and "has_order_id" in pl.get("gate", {}).get("risk_markers", []), str(pl.get("gate")))
check("拦截后未额外发群", len(ding.calls) == 1, str(len(ding.calls)))

# ---------- 4. 引擎提示词：证据结构化与答疑取证要求 ----------
from localagent.engine import PROMPT_TEMPLATE
check("提示词要求证据结构化 source_type/source_ref/strength",
      "source_type" in PROMPT_TEMPLATE and "source_ref" in PROMPT_TEMPLATE
      and "strength" in PROMPT_TEMPLATE)
check("提示词要求答疑类只读语雀/代码取证",
      "只读取证" in PROMPT_TEMPLATE and "source_type=yuque|code" in PROMPT_TEMPLATE)
check("提示词格式仍合法", bool(PROMPT_TEMPLATE.format(context="x")))

print(f"\n{sum(1 for _, ok in PASS if ok)}/{len(PASS)} passed")
