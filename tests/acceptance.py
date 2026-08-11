import asyncio
import os
os.environ["LOCALAGENT_MOCK"] = "1"
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from localagent.main import bootstrap


async def main():
    ws = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp", "acceptance_ws")
    import shutil
    data_dir = os.path.join(ws, "data")
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)
    os.makedirs(os.path.join(ws, "config"), exist_ok=True)
    os.makedirs(os.path.join(ws, "reports"), exist_ok=True)
    for f in ("agent.yaml", "auth_list.yaml", "solutions.yaml"):
        shutil.copy(os.path.join(os.path.dirname(ws), "..", "workspace", "config", f),
                    os.path.join(ws, "config", f))
    p = os.path.join(ws, "config", "auth_list.yaml")
    content = open(p).read().replace("enabled: false", "enabled: true")
    open(p, "w").write(content)
    import yaml as _y
    ap = os.path.join(ws, "config", "agent.yaml")
    _ad = _y.safe_load(open(ap)) or {}
    _ad.setdefault("dingtalk", {})["listen_all"] = True
    _y.safe_dump(_ad, open(ap, "w"), allow_unicode=True, sort_keys=False)
    # 测试自洽：强制方案门禁关闭（workspace 可能因真实使用遗留为开启），S3 再显式打开
    from localagent import solutions as _solmod
    _solmod.set_gate(ws, "TRP_INTER_MODIFY_STATUS_ADUIT", False)
    from localagent import configsync
    configsync.save_groups(ws, [
        {"name": "改签底座质量监控", "mode": "alert", "process_mode": "all", "id": "cidfdkcdGeG9tIxgxdLeLYyFQ==", "enabled": True, "auto_reply": False},
        {"name": "xxx监控钉群", "mode": "alert", "process_mode": "all", "id": "", "enabled": True, "auto_reply": False},
        {"name": "xxx审计钉群", "mode": "at_me", "id": "", "enabled": True, "auto_reply": False},
        {"name": "xxx审计播报群", "mode": "both", "process_mode": "at_me_only", "id": "", "enabled": True, "auto_reply": False},
    ])
    os.environ["LOCALAGENT_WORKSPACE"] = ws
    app = bootstrap()
    db = app.db
    await app.ding.start()
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(("PASS" if cond else "FAIL"), name)

    # 场景 A：监控群正常消息 → toast
    r = await app.pipeline.process({"msg_id": "m1", "group": "改签底座质量监控", "sender": "技术风险",
                                    "text": "change-flight-tp 改签底座 报警 已恢复，当前值正常", "at_me": False})
    check("A1 正常消息被处理", r.get("handled") and r.get("normal"))
    check("A2 toast 已发出", db.get_state("pet_toast", "").find("正常") >= 0)

    # 场景 B：监控群退改报警 → 异常弹框 + 群回复 + 写条目命中(pre)
    r = await app.pipeline.process({"msg_id": "m2", "group": "xxx监控钉群", "sender": "sunfire",
                                    "text": "【报警】flyrp 退票 金额不一致 订单号 91234567890 差异45元 P2", "at_me": False})
    check("B1 报警命中并分析", r.get("handled") and not r.get("normal"))
    pending = db.q("SELECT * FROM alerts WHERE status='pending'")
    check("B2 持久弹框(pending)已生成", len(pending) >= 1)
    check("B3 群回复已暂存待确认(pending_reply)", any(a["action"] == "reply_pending" for a in db.q("SELECT * FROM audit_logs WHERE category='dingtalk'")))
    pr = db.one("SELECT * FROM auth_exec WHERE exec_result='pending_reply'")
    app.pipeline.send_reply(pr["id"])
    check("B3b 手动发送回复成功", any(a["action"] == "reply_sent_manual" for a in db.q("SELECT * FROM audit_logs WHERE category='dingtalk'")))
    execd = db.q("SELECT * FROM auth_exec WHERE entry_id='flyrp-write-fix-fee' AND matched=1")
    check("B4 写条目命中 pre 环境执行(mock)", len(execd) >= 1)

    # 场景 C：报警中心确认流转
    aid = pending[0]["alert_id"]
    app.notifier.ack(aid)
    check("C1 确认后状态 acked", db.one("SELECT status FROM alerts WHERE alert_id=?", aid)["status"] == "acked")

    # 场景 D：审计群 @我 审计问题 → 分析 + ateye online 写建议待确认
    r = await app.pipeline.process({"msg_id": "m3", "group": "xxx审计钉群", "sender": "huayang",
                                    "text": "@机器人 帮我分析这条审计错误，需要订正", "at_me": True})
    check("D1 @我触发专项分析", r.get("handled"))
    check("D2 online 写条目进入待确认", any(
        a["entry_id"] == "ateye-write-modify-substatus" and a["exec_result"] == "pending_confirm"
        for a in db.q("SELECT * FROM auth_exec")))

    # 场景 E：@我 无关内容 → 礼貌回复
    r = await app.pipeline.process({"msg_id": "m4", "group": "xxx审计钉群", "sender": "someone",
                                    "text": "@机器人 在吗", "at_me": True})
    check("E1 无关@我返回 unrecognized", r.get("reason") == "at_me_unrecognized")
    check("E2 用法说明已回复", any("无法识别" in (a["detail"] or "") or a["action"] == "reply_sent"
                                  for a in db.q("SELECT * FROM audit_logs WHERE action='reply_sent'")))

    # 场景 F：未授权群消息 → 丢弃
    r = await app.pipeline.process({"msg_id": "m5", "group": "其他群", "sender": "x",
                                    "text": "报警 异常", "at_me": False})
    check("F1 未授权群不处理", not r.get("handled"))

    # 场景 G：监控群非退改报警 → 静默忽略
    r = await app.pipeline.process({"msg_id": "m6", "group": "xxx监控钉群", "sender": "sunfire",
                                    "text": "酒店业务 报警 异常", "at_me": False})
    check("G1 非退改报警不触发", not r.get("handled"))

    # 场景 H：报告已生成
    reps = db.q("SELECT * FROM reports_meta")
    check("H1 报告落盘", len(reps) >= 2 and os.path.exists(os.path.join(app.cfg.workspace, reps[0]["file_path"])))

    # 场景 S：审计播报处理模式 + 方案门禁
    BROADCAST = ("### 改签履约-审计汇总-告警定时播报2026-08-07 10:30\n\n"
                 "**今日未完结:2个，其中**待接手2;待反馈0;逾期0 \n\n-----\n\n"
                 "**1.<font color=#FF0000 >【高危场景】</font>【BCP】【交通】"
                 "[国际改签状态流转基础信息审计(TRP_INTER_MODIFY_STATUS_ADUIT)]"
                 "(http://bcp.alibaba-inc.com/rules/errorlist?ruleCode=TRP_INTER_MODIFY_STATUS_ADUIT)**\n\n"
                 "**告警时间:** 2026-08-07 05:25:08\n\n**规则owner:** @华扬")
    runs_before = db.one("SELECT COUNT(*) c FROM runs")["c"]
    # S1：全局静默（listen_all=false）时，非@我消息不处理、不入库、不展示
    _ad = _y.safe_load(open(ap)); _ad["dingtalk"]["listen_all"] = False
    _y.safe_dump(_ad, open(ap, "w"), allow_unicode=True, sort_keys=False)
    app.pipeline._reload_cfg()
    r = await app.pipeline.process({"msg_id": "ms1", "group": "xxx审计播报群", "sender": "bcppush",
                                    "text": BROADCAST, "at_me": False})
    check("S1 静默模式非@我不处理不入库", r.get("reason") == "non_at_me_silent"
          and db.one("SELECT COUNT(*) c FROM runs")["c"] == runs_before
          and db.one("SELECT * FROM messages WHERE msg_id='ms1'") is None)
    # 恢复全量监听，供后续场景使用
    _ad = _y.safe_load(open(ap)); _ad["dingtalk"]["listen_all"] = True
    _y.safe_dump(_ad, open(ap, "w"), allow_unicode=True, sort_keys=False)
    app.pipeline._reload_cfg()

    r = await app.pipeline.process({"msg_id": "ms4", "group": "xxx审计播报群", "sender": "huayang",
                                    "text": "@机器人 帮我分析这条审计错误，需要订正", "at_me": True})
    check("S4 仅@我模式下@我消息仍分析", r.get("handled"))

    gs = configsync.load_groups(ws)
    for g in gs:
        if g["name"] == "xxx审计播报群":
            g["process_mode"] = "all"
    configsync.save_groups(ws, gs)
    app.pipeline._reload_cfg()
    r = await app.pipeline.process({"msg_id": "ms2", "group": "xxx审计播报群", "sender": "bcppush",
                                    "text": BROADCAST, "at_me": False})
    check("S2 切全部后广播被分析", r.get("handled") and not r.get("normal"))
    check("S2b 门禁关闭拦截写操作", any(
        a["exec_result"] == "suggested" and "solution gate closed" in (a["reject_reason"] or "")
        for a in db.q("SELECT * FROM auth_exec WHERE run_id=?", r["run_id"])))

    from localagent import solutions as solmod
    solmod.set_gate(ws, "TRP_INTER_MODIFY_STATUS_ADUIT", True)
    r = await app.pipeline.process({"msg_id": "ms3", "group": "xxx审计播报群", "sender": "bcppush",
                                    "text": BROADCAST, "at_me": False})
    check("S3 开门禁后进入待确认", any(
        a["entry_id"] == "ateye-write-modify-substatus" and a["exec_result"] == "pending_confirm"
        and "modifyId" in (a["payload"] or "") and "subStatus" in (a["payload"] or "")
        for a in db.q("SELECT * FROM auth_exec WHERE run_id=?", r["run_id"])))
    import json as _json
    steps = []
    for a in db.q("SELECT * FROM auth_exec WHERE run_id=?", r["run_id"]):
        try:
            pl = _json.loads(a["payload"] or "{}")
        except Exception:
            pl = {}
        if pl.get("plan_id"):
            steps.append((pl.get("step_no"), pl.get("step_type")))
    check("S3b 严格执行计划两步(订正→回复)", sorted(steps) == [(1, "write"), (2, "dingtalk_reply")])

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


asyncio.run(main())
