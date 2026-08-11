"""V2 回归：钉群消息治理 / 展示体验 / 引擎取证策略。
运行：./.venv/bin/python tests/test_v2_ui_engine.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from localagent import engine, render, reports
from localagent.db import DB, now
from localagent.pipeline import Pipeline

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


# ---------- 1. 引擎取证策略段（记忆 → skill → MCP） ----------
T = engine.PROMPT_TEMPLATE
check("策略段标题存在", "取证通道优先级" in T)
check("P1 记忆优先", "P1 记忆优先" in T and "memory skill" in T)
check("P2 日志必须走 flyeye-log-query skill",
      "P2 skill 取证" in T and "必须使用 `flyeye-log-query` skill" in T)
check("P2 禁止直调 flyeye MCP", "严禁直接调用 flyeye MCP" in T)
check("P3 降级需标注通道", "P3 降级 MCP" in T and "标注实际所用通道" in T)
check("P4 未取证禁止臆断", "P4 未取证兜底" in T and "未取证" in T and "臆断归因" in T)
prompt = T.format(context="订单 91234567890 报警")
check("prompt 渲染后仍含策略文本",
      "取证通道优先级" in prompt and "flyeye-log-query" in prompt and "未取证" in prompt)

# ---------- 2. 钉群 Markdown 降噪 ----------
RAW = ("### 改签履约-审计汇总-告警定时播报2026-08-07 10:30\n\n"
       "**今日未完结:2个**\n\n-----\n\n"
       "**1.<font color=#FF0000 >【高危场景】</font>【BCP】"
       "[国际改签状态流转基础信息审计(TRP_INTER_MODIFY_STATUS_ADUIT)]"
       "(http://bcp.alibaba-inc.com/rules/errorlist?processStatus=0&ruleCode=TRP_X)**\n\n"
       "**告警状态:** 未处理 [接手]( dingtalk://x?id%3D88485759&pc_slide=true)\n\n"
       "裸链接 https://bcp.alibaba-inc.com/rules/errorlist?processStatus=0&ruleCode=TRP_LONG_CODE_XYZ\n")
c = render.clean_ding_text(RAW)
check("去除 font 标签", "<font" not in c and "</font>" not in c and "高危场景" in c)
check("去除粗体/标题/分隔线标记", "**" not in c and "###" not in c and "-----" not in c)
check("链接只留文案", "dingtalk://" not in c and "http://bcp.alibaba-inc.com/rules" not in c
      and "接手" in c and "国际改签状态流转基础信息审计" in c, c)
check("裸链接收敛为域名", "bcp.alibaba-inc.com/…" in c, c)
check("无连续空行", "\n\n\n" not in c)
check("截断带省略号", render.clean_ding_text(RAW, 20).endswith("…"))
check("空文本安全", render.clean_ding_text(None) == "" and render.clean_ding_text("") == "")

# ---------- 3. 报告 JSON 侧车 + 渲染页 ----------
ws = tempfile.mkdtemp()
db = DB(os.path.join(ws, "t.sqlite"))


class Cfg:
    workspace = ws


result = {"normal": False, "summary": "改签状态不一致",
          "conclusion": "改签单 bizStatus=30 与 orderStatus 不一致，需订正。",
          "evidence": [{"action": "skill:flyeye-log-query 查询 eagleEyeId=abc",
                        "finding": "命中 ERROR 日志 3 条，状态机流转异常"},
                       {"action": "memory 检索同类告警", "finding": "历史处置：订正 subStatus"}],
          "anomalies": [{"severity": "P2", "summary": "国际改签状态流转不一致"}],
          "suggestions": [{"app": "ateye", "feature": "改签单subStatus订正",
                           "action_type": "data_correction", "params": {"modifyId": "1"}}]}
db.insert("runs", run_id="run-v2", task_id="t", trigger_type="dingtalk_alert", source="改签监控群",
          status="running", engine=None, engine_version=None, started_at=now(),
          finished_at=None, report_path=None, error_msg=None, source_text=RAW)
rel = reports.write_report(Cfg(), db, "run-v2", result, "改签监控群", "qodercli")
check("md 报告落盘", os.path.exists(os.path.join(ws, rel)))
check("json 侧车落盘", os.path.exists(os.path.join(ws, rel[:-3] + ".json")))
d = render.load_report(ws, rel)
check("侧车优先加载", d["_src"] == "json" and d["conclusion"] == result["conclusion"]
      and len(d["evidence"]) == 2, str(d)[:120])
check("侧车含判定与级别", d["verdict"] == "有问题" and d["anomalies"][0]["severity"] == "P2")
html = render.render_report_html(d, rel)
check("渲染含判定徽标与结论置顶", "有问题" in html and "最高级别 P2" in html
      and html.index("bizStatus=30") < html.index("异常明细"), "结论未置顶")
check("异常明细表格化", "<table>" in html and "国际改签状态流转不一致" in html)
check("证据时间线可折叠", html.count("<details>") >= 2 and "排查证据（2 步" in html)
check("证据默认折叠(无 open)", "<details open" not in html)
check("建议动作与审计分区", "建议动作" in html and "审计摘要" in html and "subStatus订正" in html)
check("提供原始 Markdown 入口", "/reports/by-path?p=" + rel in html)

# 旧报告兼容：删除侧车后回退解析 Markdown
os.remove(os.path.join(ws, rel[:-3] + ".json"))
d2 = render.load_report(ws, rel)
check("旧报告回退解析 md", d2["_src"] == "md" and d2["verdict"] == "有问题"
      and d2["conclusion"].startswith("改签单 bizStatus=30"), str(d2)[:150])
check("旧报告解析出证据与异常", len(d2["evidence"]) == 2
      and d2["evidence"][0]["action"].startswith("skill:flyeye-log-query")
      and d2["anomalies"][0]["severity"] == "P2", str(d2["evidence"]))
html2 = render.render_report_html(d2, rel)
check("旧报告同样渲染为结论置顶页", "有问题" in html2 and "排查证据（2 步" in html2)
check("报告缺失返回 None", render.load_report(ws, "reports/none/x.md") is None)
check("None 渲染兜底", "报告不存在" in render.render_report_html(None))

# ---------- 4. 非@我静默开关 ----------
os.makedirs(os.path.join(ws, "config"), exist_ok=True)


class Notif:
    def toast(self, *a, **k):
        pass

    def raise_alerts(self, *a, **k):
        pass


class Ding:
    def reply(self, *a, **k):
        pass


class PCfg:
    workspace = ws
    agent = {"writes_disabled": False}
    notify = {"cooldown_seconds": 0}
    dingtalk = {"listen_all": False}
    auth_entries = [{"id": "read-g", "app": "dingtalk", "scope": "read",
                     "feature": "读取群消息", "enabled": True,
                     "constraints": {"groups": ["改签监控群"]},
                     "alertRules": [{"type": "keyword", "keywords": ["报警", "告警"]}]},
                    {"id": "read-atme", "app": "dingtalk", "scope": "read",
                     "feature": "读取群消息 at_me", "enabled": True,
                     "constraints": {"groups": ["改签监控群"]}}]
    groups = [{"name": "改签监控群", "mode": "alert"}]
    solutions = []
    mock = True


p = Pipeline(PCfg(), db, Notif(), Ding())
ALERT = "【报警】change-flight-tp 改签 金额不一致 订单号 91234567890 差异45元 P2"
r = asyncio.run(p.process({"msg_id": "v2-1", "group": "改签监控群", "sender": "sunfire",
                           "text": ALERT, "at_me": False}))
check("开关关闭：非@我不处理", not r.get("handled") and r["reason"] == "non_at_me_silent", str(r))
check("开关关闭：非@我不入库", db.one("SELECT * FROM messages WHERE msg_id='v2-1'") is None)
r = asyncio.run(p.process({"msg_id": "v2-2", "group": "改签监控群", "sender": "huayang",
                           "text": "@我 分析这条报警 订单号 91234567890", "at_me": True}))
check("开关关闭：@我仍处理", r.get("handled") and r.get("run_id"), str(r))
PCfg.dingtalk = {"listen_all": True}
p2 = Pipeline(PCfg(), db, Notif(), Ding())
r = asyncio.run(p2.process({"msg_id": "v2-3", "group": "改签监控群", "sender": "sunfire",
                            "text": ALERT, "at_me": False}))
check("开关开启：非@我正常分析入库", r.get("handled") and r.get("run_id")
      and db.one("SELECT * FROM messages WHERE msg_id='v2-3'") is not None, str(r))

# ---------- 5. 后台页面：消息清单分页 + 报告渲染页 ----------
from localagent.webapp import build_app


class Ctx:
    pass


ctx = Ctx()
ctx.db = db
ctx.cfg = PCfg()
ctx.cfg.web = {"host": "127.0.0.1", "port": 8765}
ctx.pipeline = p2
ctx.ding = Ding()


class N2(Notif):
    def pending(self):
        return []


ctx.notifier = N2()
for i in range(25):
    db.insert("messages", ignore=True, msg_id=f"pg-{i}", group_name="改签监控群",
              sender="sunfire", received_at=now(), matched_entry_id="read-g",
              matched_rule="alert", run_id=None, source_text=RAW)
app = build_app(ctx)
ep = {}
for r in app.routes:
    if hasattr(r, "endpoint"):
        ep.setdefault(getattr(r, "path", ""), r.endpoint)


def html_of(path, **kw):
    return ep[path](**kw).body.decode()


h1 = html_of("/alerts")
check("消息清单首屏正好 20 条", h1.count("原始全文") == 20, str(h1.count("原始全文")))
check("默认近 2 天窗口", "钉群消息清单（2 天内共" in h1, h1[h1.find("钉群消息清单"):][:60])
check("提供时间范围切换", "msg_days=1" in h1 and "msg_days=7" in h1)
check("提供翻页入口", "msg_page=2" in h1)
check("清单已降噪(无 font 标签)", "&lt;font" not in h1.split("原始全文")[0])
check("结构化呈现来源群与告警码位", "改签监控群" in h1 and "border-radius:8px" in h1)
h2 = html_of("/alerts", msg_page=2)
check("翻页可用且可回上一页", "msg_page=1" in h2 and "第 2/" in h2)
rv = html_of("/reports/view", p=rel)
check("报告渲染页结论先见 + 证据折叠",
      "有问题" in rv and "排查证据" in rv and "<details>" in rv and "异常明细" in rv)
check("渲染页沿用后台深色主题布局", "LocalAgent 管理页面" in rv and "#0f1417" in rv)
rep = db.one("SELECT report_id FROM reports_meta WHERE run_id='run-v2'")
rv2 = html_of("/reports/{report_id}", report_id=rep["report_id"])
check("报告详情页也是渲染页", "异常明细" in rv2 and "排查证据" in rv2)
bad = html_of("/reports/view", p="../../etc/passwd")
check("越权路径被拒", "非法路径" in bad or "报告不存在" in bad)

print(f"\n全部 {len(PASS)} 项断言通过")
