"""V9 回归：采集/分析解耦（队列+worker）+ 同群报警聚合窗口（批量分析统一回复）+ 回复格式收紧。
运行：./.venv/bin/python tests/test_v9_aggregation.py
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


def alert_text(ts, rule_group, metric, order):
    return (f"{ts.strftime('%Y/%m/%d %H:%M')}\n华扬\nchange-flight-tp\n{rule_group}\n{metric}\n"
            "共有1条数据触发[warning]报警，摘要：\n"
            f"* [国内,3487,国内旗舰店,b2c] 成功率 [当前值为: 0]\n订单{order}预订变价校验失败，金额不一致\n")


class FakeDing:
    def __init__(self):
        self.calls = []

    def reply(self, g, t):
        self.calls.append((g, t))
        return True


# ---------- 1. 聚合窗口：3 条报警 → 1 run / 1 回复 ----------
ws = tempfile.mkdtemp()
db = DB(os.path.join(ws, "t.sqlite"))


class AggCfg:
    workspace = ws
    agent = {}
    notify = {"aggregate_minutes": 5, "cooldown_seconds": 300}
    dingtalk = {"reply_enabled": True, "listen_all": True}
    auth_entries = [read_entry, reply_entry]
    groups = []
    solutions = []
    mock = True


cfg = AggCfg()
p = Pipeline(cfg, db, Notifier(cfg, db), FakeDing())
base = datetime.now(TZ8) - timedelta(minutes=6)
texts = [
    alert_text(base, "改签底座-改签预定指标", "【国内旗舰店】-成功率", "5762479886"),
    alert_text(base + timedelta(minutes=2), "改签底座-改签预定指标", "【国内旗舰店】-成功率",
               "10054069668141"),
    alert_text(base + timedelta(minutes=5), "改签底座-offer生单指标", "国内-offer生单成功率",
               "10054069668141"),
]
rs = []
for i, t in enumerate(texts):
    rs.append(asyncio.run(p.process({"msg_id": f"b{i}", "group": "g1", "sender": "sunfire",
                                     "text": t, "at_me": False})))
check("窗口内逐条进批不立即分析",
      all(r.get("batched") for r in rs) and rs[-1].get("batch_size") == 3, str(rs))
check("进批期间不产生 run", db.one("SELECT COUNT(*) c FROM runs")["c"] == 0)
check("窗口内报警不被 cooldown 丢弃",
      db.one("SELECT COUNT(*) c FROM messages WHERE matched_rule='cooldown'")["c"] == 0)

asyncio.run(p._flush("g1"))
check("聚合批只产生 1 个 run", db.one("SELECT COUNT(*) c FROM runs")["c"] == 1)
check("3 条消息共享同一 run_id",
      db.one("SELECT COUNT(DISTINCT run_id) c FROM messages WHERE msg_id LIKE 'b%'")["c"] == 1
      and db.one("SELECT COUNT(*) c FROM messages WHERE msg_id LIKE 'b%'")["c"] == 3)
check("留 alert_batch_analyzed 审计",
      db.one("SELECT 1 FROM audit_logs WHERE action='alert_batch_analyzed'") is not None)
rows = db.q("SELECT payload FROM auth_exec WHERE entry_id='dt-reply'")
check("聚合批只生成 1 条待回复", len(rows) == 1, str(len(rows)))
md = json.loads(rows[0]["payload"])["markdown"]
check("聚合回复头一行列全部报警",
      md.startswith("**LocalAgent 分析结论（仅供参考）**\n> 聚合分析 3 条报警：")
      and "改签底座-改签预定指标 · 【国内旗舰店】-成功率" in md
      and "改签底座-offer生单指标 · 国内-offer生单成功率" in md, md[:200])
check("聚合回复含全部预警时间",
      all(f"{(base + timedelta(minutes=m)).strftime('%H:%M')}" in md for m in (0, 2, 5)), md[:200])
check("回复无空行（紧凑格式）", "\n\n" not in md, repr(md[:120]))

# 窗口关闭后 cooldown 仍生效（300 秒内新开批被拦）
r_cd = asyncio.run(p.process({"msg_id": "b9", "group": "g1", "sender": "sunfire",
                              "text": alert_text(base + timedelta(minutes=3),
                                                 "改签底座-改签预定指标", "【国内旗舰店】-成功率",
                                                 "6666666666666"),
                              "at_me": False}))
check("聚合后冷却期内新报警走 cooldown", r_cd.get("reason") == "cooldown", str(r_cd))

# ---------- 2. no_aggregate 直通（模拟注入/@我路径不聚合） ----------
r_na = asyncio.run(p.process({"msg_id": "na1", "group": "g1", "sender": "sunfire",
                              "text": alert_text(base + timedelta(minutes=4),
                                                 "改签底座-改签预定指标", "【国内旗舰店】-成功率",
                                                 "7777777777777"),
                              "at_me": False, "no_aggregate": True}))
check("no_aggregate 直通即时分析", r_na.get("run_id") and not r_na.get("batched"), str(r_na))

# ---------- 3. 单条回复格式（无空行、身份头齐全） ----------
res = {"summary": "订单5762479886预订变价校验失败，单用户重试",
       "anomalies": [{"severity": "P3", "summary": "价格明细与总价不一致"}]}
md1 = p._build_reply_markdown(res, texts[0], received_at="2026-08-18T20:48:00+08:00")
check("单条回复紧凑无空行", "\n\n" not in md1, repr(md1[:120]))
check("单条回复身份头完整",
      "> 报警：改签底座-改签预定指标 · 【国内旗舰店】-成功率｜change-flight-tp" in md1
      and "> 预警时间" in md1 and "采集 08-18 20:48" in md1, md1[:200])

# ---------- 4. 队列解耦：enqueue + worker 消费 ----------
ws2 = tempfile.mkdtemp()
db2 = DB(os.path.join(ws2, "t.sqlite"))


class QCfg(AggCfg):
    workspace = ws2
    notify = {}  # mock 默认 agg=0 即时分析


p2 = Pipeline(QCfg(), db2, Notifier(QCfg(), db2), FakeDing())


async def worker_case():
    task = asyncio.ensure_future(p2.worker_loop())
    await p2.enqueue({"msg_id": "q1", "group": "g1", "sender": "sunfire",
                      "text": alert_text(datetime.now(TZ8) - timedelta(minutes=1),
                                         "改签底座-改签预定指标", "【国内旗舰店】-成功率",
                                         "8888888888888"),
                      "at_me": False})
    for _ in range(50):
        if db2.one("SELECT COUNT(*) c FROM runs")["c"] >= 1:
            break
        await asyncio.sleep(0.1)
    task.cancel()
    return db2.one("SELECT COUNT(*) c FROM runs")["c"]


check("enqueue+worker 串行消费产出 run", asyncio.run(worker_case()) == 1)
main_src = open(os.path.join(os.path.dirname(__file__), "..", "localagent", "main.py")).read()
check("轮询入口已切换为 enqueue", "ding.on_message = pipeline.enqueue" in main_src)
check("worker_loop 已随服务启动", "pipeline.worker_loop()" in main_src)

print(f"\n{sum(1 for _, ok in PASS if ok)}/{len(PASS)} passed")
