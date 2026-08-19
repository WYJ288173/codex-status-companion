"""Reply Risk Gate：本地确定性回复风险门禁。

分析引擎可以给出结论，但是否允许自动发群必须由本模块的确定性策略决定，
不能由模型输出或群级 auto_reply 单独决定。

输入：analysis result + source_text + 群配置 + evidence
输出：scenario_type / risk_markers / evidence_grade / reply_decision / reply_reason / reply_markdown

reply_decision 三类：
- auto_reply       允许自动发群（必须带 A 档证据引用）
- pending_confirm  生成待确认草稿（默认安全落点）
- no_reply         不回复，仅记录报告与审计
"""
import re

from . import correlate

# ---------- 场景类型 ----------
SCN_LOW_RISK_QA = "low_risk_qa"
SCN_MONITOR_RECOVERED = "monitor_recovered"
SCN_HISTORY_DUPLICATE = "history_duplicate"
SCN_MONITOR_ANOMALY = "monitor_anomaly"
SCN_AUDIT_SOLUTION = "audit_solution"
SCN_UNKNOWN = "unknown"

# ---------- 证据来源识别 ----------
# 代码路径（含行号）：change-flight-tp/.../XxxService.java:123 / ~/developer/...
_CODE_REF = re.compile(r"[\w~][\w/.\-~]*\.(?:java|py|go|js|ts|jsx|tsx|xml|groovy|sql|kt|scala|md)(?::\d+)?")
# 语雀链接
_YUQUE_REF = re.compile(r"https?://aliyuque\.antfin\.com/[^\s）)\]，,。]+")
# 历史报告 run_id
_RUN_REF = re.compile(r"run-[0-9a-f]{6,}")
# trace / eagleEyeId
_TRACE_REF = re.compile(r"(?:traceId|eagleEyeId)[=：:\s]*([0-9a-f]{16,})", re.I)
_ERR_TRACE = re.compile(r"#Err#([0-9a-f]{16,})")

# ---------- 高风险标记识别关键词 ----------
_AMOUNT_KW = ("金额", "差异", "资损", "退款", "退票费", "赔付", "补偿", "多收", "少收")
_WRITE_KW = ("订正", "修正数据", "数据订正", "回写", "forceUpdate")
_EXTERNAL_KW = ("【外部域问题】", "notify_external", "通知外部域", "外部域建议")
_TOOL_FAIL_KW = ("工具调用失败", "取证失败", "skill 不可用", "MCP 降级", "认证失败", "未取证")

GRADE_A, GRADE_B, GRADE_C = "A", "B", "C"


def _extract_refs(text):
    """从一段文本提取可回溯引用（代码路径 / 语雀 / 历史报告）。"""
    refs = []
    for m in _CODE_REF.finditer(text or ""):
        refs.append(m.group(0))
    for m in _YUQUE_REF.finditer(text or ""):
        refs.append(m.group(0))
    for m in _RUN_REF.finditer(text or ""):
        refs.append(m.group(0))
    return refs


def grade_evidence(evidence):
    """为每条 evidence 推导 source_ref 与 strength（A/B/C）。
    A 档：可回溯代码路径 / 语雀链接 / 历史报告 run_id / 监控恢复原文。
    B 档：有 action/finding 但无明确可回溯来源。
    """
    graded = []
    for e in evidence or []:
        if not isinstance(e, dict):
            continue
        item = dict(e)
        blob = " ".join([str(item.get("action", "")), str(item.get("finding", "")),
                         str(item.get("source_ref", ""))])
        refs = _extract_refs(blob)
        ref = item.get("source_ref") or (refs[0] if refs else "")
        if item.get("strength") in (GRADE_A, GRADE_B, GRADE_C):
            strength = item["strength"]
        elif ref:
            strength = GRADE_A
        elif (item.get("action") or item.get("finding")):
            strength = GRADE_B
        else:
            strength = GRADE_C
        item["source_ref"] = ref
        item["strength"] = strength
        graded.append(item)
    return graded


def best_evidence_grade(graded):
    order = {GRADE_A: 3, GRADE_B: 2, GRADE_C: 1}
    best = None
    for e in graded:
        if best is None or order.get(e.get("strength"), 0) > order.get(best, 0):
            best = e.get("strength")
    return best


