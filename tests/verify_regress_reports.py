"""校验真实报警回归产出的报告是否符合取证通道策略（记忆 → skill → MCP → 未取证）。
运行：./.venv/bin/python tests/verify_regress_reports.py [报告目录]
"""
import glob
import json
import os
import sys

BASE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp", "regress_ws", "reports")
ok = True


def chk(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        ok = False


files = sorted(glob.glob(os.path.join(BASE, "*", "*.json")))
if not files:
    print("FAIL 无回归报告", BASE)
    sys.exit(1)
for p in files:
    d = json.load(open(p, encoding="utf-8"))
    acts = " ".join((e.get("action") or "") + " " + (e.get("finding") or "")
                    for e in d.get("evidence", []))
    low = acts.lower()
    print(f"\n== {os.path.basename(p)} | 判定: {d['verdict']}")
    print("结论:", d["conclusion"])
    chk("evidence 非空(%d 步)" % len(d.get("evidence", [])), len(d.get("evidence", [])) > 0)
    chk("P1 记忆通道已检索", "memory" in low)
    chk("日志取证经 flyeye-log-query skill（未绕过）",
        ("flyeye" not in low) or ("flyeye-log-query" in low))
    chk("MCP 仅作降级且标注通道",
        ("mcp__flyeye" not in low) or ("降级" in acts and "flyeye-log-query" in low))
    chk("取证失败已标注未取证", ("取证失败" not in acts) or ("未取证" in acts))
    chk("结论有取证依据非臆断",
        any(k in d["conclusion"] for k in ("%", "口径", "业务拦截", "100", "无变更", "超时")))
print("\n真实报警回归总判定:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
