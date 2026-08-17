import json
import os
import resource
import threading

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse

from .db import now

LAYOUT = """<!doctype html><html><head><meta charset="utf-8"><title>LocalAgent</title>
<style>
body{font-family:-apple-system,'PingFang SC',sans-serif;margin:0;background:#0f1417;color:#e6edf3}
header{padding:14px 24px;background:#161d21;border-bottom:1px solid #22303a;display:flex;gap:18px;align-items:center}
header a{color:#7ee7b0;text-decoration:none;font-size:14px}
header .badge{background:#f59e0b;color:#111;border-radius:10px;padding:2px 8px;font-size:12px}
main{padding:24px;max-width:960px;margin:0 auto}
.card{background:#161d21;border:1px solid #22303a;border-radius:10px;padding:16px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:6px 8px;border-bottom:1px solid #22303a;text-align:left;vertical-align:top}
.ok{color:#7ee7b0}.warn{color:#f59e0b}.err{color:#f87171}
button{background:#00c16a;border:0;color:#06281a;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px}
button.gray{background:#374151;color:#e6edf3}
button.red{background:#f87171;color:#111}
h2{font-size:16px;margin:0 0 12px}
pre{white-space:pre-wrap;font-size:12px;color:#9fb3c0}
input,select{background:#0f1417;color:#e6edf3;border:1px solid #22303a;border-radius:6px;padding:4px 8px;font-size:12px}
</style></head><body><header>
<b style="color:#00c16a">●</b> <b>LocalAgent 管理页面</b>
<a href="/">状态</a><a href="/history">历史记录</a><a href="/alerts">报警中心 {badge}</a>
<a href="/reports">报告</a><a href="/authlist">授权清单</a><a href="/solutions">方案库</a><a href="/groups">钉群配置</a><a href="/audit">审计日志</a><a href="/storage">存储管理</a>
</header><main>{body}</main></body></html>"""

TEST_FILTER = "(r.trigger_type IS NULL OR r.trigger_type != 'simulate')"


