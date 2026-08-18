"""V7 回归：回复卡片报警身份（规则名/预警时间/采集时间）+ 迟到投递/重复报警降噪 + 历史关联标注。
运行：./.venv/bin/python tests/test_v7_reply_identity.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from localagent.db import DB, now
from localagent.matcher import parse_sunfire_alert
from localagent.notify import Notifier
from localagent.pipeline import Pipeline

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


TZ8 = timezone(timedelta(hours=8))


def sunfire_text(ts):
    return (f"{ts.strftime('%Y/%m/%d %H:%M')}\n华扬\nchange-flight-tp\n"
            "改签底座-改签预定指标\n【国内旗舰店】-成功率\n"
            "共有1条数据触发[warning]报警，摘要：\n"
            "* [国内,3487,国内旗舰店,b2c] 成功率 [当前值为: 0] 最近10分钟求平均: 70 < 90\n"
            "近30天共909次告警,日均31次,且1条指标线告警天数超过15天\n@华扬(主班)\n")


# ---------- 1. 规则名解析 ----------
SAMPLE = sunfire_text(datetime(2026, 8, 18, 11, 21, tzinfo=TZ8))
parsed = parse_sunfire_alert(SAMPLE)
check("Sunfire 规则名=监控项组·指标名",
      parsed["rule_name"] == "改签底座-改签预定指标 · 【国内旗舰店】-成功率",
      str(parsed.get("rule_name")))
check("无触发行的文本规则名为 None",
      parse_sunfire_alert("change-flight-tp 报警 已恢复")["rule_name"] is None)

# ---------- 2. 回复卡片：报警身份 + 历史关联标注 ----------
ws = tempfile.mkdtemp()
db = DB(os.path.join(ws, "t.sqlite"))
read_entry = {"id": "read-g1", "app": "dingtalk", "scope": "read", "enabled": True,
              "feature": "读取群消息",
              "constraints": {"groups": ["g1"]},
              "alertRules": [{"type": "keyword", "keywords": ["报警"]}]}


class V7Cfg:
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


cfg = V7Cfg()
ding = FakeDing()
p = Pipeline(cfg, db, Notifier(cfg, db), ding)

result = {"summary": "订单10034579823049预定失败，行业返回未查询到航班信息",
          "conclusion": "【外部域问题】行业接口未返回航班",
          "anomalies": [
              {"severity": "P2", "summary": "订单10034579823049 预定失败，行业无航班"},
              {"severity": "P3", "summary": "商家3487旗舰店预定持续失败，长期日均31次告警"}]}
md = p._build_reply_markdown(result, SAMPLE, received_at="2026-08-18T12:57:02+08:00")
check("回复头带规则名与应用",
      "> 报警：改签底座-改签预定指标 · 【国内旗舰店】-成功率｜change-flight-tp" in md, md[:200])
check("回复头带预警时间", "> 预警时间 08-18 11:21" in md, md[:200])
check("回复头带采集时间", "采集 08-18 12:57" in md, md[:200])
check("历史关联条目带 [历史关联] 前缀",
      "[历史关联] 商家3487旗舰店预定持续失败，长期日均31次告警" in md, md)
check("当前报警条目不被误标注",
      "- [P2] 订单10034579823049 预定失败，行业无航班" in md, md)
md_nosrc = p._build_reply_markdown(result)
check("无源文本时不带身份头（向后兼容）",
      "> 报警：" not in md_nosrc and md_nosrc.startswith("**LocalAgent 分析结论（仅供参考）**"))

# ---------- 3. 迟到投递 / 重复报警降噪（process 级） ----------
fresh_ts = datetime.now(TZ8) - timedelta(minutes=2)
runs_before = db.one("SELECT COUNT(*) c FROM runs")["c"]

r1 = asyncio.run(p.process({"msg_id": "d1", "group": "g1", "sender": "sunfire",
                            "text": sunfire_text(fresh_ts), "at_me": False}))
check("新鲜报警（延迟2分钟）正常分析", r1.get("handled") is True, str(r1))
check("新鲜报警产生 run",
      db.one("SELECT COUNT(*) c FROM runs")["c"] == runs_before + 1)

r2 = asyncio.run(p.process({"msg_id": "d2", "group": "g1", "sender": "sunfire",
                            "text": sunfire_text(fresh_ts), "at_me": False}))
check("同家族同预警时间重投 → duplicate_alert",
      r2.get("reason") == "duplicate_alert", str(r2))
m2 = db.one("SELECT matched_rule, run_id FROM messages WHERE msg_id='d2'")
check("重复报警只记录不分析",
      m2["matched_rule"] == "duplicate_alert" and m2["run_id"] is None, str(dict(m2)))

old_ts = datetime.now(TZ8) - timedelta(minutes=90)
r3 = asyncio.run(p.process({"msg_id": "d3", "group": "g1", "sender": "sunfire",
                            "text": sunfire_text(old_ts), "at_me": False}))
check("迟到90分钟投递 → stale_delivery", r3.get("reason") == "stale_delivery", str(r3))
m3 = db.one("SELECT matched_rule, run_id FROM messages WHERE msg_id='d3'")
check("迟到投递只记录不分析",
      m3["matched_rule"] == "stale_delivery" and m3["run_id"] is None, str(dict(m3)))
check("迟到/重复均未触发钉群回复", not ding.calls, str(ding.calls))
check("run 总数未因迟到/重复增加",
      db.one("SELECT COUNT(*) c FROM runs")["c"] == runs_before + 1)
check("留 stale_delivery 审计",
      db.one("SELECT 1 FROM audit_logs WHERE action='stale_delivery_skipped'") is not None)
check("留 duplicate_alert 审计",
      db.one("SELECT 1 FROM audit_logs WHERE action='duplicate_alert_skipped'") is not None)

# ---------- 4. 页面展示样式 ----------
wsrc = open(os.path.join(os.path.dirname(__file__), "..", "localagent", "webapp.py")).read()
check("页面展示迟到投递/重复报警中文样式",
      '"stale_delivery": "迟到投递·未分析"' in wsrc
      and '"duplicate_alert": "重复报警·已分析过"' in wsrc)

print(f"\n{sum(1 for _, ok in PASS if ok)}/{len(PASS)} passed")
