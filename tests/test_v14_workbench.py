"""V14 回归：待回复工作台 + 私聊采集分析链路 + 风险拦截 + 自动回复规则（Goal 阶段1/2/4/5）。
运行：./.venv/bin/python tests/test_v14_workbench.py
"""
import asyncio
import inspect
import json
import os
import shutil
import sys

os.environ["LOCALAGENT_MOCK"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def main():
    ws = os.path.join(REPO, "tmp", "wb_ws")
    data_dir = os.path.join(ws, "data")
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)
    os.makedirs(os.path.join(ws, "config"), exist_ok=True)
    os.makedirs(os.path.join(ws, "reports"), exist_ok=True)
    for f in ("agent.yaml", "auth_list.yaml", "solutions.yaml", "reply_policy.yaml"):
        shutil.copy(os.path.join(REPO, "workspace", "config", f),
                    os.path.join(ws, "config", f))
    import yaml as _y
    ap = os.path.join(ws, "config", "agent.yaml")
    ad = _y.safe_load(open(ap)) or {}
    ad.setdefault("dingtalk", {})["listen_all"] = True
    ad.setdefault("notify", {})["aggregate_minutes"] = 0  # 测试态即时分析
    _y.safe_dump(ad, open(ap, "w"), allow_unicode=True, sort_keys=False)
    rp = os.path.join(ws, "config", "reply_policy.yaml")
    pol = _y.safe_load(open(rp)) or {}
    pol.setdefault("auto_reply", {})["enabled"] = True  # 验证态开启门禁自动回复
    _y.safe_dump(pol, open(rp, "w"), allow_unicode=True, sort_keys=False)
    from localagent import configsync
    configsync.save_groups(ws, [
        {"name": "改签底座质量监控", "mode": "alert", "process_mode": "all", "id": "",
         "enabled": True, "auto_reply_types": ["验价"]},
    ])
    os.environ["LOCALAGENT_WORKSPACE"] = ws
    from localagent.main import bootstrap
    app = bootstrap()
    db = app.db

    def reply_sent_count():
        return db.one("SELECT COUNT(*) c FROM audit_logs "
                      "WHERE action IN ('reply_sent','reply_sent_private')")["c"]

    # ---------- 私聊工作咨询 → 分析 + 待回复草稿，默认不自动回复 ----------
    r1 = await app.pipeline.process(
        {"msg_id": "p1", "group": "私聊:张三", "sender": "张三",
         "text": "帮我查一下订单9985903660001改签为什么失败", "at_me": True,
         "conv_type": "private", "conv_id": "cidP1", "reply_target": "user-zhang-123"})
    check("私聊工作咨询被处理", r1.get("handled") and r1.get("run_id"), str(r1))
    m1 = db.one("SELECT * FROM messages WHERE msg_id='p1'")
    check("私聊消息会话模型字段正确",
          m1["conversation_type"] == "private" and m1["conversation_id"] == "cidP1"
          and m1["reply_target"] == "user-zhang-123", str(dict(m1)))
    row1 = db.one("SELECT * FROM auth_exec WHERE run_id=? AND exec_result='pending_reply'",
                  r1["run_id"])
    pl1 = json.loads(row1["payload"])
    check("私聊草稿 reply_channel/reply_target 正确",
          pl1.get("reply_channel") == "private"
          and pl1.get("reply_target") == "user-zhang-123", str(pl1.get("reply_channel")))
    check("私聊默认不自动回复", reply_sent_count() == 0, str(reply_sent_count()))

    # ---------- 私聊无关聊天 → 不分析只记录 ----------
    r2 = await app.pipeline.process(
        {"msg_id": "p2", "group": "私聊:李四", "sender": "李四",
         "text": "今天天气不错", "at_me": True,
         "conv_type": "private", "conv_id": "cidP2", "reply_target": "user-li-456"})
    check("无关私聊不进入分析",
          r2.get("reason") == "private_irrelevant", str(r2))
    check("无关私聊仅记录不产生 run",
          db.one("SELECT run_id FROM messages WHERE msg_id='p2'")["run_id"] is None)

    # ---------- 私聊高风险（订单号+金额）→ pending_confirm 且标记 ----------
    r3 = await app.pipeline.process(
        {"msg_id": "p3", "group": "私聊:王五", "sender": "王五",
         "text": "订单9985903660002 退票费金额不一致，帮我看下 traceId=213e01b217870657846782958",
         "at_me": True, "conv_type": "private", "conv_id": "cidP3",
         "reply_target": "user-wang-789"})
    row3 = db.one("SELECT * FROM auth_exec WHERE run_id=? AND exec_result='pending_reply'",
                  r3["run_id"])
    pl3 = json.loads(row3["payload"])
    mk = set(pl3.get("gate", {}).get("risk_markers", []))
    check("私聊高风险标记 订单/金额/trace",
          {"has_order_id", "has_amount", "has_trace_id"} <= mk, str(mk))
    check("私聊高风险门禁决策 pending_confirm",
          pl3.get("gate", {}).get("reply_decision") == "pending_confirm",
          str(pl3.get("gate", {}).get("reply_decision")))
    check("高风险不自动回复", reply_sent_count() == 0, str(reply_sent_count()))

    # ---------- 群监控恢复（低风险+A档证据）→ 门禁自动回复 ----------
    c0 = reply_sent_count()
    r4 = await app.pipeline.process(
        {"msg_id": "g1", "group": "改签底座质量监控", "sender": "sunfire",
         "text": "change-flight-tp 改签底座 报警 已恢复，当前值正常", "at_me": False})
    check("群低风险恢复自动回复", reply_sent_count() == c0 + 1,
          f"{c0}->{reply_sent_count()} run={r4}")

    # ---------- 待回复工作台页面 ----------
    from localagent import webapp
    w = webapp.build_app(app)
    routes = {getattr(r, "path", ""): r for r in w.routes}
    html = routes["/replies"].endpoint().body.decode()
    check("工作台页面可访问", "待回复工作台" in html)
    check("工作台展示私聊来源与门禁信息",
          "私聊" in html and "门禁：pending_confirm" in html and "风险标记" in html,
          html[:200])
    check("工作台含编辑框与发送按钮", "ed_" in html and "发送到私聊" in html)
    check("工作台展示拦截原因", "拦截原因" in html)

    class FakeReq:
        def __init__(self, data):
            self._d = data
        async def json(self):
            return self._d

    # 编辑草稿 → payload 更新
    edit_id = row3["id"]
    j = await routes["/api/auth_exec/{exec_id}/edit_reply"].endpoint(
        edit_id, FakeReq({"markdown": "已修改的草稿"}))
    check("编辑草稿成功", j.get("ok") is True, str(j))
    pl3b = json.loads(db.one("SELECT payload FROM auth_exec WHERE id=?", edit_id)["payload"])
    check("草稿内容已更新", pl3b.get("markdown") == "已修改的草稿", pl3b.get("markdown"))

    # 手动发送私聊回复 → 走 reply_private
    c0 = reply_sent_count()
    j = await routes["/api/auth_exec/{exec_id}/send_reply"].endpoint(edit_id, FakeReq({}))
    check("手动发送私聊回复成功", reply_sent_count() == c0 + 1,
          f"{j} sent={reply_sent_count()}")
    check("私聊发送审计 reply_sent_private",
          db.one("SELECT 1 FROM audit_logs WHERE action='reply_sent_private' "
                 "AND target LIKE '%user-wang-789%'") is not None)

    # 丢弃（reject_reply 为同步端点）
    j = routes["/api/auth_exec/{exec_id}/reject_reply"].endpoint(row1["id"])
    if inspect.isawaitable(j):
        j = await j
    check("丢弃草稿成功", "已丢弃" in str(j), str(j))

    print(f"\n{sum(1 for _, ok in PASS if ok)}/{len(PASS)} passed")
    return 0 if all(ok for _, ok in PASS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