def classify_scenario(result, source_text, ctx):
    """场景分类。ctx 可含 trigger/at_me/audit_parsed/correlation。"""
    ctx = ctx or {}
    text = source_text or ""
    corr = ctx.get("correlation") or result.get("correlation") or {}
    normal = bool(result.get("normal"))

    # 审计播报 / 命中解决方案
    if ctx.get("audit_parsed") or "告警定时播报" in text:
        return SCN_AUDIT_SOLUTION

    # 监控恢复：正常消息或恢复字样且结论为正常
    if normal and ("恢复" in text or "正常" in text or "已恢复" in text):
        return SCN_MONITOR_RECOVERED

    # 历史同根因重复：同类关联且单订单重试（已有历史报告可复用）
    if corr.get("same_order") and corr.get("count", 0) >= 2:
        return SCN_HISTORY_DUPLICATE

    # @我答疑：低风险提示型问题
    if ctx.get("at_me") or ctx.get("trigger") == "dingtalk_at_me":
        return SCN_LOW_RISK_QA

    # Sunfire 异常报警
    if result.get("anomalies") or not normal:
        return SCN_MONITOR_ANOMALY

    return SCN_UNKNOWN


def detect_risk_markers(result, source_text, ctx):
    """识别高风险标记（方案 §8.4）。返回命中标记列表。"""
    ctx = ctx or {}
    text = source_text or ""
    concl = result.get("conclusion") or ""
    summary = result.get("summary") or ""
    blob = "\n".join([text, concl, summary])
    markers = []

    # 订单 / 改签单 / 退票单号
    orders = correlate.extract_orders(blob)
    if orders:
        markers.append("has_order_id")
        if any(k in blob for k in ("改签", "值机", "改签单", "trp")):
            markers.append("has_modify_id")
        if any(k in blob for k in ("退票", "退款单", "flyrp")):
            markers.append("has_refund_id")

    # trace / eagleEyeId
    if _TRACE_REF.search(blob) or _ERR_TRACE.search(text):
        markers.append("has_trace_id")

    # 金额 / 资损
    if any(k in blob for k in _AMOUNT_KW):
        markers.append("has_amount")

    # 审计资损
    if ctx.get("audit_parsed") or "资损" in blob or "告警定时播报" in text:
        markers.append("has_audit_loss_risk")

    # 写操作
    sugg = result.get("suggestions") or []
    if sugg or any(k in blob for k in _WRITE_KW):
        markers.append("needs_write")

    # 外部归因
    if any(k in concl for k in _EXTERNAL_KW):
        markers.append("needs_external_attribution")

    # 批量影响
    corr = ctx.get("correlation") or result.get("correlation") or {}
    if corr.get("batch") or corr.get("risk_level") in ("medium", "high"):
        markers.append("batch_impact")

    # 工具失败
    if result.get("evidence_warning") or any(k in blob for k in _TOOL_FAIL_KW):
        markers.append("tool_failed")

    # 证据缺失
    if not result.get("evidence"):
        markers.append("evidence_missing")

    return sorted(set(markers))


def build_reply_markdown(result, graded, source_text=""):
    """自动回复短模板（方案 §8.6）：必须带 A 档证据引用。"""
    summary = (result.get("summary") or result.get("conclusion") or "").strip()
    a_refs = [e.get("source_ref") for e in graded
              if e.get("strength") == GRADE_A and e.get("source_ref")]
    lines = [f"【LocalAgent】结论：{summary}", "", "依据："]
    for i, ref in enumerate(a_refs[:3], 1):
        lines.append(f"{i}. {ref}")
    if not a_refs:
        lines.append("1. （无可回溯证据引用）")
    lines.append("")
    lines.append("仅供参考；涉及线上数据、订正、资损或责任归因请以人工确认为准。")
    return "\n".join(lines)


