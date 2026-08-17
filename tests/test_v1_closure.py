"""V1 收尾回归：P0/P1/P2 新增逻辑的关键路径断言（不依赖真实引擎/钉钉）。
运行：./.venv/bin/python tests/test_v1_closure.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from localagent import authlist
from localagent.db import DB, now
from localagent.notify import Notifier

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


ws = tempfile.mkdtemp()
os.makedirs(os.path.join(ws, "config"), exist_ok=True)
with open(os.path.join(ws, "config", "auth_list.yaml"), "w") as f:
    f.write("entries: []\n")

# 1. render_command：占位符填充与缺失拒绝
entry = {"id": "t-write", "app": "t", "scope": "write", "feature": "f",
         "constraints": {"command": "/bin/echo order={orderId} fee={fee}"}}
argv = authlist.render_command(entry, {"orderId": "123", "fee": "1.5"})
check("render_command 填充", argv == ["/bin/echo", "order=123", "fee=1.5"], str(argv))
try:
    authlist.render_command(entry, {"orderId": "123"})
    check("render_command 缺参拒绝", False)
except ValueError:
    check("render_command 缺参拒绝", True)

# 2. execute_write：真实执行成功/失败
ok, detail = authlist.execute_write(entry, {"orderId": "123", "fee": "1.5"})
check("execute_write 成功", ok and "order=123" in detail, detail)
bad = {"id": "b", "constraints": {"command": "/bin/ls /nonexistent_path_xyz"}}
ok2, detail2 = authlist.execute_write(bad, {})
check("execute_write 失败返回 rc", not ok2 and "rc=" in detail2, detail2)

# 3. disable_entry
with open(os.path.join(ws, "config", "auth_list.yaml"), "w") as f:
    f.write("entries:\n- id: t-write\n  enabled: true\n")
authlist.disable_entry(ws, "t-write")
import yaml
es = yaml.safe_load(open(os.path.join(ws, "config", "auth_list.yaml")))["entries"]
check("disable_entry 停用条目", es[0]["enabled"] is False)

# 4. notifier.pending 过滤 simulate + 携带 report_path
db = DB(os.path.join(ws, "t.sqlite"))
db.insert("runs", run_id="run-real", task_id="x", trigger_type="dingtalk_alert", source="g",
          status="success", engine="e", engine_version="1", started_at=now(),
          finished_at=now(), report_path="reports/x.md", error_msg=None, source_text="t")
db.insert("runs", run_id="run-sim", task_id="x", trigger_type="simulate", source="g",
          status="success", engine="e", engine_version="1", started_at=now(),
          finished_at=now(), report_path=None, error_msg=None, source_text="t")
for rid, aid in (("run-real", "al-real"), ("run-sim", "al-sim")):
    db.insert("alerts", alert_id=aid, run_id=rid, source_group="g", severity="P2",
              summary="s", detail="", status="pending", created_at=now(),
              acked_at=None, ignore_until=None, reopen_at=None)


class NoUI:
    def toast(self, text):
        pass

    def modal(self, alerts):
        pass


class FakeCfg:
    notify = {}


n = Notifier(FakeCfg(), db, ui=NoUI())
pend = n.pending()
check("pending 过滤 simulate", [p["alert_id"] for p in pend] == ["al-real"], str(pend))
check("pending 携带 report_path", pend[0]["report_path"] == "reports/x.md")

# 5. _handle_suggestions：紧急开关 / 真实执行 / pending_confirm 持久化
from localagent.pipeline import Pipeline


class FakeDing:
    def reply(self, g, t):
        pass


class Cfg:
    workspace = ws
    agent = {"writes_disabled": False}
    notify = {}
    dingtalk = {}
    auth_entries = [
        {"id": "w-exec", "app": "t", "scope": "write", "feature": "直接执行",
         "constraints": {"command": "/bin/echo done {orderId}"}, "enabled": True},
        {"id": "w-confirm", "app": "t", "scope": "write", "feature": "需确认",
         "env": "online", "constraints": {"command": "/bin/echo never {orderId}"},
         "enabled": True},
    ]
    mock = True


p = Pipeline(Cfg(), db, n, FakeDing())
result = {"suggestions": [
    {"app": "t", "feature": "直接执行", "action_type": "data_correction", "params": {"orderId": "1"}},
    {"app": "t", "feature": "需确认", "action_type": "data_correction", "params": {"orderId": "2"}},
    {"app": "t", "feature": "直接执行", "action_type": "data_correction", "params": {"orderId": "3"}},
]}
p._handle_suggestions("run-t1", result, "test")
rows = [(r["exec_result"], r["entry_id"]) for r in db.q("SELECT * FROM auth_exec WHERE run_id='run-t1'")]
check("写操作真实执行", ("executed", "w-exec") in rows, str(rows))
check("online 条目转 pending_confirm", ("pending_confirm", "w-confirm") in rows, str(rows))
check("单任务限 1 次写（第 3 条转建议）", ("suggested", "w-exec") in rows, str(rows))

# 6. 紧急开关：全部转建议
Cfg.agent["writes_disabled"] = True
p._handle_suggestions("run-t2", result, "test")
r2 = db.q("SELECT * FROM auth_exec WHERE run_id='run-t2'")
check("紧急开关拦截全部写", all(r["exec_result"] == "suggested" for r in r2) and len(r2) == 3)
Cfg.agent["writes_disabled"] = False

# 7. pending_confirm 确认执行（模拟 webapp confirm 逻辑核心）
row = db.one("SELECT * FROM auth_exec WHERE run_id='run-t1' AND exec_result='pending_confirm'")
payload = json.loads(row["payload"])
e = next(x for x in Cfg.auth_entries if x["id"] == payload["entry_id"])
ok, detail = authlist.execute_write(e, payload["suggestion"]["params"])
check("确认执行闭环", ok and "never 2" in detail, detail)

# 8. 回复逻辑：无白名单 → pending_reply
class ReplyDing:
    def __init__(self):
        self.calls = []
    def reply(self, g, t):
        self.calls.append((g, t))
        return True

reply_entry = {"id": "dt-reply", "app": "dingtalk", "scope": "write",
               "feature": "回复分析结论到值班群",
               "constraints": {"groups": ["test-group"]}, "enabled": True}

class ReplyCfg:
    workspace = ws
    agent = {"writes_disabled": False}
    notify = {}
    dingtalk = {"reply_enabled": True}
    auth_entries = [reply_entry]
    groups = [{"name": "test-group", "auto_reply": False}]
    mock = True

rd = ReplyDing()
rp = Pipeline(ReplyCfg(), db, n, rd)
result2 = {"summary": "测试异常", "anomalies": [{"severity": "P2", "summary": "金额不符"}]}
rp._reply_if_allowed("test-group", result2, "run-reply1")
pr = db.one("SELECT * FROM auth_exec WHERE run_id='run-reply1' AND exec_result='pending_reply'")
check("无白名单 → pending_reply", pr is not None, str(pr))
check("无白名单 不调 ding.reply", len(rd.calls) == 0, str(rd.calls))

# 9. 遗留 auto_reply=true 无类型白名单 → 不再直通（仅白名单类型可自动回复）
ReplyCfg.groups = [{"name": "test-group", "auto_reply": True}]
rd2 = ReplyDing()
rp2 = Pipeline(ReplyCfg(), db, n, rd2)
rp2._reply_if_allowed("test-group", result2, "run-reply2")
check("auto_reply=true 无白名单 → 不自动回复", len(rd2.calls) == 0, str(rd2.calls))
pr2 = db.one("SELECT * FROM auth_exec WHERE run_id='run-reply2' AND exec_result='pending_reply'")
check("auto_reply=true 无白名单 → pending_reply", pr2 is not None, str(pr2))

# 9b. 群×报警类型维度自动回复
ReplyCfg.groups = [{"name": "test-group", "auto_reply": False, "auto_reply_types": ["验价"]}]
rd4 = ReplyDing()
rp4 = Pipeline(ReplyCfg(), db, n, rd4)
rp4._reply_if_allowed("test-group", result2, "run-reply4", source_text="改签验价失败率下跌报警")
check("命中放开类型 → 自动回复", len(rd4.calls) == 1, str(rd4.calls))
rp4._reply_if_allowed("test-group", result2, "run-reply5", source_text="预订流程异常报警")
check("未命中类型 → 不自动回复", len(rd4.calls) == 1, str(rd4.calls))
pr5 = db.one("SELECT * FROM auth_exec WHERE run_id='run-reply5' AND exec_result='pending_reply'")
check("未命中类型 → pending_reply", pr5 is not None, str(pr5))
rp4._reply_if_allowed("test-group", result2, "run-reply6", source_text="无家族关键词的报警")
check("未分类报警 → 不自动回复转人工",
      len(rd4.calls) == 1
      and db.one("SELECT * FROM auth_exec WHERE run_id='run-reply6' AND exec_result='pending_reply'") is not None)
ReplyCfg.groups = [{"name": "test-group", "auto_reply": True}]

# 10. send_reply: 手动发送 pending_reply（成功路径）
rd3 = ReplyDing()
rp3 = Pipeline(ReplyCfg(), db, n, rd3)
pending = db.one("SELECT * FROM auth_exec WHERE run_id='run-reply1' AND exec_result='pending_reply'")
run_id_sent, ok_sent = rp3.send_reply(pending["id"])
check("send_reply 成功返回 (run_id, True)", (run_id_sent, ok_sent) == ("run-reply1", True),
      str((run_id_sent, ok_sent)))
check("send_reply 调 ding.reply", len(rd3.calls) == 1, str(rd3.calls))
sent = db.one("SELECT * FROM auth_exec WHERE id=?", pending["id"])
check("send_reply 更新为 replied", sent["exec_result"] == "replied", sent["exec_result"])

# 10b. send_reply: 发送失败 → 保持 pending_reply 可重试
class FailDing:
    def __init__(self):
        self.calls = []
    def reply(self, g, t):
        self.calls.append((g, t))
        return False

rpf = Pipeline(ReplyCfg(), db, n, FailDing())
pending_f = db.one("SELECT * FROM auth_exec WHERE run_id='run-reply5' AND exec_result='pending_reply'")
run_id_f, ok_f = rpf.send_reply(pending_f["id"])
check("send_reply 失败返回 (run_id, False)", (run_id_f, ok_f) == ("run-reply5", False),
      str((run_id_f, ok_f)))
row_f = db.one("SELECT * FROM auth_exec WHERE id=?", pending_f["id"])
check("发送失败保持 pending_reply", row_f["exec_result"] == "pending_reply", row_f["exec_result"])
check("发送失败写 reject_reason", "发送失败" in (row_f["reject_reason"] or ""), row_f["reject_reason"])
check("发送失败留审计", db.one("SELECT 1 FROM audit_logs WHERE action='send_manual_failed' "
                              "AND run_id='run-reply5'") is not None)

# 11. reject_reply: 丢弃 pending_reply
ReplyCfg.groups = [{"name": "test-group", "auto_reply": False}]
rd4 = ReplyDing()
rp4 = Pipeline(ReplyCfg(), db, n, rd4)
rp4._reply_if_allowed("test-group", result2, "run-reply3")
pending2 = db.one("SELECT * FROM auth_exec WHERE run_id='run-reply3' AND exec_result='pending_reply'")
run_id_rej = rp4.reject_reply(pending2["id"])
check("reject_reply 返回 run_id", run_id_rej == "run-reply3", str(run_id_rej))
check("reject_reply 不调 ding.reply", len(rd4.calls) == 0, str(rd4.calls))
rej = db.one("SELECT * FROM auth_exec WHERE id=?", pending2["id"])
check("reject_reply 更新为 rejected", rej["exec_result"] == "rejected", rej["exec_result"])

# 12. 审计播报解析器
from localagent.matcher import parse_audit_broadcast
BC = ("### 改签履约-审计汇总-告警定时播报2026-08-07 10:30\n\n"
      "**今日未完结:2个，其中**待接手2;待反馈0;逾期0 \n\n **近15日未完结:6个，其中**待接手6;待反馈0;逾期0 \n\n-----\n\n"
      "**1.<font color=#FF0000 >【高危场景】</font>【BCP】【交通】"
      "[国际改签状态流转基础信息审计(TRP_INTER_MODIFY_STATUS_ADUIT)]"
      "(http://bcp.alibaba-inc.com/rules/errorlist?processStatus=0&ruleCode=TRP_INTER_MODIFY_STATUS_ADUIT)**\n\n"
      "**告警时间:** 2026-08-07 05:25:08\n\n"
      "**告警状态:** 未处理 [接手]( dingtalk://x?id%3D88485759&pc_slide=true)\n\n"
      "**规则owner:** @华扬\n\n"
      "**2.【BCP】【交通】[国内改签费用审计(FLIGGY_ATFLIGHT_CHANGE_FEE_ADUIT)]"
      "(http://bcp.alibaba-inc.com/rules/errorlist?ruleCode=FLIGGY_ATFLIGHT_CHANGE_FEE_ADUIT)**\n\n"
      "**告警时间:** 2026-08-06 15:30:14\n\n**规则owner:** @筱剑")
pb = parse_audit_broadcast(BC)
check("播报解析 title", pb and "告警定时播报" in pb["title"], str(pb and pb["title"]))
check("播报解析 stats", pb["stats"].get("today_unfinished") == 2
      and pb["stats"].get("recent_unfinished") == 6, str(pb["stats"]))
check("播报解析 items=2", len(pb["items"]) == 2, str(len(pb["items"])))
it0 = pb["items"][0]
check("播报条目 告警码", it0["code"] == "TRP_INTER_MODIFY_STATUS_ADUIT", it0["code"])
check("播报条目 BCP链接", "ruleCode=" in it0["bcp_url"], it0["bcp_url"])
check("播报条目 级别/时间/owner", it0["level"] == "高危场景"
      and it0["alert_time"] == "2026-08-07 05:25:08" and it0["owner"] == "华扬", str(it0))
check("播报条目 checkfree_id", it0["checkfree_id"] == "88485759", str(it0.get("checkfree_id")))
check("非播报文本返回 None", parse_audit_broadcast("普通聊天 报警了") is None)

# 13. 方案库：加载/匹配/门禁/上下文渲染
from localagent import solutions as solmod
sol_ws = tempfile.mkdtemp()
solmod.save_solutions(sol_ws, [
    {"code": "TRP_X", "name": "测试方案", "enabled": False, "write_entry_id": "w-confirm",
     "diagnose": ["特征A"], "steps": ["步骤1"]}])
check("方案库 load/find", solmod.find_solutions_for_codes(
    solmod.load_solutions(sol_ws), ["TRP_X", "NOPE"])[0]["code"] == "TRP_X")
ctx = solmod.render_solution_context(solmod.load_solutions(sol_ws))
check("方案上下文含门禁提示", "未开启" in ctx and "步骤1" in ctx, ctx[:80])
solmod.set_gate(sol_ws, "TRP_X", True)
check("set_gate 生效", solmod.get_solution(solmod.load_solutions(sol_ws), "TRP_X")["enabled"] is True)

# 14. 方案写门禁：关联条目且门禁关闭 → 拦截为 suggested；开启 → pending_confirm
gate_result = {"suggestions": [
    {"app": "t", "feature": "需确认", "action_type": "data_correction", "params": {"orderId": "9"}}]}
p._handle_suggestions("run-t4", gate_result, "test",
                      solutions=[{"code": "TRP_X", "write_entry_id": "w-confirm", "enabled": False}])
g1 = db.q("SELECT * FROM auth_exec WHERE run_id='run-t4'")
check("门禁关闭拦截写操作", len(g1) == 1 and g1[0]["exec_result"] == "suggested"
      and "solution gate closed" in (g1[0]["reject_reason"] or ""), str([(r["exec_result"], r["reject_reason"]) for r in g1]))
p._handle_suggestions("run-t5", gate_result, "test",
                      solutions=[{"code": "TRP_X", "write_entry_id": "w-confirm", "enabled": True}])
g2 = db.q("SELECT * FROM auth_exec WHERE run_id='run-t5'")
check("门禁开启转 pending_confirm", len(g2) == 1 and g2[0]["exec_result"] == "pending_confirm",
      str([(r["exec_result"], r["reject_reason"]) for r in g2]))

# 15. subStatus 订正写条目：命令渲染 / 白名单 / 条目级超时 / 执行器 dry-run
import subprocess
import yaml as _y
_proj_ws = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_real_entries = _y.safe_load(open(os.path.join(_proj_ws, "workspace/config/auth_list.yaml"),
                                  encoding="utf-8"))["entries"]
sub_e = next(e for e in _real_entries if e["id"] == "ateye-write-modify-substatus")
argv = authlist.render_command(sub_e, {"modifyId": "12345678", "subStatus": "40"})
check("订正命令渲染(机器约束在命令中)",
      argv[0] == "python3" and "ateye_invoke.py" in argv[1]
      and "trphost_spe" in argv and "center" in argv and "12345678" in argv, str(argv))
ok_w = authlist.check_write(db, sub_e, {"analysis": {"source": "audit_broadcast"},
                                        "params": {"modifyId": "12345678", "subStatus": "40"}})
check("白名单通过合法参数", ok_w[0], ok_w[1])
bad_w = authlist.check_write(db, sub_e, {"analysis": {"source": "audit_broadcast"},
                                         "params": {"modifyId": "abc", "subStatus": "40"}})
check("白名单拒绝非法 modifyId", not bad_w[0], bad_w[1])
check("条目级超时 600s", sub_e["constraints"].get("timeout") == 600)
_script = os.path.join(_proj_ws, "scripts", "ateye_invoke.py")
r_dry = subprocess.run([sys.executable, _script, "--app", "trp", "--invoker", "修改改签单subStatus",
                        "--node-group", "trphost_spe", "--unit", "center",
                        "--modify-id", "12345678", "--sub-status", "40", "--dry-run"],
                       capture_output=True, timeout=30)
out_dry = r_dry.stdout.decode()
check("执行器 dry-run 输出执行 prompt",
      r_dry.returncode == 0 and "trphost_spe" in out_dry and "修改改签单subStatus" in out_dry
      and "get_machines_with_detail" in out_dry, out_dry[:120])
r_bad = subprocess.run([sys.executable, _script, "--app", "trp", "--invoker", "x",
                        "--node-group", "g", "--unit", "u",
                        "--modify-id", "abc", "--sub-status", "40", "--dry-run"],
                       capture_output=True, timeout=30)
check("执行器拒绝非法参数", r_bad.returncode == 1)

# 16. 多动作类型方案结构：归一化 / 渲染 / 旧字段兼容 / 按动作门禁
sol_multi = {"code": "MULTI_X", "name": "多动作", "enabled": False,
             "actions": [{"type": "dingtalk_reply", "template": "结论+建议"},
                         {"type": "ateye_write", "write_entry_id": "w-confirm",
                          "app": "t", "feature": "需确认", "params": ["orderId"]},
                         {"type": "aone_bug", "title_hint": "状态流转缺陷"}]}
acts = solmod.normalize_actions(sol_multi)
check("多动作归一化", [a["type"] for a in acts] == ["dingtalk_reply", "ateye_write", "aone_bug"], str(acts))
check("写类动作过滤(2项)", len(solmod.write_actions(sol_multi)) == 2)
ctx_m = solmod.render_solution_context([sol_multi])
check("多动作上下文渲染", "钉群回复" in ctx_m and "创建线上缺陷" in ctx_m and "门禁未开启" in ctx_m, ctx_m[:150])
legacy = solmod.normalize_actions({"write_entry_id": "w-confirm",
                                   "write_hint": {"app": "t", "feature": "需确认", "params": ["orderId"]}})
check("旧字段归一为 ateye_write 动作", legacy[0]["type"] == "ateye_write"
      and legacy[0]["write_entry_id"] == "w-confirm")
check("按动作查门禁方案", (solmod.find_gate_for_entry([sol_multi], "w-confirm") or {}).get("code") == "MULTI_X")
check("无引用条目返回 None", solmod.find_gate_for_entry([sol_multi], "w-exec") is None)
p._handle_suggestions("run-t6", gate_result, "test", solutions=[sol_multi])
g3 = db.q("SELECT * FROM auth_exec WHERE run_id='run-t6'")
check("多动作结构门禁拦截", len(g3) == 1 and g3[0]["exec_result"] == "suggested"
      and "solution gate closed" in (g3[0]["reject_reason"] or ""),
      str([(r["exec_result"], r["reject_reason"]) for r in g3]))

# 17. 严格执行计划：按配置顺序生成步骤，suggestions 仅参数源
sol_plan = {"code": "PLAN_X", "name": "计划测试", "enabled": True,
            "actions": [{"type": "ateye_write", "name": "订正步骤",
                         "write_entry_id": "w-confirm", "app": "t",
                         "feature": "需确认", "params": ["orderId"]},
                        {"type": "dingtalk_reply", "name": "回复步骤", "template": "结论"}]}
plan = solmod.build_execution_plan(sol_plan, [{"app": "t", "feature": "需确认",
                                               "params": {"orderId": "77"}}])
check("计划按配置生成两步", [s["type"] for s in plan] == ["ateye_write", "dingtalk_reply"]
      and plan[0]["params"] == {"orderId": "77"} and [s["step_no"] for s in plan] == [1, 2], str(plan))
plan_miss = solmod.build_execution_plan(sol_plan, [{"app": "t", "feature": "需确认", "params": {}}])
check("缺参数计划截断在第一步", len(plan_miss) == 1 and plan_miss[0]["missing"] == ["orderId"], str(plan_miss))
p._handle_suggestions("run-t7", {"suggestions": [{"app": "t", "feature": "需确认",
                                                   "params": {"orderId": "77"}}]},
                      "audit_broadcast", solutions=[sol_plan], group="测试群")
g7 = db.q("SELECT * FROM auth_exec WHERE run_id='run-t7' ORDER BY id")
check("门禁开启生成两步待确认", len(g7) == 2
      and all(r["exec_result"] == "pending_confirm" for r in g7), str([(r["entry_id"], r["exec_result"]) for r in g7]))
pl1, pl2 = json.loads(g7[0]["payload"]), json.loads(g7[1]["payload"])
check("步骤载荷含计划与序号", pl1["step_no"] == 1 and pl2["step_no"] == 2
      and pl1["plan_id"] == pl2["plan_id"] and pl2["step_type"] == "dingtalk_reply", str((pl1, pl2)))
ok_o, reason_o = authlist.check_plan_order(db, "run-t7", pl1["plan_id"], 2)
check("前序未执行拦截第2步", not ok_o and "第 1 步" in reason_o, reason_o)
db.update("auth_exec", "id", g7[0]["id"], exec_result="executed")
ok_o2, _ = authlist.check_plan_order(db, "run-t7", pl1["plan_id"], 2)
check("前序已执行放行第2步", ok_o2)
ok_np, _ = authlist.check_plan_order(db, "run-t7", None, None)
check("无计划信息兼容放行", ok_np)

# 17b. 固定参数（配置强指定，覆盖引擎值）
sol_fixed = {"code": "FIX_X", "name": "固定参数", "enabled": True,
             "actions": [{"type": "ateye_write", "name": "订正",
                          "write_entry_id": "w-confirm", "app": "t", "feature": "需确认",
                          "params": ["orderId", "subStatus"],
                          "fixed_params": {"subStatus": "61"}}]}
pf1 = solmod.build_execution_plan(sol_fixed, [{"app": "t", "feature": "需确认",
                                               "params": {"orderId": "88", "subStatus": "99"}}])
check("固定参数覆盖引擎值", pf1[0]["params"] == {"orderId": "88", "subStatus": "61"}, str(pf1[0]["params"]))
pf2 = solmod.build_execution_plan(sol_fixed, [{"app": "t", "feature": "需确认",
                                               "params": {"orderId": "88"}}])
check("固定键不计缺参(引擎可不提供)", pf2[0]["missing"] == [] and pf2[0]["params"]["subStatus"] == "61")
ctx_f = solmod.render_solution_context([sol_fixed])
check("上下文渲染固定参数说明", "subStatus=61" in ctx_f and "不得更改" in ctx_f, ctx_f[:120])

# 18. 语雀方案同步脚本：结构校验 + dry-run
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("yuque_solution_sync", os.path.join(_proj_ws, "scripts", "yuque_solution_sync.py"))
_yss = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_yss)
ok_s, _ = _yss.validate_solution({"code": "TRP_TEST_CODE", "name": "测试",
                                  "actions": [{"type": "ateye_write", "write_entry_id": "x",
                                               "feature": "f", "params": ["a"]},
                                              {"type": "dingtalk_reply", "name": "回复"}]})
check("同步校验通过合法方案", ok_s)
ok_b1, r_b1 = _yss.validate_solution({"code": "bad_code", "name": "x", "actions": [{"type": "manual"}]})
check("同步校验拒绝小写 code", not ok_b1, r_b1)
ok_b2, r_b2 = _yss.validate_solution({"code": "TRP_X", "name": "x",
                                      "actions": [{"type": "ateye_write"}]})
check("同步校验拒绝缺 write_entry_id", not ok_b2, r_b2)
ok_b3, r_b3 = _yss.validate_solution({"error": "不是方案文档"})
check("同步校验透传文档错误", not ok_b3 and "不是方案文档" in r_b3, r_b3)
r_y = subprocess.run([sys.executable, os.path.join(_proj_ws, "scripts", "yuque_solution_sync.py"),
                      "--url", "https://aliyuque.antfin.com/x/y/z", "--dry-run"],
                     capture_output=True, timeout=30)
check("同步脚本 dry-run 输出提取 prompt", r_y.returncode == 0
      and "aliyuque.antfin.com" in r_y.stdout.decode() and "actions" in r_y.stdout.decode())

# 被抑制报警：解析与提示词条款
from localagent.matcher import parse_sunfire_alert
from localagent.engine import PROMPT_TEMPLATE
SUP_TXT = ("2026/08/13 08:03 publish 华扬 change-flight-tp 改签底座-改签预定指标 【国际代理人】-成功率 "
           "共有2条数据触发[warning]报警 采样: 33.39.141.229#Err#213e016817865793116126796e10e5 "
           "底座报警群 报警统计：持续3分，已发送2条，被抑制1条 指标趋势")
sp = parse_sunfire_alert(SUP_TXT)
check("解析被抑制统计", sp["alert_stats"] == {"sent": 2, "suppressed": 1}, str(sp.get("alert_stats")))
check("行内应用名兜底提取", sp["app"] == "change-flight-tp", str(sp.get("app")))
check("提示词含被抑制取证条款",
      "被抑制报警取证" in PROMPT_TEMPLATE and "sf alarm list" in PROMPT_TEMPLATE)
check("提示词含仅标题报警取证条款", "仅标题报警取证" in PROMPT_TEMPLATE)

# 被抑制报警（仅标题消息）命中匹配规则
from localagent.matcher import match_alert
SUP_ENTRY = {"alertRules": [
    {"type": "compound_or", "rules": [
        {"type": "keyword", "keywords": ["报警", "告警"]},
        {"type": "format", "pattern": r"^\[\d{4}[/-]\d{2}[/-]\d{2} \d{2}:\d{2}\]"}]},
    {"type": "keyword", "keywords": ["改签", "退票", "底座"]},
]}
check("抑制标题消息命中规则（横线日期）",
      match_alert(SUP_ENTRY, {"text": "[2026-08-13 19:16]改签底座-验座明细_改签底座--国际验座失败",
                              "sender": "sunfire"}) is not None)
check("时间戳后非底座开头的抑制标题也命中（斜杠日期）",
      match_alert(SUP_ENTRY, {"text": "[2026/08/14 08:20]改签底座-offer生单指标_国内-offer生单成功率",
                              "sender": "sunfire"}) is not None)
check("普通消息不误匹配",
      match_alert(SUP_ENTRY, {"text": "今天中午吃什么改签底座", "sender": "x"}) is None)

# owner 维度：报警带 owner=@华扬 时即使无域关键词也采集
OWNER_ENTRY = {"alertRules": [
    {"type": "keyword", "keywords": ["报警", "告警"]},
    {"type": "compound_or", "rules": [
        {"type": "keyword", "keywords": ["改签", "退票"]},
        {"type": "keyword", "keywords": ["@华扬", "owner=华扬"]}]},
]}
check("owner 报警无域关键词也命中",
      match_alert(OWNER_ENTRY, {"text": "xxx应用成功率报警 @华扬(主班) @安心(备班)",
                                "sender": "sunfire"}) is not None)
check("owner 维度不误匹配普通聊天",
      match_alert(OWNER_ENTRY, {"text": "@华扬 中午一起吃饭", "sender": "x"}) is None)

# ---------- 审计播报 owner 过滤 ----------
import asyncio
from localagent.pipeline import Pipeline


class _OwnerCfg:
    workspace = ws
    agent = {}
    notify = {"cooldown_seconds": 0}
    dingtalk = {"reply_enabled": False, "listen_all": True, "owner_name": "华扬"}
    auth_entries = [{"id": "rd-broadcast", "app": "dingtalk", "scope": "read",
                     "feature": "读取底座报警群消息",
                     "constraints": {"groups": ["改签审计报警群"]},
                     "alertRules": [
                         {"type": "keyword", "keywords": ["报警", "告警"]},
                         {"type": "keyword", "keywords": ["改签"]}],
                     "enabled": True}]
    groups = [{"name": "改签审计报警群", "mode": "both", "enabled": True}]
    solutions = []
    mock = True


class _NoopDing:
    def reply(self, g, t):
        pass


BROADCAST_OTHER = ("### 改签履约-审计汇总-告警定时播报2026-08-14 10:30\n\n"
                   "**1.【BCP】【交通】[国内改签费用审计(FLIGGY_X)](http://bcp.alibaba-inc.com/x)**\n\n"
                   "**告警时间:** 2026-08-12 21:12:10\n\n**规则owner:** @筱剑")
p_own = Pipeline(_OwnerCfg(), db, n, _NoopDing())
res_other = asyncio.run(p_own.process({"msg_id": "bc-other", "group": "改签审计报警群",
                                       "sender": "sunfire", "text": BROADCAST_OTHER,
                                       "at_me": False}))
check("他人owner播报不采集", res_other.get("reason") == "broadcast_owner_not_me")
check("他人owner播报落库标记",
      db.one("SELECT matched_rule FROM messages WHERE msg_id='bc-other'")["matched_rule"]
      == "broadcast_not_my_owner")
res_mine = asyncio.run(p_own.process({"msg_id": "bc-mine", "group": "改签审计报警群",
                                      "sender": "sunfire",
                                      "text": BROADCAST_OTHER.replace("@筱剑", "@华扬"),
                                      "at_me": False}))
check("本人owner播报正常进入分析", res_mine.get("reason") != "broadcast_owner_not_me"
      and res_mine.get("run_id") is not None)

print(f"\n全部 {len(PASS)} 项断言通过")