def build_app(app_ctx):
    app = FastAPI(docs_url=None, redoc_url=None)
    db = app_ctx.db
    cfg = app_ctx.cfg

    @app.middleware("http")
    async def token_guard(request: Request, call_next):
        token = app_ctx.cfg.web.get("token", "")
        path = request.url.path
        if not token or path == "/api/state" or path.startswith("/assets"):
            return await call_next(request)
        supplied = request.query_params.get("token") or request.cookies.get("la_token")
        if supplied != token:
            return HTMLResponse("<h3>403 需要访问令牌（在 URL 后追加 ?token=xxx）</h3>", status_code=403)
        resp = await call_next(request)
        if request.query_params.get("token") == token:
            resp.set_cookie("la_token", token, max_age=30 * 86400)
        return resp

    def page(body, badge=""):
        html = LAYOUT.replace("{body}", body).replace(
            "{badge}", f'<span class="badge">{badge}</span>' if badge else "")
        return HTMLResponse(html)

    @app.get("/", response_class=HTMLResponse)
    def status():
        from datetime import datetime, timedelta
        from .dingtalk import CST
        cutoff_24h = (datetime.now(CST) - timedelta(hours=24)).isoformat(timespec="seconds")
        conn = db.get_state("dingtalk_conn", "unknown")
        runs = db.q("SELECT COUNT(*) c FROM runs")
        today_alerts = db.q("SELECT COUNT(*) c FROM alerts WHERE date(created_at)=date('now','localtime')")
        pending = len(app_ctx.notifier.pending())
        cur = db.q("SELECT run_id, source, status, started_at FROM runs WHERE started_at >= ? ORDER BY started_at DESC LIMIT 5", cutoff_24h)
        running = db.q("SELECT run_id, source, started_at FROM runs WHERE status='running'")
        mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
        versions = db.get_state("engine_versions", "{}")
        writes_disabled = bool(app_ctx.cfg.agent.get("writes_disabled", False))
        paused = db.get_state("tasks_paused") == "1"
        unavail = db.one("SELECT COUNT(*) c FROM runs WHERE status='engine_unavailable'")["c"]
        try:
            probe = json.loads(db.get_state("engine_probe", "{}") or "{}")
        except Exception:
            probe = {}
        skw = db.get_state("skill_config_warnings")
        if skw is None:
            skw_cls, skw_txt = "", "未探测"
        elif skw == "0":
            skw_cls, skw_txt = "ok", "0 条（skill 全部正常注册）"
        else:
            skw_cls, skw_txt = "warn", (
                f"{skw} 条扫描预算提示（skill 发现深度上限 4，官方包 pptx/docx 的 schemas 超深所致；"
                "已核查全部 skill 正常注册、取证不受影响。排查：tests/validate_skill_configs.py，"
                "明细：~/.qoder/logs/latest/qodercli.log 搜 [SkillManager]）")
        probe_txt = "未探测"
        if probe:
            probe_txt = "　".join(
                "<span class='%s'>%s=%s</span>" % ("ok" if v == "ok" else "err", k, v)
                for k, v in probe.items())
            probe_txt += ("<span style='color:#9fb3c0;font-size:11px'>（探测于 %s）</span>"
                          % db.get_state("engine_probe_at", "-"))
        trend_r = {r["d"]: r["c"] for r in db.q(
            "SELECT substr(started_at,1,10) d, COUNT(*) c FROM runs "
            "WHERE date(started_at) >= date('now','localtime','-6 day') GROUP BY d")}
        trend_a = {r["d"]: r["c"] for r in db.q(
            "SELECT substr(created_at,1,10) d, COUNT(*) c FROM alerts "
            "WHERE date(created_at) >= date('now','localtime','-6 day') GROUP BY d")}
        days7 = [ (datetime.now(CST) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
        max_c = max([trend_r.get(d, 0) + trend_a.get(d, 0) for d in days7] + [1])
        bars = "".join(
            f"<div style='flex:1;text-align:center'>"
            f"<div style='display:flex;gap:2px;align-items:flex-end;height:64px;justify-content:center'>"
            f"<div title='执行 {trend_r.get(d,0)}' style='width:12px;height:{max(2, int(trend_r.get(d,0)/max_c*60))}px;background:#38bdf8;border-radius:2px 2px 0 0'></div>"
            f"<div title='报警 {trend_a.get(d,0)}' style='width:12px;height:{max(2, int(trend_a.get(d,0)/max_c*60))}px;background:#f59e0b;border-radius:2px 2px 0 0'></div>"
            f"</div><div style='font-size:10px;color:#9fb3c0'>{d[5:]}</div></div>"
            for d in days7)
        unavail_link = ("<a style='color:#7ee7b0' href='/history?days=7&"
                        "status_f=engine_unavailable'>查看并一键重跑</a>" if unavail else "-")
        body = f"""<script>setTimeout(()=>location.reload(), 5000)</script>
        <div class="card"><h2>实时状态（5 秒自动刷新）</h2><table>
        <tr><td>常驻进程</td><td class="ok">running（启动于 {db.get_state('agent_started_at', '-')}，内存 {mem_mb:.0f} MB）</td></tr>
        <tr><td>钉钉连接</td><td class="{ 'ok' if conn in ('connected','mock','dws_polling') else 'warn' }">{'● 健康' if conn in ('connected','mock','dws_polling') else '● 异常'}（{conn}）</td></tr>
        <tr><td>上次轮询 / 最近命中</td><td>{db.get_state('dws_last_poll','-') or '-'} / {db.get_state('dws_last_hit','-') or '-'}</td></tr>
        <tr><td>引擎可用性</td><td>{db.get_state('engine_versions','-')}</td></tr>
        <tr><td>引擎/模型探针</td><td>{probe_txt}</td></tr>
        <tr><td>Skill 配置告警</td><td class="{skw_cls}">{skw_txt}</td></tr>
        <tr><td>引擎资源不可用（可重试）</td><td class="{'warn' if unavail else 'ok'}">{unavail} 条</td><td>{unavail_link}</td></tr>
        <tr><td>累计执行 / 今日异常 / 待确认</td><td>{runs[0]['c']} / {today_alerts[0]['c']} /
            <span class="{'warn' if pending else 'ok'}">{pending}</span></td></tr>
        <tr><td>写操作紧急开关</td><td class="{'err' if writes_disabled else 'ok'}">{'已禁用全部写操作' if writes_disabled else '正常'}</td>
            <td><button class="{'red' if not writes_disabled else 'gray'}" onclick="fetch('/api/settings/writes_toggle',{{method:'POST'}}).then(()=>location.reload())">{'一键禁用所有写操作' if not writes_disabled else '恢复写操作'}</button></td></tr>
        <tr><td>任务调度</td><td class="{'warn' if paused else 'ok'}">{'已暂停' if paused else '运行中'}</td>
            <td><button class="gray" onclick="fetch('/api/settings/pause_toggle',{{method:'POST'}}).then(()=>location.reload())">{'恢复任务' if paused else '暂停所有任务'}</button></td></tr>
        <tr><td>群回复</td><td>{'开' if app_ctx.cfg.dingtalk.get('reply_enabled') else '关'}</td>
            <td><button class='gray' onclick="fetch('/api/settings/reply_toggle',{{method:'POST'}}).then(()=>location.reload())">切换</button></td></tr>
        </table></div>
        <div class="card"><h2>执行中的任务实例</h2><table>
        {''.join(f"<tr><td>{r['run_id']}</td><td>{r['source']}</td><td>{r['started_at']}</td></tr>" for r in running) or '<tr><td>无</td></tr>'}
        </table></div>
        <div class="card"><h2>最近执行</h2><table>
        <tr><th>run</th><th>来源</th><th>状态</th><th>开始</th></tr>
        {''.join(f"<tr><td>{r['run_id']}</td><td>{r['source']}</td><td>{r['status']}</td><td>{r['started_at']}</td></tr>" for r in cur) or '<tr><td>无（最近24小时无执行）</td></tr>'}
        </table></div>
        <div class="card"><h2>最近 7 天趋势（<span style="color:#38bdf8">蓝=执行</span> / <span style="color:#f59e0b">橙=报警</span>）</h2>
        <div style="display:flex;gap:8px">{bars}</div></div>"""
        return page(body, pending or "")

    @app.get("/history", response_class=HTMLResponse)
    def history(days: int = 1, trigger: str = "", status_f: str = "", source: str = "",
                show_test: int = 0, page_n: int = 1):
        from datetime import datetime, timedelta
        from .dingtalk import CST
        days = min(days, 30)
        cutoff = (datetime.now(CST) - timedelta(days=days)).isoformat(timespec="seconds")
        sql = ("SELECT r.* FROM runs r LEFT JOIN messages m ON r.run_id=m.run_id "
               f"WHERE r.started_at >= ? AND ({TEST_FILTER} OR ?)")
        args = [cutoff, show_test]
        if trigger:
            sql += " AND r.trigger_type=?"; args.append(trigger)
        if status_f:
            sql += " AND r.status=?"; args.append(status_f)
        if source:
            sql += " AND r.source LIKE ?"; args.append(f"%{source}%")
        total = db.one(f"SELECT COUNT(*) c FROM ({sql})", *args)["c"]
        per = 50
        pages = max(1, (total + per - 1) // per)
        page_n = max(1, min(page_n, pages))
        rows = db.q(sql + f" ORDER BY r.started_at DESC LIMIT {per} OFFSET {(page_n-1)*per}", *args)
        opts = lambda cur, vals: "".join(
            f"<option value='{v}'{' selected' if v == cur else ''}>{v or '全部'}</option>" for v in vals)
        qs = f"days={days}&trigger={trigger}&status_f={status_f}&source={source}&show_test={show_test}"
        body = f"<div class='card'><h2>历史工作记录（最近 {days} 天，共 {total} 条，第 {page_n}/{pages} 页）</h2>"
        body += "<p>" + " ".join(
            f"<a style='color:#7ee7b0' href='/history?days={d}'>{d}天</a>" for d in (1, 7, 30)) \
            + f" <a style='color:#38bdf8' href='/history.csv?{qs}'>导出 CSV</a></p>"
        body += (f"<form method='get'><input type='hidden' name='days' value='{days}'>"
                 f"触发 <select name='trigger'>{opts(trigger, ['', 'dingtalk_alert', 'dingtalk_at_me', 'reanalyze', 'simulate'])}</select> "
                 f"状态 <select name='status_f'>{opts(status_f, ['', 'success', 'failed', 'running', 'engine_unavailable'])}</select> "
                 f"来源 <input name='source' value='{source}' placeholder='群名/来源'> "
                 f"<label><input type='checkbox' name='show_test' value='1' {'checked' if show_test else ''}> 含测试注入</label> "
                 f"<button class='gray'>筛选</button></form>")
        body += ("<script>async function rerun(run){"
                 "if(!confirm('引擎资源不可用，按原始报警内容重跑分析？将自动按模型降级链选择可用模型。'))return;"
                 "const r=await fetch('/api/rerun/'+run,{method:'POST'});const j=await r.json();"
                 "alert(j.submitted?('已提交重跑，后台执行中（原 run：'+j.run_id+'）'):('失败：'+(j.error||'')));location.reload()}</script>")
        body += ("<script>async function retry(run){if(!confirm('对该失败任务发起重新分析？'))return;"
                 "const r=await fetch('/api/reanalyze/'+run,{method:'POST',headers:{'Content-Type':'application/json'},"
                 "body:JSON.stringify({note:'失败重试'})});const j=await r.json();"
                 "alert(j.new_run?('已触发重新分析：'+j.new_run):('失败：'+(j.error||'')));location.reload()}</script>")
        body += "<table><tr><th>run</th><th>触发</th><th>来源</th><th>状态</th><th>引擎</th><th>开始</th><th>报告</th><th>操作</th></tr>"
        for r in rows:
            if r["status"] == "engine_unavailable":
                op = f"<button onclick=\"rerun('{r['run_id']}')\">重跑</button>"
            elif r["status"] == "failed":
                op = f"<button class='gray' onclick=\"retry('{r['run_id']}')\">重试</button>"
            else:
                op = "-"
            body += (f"<tr><td><a style='color:#7ee7b0' href='/history/{r['run_id']}'>{r['run_id']}</a></td>"
                     f"<td>{r['trigger_type']}</td><td>{r['source']}</td><td>{r['status']}</td>"
                     f"<td>{r['engine'] or '-'}</td><td>{r['started_at']}</td>"
                     f"<td>{r['report_path'] or '-'}</td><td>{op}</td></tr>")
        body += "</table>"
        if pages > 1:
            nav = []
            if page_n > 1:
                nav.append(f"<a style='color:#7ee7b0' href='/history?{qs}&page_n={page_n-1}'>上一页</a>")
            nav.append(f"第 {page_n}/{pages} 页")
            if page_n < pages:
                nav.append(f"<a style='color:#7ee7b0' href='/history?{qs}&page_n={page_n+1}'>下一页</a>")
            body += "<p>" + " ".join(nav) + "</p>"
        return page(body + "</div>")

    @app.get("/history.csv")
    def history_csv(days: int = 1, trigger: str = "", status_f: str = "", source: str = "",
                    show_test: int = 0):
        from datetime import datetime, timedelta
        from fastapi.responses import Response
        from .dingtalk import CST
        days = min(days, 30)
        cutoff = (datetime.now(CST) - timedelta(days=days)).isoformat(timespec="seconds")
        sql = ("SELECT r.run_id, r.trigger_type, r.source, r.status, r.engine, r.started_at, "
               "r.finished_at, r.report_path, r.error_msg FROM runs r "
               f"WHERE r.started_at >= ? AND ({TEST_FILTER} OR ?)")
        args = [cutoff, show_test]
        if trigger:
            sql += " AND r.trigger_type=?"; args.append(trigger)
        if status_f:
            sql += " AND r.status=?"; args.append(status_f)
        if source:
            sql += " AND r.source LIKE ?"; args.append(f"%{source}%")
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["run_id", "trigger", "source", "status", "engine", "started_at",
                    "finished_at", "report_path", "error_msg"])
        for r in db.q(sql + " ORDER BY r.started_at DESC LIMIT 5000", *args):
            w.writerow([r["run_id"], r["trigger_type"], r["source"], r["status"], r["engine"],
                        r["started_at"], r["finished_at"], r["report_path"], r["error_msg"]])
        return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=localagent_history.csv"})

    @app.get("/history/{run_id}", response_class=HTMLResponse)
    def run_detail(run_id: str):
        r = db.one("SELECT * FROM runs WHERE run_id=?", run_id)
        msgs = db.q("SELECT * FROM messages WHERE run_id=?", run_id)
        auth = db.q("SELECT * FROM auth_exec WHERE run_id=?", run_id)
        audits = db.q("SELECT * FROM audit_logs WHERE run_id=? ORDER BY log_id", run_id)
        body = f"""<div class="card"><h2>{run_id}</h2><pre>{json.dumps(dict(r), ensure_ascii=False, indent=1)}</pre></div>
        <div class="card"><h2>触发消息</h2><pre>{json.dumps([dict(m) for m in msgs], ensure_ascii=False, indent=1)}</pre></div>
        <div class="card"><h2>授权判定</h2><pre>{json.dumps([dict(a) for a in auth], ensure_ascii=False, indent=1)}</pre></div>
        <div class="card"><h2>执行链路（审计）</h2><pre>{chr(10).join(f"{a['ts']} [{a['category']}] {a['action']} {a['target']} {a['detail'][:120]}" for a in audits)}</pre></div>"""
        return page(body)

    @app.get("/alerts", response_class=HTMLResponse)
    def alerts(show_test: int = 0, range: str = "2h", sev: str = "", kw: str = "",
               f_group: str = "", f_rule: str = "", msg_page: int = 1):
        from datetime import datetime, timedelta
        from .dingtalk import CST
        if range not in ("2h", "today", "yesterday", "3d"):
            range = "2h"
        _d0 = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = {"2h": datetime.now(CST) - timedelta(hours=2),
                  "today": _d0,
                  "yesterday": _d0 - timedelta(days=1),
                  "3d": datetime.now(CST) - timedelta(days=3)}[range]
        cutoff = cutoff.isoformat(timespec="seconds")
        range_label = {"2h": "最近2小时", "today": "今天", "yesterday": "昨天起", "3d": "近3天"}[range]

        def sol_codes_for(a):
            from . import solutions as solmod
            codes = []
            m = db.one("SELECT parsed_json FROM messages WHERE run_id=?", a["run_id"])
            if m and m["parsed_json"]:
                try:
                    pj = json.loads(m["parsed_json"])
                    for it in pj.get("items", []):
                        if it.get("code"):
                            codes.append(it["code"])
                except Exception:
                    pass
            sols = solmod.load_solutions(app_ctx.cfg.workspace)
            matched = []
            for c in codes:
                s = solmod.get_solution(sols, c)
                if s and c not in [x[0] for x in matched]:
                    req = []
                    for act in solmod.write_actions(s):
                        fixed = set((act.get("fixed_params") or {}).keys())
                        for k in (act.get("params") or []):
                            if k not in fixed and k not in req:
                                req.append(k)
                    matched.append((c, req))
            return matched

        def rows(status):
            sql = ("SELECT a.*, r.report_path AS report_path, r.trigger_type AS trigger_type "
                   "FROM alerts a LEFT JOIN runs r ON a.run_id=r.run_id "
                   f"WHERE a.status=? AND a.created_at >= ? AND ({TEST_FILTER} OR ?)")
            args = [status, cutoff, show_test]
            if sev:
                sql += " AND a.severity=?"; args.append(sev)
            if kw:
                sql += " AND a.summary LIKE ?"; args.append(f"%{kw}%")
            if f_group:
                sql += " AND a.source_group LIKE ?"; args.append(f"%{f_group}%")
            rs = db.q(sql + " ORDER BY a.created_at DESC LIMIT 100", *args)
            out = ""
            for a in rs:
                link = (f"<a style='color:#7ee7b0' href='/reports/view?"
                        f"p={a['report_path']}'>报告</a>" if a["report_path"] else "-")
                btns = ""
                chk = ""
                if status == "pending":
                    chk = f"<td><input type='checkbox' class='sel' value='{a['alert_id']}'></td>"
                    btns = (f"<button onclick=\"act('{a['alert_id']}','ack')\">确认</button> "
                            f"<button class='gray' onclick=\"ignoreA('{a['alert_id']}')\">忽略</button> ")
                btns += f"<button class='gray' onclick=\"reanalyze('{a['run_id']}')\">重新分析</button>"
                for c, req in sol_codes_for(a):
                    btns += (f" <button onclick=\"trigSol('{a['alert_id']}','{c}',"
                             f"{json.dumps(req)})\">触发方案</button>")
                tag = " <span class='warn'>[测试]</span>" if a["trigger_type"] == "simulate" else ""
                out += (f"<tr>{chk}<td>{a['severity']}</td><td>{a['summary']}{tag}</td><td>{a['source_group']}</td>"
                        f"<td>{a['created_at'][11:19]}</td><td>{link}</td><td>{a['status']}</td><td>{btns}</td></tr>")
            return out
        body = """<script>
function act(id,op){fetch('/alerts/'+id+'/'+op,{method:'POST'}).then(r=>{if(!r.ok)throw new Error(r.status);location.reload()}).catch(e=>alert('操作失败（服务可能正在重启），请稍后刷新重试：'+e))}
function ignoreA(id){if(confirm('确认忽略此报警？忽略后进入 4 小时冷却，期间同类报警不再弹窗。'))act(id,'ignore')}
async function bulk(op){const ids=[...document.querySelectorAll('.sel:checked')].map(x=>x.value);
if(!ids.length){alert('请先勾选要操作的报警');return}
if(!confirm('对 '+ids.length+' 条报警执行「'+(op==='ack'?'确认':'忽略')+'」？'))return;
for(const id of ids){await fetch('/alerts/'+id+'/'+op,{method:'POST'})}location.reload()}
async function reanalyze(run){const note=prompt('分析哪里不准？补充你的判断/线索，将携带该输入重新触发分析：');if(note===null)return;
const r=await fetch('/api/reanalyze/'+run,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note:note})});
const j=await r.json();alert(j.new_run?('已触发重新分析：'+j.new_run):('失败：'+(j.error||'')));location.reload()}
async function trigSol(alertId,code,reqParams){let params={};
for(const k of (reqParams||[])){const v=prompt('方案 '+code+' 需要参数 '+k+'，请输入：');if(v===null)return;params[k]=v.trim()}
if(!confirm('手动触发解决方案 '+code+'？\n将按方案生成执行计划并进入二次确认（不会立即执行写操作）。'))return;
const r=await fetch('/api/alerts/'+alertId+'/trigger_solution',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:code,params:params})});
const j=await r.json();if(j.error){alert('触发失败：'+j.error);return}
alert('已生成执行计划 '+j.run_id+'（'+j.steps.length+' 步）。请到「权限设置」页二次确认后再执行。');location.reload()}
async function jfetch(u,opt){const r=await fetch(u,opt);if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}
async function sendReply(id){if(!confirm('确认发送回复到钉群？'))return;try{
const j=await jfetch('/api/auth_exec/'+id+'/send_reply',{method:'POST'});
alert(j.result||('失败：'+(j.error||'未知错误')));location.reload()}catch(e){alert('发送失败：'+e.message)}}
async function rejectReply(id){if(!confirm('确认丢弃该回复？'))return;try{
const j=await jfetch('/api/auth_exec/'+id+'/reject_reply',{method:'POST'});
alert(j.result||('失败：'+(j.error||'未知错误')));location.reload()}catch(e){alert('操作失败：'+e.message)}}
async function saveReply(id){const t=document.getElementById('ed_'+id);try{
const j=await jfetch('/api/auth_exec/'+id+'/edit_reply',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({markdown:t.value})});alert(j.ok?'已保存修改':'失败：'+(j.error||''))}catch(e){alert('保存失败：'+e.message)}}
async function gateSol(code){const r=await fetch('/api/solutions/gate/'+code,{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify({})});const j=await r.json();
alert(j.ok?('门禁已：'+(j.enabled?'开启':'关闭')):(j.error||'失败'));location.reload()}
</script>"""
        # 同类报警聚合卡片（近 10 分钟归组研判）
        from . import correlate as corrmod
        try:
            cutoff10 = (datetime.now(CST) - timedelta(minutes=10)).isoformat(timespec="seconds")
            recent = db.q("SELECT run_id, source_text FROM runs WHERE started_at >= ? LIMIT 200",
                          cutoff10)
            groups = {}
            for r in recent:
                key = corrmod.family_key(r["source_text"] or "")
                if key:
                    g = groups.setdefault(key, {"n": 0, "orders": set()})
                    g["n"] += 1
                    g["orders"] |= set(corrmod.extract_orders(r["source_text"]))
            for key, g in groups.items():
                if g["n"] < 2:
                    continue
                kind = key.split(":", 1)[1]
                m = len(g["orders"])
                if m >= 2:
                    impact = f"<b style='color:#f87171'>多订单批量信号（{m} 订单），重点排查发布/变更关联</b>"
                    style = "border-left:4px solid #f87171"
                else:
                    impact = "单订单重试，影响单一用户"
                    style = "border-left:4px solid #f59e0b"
                body += (f"<div class='card' style='{style};padding:8px 12px;margin:6px 0'>"
                         f"⚠ 同类报警聚合（近10分钟）：{kind} 类 {g['n']} 条 / 涉及订单 {m} 个 / "
                         f"影响面：{impact}</div>")
        except Exception:
            pass
        pr_rows = db.q("SELECT ae.*, r.report_path AS report_path FROM auth_exec ae "
                        "LEFT JOIN runs r ON ae.run_id=r.run_id "
                        "WHERE ae.exec_result='pending_reply' AND ae.ts >= ? ORDER BY ae.id DESC LIMIT 100",
                        cutoff)
        pending_reply_map = {}

        def reply_html(h, anchor_id=""):
            try:
                pl = json.loads(h.get("payload") or "{}")
            except Exception:
                pl = {}
            anom = pl.get("anomalies") or []
            anom_str = "；".join(f"[{a.get('severity')}] {a.get('summary')}" for a in anom) or "-"
            link = (f"<a style='color:#7ee7b0' href='/reports/view?p={h['report_path']}'>报告</a>"
                    if h.get("report_path") else "")
            md = (pl.get("markdown") or "").replace("<", "&lt;")
            anchor = f"id='{anchor_id}' " if anchor_id else ""
            return (f"<div class='reply-block' {anchor}style='margin-top:8px;padding:8px;"
                    f"border:1px dashed #f59e0b;border-radius:6px'>"
                    f"<b style='color:#f59e0b'>待回复到钉群</b> "
                    f"<span style='color:#e6edf3;font-size:12px;border:1px solid #22303a;"
                    f"border-radius:4px;padding:0 4px'>{_esc(pl.get('alert_type') or 'unclassified')}</span> "
                    f"<span style='color:#9fb3c0;font-size:12px'>{_esc(pl.get('group', ''))} · "
                    f"{_esc(pl.get('summary', ''))} · {h['ts']}</span> {link}"
                    f"<div style='font-size:12px;color:#e6edf3;margin:4px 0'>{_esc(anom_str)}</div>"
                    f"<button onclick=\"sendReply({h['id']})\">发送回复</button> "
                    f"<button class='red' onclick=\"rejectReply({h['id']})\">丢弃</button> "
                    f"<details style='display:inline-block;margin-left:8px'><summary style='cursor:pointer;"
                    f"color:#9fb3c0;font-size:12px;display:inline'>编辑内容</summary>"
                    f"<textarea id='ed_{h['id']}' style='width:96%;height:110px;background:#0f1417;color:#e6edf3;"
                    f"border:1px solid #22303a;border-radius:6px;font-size:12px'>{md}</textarea><br>"
                    f"<button class='gray' onclick=\"saveReply({h['id']})\">保存修改</button></details></div>")

        for i, h in enumerate(pr_rows):
            h = dict(h)
            pr_rows[i] = h
            if h.get("run_id"):
                pending_reply_map[h["run_id"]] = h
        if pr_rows:
            body += (f"<p><a href='#reply_first' style='color:#f59e0b;font-weight:bold'>"
                     f"⚠ 待回复 {len(pr_rows)} 条 →</a></p>")
        listen_all = bool(app_ctx.cfg.dingtalk.get("listen_all", False))
        body += (f"<p style='font-size:12px;color:#9fb3c0'>监听范围："
                 f"{'监听处理所有告警消息' if listen_all else '仅处理@我的消息'}"
                 f"（<a style='color:#7ee7b0' href='/groups'>配置</a>）</p>")
        # 消息清单（与报警共用统一时间窗口，20 条/页分页，卡片化展示）
        from . import render as rdr
        msg_sql = ("SELECT m.*, r.status AS run_status, r.report_path AS report_path "
                   "FROM messages m LEFT JOIN runs r ON m.run_id=r.run_id WHERE m.received_at >= ?")
        msg_args = [cutoff]
        if f_group:
            msg_sql += " AND m.group_name LIKE ?"; msg_args.append(f"%{f_group}%")
        if f_rule:
            msg_sql += " AND m.matched_rule=?"; msg_args.append(f_rule)
        if kw:
            msg_sql += " AND m.source_text LIKE ?"; msg_args.append(f"%{kw}%")
        PAGE_SZ = 20
        total_msg = db.one("SELECT COUNT(*) c FROM (" + msg_sql + ")", *msg_args)["c"]
        msg_pages = max(1, (total_msg + PAGE_SZ - 1) // PAGE_SZ)
        msg_page = max(1, min(msg_page, msg_pages))
        msg_rows = db.q(msg_sql + " ORDER BY m.received_at DESC LIMIT ? OFFSET ?",
                        *msg_args, PAGE_SZ, (msg_page - 1) * PAGE_SZ)
        groups = [r["g"] for r in db.q("SELECT DISTINCT group_name g FROM messages WHERE received_at >= ?", cutoff)]
        rules = [r["v"] for r in db.q(
            "SELECT DISTINCT matched_rule v FROM messages WHERE received_at >= ? AND matched_rule IS NOT NULL", cutoff)]
        from . import solutions as solmod
        sols_by_code = {s.get("code"): s for s in app_ctx.cfg.solutions}
        _esc = rdr.esc

        def _broadcast_card(p):
            st = p.get("stats") or {}
            badges = []
            if st.get("today_unfinished") is not None:
                badges.append(f"今日未完结 {st['today_unfinished']}（接手{st.get('today_wait_take', 0)}/"
                              f"反馈{st.get('today_wait_feedback', 0)}/逾期{st.get('today_overdue', 0)}）")
            if st.get("recent_unfinished") is not None:
                badges.append(f"近15日未完结 {st['recent_unfinished']}")
            rows = ""
            for it in p.get("items", []):
                lv = f"<span class='err'>[{_esc(it.get('level'))}]</span> " if it.get("level") else ""
                sol = sols_by_code.get(it.get("code"))
                sol_tag = ""
                if sol:
                    on = bool(sol.get("enabled"))
                    sol_tag = (f" <span class='{'ok' if on else 'warn'}'>[方案已沉淀·门禁{'开启' if on else '关闭'}]</span>"
                               + (f" <button class='gray' style='font-size:10px;padding:1px 6px' "
                                  f"onclick=\"gateSol('{_esc(sol['code'])}')\">切换门禁</button>"
                                  if solmod.write_actions(sol) else ""))
                rows += (f"<tr><td>{it.get('index')}</td>"
                         f"<td>{lv}{_esc(it.get('name'))} <code style='color:#7ee7b0'>{_esc(it.get('code'))}</code>{sol_tag}</td>"
                         f"<td>{_esc(it.get('alert_time'))}</td>"
                         f"<td><a style='color:#38bdf8' target='_blank' href='{_esc(it.get('bcp_url'))}'>BCP</a></td>"
                         f"<td>{_esc(it.get('owner'))}</td></tr>")
            return (f"<div style='margin-top:6px'>"
                    f"<b>{_esc(rdr.clean_ding_text(p.get('title'), 80))}</b> "
                    f"<span style='color:#9fb3c0;font-size:11px'>{'　'.join(badges)}</span>"
                    f"<table style='margin-top:4px'><tr><th>#</th><th>告警</th><th>时间</th><th>链接</th><th>owner</th></tr>"
                    f"{rows}</table></div>")

        _st_style = {"success": ("#7ee7b0", "✓ 已分析"), "failed": ("#f87171", "✗ 分析失败"),
                     "running": ("#f59e0b", "… 分析中"),
                     "engine_unavailable": ("#f59e0b", "⚠ 引擎不可用")}
        _rule_style = {"cooldown": "跳过(冷却)", "unrecognized": "未识别", "no_match": "未匹配",
                       "broadcast_record_only": "记录不分析(仅@我)"}
        msg_out = ""
        reply_anchor = {"done": False}
        for m in msg_rows:
            m = dict(m)
            parsed = None
            if m.get("parsed_json"):
                try:
                    parsed = json.loads(m["parsed_json"])
                except Exception:
                    parsed = None
            codes = [it.get("code") for it in (parsed or {}).get("items", []) if it.get("code")]
            chips = "".join(f"<code style='background:#0f1417;border:1px solid #22303a;border-radius:4px;"
                            f"padding:1px 5px;color:#7ee7b0;font-size:11px;margin-right:4px'>{_esc(c)}</code>"
                            for c in codes[:4])
            if m.get("run_id"):
                color, label = _st_style.get(m.get("run_status") or "", ("#9fb3c0", m.get("run_status") or "-"))
                rp = m.get("report_path")
                st_tag = (f"<a style='color:{color}' href='/reports/view?p={_esc(rp)}'>{label} · 报告</a>"
                          if rp else f"<span style='color:{color}'>{label}</span>")
                if m.get("run_status") in ("failed", "engine_unavailable"):
                    st_tag += (f" <button class='gray' style='font-size:11px;padding:1px 8px' "
                               f"onclick=\"reanalyze('{_esc(m['run_id'])}')\">重新分析</button>")
            else:
                st_tag = (f"<span style='color:#9fb3c0'>"
                          f"{_rule_style.get(m.get('matched_rule') or '', '未分析')}</span>")
            clean = rdr.clean_ding_text(m.get("source_text"))
            body_html = _broadcast_card(parsed) if parsed else (
                f"<div style='margin-top:6px;font-size:13px;color:#e6edf3;white-space:pre-wrap'>"
                f"{_esc(clean[:180])}{'…' if len(clean) > 180 else ''}</div>")
            reply_block = ""
            if m.get("run_id") and m["run_id"] in pending_reply_map:
                anchor = "reply_first" if not reply_anchor["done"] else ""
                reply_anchor["done"] = True
                reply_block = reply_html(pending_reply_map[m["run_id"]], anchor)
            msg_out += (
                "<div style='border:1px solid #22303a;border-radius:8px;padding:10px;margin-bottom:8px;"
                "background:#0f1417'>"
                f"<div style='display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:12px'>"
                f"<span style='color:#9fb3c0'>{_esc((m.get('received_at') or '')[5:19])}</span>"
                f"<span style='background:#22303a;border-radius:4px;padding:1px 8px'>"
                f"{_esc(m.get('group_name'))}</span>"
                f"<span style='color:#9fb3c0'>{_esc(m.get('sender'))}</span>{chips}"
                f"<span style='margin-left:auto'>{st_tag}</span></div>"
                f"{body_html}{reply_block}"
                f"<details style='margin-top:6px'><summary style='cursor:pointer;color:#9fb3c0;"
                f"font-size:11px'>原始全文</summary><pre style='font-size:11px'>"
                f"{_esc(m.get('source_text'))}</pre></details></div>")
        g_opts = "".join(f"<option value='{_esc(g)}'{' selected' if g == f_group else ''}>{_esc(g)}</option>" for g in groups)
        r_opts = "".join(f"<option value='{_esc(v)}'{' selected' if v == f_rule else ''}>{_esc(v)}</option>"
                         for v in rules)
        base_qs = (f"f_group={f_group}&f_rule={f_rule}&sev={sev}&kw={kw}&show_test={show_test}")
        range_links = " ".join(
            f"<a style='color:{'#00c16a' if v == range else '#7ee7b0'}' "
            f"href='/alerts?range={v}&{base_qs}'>{label}</a>"
            for v, label in (("2h", "近2小时"), ("today", "今天"), ("yesterday", "昨天"), ("3d", "近3天")))
        nav = []
        msg_qs = f"range={range}&{base_qs}"
        if msg_page > 1:
            nav.append(f"<a style='color:#7ee7b0' href='/alerts?{msg_qs}&msg_page={msg_page-1}'>上一页</a>")
        nav.append(f"<span style='color:#9fb3c0'>第 {msg_page}/{msg_pages} 页</span>")
        if msg_page < msg_pages:
            nav.append(f"<a style='color:#7ee7b0' href='/alerts?{msg_qs}&msg_page={msg_page+1}'>下一页</a>")
        # 兜底：窗口内无对应消息的 pending_reply（如手动触发）单独渲染
        win_run_ids = {r["run_id"] for r in db.q(
            "SELECT run_id FROM messages WHERE received_at >= ? AND run_id IS NOT NULL", cutoff)}
        orphan_html = ""
        for h in pr_rows:
            if (h.get("run_id") or "") not in win_run_ids:
                anchor = "reply_first" if not reply_anchor["done"] else ""
                reply_anchor["done"] = True
                orphan_html += reply_html(h, anchor)
        body += ("<div class='card'><h2>搜索</h2>"
                 f"<form method='get' style='display:flex;gap:8px;flex-wrap:wrap;align-items:center'>"
                 f"时间 <select name='range'>"
                 + "".join(f"<option value='{v}'{' selected' if v == range else ''}>{label}</option>"
                           for v, label in (("2h", "近2小时"), ("today", "今天"), ("yesterday", "昨天起"), ("3d", "近3天")))
                 + f"</select> "
                 f"群 <select name='f_group'><option value=''>全部</option>{g_opts}</select> "
                 f"级别 <select name='sev'><option value=''>全部</option>"
                 + "".join(f"<option value='{s}'{' selected' if s == sev else ''}>{s}</option>"
                           for s in ("P1", "P2", "P3", "OK"))
                 + f"</select> "
                 f"匹配 <select name='f_rule'><option value=''>全部</option>{r_opts}</select> "
                 f"<input name='kw' value='{_esc(kw)}' placeholder='关键词'> "
                 f"<label style='font-size:12px'><input type='checkbox' name='show_test' value='1' "
                 f"{'checked' if show_test else ''}> 测试数据</label> "
                 f"<button class='gray'>筛选</button></form></div>")
        pend = rows("pending")
        body += ("<div class='card'><h2>待确认（有问题，强提醒）</h2>"
                 "<p><button onclick=\"bulk('ack')\">批量确认</button> "
                 "<button class='gray' onclick=\"bulk('ignore')\">批量忽略</button> "
                 "<label style='font-size:12px;color:#9fb3c0'><input type='checkbox' onclick=\"document.querySelectorAll('.sel').forEach(c=>c.checked=this.checked)\"> 全选</label></p>"
                 "<table><tr><th></th><th>级别</th><th>摘要</th><th>来源</th><th>时间</th><th>报告</th><th>状态</th><th>操作</th></tr>"
                 + (pend or "<tr><td colspan=8>无</td></tr>") + "</table></div>")
        body += ("<div class='card'><h2>钉群消息清单"
                 f"（{range_label}共 {total_msg} 条，每页 {PAGE_SZ} 条）</h2>"
                 f"<p style='font-size:12px'>{range_links}</p>"
                 + (msg_out or "<p style='color:#9fb3c0'>无消息</p>")
                 + (orphan_html or "")
                 + "<p style='font-size:12px'>" + "　".join(nav) + "</p></div>")
        th = "<tr><th>级别</th><th>摘要</th><th>来源</th><th>时间</th><th>报告</th><th>状态</th><th>操作</th></tr>"
        has_filter = bool(sev or kw or f_group or f_rule)
        hist = ""
        for title, st in (("无问题标注", "no_problem"), ("已确认", "acked"),
                          ("已忽略", "ignored"), ("已重新分析", "reanalyzed")):
            r_out = rows(st)
            hist += f"<h3>{title}</h3><table>{th}" + (r_out or "<tr><td colspan=7>无</td></tr>") + "</table>"
        body += (f"<details {'open' if has_filter else ''} style='margin:12px 0'>"
                 f"<summary style='cursor:pointer;color:#9fb3c0'>历史状态记录"
                 f"（无问题标注 / 已确认 / 已忽略 / 已重新分析）</summary>{hist}</details>")
        return page(body, len(app_ctx.notifier.pending()) or "")

    @app.get("/reports/by-path")
    def report_by_path(p: str):
        fp = os.path.join(app_ctx.cfg.workspace, p)
        return FileResponse(fp, media_type="text/markdown")

    @app.get("/reports/view", response_class=HTMLResponse)
    def report_view(p: str):
        from . import render as rdr
        base = os.path.normpath(app_ctx.cfg.workspace)
        fp = os.path.normpath(os.path.join(base, p))
        if not fp.startswith(base):
            return page("<div class='card'><h2>非法路径</h2></div>")
        data = rdr.load_report(app_ctx.cfg.workspace, os.path.relpath(fp, base))
        if data is None:
            return page("<div class='card'><h2>报告不存在</h2>"
                        f"<p style='color:#9fb3c0'>{rdr.esc(p)}</p></div>")
        return page(f"<h2 style='margin:0 0 12px'>{rdr.esc(data.get('title') or '分析报告')}</h2>"
                    + rdr.render_report_html(data, os.path.relpath(fp, base))
                    + "<script>async function createAoneReq(run,idx){"
                    "const d=await (await fetch('/api/aone_req/draft/'+run+'/'+idx)).json();"
                    "if(d.error){alert(d.error);return}"
                    "const proj=prompt('目标 Aone 需求空间（项目名/ID，可修改）：',d.project||'');"
                    "if(proj===null)return;"
                    "if(!confirm('确认创建 Aone 技术需求？\\n\\n标题：'+d.title+'\\n\\n描述预览：\\n'+d.desc.slice(0,500)+'\\n\\n点击确定后由引擎调用 aone-requirement-create 创建。'))return;"
                    "const r=await fetch('/api/aone_req/create/'+run+'/'+idx,{method:'POST',"
                    "headers:{'Content-Type':'application/json'},"
                    "body:JSON.stringify({title:d.title,desc:d.desc,project:proj})});"
                    "const j=await r.json();alert(j.submitted?'已提交创建，完成后报告页将显示需求链接':('失败：'+(j.error||'')));location.reload()}</script>")

    @app.post("/api/reanalyze/{run_id}")
    async def reanalyze(run_id: str, request: Request, bg: BackgroundTasks):
        data = await request.json()
        note = (data.get("note") or "").strip()
        if not note:
            return {"error": "请输入补充信息"}
        prep = app_ctx.pipeline._reanalyze_prepare(run_id, note)
        if not prep:
            return {"error": "原记录不存在"}
        new_run, ctx = prep
        # 分析放后台：客户端断开不再中断分析
        bg.add_task(app_ctx.pipeline._reanalyze_execute, new_run, ctx)
        return {"new_run": new_run}

    @app.post("/api/rerun/{run_id}")
    async def api_rerun(run_id: str, bg: BackgroundTasks):
        run0 = app_ctx.db.one("SELECT run_id FROM runs WHERE run_id=?", run_id)
        if not run0:
            return {"error": "原记录不存在或无原始消息内容"}
        bg.add_task(app_ctx.pipeline.rerun, run_id)
        return {"submitted": True, "run_id": run_id}

    @app.get("/api/aone_req/draft/{run_id}/{idx}")
    def aone_req_draft(run_id: str, idx: int):
        from . import aone as aonemod
        return aonemod.draft(app_ctx.cfg, app_ctx.db, run_id, idx)

    @app.post("/api/aone_req/create/{run_id}/{idx}")
    async def aone_req_create(run_id: str, idx: int, request: Request, bg: BackgroundTasks):
        from . import aone as aonemod
        data = await request.json()
        title = (data.get("title") or "").strip()
        desc = (data.get("desc") or "").strip()
        project = (data.get("project") or "").strip()
        if not title or not desc:
            return {"error": "标题与描述不能为空（须经确认页确认）"}
        chk = aonemod.draft(app_ctx.cfg, app_ctx.db, run_id, idx)
        if chk.get("error"):
            return chk
        bg.add_task(aonemod.execute, app_ctx.cfg, app_ctx.db, run_id, idx,
                    title, desc, project)
        return {"submitted": True, "run_id": run_id}

    @app.post("/api/alerts/{alert_id}/trigger_solution")
    async def alert_trigger_solution(alert_id: str, request: Request):
        data = await request.json()
        code = (data.get("code") or "").strip()
        params = data.get("params") or {}
        if not code:
            return {"error": "缺少解决方案告警码"}
        a = db.one("SELECT * FROM alerts WHERE alert_id=?", alert_id)
        group = a["source_group"] if a else ""
        return app_ctx.pipeline.manual_trigger_solution(code, params, group)

    @app.post("/alerts/{alert_id}/{op}")
    def alert_op(alert_id: str, op: str):
        if op == "ack":
            app_ctx.notifier.ack(alert_id)
        elif op == "ignore":
            app_ctx.notifier.ignore(alert_id)
        return {"ok": True}

    @app.get("/reports", response_class=HTMLResponse)
    def reports(kw: str = "", days: int = 30):
        from datetime import datetime, timedelta
        from .dingtalk import CST
        days = min(days, 30)
        cutoff = (datetime.now(CST) - timedelta(days=days)).isoformat(timespec="seconds")
        hits = []
        if kw:
            base = os.path.join(app_ctx.cfg.workspace, "reports")
            for root, _, files in os.walk(base):
                for fn in files:
                    if not fn.endswith(".md"):
                        continue
                    fp = os.path.join(root, fn)
                    try:
                        with open(fp, encoding="utf-8") as f:
                            if kw in f.read():
                                hits.append(os.path.relpath(fp, app_ctx.cfg.workspace))
                    except Exception:
                        pass
        rs = db.q("SELECT * FROM reports_meta WHERE created_at >= ? ORDER BY created_at DESC LIMIT 100", cutoff)
        links = " ".join(
            f"<a style='color:#7ee7b0' href='/reports?days={d}'>{label}</a>"
            for d, label in ((1, "1天"), (7, "7天"), (30, "30天")))
        body = (f"<div class='card'><h2>报告列表（最近 {days} 天）</h2>"
                f"<p>{links}</p>"
                f"<form method='get'><input type='hidden' name='days' value='{days}'>"
                f"<input name='kw' value='{kw}' placeholder='全文关键词搜索' style='width:260px'> "
                f"<button class='gray'>搜索</button></form>")
        if kw:
            body += "<p>命中文件：" + ("、".join(
                f"<a style='color:#7ee7b0' href='/reports/view?p={h}'>{h}</a>" for h in hits) or "无") + "</p>"
        body += ("<script>async function fb(id){const r=await fetch('/api/reports/'+id+'/feedback',"
                 "{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({state:'分析太简单'})});"
                 "const j=await r.json();alert(j.ok?'已记录反馈':'失败');location.reload()}</script>")
        body += "<table><tr><th>标题</th><th>时间</th><th>反馈</th><th>操作</th></tr>"
        for r in rs:
            body += (f"<tr><td><a style='color:#7ee7b0' href='/reports/{r['report_id']}'>{r['title']}</a></td>"
                     f"<td>{r['created_at']}</td><td>{r['feedback_state']}</td>"
                     f"<td><button class='gray' onclick=\"fb('{r['report_id']}')\">分析太简单</button></td></tr>")
        return page(body + "</table></div>")

    @app.post("/api/reports/{report_id}/feedback")
    async def report_feedback(report_id: str, request: Request):
        data = await request.json()
        state = (data.get("state") or "").strip()[:20]
        if not state:
            return {"error": "反馈内容不能为空"}
        r = db.one("SELECT report_id FROM reports_meta WHERE report_id=?", report_id)
        if r is None:
            return {"error": "报告不存在"}
        db.update("reports_meta", "report_id", report_id, feedback_state=state)
        db.audit("report", "feedback", state, "", None)
        return {"ok": True}

    @app.get("/reports/{report_id}", response_class=HTMLResponse)
    def report_file(report_id: str):
        from . import render as rdr
        r = db.one("SELECT file_path FROM reports_meta WHERE report_id=?", report_id)
        if r is None:
            return page("<div class='card'><h2>报告不存在</h2></div>")
        data = rdr.load_report(app_ctx.cfg.workspace, r["file_path"])
        if data is None:
            return page("<div class='card'><h2>报告文件已被清理</h2>"
                        f"<p style='color:#9fb3c0'>{rdr.esc(r['file_path'])}</p></div>")
        return page(f"<h2 style='margin:0 0 12px'>{rdr.esc(data.get('title') or '分析报告')}</h2>"
                    + rdr.render_report_html(data, r["file_path"]))

    @app.get("/authlist", response_class=HTMLResponse)
    def authlist_page(hours: int = 24):
        from datetime import datetime, timedelta
        from .dingtalk import CST
        hours = min(hours, 720)
        cutoff = (datetime.now(CST) - timedelta(hours=hours)).isoformat(timespec="seconds")
        body = """<script>
async function aop(op,i){if(op!='toggle'&&!confirm(op+' 该条目？'))return;
const r=await fetch('/api/authlist/'+op+'/'+i,{method:'POST'});const j=await r.json();if(j.error)alert(j.error);location.reload()}
async function aedit(i){const y=prompt('粘贴该条目的完整 YAML（含 id/app/scope/feature/constraints 等）：');if(!y)return;
const r=await fetch('/api/authlist/edit/'+i,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({yaml:y})});
const j=await r.json();if(j.error)alert(j.error);location.reload()}
async function aadd(){const y=prompt('粘贴新条目 YAML（含 id/app/scope/feature，写条目含 env/constraints/expiry/maxExecutions）：');if(!y)return;
const r=await fetch('/api/authlist/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({yaml:y})});
const j=await r.json();if(j.error)alert(j.error);location.reload()}
async function amax(i){const v=prompt('新的执行上限 maxExecutions（留空=不限制，输入非负整数）：');if(v===null)return;
const r=await fetch('/api/authlist/maxexec/'+i,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({max:v})});
const j=await r.json();if(j.error)alert(j.error);location.reload()}
async function confirmExec(id){if(!confirm('确认执行该写操作？线上写操作将真实执行命令。'))return;
const r=await fetch('/api/auth_exec/'+id+'/confirm',{method:'POST'});const j=await r.json();
toast(j.result||j.error||'已提交');setTimeout(()=>location.reload(),600)}
function toast(msg){const d=document.createElement('div');d.textContent=msg;
d.style.cssText='position:fixed;top:16px;left:50%;transform:translateX(-50%);background:#1f6feb;color:#fff;padding:10px 18px;border-radius:8px;z-index:99;box-shadow:0 4px 16px rgba(0,0,0,.4)';
document.body.appendChild(d);setTimeout(()=>d.remove(),2500)}
function watchExecuting(){if(document.querySelector('[data-exec="executing"]'))setTimeout(()=>location.reload(),3000)}
window.addEventListener('load',watchExecuting)
</script>"""
        wd = bool(app_ctx.cfg.agent.get("writes_disabled", False))
        body += (f"<div class='card'><h2>三维授权清单（应用 × 读/写 × 具体功能）</h2>"
                 f"<p class='{'err' if wd else 'ok'}'>写操作紧急开关：{'已禁用' if wd else '正常'}</p>"
                 f"<table><tr><th>应用</th><th>读/写</th><th>功能</th><th>env</th><th>上限</th><th>已用</th><th>到期</th><th>启用</th><th>操作</th></tr>")
        for i, e in enumerate(app_ctx.cfg.auth_entries):
            mx = e.get("maxExecutions")
            used = db.count_exec(e.get("id")) if e.get("scope") == "write" else "-"
            mx_txt = "-" if mx is None else str(mx)
            if mx is not None and isinstance(used, int) and used >= mx:
                mx_cls = "err"
            else:
                mx_cls = "ok"
            exp = (e.get("expiry") or "-")[:10]
            body += (f"<tr><td>{e.get('app')}</td><td>{e.get('scope')}</td><td>{e.get('feature')}</td>"
                     f"<td>{e.get('env','-')}</td><td class='{mx_cls}'>{mx_txt}</td><td>{used}</td><td>{exp}</td>"
                     f"<td class=\"{'ok' if e.get('enabled') else 'err'}\">{e.get('enabled')}</td>"
                     f"<td><button class='gray' onclick=\"aop('toggle',{i})\">启停</button> "
                     f"<button class='gray' onclick=\"amax({i})\">改上限</button> "
                     f"<button class='gray' onclick=\"aedit({i})\">编辑</button> "
                     f"<button class='red' onclick=\"aop('delete',{i})\">删除</button></td></tr>")
        body += ("</table><p><button onclick='aadd()'>新增条目</button> "
                 "<span style='color:#9fb3c0;font-size:12px'>保存即热加载生效；变更写入审计日志</span></p></div>")
        pend = db.q("SELECT * FROM auth_exec WHERE exec_result='pending_confirm' AND ts >= ? ORDER BY id DESC LIMIT 20", cutoff)
        body += ("<div class='card'><h2>待二次确认的写操作（env=online 强制确认；计划步骤须严格按序执行）</h2>"
                 "<table><tr><th>条目</th><th>计划步骤</th><th>run</th><th>时间</th><th>操作</th></tr>")
        for h in pend:
            try:
                pl = json.loads(h["payload"] or "{}")
            except Exception:
                pl = {}
            if pl.get("plan_id"):
                step_txt = (f"{pl.get('plan_id', '').split(':')[-1]} "
                            f"第{pl.get('step_no')}步·{pl.get('step_name', '')}")
            else:
                step_txt = "-"
            body += (f"<tr><td>{h['entry_id']}</td><td style='font-size:11px'>{step_txt}</td>"
                     f"<td>{h['run_id']}</td><td>{h['ts']}</td>"
                     f"<td><button onclick=\"confirmExec({h['id']})\">确认执行</button></td></tr>")
        body += "</table></div>"
        rung = db.q("SELECT * FROM auth_exec WHERE exec_result='executing' AND ts >= ? ORDER BY id DESC LIMIT 20", cutoff)
        body += ("<div class='card'><h2>执行中的写操作（后台运行，页面自动刷新结果）</h2>"
                 "<table><tr><th>条目</th><th>计划步骤</th><th>run</th><th>时间</th><th>状态</th></tr>")
        for h in rung:
            try:
                pl = json.loads(h["payload"] or "{}")
            except Exception:
                pl = {}
            step_txt = (f"{pl.get('plan_id', '').split(':')[-1]} 第{pl.get('step_no')}步·{pl.get('step_name', '')}"
                        if pl.get("plan_id") else "-")
            body += (f"<tr><td>{h['entry_id']}</td><td style='font-size:11px'>{step_txt}</td>"
                     f"<td>{h['run_id']}</td><td>{h['ts']}</td>"
                     f"<td data-exec='executing' class='warn'>后台执行中…</td></tr>")
        body += "</table></div>"
        hist = db.q("SELECT * FROM auth_exec WHERE ts >= ? ORDER BY id DESC LIMIT 50", cutoff)
        links = " ".join(
            f"<a style='color:#7ee7b0' href='/authlist?hours={h}'>{label}</a>"
            for h, label in ((24, "24小时"), (168, "7天"), (720, "30天")))
        body += f"<p>{links}</p>"
        body += "<div class='card'><h2>判定/执行历史</h2><table>"
        for h in hist:
            body += f"<tr><td>{h['entry_id']}</td><td>{h['action_type']}</td><td>{'命中' if h['matched'] else '未命中'}</td><td>{h['reject_reason']}</td><td>{h['exec_result']}</td></tr>"
        return page(body + "</table></div>")

    @app.post("/api/authlist/{op}/{idx}")
    def authlist_op(op: str, idx: int):
        from . import configsync
        ws = app_ctx.cfg.workspace
        entries = configsync.load_auth(ws)
        if idx < 0 or idx >= len(entries):
            return {"error": "索引越界"}
        if op == "toggle":
            entries[idx]["enabled"] = not entries[idx].get("enabled", False)
        elif op == "delete":
            entries.pop(idx)
        else:
            return {"error": "未知操作"}
        configsync.save_auth(ws, entries)
        _reload()
        db.audit("auth", f"entry_{op}", entries[idx].get("id", "") if op == "toggle" else str(idx), "", None)
        return {"ok": True}

    @app.post("/api/authlist/maxexec/{idx}")
    async def authlist_maxexec(idx: int, request: Request):
        from . import configsync
        data = await request.json()
        entries = configsync.load_auth(app_ctx.cfg.workspace)
        if idx < 0 or idx >= len(entries):
            return {"error": "索引越界"}
        raw = str(data.get("max", "")).strip()
        if raw == "":
            entries[idx].pop("maxExecutions", None)
            new = None
        else:
            try:
                new = int(raw)
                if new < 0:
                    raise ValueError
            except Exception:
                return {"error": "maxExecutions 必须是非负整数，或留空表示不限制"}
            entries[idx]["maxExecutions"] = new
        configsync.save_auth(app_ctx.cfg.workspace, entries)
        _reload()
        db.audit("auth", "entry_maxexec_updated", entries[idx].get("id", ""),
                 f"maxExecutions={new}", None)
        return {"ok": True, "maxExecutions": new}

    @app.post("/api/authlist/edit/{idx}")
    async def authlist_edit(idx: int, request: Request):
        import yaml as _yaml
        from . import configsync
        data = await request.json()
        try:
            e = _yaml.safe_load(data.get("yaml") or "")
        except Exception as ex:
            return {"error": f"YAML 解析失败：{ex}"}
        if not isinstance(e, dict) or not all(e.get(k) for k in ("id", "app", "scope", "feature")):
            return {"error": "条目必须包含 id/app/scope/feature"}
        entries = configsync.load_auth(app_ctx.cfg.workspace)
        if idx < 0 or idx >= len(entries):
            return {"error": "索引越界"}
        entries[idx] = e
        configsync.save_auth(app_ctx.cfg.workspace, entries)
        _reload()
        db.audit("auth", "entry_edited", e["id"], "", None)
        return {"ok": True}

    @app.post("/api/authlist/add")
    async def authlist_add(request: Request):
        import yaml as _yaml
        from . import configsync
        data = await request.json()
        try:
            e = _yaml.safe_load(data.get("yaml") or "")
        except Exception as ex:
            return {"error": f"YAML 解析失败：{ex}"}
        if not isinstance(e, dict) or not all(e.get(k) for k in ("id", "app", "scope", "feature")):
            return {"error": "条目必须包含 id/app/scope/feature"}
        entries = configsync.load_auth(app_ctx.cfg.workspace)
        if any(x.get("id") == e["id"] for x in entries):
            return {"error": "条目 id 已存在"}
        entries.append(e)
        configsync.save_auth(app_ctx.cfg.workspace, entries)
        _reload()
        db.audit("auth", "entry_added", e["id"], "", None)
        return {"ok": True}

    @app.post("/api/auth_exec/{exec_id}/confirm")
    def auth_exec_confirm(exec_id: int):
        from . import authlist
        row = db.one("SELECT * FROM auth_exec WHERE id=? AND exec_result='pending_confirm'", exec_id)
        if not row:
            return {"error": "记录不存在或已处理"}
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            return {"error": "payload 损坏"}
        ok, reason = authlist.check_plan_order(db, row["run_id"],
                                               payload.get("plan_id"), payload.get("step_no"))
        if not ok:
            return {"error": reason}
        if payload.get("step_type") == "dingtalk_reply":
            if not app_ctx.cfg.dingtalk.get("reply_enabled", True):
                return {"error": "群回复开关已关闭"}
            md = payload.get("markdown", "")
            if payload.get("plan_id"):
                writes = db.q("SELECT exec_result, payload FROM auth_exec "
                              "WHERE run_id=? AND action_type='ateye_write' ORDER BY id",
                              row["run_id"])
                if writes:
                    lines = []
                    for w in writes:
                        try:
                            wp = json.loads(w["payload"] or "{}")
                            prm = wp.get("suggestion", {}).get("params", {})
                        except Exception:
                            prm = {}
                        mid, sub = prm.get("modifyId"), prm.get("subStatus")
                        if w["exec_result"] == "executed":
                            lines.append(f"审计告警已处理，当前改签单{mid} subStatus已置为{sub}")
                        else:
                            lines.append(f"审计告警处理失败：改签单{mid} 订正未成功，请人工介入")
                    md = "**LocalAgent 方案执行结论**\n\n" + "\n".join(lines)
            if not app_ctx.ding.reply(payload.get("group", ""), md):
                db.audit("dingtalk", "plan_reply_send_failed", payload.get("group", ""),
                         "dws 未投递成功", row["run_id"])
                return {"error": "回复发送失败：消息未投递到钉群，请重试"}
            db.update("auth_exec", "id", exec_id, exec_result="replied", reject_reason="")
            db.audit("dingtalk", "plan_reply_sent", payload.get("group", ""),
                     f"plan step {payload.get('step_no')}", row["run_id"])
            return {"result": f"第{payload.get('step_no')}步已执行：回复已发送到 {payload.get('group')}"}
        entry = next((e for e in app_ctx.cfg.auth_entries if e.get("id") == payload.get("entry_id")), None)
        if entry is None or not entry.get("enabled", False):
            db.update("auth_exec", "id", exec_id, exec_result="rejected", reject_reason="entry missing or disabled")
            return {"error": "授权条目不存在或已停用"}
        params = payload.get("suggestion", {}).get("params", {})
        # 快速预校验：命令渲染失败直接返回，不进后台
        try:
            authlist.render_command(entry, params)
        except ValueError as e:
            db.update("auth_exec", "id", exec_id, exec_result="failed",
                      reject_reason=f"命令渲染失败: {e}"[:200])
            return {"error": f"命令渲染失败：{e}"}
        # 置为执行中并转后台线程，前端立即得到响应，不再阻塞等待 Ateye
        db.update("auth_exec", "id", exec_id, exec_result="executing",
                  reject_reason="后台执行中")
        db.audit("auth", "write_confirm_submitted", entry["id"],
                 f"async exec submitted, entry={entry['id']}", row["run_id"])

        def _run_write(eid, ent, prm, run_id):
            try:
                ok, detail = authlist.execute_write(ent, prm)
            except Exception as ex:
                ok, detail = False, f"执行异常: {ex}"
            db.update("auth_exec", "id", eid,
                      exec_result="executed" if ok else "failed",
                      reject_reason="" if ok else detail[:200])
            mark = "【线上写操作】" if ent.get("env") == "online" else ""
            db.audit("auth", "write_executed_confirmed", ent["id"],
                     mark + detail[:280], run_id)
            if not ok and ent.get("disableOnFailure"):
                authlist.disable_entry(app_ctx.cfg.workspace, ent["id"])
                _reload()

        threading.Thread(target=_run_write,
                         args=(exec_id, entry, params, row["run_id"]),
                         daemon=True).start()
        return {"result": "已提交后台执行，正在运行…（页面将自动刷新结果）"}

    @app.post("/api/auth_exec/{exec_id}/send_reply")
    def auth_exec_send_reply(exec_id: int):
        try:
            out = app_ctx.pipeline.send_reply(exec_id)
        except Exception as e:
            db.audit("ui", "send_reply_error", str(exec_id), repr(e)[:200])
            return {"error": f"发送异常：{e}"[:200]}
        if out is None:
            return {"error": "记录不存在或已处理"}
        _run_id, ok = out
        if ok:
            return {"result": "已发送回复到钉群"}
        return {"error": "发送失败：消息未投递到钉群（已保留待回复，可重试），详见审计日志"}

    @app.post("/api/auth_exec/{exec_id}/reject_reply")
    def auth_exec_reject_reply(exec_id: int):
        run_id = app_ctx.pipeline.reject_reply(exec_id)
        if run_id is None:
            return {"error": "记录不存在或已处理"}
        return {"result": "已丢弃该回复"}

    @app.post("/api/auth_exec/{exec_id}/edit_reply")
    async def auth_exec_edit_reply(exec_id: int, request: Request):
        data = await request.json()
        md = data.get("markdown")
        if not isinstance(md, str) or not md.strip():
            return {"error": "回复内容不能为空"}
        row = db.one("SELECT * FROM auth_exec WHERE id=? AND exec_result='pending_reply'", exec_id)
        if row is None:
            return {"error": "记录不存在或已处理"}
        try:
            pl = json.loads(row["payload"] or "{}")
        except Exception:
            pl = {}
        pl["markdown"] = md
        db.exec("UPDATE auth_exec SET payload=? WHERE id=?",
                json.dumps(pl, ensure_ascii=False), exec_id)
        db.audit("dingtalk", "reply_edited", pl.get("group", ""), "", row["run_id"])
        return {"ok": True}

    def _toggle_yaml_flag(key):
        import yaml as _yaml
        p = os.path.join(app_ctx.cfg.workspace, "config", "agent.yaml")
        with open(p, encoding="utf-8") as f:
            agent = _yaml.safe_load(f)
        cur = bool(agent.get(key, False)) if key == "writes_disabled" else bool(agent.get("dingtalk", {}).get(key, True))
        if key == "writes_disabled":
            agent[key] = not cur
        else:
            agent.setdefault("dingtalk", {})[key] = not cur
        with open(p, "w", encoding="utf-8") as f:
            _yaml.safe_dump(agent, f, allow_unicode=True, sort_keys=False)
        _reload()
        return not cur

    @app.post("/api/settings/reply_toggle")
    def reply_toggle():
        return {"reply_enabled": _toggle_yaml_flag("reply_enabled")}

    @app.post("/api/settings/writes_toggle")
    def writes_toggle():
        v = _toggle_yaml_flag("writes_disabled")
        db.audit("auth", "writes_switch_toggled", "", f"writes_disabled={v}", None)
        return {"writes_disabled": v}

    @app.post("/api/settings/pause_toggle")
    def pause_toggle():
        cur = db.get_state("tasks_paused") == "1"
        db.set_state("tasks_paused", "0" if cur else "1")
        db.audit("task", "tasks_pause_toggled", "", str(not cur), None)
        return {"paused": not cur}

    @app.post("/api/settings/process_mode_toggle")
    def process_mode_toggle():
        import yaml as _yaml
        p = os.path.join(app_ctx.cfg.workspace, "config", "agent.yaml")
        with open(p, encoding="utf-8") as f:
            agent = _yaml.safe_load(f) or {}
        dt = agent.setdefault("dingtalk", {})
        new = "all" if dt.get("process_mode", "at_me_only") == "at_me_only" else "at_me_only"
        dt["process_mode"] = new
        with open(p, "w", encoding="utf-8") as f:
            _yaml.safe_dump(agent, f, allow_unicode=True, sort_keys=False)
        _reload()
        db.audit("task", "process_mode_toggled", "", new)
        return {"mode": new}

    @app.post("/api/settings/listen_all_toggle")
    def listen_all_toggle():
        import yaml as _yaml
        p = os.path.join(app_ctx.cfg.workspace, "config", "agent.yaml")
        with open(p, encoding="utf-8") as f:
            agent = _yaml.safe_load(f) or {}
        dt = agent.setdefault("dingtalk", {})
        new = not bool(dt.get("listen_all", False))
        dt["listen_all"] = new
        with open(p, "w", encoding="utf-8") as f:
            _yaml.safe_dump(agent, f, allow_unicode=True, sort_keys=False)
        _reload()
        db.audit("task", "listen_all_toggled", "", f"listen_all={new}")
        return {"listen_all": new}

    @app.get("/groups", response_class=HTMLResponse)
    def groups_page():
        from . import configsync
        from .correlate import FAMILIES
        gs = configsync.load_groups(app_ctx.cfg.workspace)
        rows = ""
        for i, g in enumerate(gs):
            types = g.get("auto_reply_types") or []
            boxes = "".join(
                f"<label style='margin-right:6px;font-size:12px'>"
                f"<input type='checkbox' value='{f}'{' checked' if f in types else ''}>{f}</label>"
                for f in FAMILIES)
            types_cell = (f"<span id='types_lbl_{i}' style='font-size:12px;color:"
                          f"{'#7ee7b0' if types else '#9fb3c0'}'>"
                          f"{'、'.join(types) if types else '未放开（全部转人工确认）'}</span> "
                          f"<button class='gray' onclick=\"gtoggleEdit({i})\">设置</button>"
                          f"<div id='types_{i}' style='display:none;margin-top:6px'>"
                          f"{boxes}<br><button class='gray' onclick=\"gsaveTypes({i})\">保存</button></div>")
            rows += (f"<tr><td>{g.get('name')}</td><td>{g.get('mode')}</td>"
                     f"<td>{g.get('id') or '-'}</td><td>{'启用' if g.get('enabled', True) else '停用'}</td>"
                     f"<td style='font-size:12px'>{types_cell}</td>"
                     f"<td><button class='gray' onclick=\"gact('toggle',{i})\">启停</button> "
                     f"<button class='gray' onclick=\"gact('resolve',{i})\">解析ID</button> "
                     f"<button class='gray' onclick=\"gact('remove',{i})\">移除</button></td></tr>")
        fam_txt = "、".join(FAMILIES)
        body = f"""<script>
async function gact(op,i){{try{{const r=await fetch('/api/groups/'+op+'/'+i,{{method:'POST'}});
if(!r.ok)throw new Error('HTTP '+r.status);const j=await r.json();if(j.error)alert(j.error);location.reload()}}catch(e){{alert('操作失败：'+e.message)}}}}
async function gadd(){{const name=prompt('钉群名称');if(!name)return;const mode=prompt('模式 alert=报警匹配 / at_me=@我专项 / both','alert');if(!mode)return;
try{{const r=await fetch('/api/groups/add',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{name:name,mode:mode}})}});
if(!r.ok)throw new Error('HTTP '+r.status);const j=await r.json();if(j.error)throw new Error(j.error);location.reload()}}catch(e){{alert('添加失败：'+e.message)}}}}
function gtoggleEdit(i){{const d=document.getElementById('types_'+i);d.style.display=(d.style.display==='none')?'':'none'}}
async function gsaveTypes(i){{const v=[...document.querySelectorAll('#types_'+i+' input:checked')].map(x=>x.value).join(',');
try{{const r=await fetch('/api/groups/auto_reply_types/'+i,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{types:v}})}});
if(!r.ok)throw new Error('HTTP '+r.status);const j=await r.json();if(j.error)throw new Error(j.error);location.reload()}}catch(e){{alert('保存失败：'+e.message)}}}}
</script>
<div class="card"><h2>钉钉群配置（授权范围）</h2>
<table><tr><th>群名</th><th>模式</th><th>会话ID</th><th>状态</th><th>自动回复报警类型</th><th>操作</th></tr>{rows}</table>
<p><button onclick="gadd()">添加钉群</button>
<span style="color:#9fb3c0;font-size:12px">保存即热加载生效；LocalAgent 仅处理启用群的消息。自动回复精确到「群×报警类型」：仅勾选的类型自动回复，未勾选与未分类报警一律转人工确认卡片。合法类型：{fam_txt}</span></p></div>"""
        listen_all = bool(app_ctx.cfg.dingtalk.get("listen_all", False))
        gmode = app_ctx.cfg.dingtalk.get("process_mode", "at_me_only")
        overrides = "、".join(f"{g['name']}={g['process_mode']}"
                             for g in app_ctx.cfg.groups if g.get("process_mode"))
        body += ("<div class='card'><h2>监听范围</h2>"
                 f"<span class='{'warn' if not listen_all else 'ok'}'>"
                 f"{'仅处理@我的消息（非@我不处理、不展示）' if not listen_all else '监听处理所有告警消息'}</span>"
                 + "　<button class='gray' onclick=\"fetch('/api/settings/listen_all_toggle',"
                 "{method:'POST'}).then(()=>location.reload())\">"
                 f"{'开启全量监听' if not listen_all else '恢复仅@我'}</button>"
                 + (f"<br><span style='color:#9fb3c0;font-size:12px'>兼容旧处理模式：默认 {gmode}"
                    + (f"；群覆盖：{overrides}" if overrides else "") + "</span>" if overrides or gmode else "")
                 + "</div>")
        return page(body)

    @app.post("/api/groups/add")
    async def groups_add(request: Request):
        from . import configsync
        data = await request.json()
        name = (data.get("name") or "").strip()
        mode = data.get("mode") or "alert"
        if not name or mode not in ("alert", "at_me", "both"):
            return {"error": "参数无效：name 必填，mode 为 alert/at_me/both"}
        ws = app_ctx.cfg.workspace
        gs = configsync.load_groups(ws)
        if any(g.get("name") == name for g in gs):
            return {"error": "群已存在"}
        gs.append({"name": name, "mode": mode, "id": "", "enabled": True, "auto_reply": False})
        configsync.save_groups(ws, gs)
        _reload()
        return {"ok": True}

    @app.post("/api/groups/auto_reply_types/{idx}")
    async def groups_auto_reply_types(idx: int, request: Request):
        from . import configsync
        from .correlate import FAMILIES
        data = await request.json()
        raw = (data.get("types") or "").strip()
        types = [t.strip() for t in raw.replace("，", ",").split(",") if t.strip()]
        bad = [t for t in types if t not in FAMILIES]
        if bad:
            return {"error": f"非法类型：{'、'.join(bad)}；合法值：{'、'.join(FAMILIES)}"}
        ws = app_ctx.cfg.workspace
        gs = configsync.load_groups(ws)
        if idx < 0 or idx >= len(gs):
            return {"error": "索引越界"}
        gs[idx]["auto_reply_types"] = types
        configsync.save_groups(ws, gs)
        _reload()
        db.audit("config", "auto_reply_types_updated", gs[idx].get("name", ""),
                 ",".join(types) or "(清空)")
        return {"ok": True}

    @app.post("/api/groups/{op}/{idx}")
    def groups_op(op: str, idx: int):
        import json as _json
        import subprocess
        from . import configsync
        ws = app_ctx.cfg.workspace
        gs = configsync.load_groups(ws)
        if idx < 0 or idx >= len(gs):
            return {"error": "索引越界"}
        g = gs[idx]
        if op == "toggle":
            g["enabled"] = not g.get("enabled", True)
        elif op == "remove":
            gs.pop(idx)
        elif op == "resolve":
            r = subprocess.run(["dws", "chat", "search", "--query", g.get("name", ""),
                                "--format", "json"], capture_output=True, timeout=30)
            try:
                items = _json.loads(r.stdout.decode()).get("result", {}).get("groups") or []
                hit = next((x for x in items if x.get("title") == g.get("name")), None)
                if not hit:
                    return {"error": "未搜到同名群，请确认群名"}
                g["id"] = hit.get("openConversationId", "")
            except Exception as e:
                return {"error": f"解析失败：{e}"}
        else:
            return {"error": "未知操作"}
        configsync.save_groups(ws, gs)
        _reload()
        return {"ok": True}

    def _reload():
        from .config import Config
        cfg = Config()
        app_ctx.cfg = cfg
        app_ctx.pipeline.cfg = cfg
        app_ctx.notifier.cfg = cfg
        app_ctx.ding.cfg = cfg
        if hasattr(app_ctx.ding, "groups"):
            app_ctx.ding.groups = {g.get("name"): g for g in cfg.groups}

    @app.get("/solutions", response_class=HTMLResponse)
    def solutions_page():
        from . import solutions as solmod
        sols = solmod.load_solutions(app_ctx.cfg.workspace)
        entries_by_id = {e.get("id"): e for e in app_ctx.cfg.auth_entries}
        wd = bool(app_ctx.cfg.agent.get("writes_disabled", False))
        sol_src = app_ctx.cfg.agent.get("solutions", {})
        src_url = sol_src.get("source_yuque", "")
        src_dir = sol_src.get("source_dir", "")
        tpl_url = sol_src.get("template_url", "")
        body = """<script>
async function gate(code){const r=await fetch('/api/solutions/gate/'+code,{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify({})});const j=await r.json();
alert(j.ok?('门禁已：'+(j.enabled?'开启':'关闭')):(j.error||'失败'));location.reload()}
async function sedit(code){const y=prompt('粘贴该方案的完整 YAML（code/name/enabled/write_entry_id/diagnose/steps）：');if(!y)return;
const r=await fetch('/api/solutions/edit/'+code,{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({yaml:y})});const j=await r.json();if(j.error)alert(j.error);location.reload()}
async function sadd(){const y=prompt('粘贴新方案 YAML（必含 code/name；写操作需 write_entry_id 且 enabled 默认 false）：');if(!y)return;
const r=await fetch('/api/solutions/add',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({yaml:y})});const j=await r.json();if(j.error)alert(j.error);location.reload()}
async function sdel(code){if(!confirm('删除方案 '+code+' ？'))return;
const r=await fetch('/api/solutions/delete/'+code,{method:'POST'});const j=await r.json();
if(j.error)alert(j.error);location.reload()}
async function syuque(){const u=prompt('粘贴语雀方案文档 URL（同步后门禁重置为关闭，执行仍需人工开启）：','SYNC_DEFAULT_URL');if(!u)return;
const r=await fetch('/api/solutions/sync_yuque',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({url:u})});const j=await r.json();
alert(j.ok?('已同步方案 '+j.code+'（门禁关闭）'):(j.error||'同步失败'));location.reload()}
</script>"""
        body += (f"<div class='card'><h2>解决方案库（按告警码沉淀）</h2>"
                 f"<p class='{'err' if wd else 'ok'}'>写操作紧急开关：{'已禁用（优先级最高）' if wd else '正常'}</p>"
                 + (f"<p style='font-size:12px;color:#9fb3c0'>默认方案来源："
                    f"<a style='color:#38bdf8' target='_blank' href='{src_url}'>语雀·{src_dir or '团队空间'}</a>"
                    + (f"　<a style='color:#38bdf8' target='_blank' href='{tpl_url}'>解决方案模板</a>" if tpl_url else "")
                    + "（方案默认从该空间获取）</p>" if src_url else "")
                 + f"<p><button onclick='sadd()'>新增方案</button> "
                 f"<button class='gray' onclick='syuque()'>从语雀同步</button> "
                 f"<span style='color:#9fb3c0;font-size:12px'>方案知识始终注入分析；写操作须门禁开启 + 授权条目启用 + 二次确认</span></p>"
                 f"<table><tr><th>告警码</th><th>名称</th><th>写门禁</th><th>动作列表</th><th>方案内容</th><th>操作</th></tr>")
        for s in sols:
            code = s.get("code", "")
            acts_txt = ""
            for a in solmod.normalize_actions(s):
                label = solmod.ACTION_LABELS.get(a.get("type"), a.get("type"))
                extra = ""
                if a.get("type") == "ateye_write" and a.get("write_entry_id"):
                    we = entries_by_id.get(a["write_entry_id"])
                    extra = (f" {a['write_entry_id']} <span class='{'ok' if we and we.get('enabled') else 'err'}'>"
                             f"{'已启用' if we and we.get('enabled') else ('已禁用' if we else '条目不存在')}</span>")
                elif a.get("feature"):
                    extra = f" {a['feature']}"
                acts_txt += f"<div style='font-size:11px'>· {label}{extra}</div>"
            gate_txt = (f"<span class='{'ok' if s.get('enabled') else 'warn'}'>"
                        f"{'开启' if s.get('enabled') else '关闭'}</span>")
            detail = "".join(f"诊断：{d}<br>" for d in (s.get("diagnose") or [])[:2]) \
                + "".join(f"步骤：{t}<br>" for t in (s.get("steps") or [])[:2])
            body += (f"<tr><td><code style='color:#7ee7b0'>{code}</code></td><td>{s.get('name','')}</td>"
                     f"<td>{gate_txt}</td><td>{acts_txt or '-'}</td>"
                     f"<td style='font-size:11px;color:#9fb3c0'>{detail}"
                     f"<details><summary style='cursor:pointer'>完整</summary><pre>"
                     f"诊断：{chr(10).join(s.get('diagnose') or []) or '无'}{chr(10)}"
                     f"步骤：{chr(10).join(s.get('steps') or []) or '无'}</pre></details></td>"
                     f"<td><button class='gray' onclick=\"gate('{code}')\">切换门禁</button> "
                     f"<button class='gray' onclick=\"sedit('{code}')\">编辑</button> "
                     f"<button class='red' onclick=\"sdel('{code}')\">删除</button></td></tr>")
        body = body.replace("SYNC_DEFAULT_URL", src_url)
        return page(body + "</table></div>")

    @app.get("/api/solutions")
    def api_solutions():
        from . import solutions as solmod
        return {"solutions": solmod.load_solutions(app_ctx.cfg.workspace)}

    @app.post("/api/solutions/gate/{code}")
    async def api_solution_gate(code: str, request: Request):
        from . import solutions as solmod
        data = await request.json()
        sols = solmod.load_solutions(app_ctx.cfg.workspace)
        s = solmod.get_solution(sols, code)
        if s is None:
            return {"error": "方案不存在"}
        new = bool(data.get("enabled")) if "enabled" in data else not bool(s.get("enabled"))
        solmod.set_gate(app_ctx.cfg.workspace, code, new)
        _reload()
        db.audit("auth", "solution_gate_toggled", code, f"enabled={new}")
        return {"ok": True, "enabled": new}

    @app.post("/api/solutions/add")
    async def api_solution_add(request: Request):
        import yaml as _yaml
        from . import solutions as solmod
        data = await request.json()
        try:
            s = _yaml.safe_load(data.get("yaml") or "")
        except Exception as e:
            return {"error": f"YAML 解析失败：{e}"}
        if not isinstance(s, dict) or not s.get("code") or not s.get("name"):
            return {"error": "方案必须包含 code 与 name"}
        sols = solmod.load_solutions(app_ctx.cfg.workspace)
        if solmod.get_solution(sols, s["code"]):
            return {"error": f"方案 {s['code']} 已存在"}
        s.setdefault("enabled", False)
        s["updated_at"] = now()
        sols.append(s)
        solmod.save_solutions(app_ctx.cfg.workspace, sols)
        _reload()
        db.audit("auth", "solution_added", s["code"])
        return {"ok": True}

    @app.post("/api/solutions/edit/{code}")
    async def api_solution_edit(code: str, request: Request):
        import yaml as _yaml
        from . import solutions as solmod
        data = await request.json()
        try:
            s = _yaml.safe_load(data.get("yaml") or "")
        except Exception as e:
            return {"error": f"YAML 解析失败：{e}"}
        if not isinstance(s, dict) or not s.get("code") or not s.get("name"):
            return {"error": "方案必须包含 code 与 name"}
        sols = solmod.load_solutions(app_ctx.cfg.workspace)
        old = solmod.get_solution(sols, code)
        if old is None:
            return {"error": "方案不存在"}
        s.setdefault("enabled", False)
        s["updated_at"] = now()
        sols[sols.index(old)] = s
        solmod.save_solutions(app_ctx.cfg.workspace, sols)
        _reload()
        db.audit("auth", "solution_edited", code)
        return {"ok": True}

    @app.post("/api/solutions/delete/{code}")
    def api_solution_delete(code: str):
        from . import solutions as solmod
        sols = solmod.load_solutions(app_ctx.cfg.workspace)
        old = solmod.get_solution(sols, code)
        if old is None:
            return {"error": "方案不存在"}
        sols.remove(old)
        solmod.save_solutions(app_ctx.cfg.workspace, sols)
        _reload()
        db.audit("auth", "solution_deleted", code)
        return {"ok": True}

    @app.post("/api/solutions/sync_yuque")
    async def api_solution_sync_yuque(request: Request):
        import subprocess
        import sys as _sys
        from . import solutions as solmod
        data = await request.json()
        url = (data.get("url") or "").strip()
        if not url.startswith("http"):
            return {"error": "请提供语雀文档 URL"}
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "scripts", "yuque_solution_sync.py")
        try:
            r = subprocess.run([_sys.executable, script, "--url", url],
                               capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            return {"error": "同步超时(300s)"}
        out = r.stdout.decode(errors="replace")
        obj = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
        if obj is None:
            return {"error": f"同步失败：{out[:200] or r.stderr.decode(errors='replace')[:200]}"}
        if obj.get("error"):
            return {"error": obj["error"]}
        obj["enabled"] = False  # 同步后门禁强制关闭，执行须人工开启
        obj["updated_at"] = now()
        sols = solmod.load_solutions(app_ctx.cfg.workspace)
        old = solmod.get_solution(sols, obj["code"])
        if old is not None:
            sols[sols.index(old)] = obj
        else:
            sols.append(obj)
        solmod.save_solutions(app_ctx.cfg.workspace, sols)
        _reload()
        db.audit("auth", "solution_synced_yuque", obj["code"], url[:200])
        return {"ok": True, "code": obj["code"], "enabled": False}

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(days: int = 7, cat: str = ""):
        from datetime import datetime, timedelta
        from .dingtalk import CST
        days = min(days, 30)
        cutoff = (datetime.now(CST) - timedelta(days=days)).isoformat(timespec="seconds")
        sql = "SELECT * FROM audit_logs WHERE ts >= ?"
        args = [cutoff]
        if cat:
            sql += " AND category=?"; args.append(cat)
        rows = db.q(sql + " ORDER BY log_id DESC LIMIT 500", *args)
        cats = [r["c"] for r in db.q("SELECT DISTINCT category c FROM audit_logs WHERE ts >= ?", cutoff)]
        sensitive = ("writes", "reply", "toggle", "pause", "quit", "delete", "confirm", "edited")
        c_opts = "".join(f"<option value='{c}'{' selected' if c == cat else ''}>{c}</option>" for c in cats)
        links = " ".join(f"<a style='color:#7ee7b0' href='/audit?days={d}'>{label}</a>"
                         for d, label in ((1, "1天"), (7, "7天"), (30, "30天")))
        body = (f"<div class='card'><h2>审计日志（最近 {days} 天，前 500 条，<span class='err'>红色=敏感操作</span>）</h2>"
                f"<p>{links}</p>"
                f"<form method='get'><input type='hidden' name='days' value='{days}'>"
                f"分类 <select name='cat'><option value=''>全部</option>{c_opts}</select> "
                f"<button class='gray'>筛选</button></form><table>"
                "<tr><th>时间</th><th>分类</th><th>动作</th><th>对象</th><th>详情</th><th>run</th></tr>")
        for a in rows:
            act = a["action"] or ""
            is_sens = a["category"] == "auth" or any(k in act for k in sensitive)
            cls = " style='color:#f87171'" if is_sens else ""
            body += (f"<tr{cls}><td>{a['ts']}</td><td>{a['category']}</td><td>{act}</td>"
                     f"<td>{a['target']}</td><td>{(a['detail'] or '')[:120]}</td><td>{a['run_id'] or '-'}</td></tr>")
        return page(body + "</table></div>")

    @app.get("/storage", response_class=HTMLResponse)
    def storage_page():
        from . import storage as st
        ws = app_ctx.cfg.workspace
        sizes = st.scan(ws)
        quota_gb = int(app_ctx.cfg.agent.get("storage", {}).get("quota_gb", 5))
        used_gb = sizes["total"] / 1024 ** 3
        pct = min(100, used_gb / quota_gb * 100)
        rows = "".join(f"<tr><td>{k}</td><td>{v/1024/1024:.2f} MB</td></tr>"
                       for k, v in sizes.items() if k != "total")
        counts = {t: db.one(f"SELECT COUNT(*) c FROM {t}")["c"]
                  for t in ("runs", "reports_meta", "evidence", "audit_logs", "messages")}
        body = f"""<script>async function sact(op){{if(!confirm('执行 '+op+' ？'))return;
const r=await fetch('/api/storage/'+op,{{method:'POST'}});const j=await r.json();alert(JSON.stringify(j));location.reload()}}</script>
<div class="card"><h2>存储概览</h2>
<table><tr><td>工作区路径</td><td>{ws}</td></tr>
<tr><td>总占用 / 配额</td><td>{used_gb:.2f} GB / {quota_gb} GB（{pct:.1f}%）</td></tr></table>
<table><tr><th>目录</th><th>大小</th></tr>{rows}</table>
<p>记录数：runs={counts['runs']} reports={counts['reports_meta']} evidence={counts['evidence']} audit={counts['audit_logs']} messages={counts['messages']}</p></div>
<div class="card"><h2>存储管理</h2>
<p><button onclick="sact('cleanup_expired')">清理超期数据</button>
<button onclick="sact('cleanup_analysis')">清理分析记录</button>
<button onclick="sact('archive')">归档 6-12 个月数据</button>
<button onclick="sact('purge_year')">清空 1 年以上数据</button>
<button onclick="sact('enforce_quota')">超容量清理</button></p>
<p style="color:#9fb3c0;font-size:12px">保留策略：报告 90 天 / 证据 30 天 / 审计 180 天 / 分析记录与报警消息 3 天；6-12 个月压缩归档；&gt;1 年清空。可在 agent.yaml storage 段调整。</p></div>"""
        return page(body)

    @app.post("/api/storage/{op}")
    def storage_op(op: str):
        from . import storage as st
        ws = app_ctx.cfg.workspace
        if op == "cleanup_expired":
            return st.cleanup_expired(db, ws, app_ctx.cfg)
        if op == "cleanup_analysis":
            days = int(app_ctx.cfg.agent.get("storage", {}).get("analysis_days", 3))
            return {"deleted": st.cleanup_analysis(db, days)}
        if op == "archive":
            return {"archived": len(st.archive_old(db, ws))}
        if op == "purge_year":
            return st.purge_year(db, ws)
        if op == "enforce_quota":
            return {"cleaned": st.enforce_quota(db, ws, app_ctx.cfg)}
        return {"error": "未知操作"}

    @app.get("/api/state")
    def api_state():
        has_running = db.one("SELECT 1 FROM runs WHERE status='running' LIMIT 1") is not None
        status = ("working" if has_running
                  else "attention" if app_ctx.notifier.pending()
                  else db.get_state("agent_status", "idle"))
        return {
            "status": status,
            "pending": app_ctx.notifier.pending(),
            "toast": db.get_state("pet_toast", ""),
            "toast_ts": db.get_state("pet_toast_ts", ""),
            "last_report": db.get_state("last_report", ""),
            "ding_conn": db.get_state("dingtalk_conn", "unknown"),
            "dws_last_poll": db.get_state("dws_last_poll", ""),
            "dws_last_hit": db.get_state("dws_last_hit", ""),
            "writes_disabled": bool(app_ctx.cfg.agent.get("writes_disabled", False)),
            "paused": db.get_state("tasks_paused") == "1",
            "today_alerts": db.one("SELECT COUNT(*) c FROM alerts WHERE date(created_at)=date('now','localtime')")["c"],
            "last_run": (lambda r: f"{r['source']} {r['status']} {r['started_at'][:16]}" if r else "")(
                db.one("SELECT source, status, started_at FROM runs WHERE "
                       "(trigger_type IS NULL OR trigger_type != 'simulate')"
                       " ORDER BY started_at DESC LIMIT 1")),
        }

    @app.get("/assets/{path:path}")
    def asset(path: str):
        fp = os.path.normpath(os.path.join(app_ctx.cfg.workspace, "assets", path))
        base = os.path.normpath(os.path.join(app_ctx.cfg.workspace, "assets"))
        if not fp.startswith(base) or not os.path.exists(fp):
            return HTMLResponse("not found", status_code=404)
        return FileResponse(fp)

    @app.post("/api/simulate")
    async def simulate(request: Request):
        data = await request.json()
        import uuid
        msg = {"msg_id": data.get("msg_id") or f"sim-{uuid.uuid4().hex[:8]}",
               "group": data["group"], "sender": data.get("sender", "tester"),
               "text": data["text"], "at_me": data.get("at_me", False)}
        result = await app_ctx.pipeline.process(msg)
        if result.get("run_id") and msg["msg_id"].startswith("sim-"):
            db.update("runs", "run_id", result["run_id"], trigger_type="simulate")
        return result

    return app