def decide(policy, result, source_text="", ctx=None, group_auto=False, reply_enabled=True):
    """回复风险门禁决策。返回结构化 dict。
    group_auto / reply_enabled 为外层放行条件，不能绕过本门禁。
    """
    ctx = ctx or {}
    policy = policy or {}
    auto = policy.get("auto_reply", {}) or {}
    scenario = classify_scenario(result, source_text, ctx)
    markers = detect_risk_markers(result, source_text, ctx)
    if scenario == SCN_UNKNOWN and "unknown_scope" not in markers:
        markers.append("unknown_scope")
    graded = grade_evidence(result.get("evidence"))
    # 方案 §8.2：监控恢复原文可推导为 A 档
    if scenario == SCN_MONITOR_RECOVERED and graded and \
            not any(e.get("strength") == GRADE_A for e in graded):
        graded[0]["strength"] = GRADE_A
        graded[0]["source_ref"] = graded[0].get("source_ref") or "监控报警原文（已恢复）"
    grade = best_evidence_grade(graded)

    decision = "pending_confirm"
    reasons = []

    allowed_scenarios = auto.get("allowed_scenarios",
                                 [SCN_LOW_RISK_QA, SCN_MONITOR_RECOVERED, SCN_HISTORY_DUPLICATE])
    block_markers = set(auto.get("block_risk_markers", [])) 
    require_strength = auto.get("require_evidence_strength", GRADE_A)
    require_ref_in_reply = auto.get("require_evidence_in_reply", True)

    if not reply_enabled:
        decision, reasons = "no_reply", ["reply_enabled=false"]
    elif not auto.get("enabled", False):
        decision, reasons = "pending_confirm", ["策略 auto_reply.enabled=false（灰度未开启）"]
    elif not group_auto:
        decision, reasons = "pending_confirm", ["群级自动回复未开启或类型未命中"]
    elif scenario not in allowed_scenarios:
        decision, reasons = "pending_confirm", [f"场景 {scenario} 不在自动回复白名单"]
    else:
        hit = sorted(block_markers & set(markers))
        if hit:
            decision, reasons = "pending_confirm", [f"命中高风险标记 {','.join(hit)}，禁止自动回复"]
        else:
            a_refs = [e.get("source_ref") for e in graded
                      if e.get("strength") == GRADE_A and e.get("source_ref")]
            strength_ok = {GRADE_A: 3, GRADE_B: 2, GRADE_C: 1}.get(grade, 0) >= \
                {GRADE_A: 3, GRADE_B: 2, GRADE_C: 1}.get(require_strength, 3)
            if not strength_ok or not a_refs:
                decision, reasons = "pending_confirm", ["缺少 A 档可回溯证据，禁止自动回复"]
            else:
                md = build_reply_markdown(result, graded, source_text)
                if require_ref_in_reply and not any(r in md for r in a_refs):
                    decision, reasons = "pending_confirm", ["回复正文未包含 A 档证据引用"]
                else:
                    decision, reasons = "auto_reply", ["通过 Reply Risk Gate"]

    markdown = build_reply_markdown(result, graded, source_text) if decision == "auto_reply" else ""
    return {
        "scenario_type": scenario,
        "risk_markers": markers,
        "evidence_grade": grade,
        "reply_decision": decision,
        "reply_reason": "；".join(reasons),
        "reply_markdown": markdown,
        "evidence": graded,
    }


def load_policy(workspace):
    """加载 workspace/config/reply_policy.yaml；不存在返回默认保守策略。"""
    import os
    import yaml
    p = os.path.join(workspace, "config", "reply_policy.yaml")
    if not os.path.exists(p):
        return default_policy()
    try:
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or default_policy()
    except Exception:
        return default_policy()


def default_policy():
    return {
        "auto_reply": {
            "enabled": False,
            "allowed_scenarios": [SCN_LOW_RISK_QA, SCN_MONITOR_RECOVERED, SCN_HISTORY_DUPLICATE],
            "require_evidence_strength": GRADE_A,
            "require_evidence_in_reply": True,
            "block_risk_markers": [
                "has_order_id", "has_modify_id", "has_refund_id", "has_trace_id",
                "has_amount", "has_audit_loss_risk", "needs_write",
                "needs_external_attribution", "batch_impact", "tool_failed",
                "evidence_missing", "unknown_scope",
            ],
        }
    }
