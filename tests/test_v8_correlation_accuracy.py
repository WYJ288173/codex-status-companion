"""V8 回归：关联准确性——订单提取降噪 / 自回复拦截 / 预警时间窗口 / 批量定级门槛收紧。
背景：2 单已值机拦截（预警时间相隔 75 分钟）被误判 P1 批量失败。
运行：./.venv/bin/python tests/test_v8_correlation_accuracy.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from localagent import correlate
from localagent.db import DB
from localagent.notify import Notifier
from localagent.pipeline import Pipeline

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


def alert_text(ts, order=None, family="生单"):
    line1 = ts.strftime("%Y/%m/%d %H:%M")
    body = (f"{line1}\n华扬\nchange-flight-tp\n改签底座-offer{family}指标\n"
            f"国内-offer{family}成功率\n共有1条数据触发[critical]报警，摘要：\n"
            "* [国内,C-1-1009,选择乘机人已值机不可改,b2c] 成功率 [当前值为: 100]\n")
    if order:
        body += f"订单{order}乘机人已值机，改签{family}被业务规则拦截\n"
    return body


# ---------- 1. G1 订单提取降噪 ----------
NOISE = ("2026/08/18 14:10\nchange-flight-tp\n改签底座-offer生单指标\n"
         "共有1条数据触发[critical]报警，摘要：\n"
         "* [国内,C-1-1009,选择乘机人已值机不可改,b2c] 成功率\n"
         "订单9982473862805乘机人已值机\n"
         " 采样:  11.82.61.210#Err#2150481117870334001234567e1db3\n"
         "[https://x.alibaba-inc.com/alarmConfig/detail/218709694"
         "?alarmId=abc&alarmTime=1787033400000](https://x.alibaba-inc.com)\n")
check("alarmTime 时间戳与采样串不算订单", correlate.extract_orders(NOISE) == ["9982473862805"],
      str(correlate.extract_orders(NOISE)))
check("13位毫秒时间戳被过滤", correlate.extract_orders("异常 1787033220453 和 1787033400000") == [])
check("URL内时间戳随链接整体剔除",
      correlate.extract_orders("详见 https://x.com/a?alarmTime=17870332204537094 报警") == [])
check("真实订单不受影响",
      correlate.extract_orders("订单9626543917565和1234567890123") == ["1234567890123", "9626543917565"])

# ---------- 2. G3 关联窗口锚定预警时间 ----------
ws = tempfile.mkdtemp()
db = DB(os.path.join(ws, "t.sqlite"))
now = datetime.now().astimezone()


def add_run(rid, text, started_minutes_ago):
    ts = (now - timedelta(minutes=started_minutes_ago)).isoformat(timespec="seconds")
    db.insert("runs", run_id=rid, task_id="t", trigger_type="dingtalk_alert", source="改签监控群",
              status="success", engine="x", engine_version="x", started_at=ts,
              finished_at=ts, report_path=None, error_msg=None, source_text=text)


# 今日真实形态：12:55 的报警 13:35 才分析（迟到），14:10 的报警 14:12 分析；
# 按分析时间相距仅 37 分钟，但预警时间相隔 75 分钟 → 不得关联
early_alert = now - timedelta(minutes=75)
add_run("run-early", alert_text(early_alert, "9982473862805"), 40)
cur_text = alert_text(now - timedelta(minutes=1), "9982473862999")
c = correlate.build_context(db, cur_text, None)
check("预警时间相隔75分钟不按分析时间拉近关联", c is None, str(c))

# 预警时间在窗口内（8 分钟前）→ 正常关联
add_run("run-near", alert_text(now - timedelta(minutes=9), "9982473862777"), 8)
c2 = correlate.build_context(db, cur_text, None)
check("预警时间在30分钟窗口内正常关联", c2 is not None and c2["count"] == 2, str(c2))

# ---------- 3. G4 批量定级门槛收紧 ----------
check("2单跨度8分钟至多low（体量小）", c2["risk_level"] == "low", c2["risk_level"])
res = {"normal": False, "anomalies": [{"severity": "P3", "summary": "已值机拦截"}]}
check("低风险不升级P1/P2", correlate.apply_batch_escalation(res, c2) is False
      and res["anomalies"][0]["severity"] == "P3")
rendered = correlate.render_context(c2)
check("低风险注入严禁P1/P2约束", "严禁定级升级为 P1/P2" in rendered, rendered[:120])

ws2 = tempfile.mkdtemp()
db2 = DB(os.path.join(ws2, "t.sqlite"))


def add_run2(rid, text, started_minutes_ago):
    ts = (now - timedelta(minutes=started_minutes_ago)).isoformat(timespec="seconds")
    db2.insert("runs", run_id=rid, task_id="t", trigger_type="dingtalk_alert", source="改签监控群",
               status="success", engine="x", engine_version="x", started_at=ts,
               finished_at=ts, report_path=None, error_msg=None, source_text=text)


# 密集 ≥3 单且跨度 ≤10 分钟 → high 仍可触发（收紧不误伤真批量）
add_run2("b1", alert_text(now - timedelta(minutes=8), "5555555555551"), 8)
add_run2("b2", alert_text(now - timedelta(minutes=5), "5555555555552"), 5)
add_run2("b3", alert_text(now - timedelta(minutes=3), "5555555555553"), 3)
c3 = correlate.build_context(db2, alert_text(now - timedelta(minutes=1), "5555555555554"), None)
check("密集4单跨度≤10分钟仍判high", c3["risk_level"] == "high", str(c3 and c3["risk_level"]))

# 长间隔报警被预警时间窗口排除：45 分钟前的不进窗口，剩余 2 单小体量判 low
ws3 = tempfile.mkdtemp()
db3 = DB(os.path.join(ws3, "t.sqlite"))
for i, mm in enumerate((45, 25)):
    ts = (now - timedelta(minutes=mm - 10)).isoformat(timespec="seconds")
    db3.insert("runs", run_id=f"w{i}", task_id="t", trigger_type="dingtalk_alert", source="g",
               status="success", engine="x", engine_version="x", started_at=ts, finished_at=ts,
               report_path=None, error_msg=None,
               source_text=alert_text(now - timedelta(minutes=mm), f"666666666666{i}"))
c4 = correlate.build_context(db3, alert_text(now - timedelta(minutes=1), "6666666666669"), None)
check("45分钟前报警被窗口排除且剩余2单判low",
      c4 is not None and c4["count"] == 2 and c4["risk_level"] == "low",
      str(c4 and (c4["count"], c4["risk_level"])))

# ---------- 4. G2 自回复消息拦截 ----------
read_entry = {"id": "read-g1", "app": "dingtalk", "scope": "read", "enabled": True,
              "feature": "读取群消息", "constraints": {"groups": ["g1"]},
              "alertRules": [{"type": "keyword", "keywords": ["生单", "报警"]}]}


class V8Cfg:
    workspace = ws
    agent = {}
    notify = {}
    dingtalk = {"reply_enabled": True, "listen_all": True}
    auth_entries = [read_entry]
    groups = []
    solutions = []
    mock = True


class FakeDing:
    def __init__(self):
        self.calls = []

    def reply(self, g, t):
        self.calls.append((g, t))
        return True


cfg = V8Cfg()
p = Pipeline(cfg, db, Notifier(cfg, db), FakeDing())
runs_before = db.one("SELECT COUNT(*) c FROM runs")["c"]
SELF_REPLY = ("**LocalAgent 分析结论（仅供参考）**\n"
              "订单9982473862805乘机人已值机，改签生单被业务规则拦截\n"
              "- [P3] 已值机乘机人提改被拦截，设计内业务告警非缺陷")
r = asyncio.run(p.process({"msg_id": "sr1", "group": "g1", "sender": "LocalAgent",
                           "text": SELF_REPLY, "at_me": False}))
check("自回复消息不当成新报警", r.get("reason") == "self_reply", str(r))
check("自回复不产生 run", db.one("SELECT COUNT(*) c FROM runs")["c"] == runs_before)
check("留 self_reply_skipped 审计",
      db.one("SELECT 1 FROM audit_logs WHERE action='self_reply_skipped'") is not None)

# ---------- 5. 引擎提示词同步 ----------
from localagent.engine import PROMPT_TEMPLATE
check("提示词定级矩阵含跨度约束",
      "报警时间跨度>30 分钟的一律按偶发处理" in PROMPT_TEMPLATE
      and "预警时间跨度 ≤10 分钟" in PROMPT_TEMPLATE)

print(f"\n{sum(1 for _, ok in PASS if ok)}/{len(PASS)} passed")
