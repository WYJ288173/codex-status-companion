"""P1-3 真实回放集：历史报告回放 Reply Risk Gate，校验决策与期望标注一致。

数据源：本地 workspace DB + 报告 JSON 侧车（不入库提交；无数据时跳过）。
期望标注规则（按休假自治 Goal 的业务口径，独立于门禁实现）：
- 审计播报 → pending_confirm
- 无问题/恢复类（normal 且正文含 恢复/正常）+ 有证据 + 无高风险内容 → auto_reply
- @我低风险答疑 + 无高风险内容 + A 档证据 → auto_reply
- 其余（异常结论/订单/金额/traceId/订正/归因/写操作/批量）→ pending_confirm
运行：./.venv/bin/python tests/replay_risk_gate.py
"""
import glob
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from localagent import correlate
from localagent import reply_policy as rp

WS = os.environ.get("LOCALAGENT_WORKSPACE",
                    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "workspace"))
DB_PATH = os.path.join(WS, "data", "localagent.sqlite")

HIGH_RISK_KW = ("金额", "差异", "资损", "退款", "退票费", "赔付", "补偿", "订正")


def has_high_risk(text, result):
    blob = "\n".join([text or "", result.get("conclusion") or "", result.get("summary") or ""])
    if correlate.extract_orders(blob):
        return True
    if rp._TRACE_REF.search(blob) or rp._ERR_TRACE.search(text or ""):
        return True
    if any(k in blob for k in HIGH_RISK_KW):
        return True
    if (result.get("conclusion") or "").startswith("【外部域问题】"):
        return True
    if result.get("suggestions"):
        return True
    corr = result.get("correlation") or {}
    if corr.get("batch") and corr.get("risk_level") in ("medium", "high"):
        return True
    if result.get("evidence_warning"):
        return True
    return False


def expected_label(trigger, text, result):
    if trigger == "audit_broadcast" or "告警定时播报" in (text or ""):
        return "pending_confirm"
    risky = has_high_risk(text, result)
    if result.get("normal"):
        if risky:
            return "pending_confirm"
        if "恢复" in (text or "") or "正常" in (text or ""):
            return "auto_reply" if (result.get("evidence") or []) else "pending_confirm"
        return "pending_confirm"
    if trigger == "dingtalk_at_me" and not risky:
        graded = rp.grade_evidence(result.get("evidence"))
        if any(e.get("strength") == "A" for e in graded):
            return "auto_reply"
    return "pending_confirm"


def main():
    if not os.path.exists(DB_PATH):
        print("SKIP：无本地数据库")
        return 0
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    policy = rp.default_policy()
    policy["auto_reply"]["enabled"] = True

    cases, dist, mismatches, rows_out = 0, {}, [], []
    false_auto, auto_checked = 0, 0
    for jf in sorted(glob.glob(os.path.join(WS, "reports", "*", "run-*.json"))):
        try:
            with open(jf, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        run_id = data.get("run_id")
        row = db.execute("SELECT source_text, trigger_type FROM runs WHERE run_id=?",
                         (run_id,)).fetchone()
        if not row:
            continue
        text = row["source_text"] or ""
        trigger = row["trigger_type"] or ""
        result = {k: data.get(k) for k in ("normal", "conclusion", "summary", "evidence",
                                           "anomalies", "suggestions", "correlation",
                                           "evidence_warning")}
        label = expected_label(trigger, text, result)
        ctx = {"trigger": trigger, "at_me": trigger == "dingtalk_at_me",
               "audit_parsed": {"items": [{}]} if "告警定时播报" in text else None}
        gate = rp.decide(policy, result, text, ctx=ctx, group_auto=True)
        cases += 1
        dist[label] = dist.get(label, 0) + 1
        # 高风险误自动判定：期望非 auto_reply 但门禁给出 auto_reply
        if label != "auto_reply" and gate["reply_decision"] == "auto_reply":
            false_auto += 1
        if gate["reply_decision"] == "auto_reply":
            auto_checked += 1
            md = gate["reply_markdown"]
            a_refs = [e["source_ref"] for e in gate["evidence"]
                      if e.get("strength") == "A" and e.get("source_ref")]
            if not (a_refs and all(r in md for r in a_refs[:3])):
                mismatches.append((run_id, "auto含A档引用", "缺失", md[:80]))
        rows_out.append("| %s | %s | %s | %s | %s | %s |" % (
            run_id, gate["scenario_type"],
            ",".join(gate["risk_markers"]) or "无",
            label, gate["reply_decision"],
            "一致" if gate["reply_decision"] == label else "**不一致**"))
        if gate["reply_decision"] != label:
            mismatches.append((run_id, label, gate["reply_decision"], gate["reply_reason"]))

    print(f"回放样例：{cases} 条")
    print("期望标注分布：" + ", ".join(f"{k}={v}" for k, v in sorted(dist.items())))
    print("\n| run_id | scenario | risk_markers | 期望 | 实际 | 一致性 |")
    print("|---|---|---|---|---|---|")
    print("\n".join(rows_out[:30]))
    if len(rows_out) > 30:
        print(f"（其余 {len(rows_out) - 30} 条略）")
    print(f"\n高风险误自动回复数：{false_auto}（验收要求 = 0）")
    print(f"auto_reply 样例数：{auto_checked}（全部要求含 A 档 source_ref 引用）")
    if cases < 50:
        print(f"提示：样例数 {cases} < 50，未达到 Goal 建议的 50-100 条规模")
    if false_auto > 0:
        print("FAIL：存在高风险误自动回复，P0 阻塞未解除")
        return 1
    if mismatches:
        print(f"FAIL：{len(mismatches)} 条决策与期望不一致")
        for run_id, label, got, reason in mismatches[:10]:
            print(f"  {run_id}: 期望 {label} 实际 {got}（{reason}）")
        return 1
    print("PASS：全部门禁决策与期望标注一致，高风险误自动=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
