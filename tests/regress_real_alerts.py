"""真实报警回归：取最近 2 条改签监控群真实报警，用真实引擎跑分析并校验取证通道。
运行：./.venv/bin/python tests/regress_real_alerts.py
校验：evidence 非空；日志取证走 flyeye-log-query skill；无"直调 flyeye MCP 失败"类证据。
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_DB = os.path.join(PROJ, "workspace", "data", "localagent.sqlite")
GROUP = "改签底座质量监控"


def latest_alerts(n=2):
    c = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT msg_id, group_name, sender, received_at, source_text FROM messages "
        "WHERE group_name=? AND source_text IS NOT NULL AND matched_rule LIKE 'rule%' "
        "ORDER BY received_at DESC LIMIT ?", (GROUP, n)).fetchall()
    return [dict(r) for r in rows]


async def main():
    msgs = latest_alerts(2)
    if len(msgs) < 2:
        print("FAIL 真实报警样本不足", len(msgs))
        sys.exit(1)
    ws = os.path.join(PROJ, "tmp", "regress_ws")
    if os.path.exists(ws):
        shutil.rmtree(ws)
    os.makedirs(os.path.join(ws, "config"), exist_ok=True)
    for f in ("agent.yaml", "auth_list.yaml", "solutions.yaml", "groups.yaml"):
        src = os.path.join(PROJ, "workspace", "config", f)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(ws, "config", f))
    import yaml
    ap = os.path.join(ws, "config", "agent.yaml")
    a = yaml.safe_load(open(ap, encoding="utf-8"))
    a["mock"] = False
    a.setdefault("dingtalk", {})["listen_all"] = True
    a["dingtalk"]["reply_enabled"] = False
    yaml.safe_dump(a, open(ap, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
    os.environ["LOCALAGENT_WORKSPACE"] = ws
    os.environ.pop("LOCALAGENT_MOCK", None)

    from localagent.main import bootstrap
    app = bootstrap()
    ok = True
    for i, m in enumerate(msgs, 1):
        print(f"\n=== 样本 {i}: {m['received_at']} {m['msg_id']} ===")
        print(m["source_text"][:200].replace("\n", " | "))
        r = await app.pipeline.process({"msg_id": f"rg-{i}-{m['msg_id']}", "group": GROUP,
                                       "sender": m["sender"] or "sunfire",
                                       "text": m["source_text"], "at_me": False})
        print("process:", r)
        if not r.get("run_id"):
            print("FAIL 未产出 run"); ok = False; continue
        run = app.db.one("SELECT * FROM runs WHERE run_id=?", r["run_id"])
        rel = run["report_path"]
        if not rel:
            print("FAIL 无报告", run["status"], run["error_msg"]); ok = False; continue
        js = os.path.join(ws, rel[:-3] + ".json")
        data = json.load(open(js, encoding="utf-8"))
        print("判定:", data["verdict"], "| 结论:", data["conclusion"])
        for e in data["evidence"]:
            print("  证据:", e.get("action"), "→", (e.get("finding") or "")[:160])
        actions = " ".join((e.get("action") or "") + " " + (e.get("finding") or "")
                           for e in data["evidence"])
        low = actions.lower()
        if not data["evidence"]:
            print("FAIL evidence 为空"); ok = False
        if "memory" not in low:
            print("FAIL 未见记忆通道(P1 memory)取证记录"); ok = False
        if "flyeye" in low and "flyeye-log-query" not in low:
            print("FAIL 绕过 flyeye-log-query skill 直调 flyeye MCP"); ok = False
        if "mcp__flyeye" in low and "flyeye-log-query" not in low:
            print("FAIL 日志取证未经 skill 直接使用 flyeye MCP"); ok = False
        if "flyeye-log-query" in low and "取证失败" in actions and "未取证" not in actions:
            print("FAIL 日志取证失败但未标注「未取证」"); ok = False
        print("报告渲染页:", f"/reports/view?p={rel}")
    print("\n回归结果:", "PASS" if ok else "FAIL", "| workspace:", ws)
    sys.exit(0 if ok else 1)


asyncio.run(main())
