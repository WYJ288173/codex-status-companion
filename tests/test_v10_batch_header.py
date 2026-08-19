"""V10 回归：聚合回复头统计式收敛（42条不逐条罗列）+ 批量结论句式约束 + 采集时间取最早到达。
运行：./.venv/bin/python tests/test_v10_batch_header.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from localagent.db import DB
from localagent.notify import Notifier
from localagent.pipeline import Pipeline

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


TZ8 = timezone(timedelta(hours=8))
read_entry = {"id": "read-g1", "app": "dingtalk", "scope": "read", "enabled": True,
              "feature": "读取群消息", "constraints": {"groups": ["g1"]},
              "alertRules": [{"type": "keyword", "keywords": ["报警"]}]}
reply_entry = {"id": "dt-reply", "app": "dingtalk", "scope": "write",
               "feature": "回复分析结论到值班群",
               "constraints": {"groups": ["g1"]}, "enabled": True}


def alert_text(ts, order):
    return (f"{ts.strftime('%Y/%m/%d %H:%M')}\n华扬\nchange-flight-tp\n"
            "改签底座-验价指标\n失败量\n共有1条数据触发[warning]报警，摘要：\n"
            f"* [国内,3487] 失败量 [当前值为: 3]\n订单{order}验价失败，金额不一致\n")


class FakeDing:
    def reply(self, g, t):
        return True


ws = tempfile.mkdtemp()
db = DB(os.path.join(ws, "t.sqlite"))


class AggCfg:
    workspace = ws
    agent = {}
    notify = {"aggregate_minutes": 5, "cooldown_seconds": 0}
    dingtalk = {"reply_enabled": True, "listen_all": True}
    auth_entries = [read_entry, reply_entry]
    groups = []
    solutions = []
    mock = True


cfg = AggCfg()
p = Pipeline(cfg, db, Notifier(cfg, db), FakeDing())

# ---------- 1. 42 条同规则聚合：头部为计数式，不逐条罗列 ----------
base = datetime.now(TZ8).replace(second=0, microsecond=0) - timedelta(minutes=4)
rs = []
for i in range(42):
    ts = base if i % 2 == 0 else base + timedelta(minutes=2)
    rs.append(asyncio.run(p.process({"msg_id": f"m{i}", "group": "g1", "sender": "sunfire",
                                     "text": alert_text(ts, f"998590366{i:04d}"),
                                     "at_me": False})))
check("42 条全部进批", all(r.get("batched") for r in rs) and rs[-1]["batch_size"] == 42)

# 模拟批内最早到达时间为真实采集时刻（00:17）
p._batches["g1"]["items"][0]["arrived_at"] = base.isoformat(timespec="seconds")
asyncio.run(p._flush("g1"))
rows = db.q("SELECT payload FROM auth_exec WHERE entry_id='dt-reply'")
check("聚合批仅 1 条回复", len(rows) == 1, str(len(rows)))
md = json.loads(rows[0]["payload"])["markdown"]
head2 = md.split("\n")[1]
t0 = base.strftime("%m-%d %H:%M")
t1 = (base + timedelta(minutes=2)).strftime("%m-%d %H:%M")
check("头部为计数式（规则+次数+时间范围）",
      head2 == f"> 聚合分析 42 条报警：改签底座-验价指标 · 失败量 42次（{t0}~{t1}）｜change-flight-tp",
      head2)
check("头部长度可控（<120字符）", len(head2) < 120, str(len(head2)))
check("不逐条罗列 42 个身份", head2.count("验价指标") == 1, head2)
check("采集时间取批内最早到达而非分析完成时刻",
      md.split("\n")[2] == f"> 采集 {base.strftime('%m-%d %H:%M')}", md.split("\n")[2])

# ---------- 2. 多规则聚合：分号分隔 ----------
res2 = {"summary": "s", "batch_alerts": [
    {"time": "2026-08-19 00:17", "rule": "改签底座-验价指标 · 失败量"},
    {"time": "2026-08-19 00:18", "rule": "改签底座-验价指标 · 失败量"},
    {"time": "2026-08-19 00:19", "rule": "改签底座-offer生单指标 · 国内-offer生单成功率"}]}
md2 = p._build_reply_markdown(res2, alert_text(base, "1"))
check("多规则按「规则 N次（范围）」分号聚合",
      md2.split("\n")[1] == ("> 聚合分析 3 条报警：改签底座-验价指标 · 失败量 2次（08-19 00:17~08-19 00:18）；"
                             "改签底座-offer生单指标 · 国内-offer生单成功率 1次（08-19 00:19）｜change-flight-tp"),
      md2.split("\n")[1])

# ---------- 3. 超长头部截断：前 3 规则 + 等N条 ----------
res3 = {"summary": "s", "batch_alerts": [
    {"time": f"2026-08-19 00:{10 + i:02d}",
     "rule": f"很长的监控规则名称用于测试截断逻辑-{i} · 指标项名称也很长-{i}"}
    for i in range(8)]}
md3 = p._build_reply_markdown(res3, alert_text(base, "1"))
head3 = md3.split("\n")[1]
check("超长头部截断为前3规则+等N条", "等8条" in head3 and head3.count("很长的监控规则名称") == 3, head3)
check("截断后头部长度受控（<260字符）", len(head3) < 260, str(len(head3)))

# ---------- 4. 引擎提示词含批量结论句式约束 ----------
from localagent.engine import PROMPT_TEMPLATE
check("提示词要求批量汇总句式",
      "聚合批分析" in PROMPT_TEMPLATE and "累计 M 次" in PROMPT_TEMPLATE
      and "严禁逐条复述" in PROMPT_TEMPLATE)

# ---------- 5. 应用名解析不把发送方 publish 当 app ----------
from localagent.matcher import parse_sunfire_alert
REAL = ("2026/08/19 00:17\npublish\n华扬\nchange-flight-tp\n改签底座-验价指标\n失败量\n"
        "共有1条数据触发[warning]报警，摘要：\n* [国际,-] 失败数 [当前值为: 6]\n")
check("发送方publish不误识别为应用名", parse_sunfire_alert(REAL)["app"] == "change-flight-tp",
      str(parse_sunfire_alert(REAL).get("app")))

print(f"\n{sum(1 for _, ok in PASS if ok)}/{len(PASS)} passed")
