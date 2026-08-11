import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from localagent.config import Config
from localagent.db import DB
from localagent.dingtalk import DwsDingTalk
from localagent.matcher import parse_sunfire_alert

SAMPLE = """2026-08-04 21:40
华扬
change-flight-tp
改签底座-offer生单指标
国内-offer生单成功率
共有1条数据触发[critical]报警，摘要：
* [国内,C-1-1009,选择乘机人已值机不可改,b2c]  成功率  [当前值为: 0] 最近10分钟求平均: 50 < 80,失败数 [当前值为: 1] 最近10分钟求和值: 11 > 1, 采样:  11.80.242.170#Err#213e01ef17858508501112040e1247
@华扬(主班) @安心(备班)
https://x.alibaba-inc.com/custom/59/product/preview/spm/18093"""


async def main():
    p = parse_sunfire_alert(SAMPLE)
    assert p and p["app"] == "change-flight-tp", p
    assert p["severity"] == "P1" and p["trace_id"] == "213e01ef17858508501112040e1247", p
    assert p["sample_ip"] == "11.80.242.170", p
    print("PASS parse_sunfire_alert:", p["app"], p["severity"], p["trace_id"])
    assert parse_sunfire_alert("普通聊天消息") is None
    print("PASS parse non-alert -> None")

    cfg = Config()
    db = DB(os.path.join(cfg.workspace, "data", "localagent.sqlite"))
    collected = []

    async def collector(msg):
        collected.append(msg)

    ding = DwsDingTalk(cfg, db, collector)
    hits = await ding.poll_once(None)
    print(f"PASS poll_once: hits={hits}, collected={len(collected)}")
    for m in collected[:5]:
        kind = "ALERT" if parse_sunfire_alert(m["text"]) else ("AT_ME" if m["at_me"] else "OTHER")
        print(f"  [{kind}] {m['group']} | {m['sender']} | {m['text'][:50].replace(chr(10),' ')}")


asyncio.run(main())
