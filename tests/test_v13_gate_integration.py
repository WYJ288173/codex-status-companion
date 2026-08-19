"""V13 集成验证（Goal §3）：LOCALAGENT_MOCK=1 全流程跑 Reply Risk Gate。
覆盖：低风险答疑自动回复带依据、监控恢复自动回复、高风险（订单/金额）不自动、
审计播报写操作 pending_confirm、批量报警 batch_impact 不自动、
报告 JSON 与 pending payload 的门禁可回溯。
运行：./.venv/bin/python tests/test_v13_gate_integration.py
"""
import asyncio
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
    ws = os.path.join(REPO, "tmp", "gate_ws")
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
    ad["dingtalk"]["reply_on_normal"] = True
    ad.setdefault("notify", {})["aggregate_minutes"] = 0
    _y.safe_dump(ad, open(ap, "w"), allow_unicode=True, sort_keys=False)
    rp_path = os.path.join(ws, "config", "reply_policy.yaml")
    pol = _y.safe_load(open(rp_path)) or {}
    pol.setdefault("auto_reply", {})["enabled"] = True  # 验证态开启门禁自动回复
    _y.safe_dump(pol, open(rp_path, "w"), allow_unicode=True, sort_keys=False)
    from localagent import configsync, solutions as solmod
    configsync.save_groups(ws, [
        {"name": "改签底座质量监控", "mode": "alert", "process_mode": "all", "id": "",
         "enabled": True, "auto_reply_types": ["验价", "生单"]},
        {"name": "改签审计报警群", "mode": "both", "process_mode": "at_me_only", "id": "",
         "enabled": True, "auto_reply_types": ["验价"]},
    ])
    solmod.set_gate(ws, "TRP_INTER_MODIFY_STATUS_ADUIT", True)
    os.environ["LOCALAGENT_WORKSPACE"] = ws
    from localagent.main import bootstrap
    app = bootstrap()
    db = app.db
    await app.ding.start()

    def reply_count():
        return db.one("SELECT COUNT(*) c FROM audit_logs WHERE action='reply_sent'")["c"]

    # I1 低风险答疑（@我）→ 自动回复，正文含「依据」与 A 档 source_ref
    r1 = await app.pipeline.process({"msg_id": "i1", "group": "改签审计报警群", "sender": "同事",
                                     "text": "@LocalAgent 帮我分析下验价失败的原因", "at_me": True})
    check("I1 低风险答疑被处理", r1.get("handled") and r1.get("normal"), str(r1))
    lr = db.get_state("last_reply", "")
    check("I1 自动回复含「依据」与 A 档 source_ref（state 截断 120 字内可见）",
          "依据：" in lr and "change-flight-tp/src/main/java/com/mock/" in lr, lr)
    rep1 = db.one("SELECT report_path FROM runs WHERE run_id=?", r1["run_id"])
    j1 = json.load(open(os.path.join(ws, rep1["report_path"][:-3] + ".json")))
    check("I1 报告 JSON 含 reply_decision=auto_reply 与结构化字段",
          j1.get("reply_decision") == "auto_reply" and "risk_markers" in j1
          and "reply_reason" in j1, str({k: j1.get(k) for k in
                                          ("reply_decision", "reply_reason", "risk_markers")}))

    # I2 监控恢复 → 自动回复（monitor_recovered）
    c0 = reply_count()
    r2 = await app.pipeline.process({"msg_id": "i2", "group": "改签底座质量监控", "sender": "sunfire",
                                     "text": "change-flight-tp 改签底座 报警 已恢复，当前值正常",
                                     "at_me": False})
    check("I2 监控恢复被处理", r2.get("handled") and r2.get("normal"), str(r2))
    check("I2 监控恢复自动发群", reply_count() == c0 + 1, f"{c0}->{reply_count()}")
    lr2 = db.get_state("last_reply", "")
    check("I2 回复含依据引用", "依据：" in lr2 and "change-flight-tp/src/main/java/com/mock/" in lr2, lr2)

    # I3 高风险：订单号+金额 → 不自动回复，转待确认且 payload 带门禁结果
    c0 = reply_count()
    r3 = await app.pipeline.process({"msg_id": "i3", "group": "改签底座质量监控", "sender": "sunfire",
                                     "text": "改签验价失败报警 订单9985903664559 退票费金额不一致",
                                     "at_me": False})
    check("I3 高风险报警被分析", r3.get("handled") and r3.get("run_id"), str(r3))
    check("I3 高风险不自动发群", reply_count() == c0, f"{c0}->{reply_count()}")
    row3 = db.one("SELECT * FROM auth_exec WHERE run_id=? AND exec_result='pending_reply'",
                  r3["run_id"])
    pl3 = json.loads(row3["payload"])
    check("I3 pending payload 带门禁决策与拦截原因",
          pl3.get("gate", {}).get("reply_decision") == "pending_confirm"
          and "has_order_id" in pl3["gate"]["risk_markers"]
          and "has_amount" in pl3["gate"]["risk_markers"], str(pl3.get("gate")))
    rep3 = db.one("SELECT report_path FROM runs WHERE run_id=?", r3["run_id"])
    j3 = json.load(open(os.path.join(ws, rep3["report_path"][:-3] + ".json")))
    check("I3 报告 JSON 记录 risk_markers", "has_order_id" in j3.get("risk_markers", []),
          str(j3.get("risk_markers")))

    # I4 审计播报命中方案 → 写操作 pending_confirm，回复转待确认
    c0 = reply_count()
    from datetime import datetime as _dt
    _bt = _dt.now().strftime("%Y-%m-%d %H:%M")
    BROADCAST = (f"### 改签履约-审计汇总-告警定时播报{_bt}\n\n"
                 "**今日未完结:1个，其中**待接手1;待反馈0;逾期0 \n\n-----\n\n"
                 "**1.<font color=#FF0000 >【高危场景】</font>【BCP】【交通】"
                 "[国际改签状态流转基础信息审计(TRP_INTER_MODIFY_STATUS_ADUIT)]"
                 "(http://bcp.alibaba-inc.com/rules/errorlist?ruleCode=TRP_INTER_MODIFY_STATUS_ADUIT)**\n\n"
                 f"**告警时间:** {_bt}:08\n\n**规则owner:** @华扬")
    r4 = await app.pipeline.process({"msg_id": "i4", "group": "改签审计报警群", "sender": "bcppush",
                                     "text": BROADCAST, "at_me": False})
    check("I4 审计播报被分析", r4.get("handled") and r4.get("run_id"), str(r4))
    check("I4 审计播报不自动发群", reply_count() == c0, f"{c0}->{reply_count()}")
    pc4 = db.q("SELECT * FROM auth_exec WHERE run_id=? AND exec_result='pending_confirm'",
               r4["run_id"])
    check("I4 写操作进入 pending_confirm", len(pc4) >= 1, str(len(pc4)))
    row4 = db.one("SELECT * FROM auth_exec WHERE run_id=? AND exec_result='pending_reply'",
                  r4["run_id"])
    pl4 = json.loads(row4["payload"])
    check("I4 审计场景门禁标记 has_audit_loss_risk/needs_write",
          {"has_audit_loss_risk", "needs_write"} <= set(pl4.get("gate", {}).get("risk_markers", [])),
          str(pl4.get("gate", {}).get("risk_markers")))

    # I5 多订单批量（3 单）→ batch_impact 标记，不自动回复
    c0 = reply_count()
    for i, od in enumerate(("9985903660001", "9985903660002", "9985903660003")):
        r5 = await app.pipeline.process(
            {"msg_id": f"i5{i}", "group": "改签底座质量监控", "sender": "sunfire",
             "text": f"改签验价失败报警 订单{od} 金额不一致", "at_me": False})
    check("I5 批量报警逐条被分析", r5.get("handled") and r5.get("run_id"), str(r5))
    check("I5 批量不自动发群", reply_count() == c0, f"{c0}->{reply_count()}")
    rep5 = db.one("SELECT report_path FROM runs WHERE run_id=?", r5["run_id"])
    j5 = json.load(open(os.path.join(ws, rep5["report_path"][:-3] + ".json")))
    check("I5 报告 JSON risk_markers 含 batch_impact",
          "batch_impact" in j5.get("risk_markers", []), str(j5.get("risk_markers")))
    check("I5 报告 JSON reply_decision=pending_confirm",
          j5.get("reply_decision") == "pending_confirm", str(j5.get("reply_decision")))

    print(f"\n{sum(1 for _, ok in PASS if ok)}/{len(PASS)} passed")
    return 0 if all(ok for _, ok in PASS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
