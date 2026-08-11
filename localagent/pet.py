import os
import subprocess
import webbrowser

HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:transparent;overflow:hidden;user-select:none;-webkit-user-select:none}
#wrap{position:relative;width:100px;height:100px}
img{width:80px;height:80px;position:absolute;left:10px;top:10px;border-radius:18px;object-fit:cover;-webkit-user-drag:none}
#dot{width:80px;height:80px;position:absolute;left:10px;top:10px;border-radius:40px;background:#00c16a;display:none}
#toast{position:absolute;left:2px;bottom:0;max-width:96px;background:rgba(22,29,33,.92);
color:#7ee7b0;font:11px -apple-system,'PingFang SC';padding:4px 6px;border-radius:8px;display:none;cursor:pointer}
#badge{position:absolute;right:6px;top:6px;background:#f59e0b;color:#111;border-radius:10px;
padding:1px 6px;font:11px sans-serif;display:none}
#bubble{position:absolute;left:2px;top:0;max-width:150px;background:rgba(22,29,33,.95);color:#e6edf3;
font:11px -apple-system,'PingFang SC';padding:6px 8px;border-radius:8px;display:none;white-space:pre-line;z-index:8}
#menu{position:absolute;background:#161d21;border:1px solid #22303a;border-radius:8px;display:none;z-index:9}
#menu div{padding:6px 12px;font:12px -apple-system,'PingFang SC';color:#e6edf3;cursor:pointer;white-space:nowrap}
#menu div:hover{background:#22303a}
#menu div.danger{color:#f87171}
#modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;
color:#e6edf3;font:11px -apple-system,'PingFang SC';padding:6px;z-index:10}
#modal .box{background:#161d21;border:1px solid #f59e0b;border-radius:10px;padding:6px;max-height:88px;overflow:auto}
button{background:#00c16a;border:0;border-radius:6px;padding:4px 8px;margin:2px;cursor:pointer}
button.gray{background:#374151;color:#e6edf3}
button.arm{background:#f87171;color:#111}
</style></head><body><div id="wrap">
<img id="img" class="idle"><div id="dot"></div><div id="badge"></div>
<div id="toast"></div><div id="bubble"></div>
<div id="menu"></div>
<div id="modal"><b style="color:#f59e0b">⚠ 待确认异常</b><div class="box" id="mlist"></div></div>
</div>
<script>
const base='http://127.0.0.1:PORT';
const ASSETS='ASSETS_URL';
const EXT=EXT_MAP;
function srcOf(st){return ASSETS+st+'.'+(EXT[st]||'png')}
let lastToastTs='', lastReport='', pinned=false, paused=false, curStatus='', S=null;
function el(id){return document.getElementById(id)}
function api(){return window.pywebview&&window.pywebview.api}
function openU(path){const a=api(); if(a) a.open_url(path)}
const img=el('img');
img.onerror=()=>{img.style.display='none';el('dot').style.display='block'};
img.src=srcOf('idle');
function bubbleText(s,n){
 return `状态:${s.status} | 钉钉:${s.ding_conn}`
  + `\\n最近任务:${s.last_run||'无'}`
  + `\\n今日异常:${s.today_alerts} 待确认:${n}`
  + (s.paused?' | 已暂停':'') + (s.writes_disabled?' | 写已禁用':'');
}
function renderBubble(){
 const b=el('bubble');
 if(S && pinned) { b.textContent=bubbleText(S,S.pending.length); b.style.display='block'; }
 else if(!pinned && b.dataset.hover==='1' && S){ b.textContent=bubbleText(S,S.pending.length); b.style.display='block'; }
 else b.style.display='none';
}
async function tick(){
 try{
  S=await (await fetch(base+'/api/state')).json();
  const st=S.status||'idle';
  if(st!==curStatus){ curStatus=st; img.src=srcOf(st);
    el('dot').style.background={idle:'#00c16a',working:'#38bdf8',attention:'#f59e0b',error:'#f87171'}[st]||'#00c16a'; }
  lastReport=S.last_report||''; paused=!!S.paused;
  const n=S.pending.length;
  el('badge').style.display=n?'block':'none'; el('badge').textContent=n;
  el('modal').style.display=n?'block':'none';
  if(n){
    el('mlist').innerHTML=S.pending.map(a=>`<div>[${a.severity}] ${a.summary}<br>
      <button onclick="op('${a.alert_id}','ack')">确认</button>
      <button class="gray" onclick="twoStep(this,()=>op('${a.alert_id}','ignore'),'确认忽略?')">忽略</button>
      ${a.report_path?`<button class="gray" onclick="openU('/reports/by-path?p='+encodeURIComponent('${a.report_path}'))">查看报告</button>`:''}
      <button class="gray" onclick="closeModal()">关闭</button></div>`).join('');
  }
  if(S.toast_ts!==lastToastTs && S.toast){ lastToastTs=S.toast_ts;
    el('toast').style.display='block'; el('toast').textContent=S.toast;
    setTimeout(()=>el('toast').style.display='none', 4000); }
  renderBubble();
 }catch(e){}
}
function closeModal(){el('modal').style.display='none'}
async function op(id,act){await fetch(base+'/alerts/'+id+'/'+act,{method:'POST'});tick()}
function twoStep(btn,fn,label){
 if(btn.dataset.armed==='1'){fn();return}
 btn.dataset.armed='1'; btn.dataset.old=btn.textContent;
 btn.textContent=label||'再点一次确认'; btn.classList.add('arm');
 setTimeout(()=>{btn.dataset.armed='0';btn.textContent=btn.dataset.old;btn.classList.remove('arm')},3000);
}
function showMenu(x,y){
 const m=el('menu');
 const items=[
  ['打开管理页面',()=>openU('/')],
  ['打开最近报告',()=>openU(lastReport?'/reports/by-path?p='+encodeURIComponent(lastReport):'/')],
  ['打开报警中心',()=>openU('/alerts')],
  [paused?'恢复任务':'暂停任务',async()=>{const a=api(); if(a){await a.pause_toggle(); tick()}}],
  ['隐藏小窗',()=>{const a=api(); if(a) a.hide_pet()}],
  ['退出 LocalAgent',()=>{const a=api(); if(a) a.quit_agent()},'danger'],
 ];
 m.innerHTML='';
 items.forEach(([t,fn,cls])=>{const d=document.createElement('div');d.textContent=t;
   if(cls)d.className=cls;
   d.onclick=e=>{e.stopPropagation();m.style.display='none';fn()};m.appendChild(d)});
 m.style.left=Math.min(x,16)+'px'; m.style.top=Math.min(y,28)+'px'; m.style.display='block';
}
el('toast').addEventListener('click',()=>openU(lastReport?'/reports/by-path?p='+encodeURIComponent(lastReport):'/'));
img.addEventListener('dblclick',()=>openU('/'));
el('dot').addEventListener('dblclick',()=>openU('/'));
el('wrap').addEventListener('click',e=>{
 if(el('menu').style.display==='block'){el('menu').style.display='none';return}
 pinned=!pinned; renderBubble();
});
el('wrap').addEventListener('mouseenter',()=>{el('bubble').dataset.hover='1';renderBubble()});
el('wrap').addEventListener('mouseleave',()=>{el('bubble').dataset.hover='0';renderBubble()});
document.addEventListener('contextmenu',e=>{e.preventDefault();showMenu(e.clientX,e.clientY)});
document.addEventListener('click',e=>{if(!el('menu').contains(e.target))el('menu').style.display='none'});
setInterval(tick,2000);tick();
</script></body></html>"""


class _Api:
    """JS 桥：pywebview 的 WKWebView 拦截 window.open，打开链接/控制类操作统一走这里。"""

    def __init__(self, db, port):
        self.db = db
        self.port = port
        self.window = None

    def open_url(self, path):
        if isinstance(path, str) and path.startswith("/"):
            webbrowser.open(f"http://127.0.0.1:{self.port}{path}")

    def pause_toggle(self):
        cur = self.db.get_state("tasks_paused") == "1"
        self.db.set_state("tasks_paused", "0" if cur else "1")
        self.db.audit("task", "tasks_pause_toggled", "", f"{not cur} (pet)")
        return not cur

    def hide_pet(self):
        self.db.set_state("pet_hidden", "1")
        self.db.audit("ui", "pet_hidden")
        if self.window:
            self.window.destroy()

    def quit_agent(self):
        self.db.audit("task", "agent_quit_requested", "", "pet menu")
        stop = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "scripts", "stop.sh")
        try:
            subprocess.run(["bash", stop], capture_output=True, timeout=15)
        except Exception:
            pass
        if self.window:
            self.window.destroy()


class PetUI:
    """pywebview 桌面小角色；无 pywebview 时降级为 no-op（提醒走系统通知）。"""

    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.port = cfg.web.get("port", 8765)

    def toast(self, text):
        self.db.set_state("pet_toast", text)
        self.db.set_state("pet_toast_ts", __import__("localagent.db", fromlist=["now"]).now())

    def modal(self, alerts):
        pass  # pet 通过 /api/state 轮询 pending 自行弹 modal

    def start_gui_process(self):
        """AppKit 要求 NSWindow 在进程主线程创建，守护进程主线程被 asyncio 占用，
        因此小窗跑在独立子进程里。已存在或已隐藏则不启动。
        用 PID 文件守卫：pgrep -f 在守护进程上下文会误判“已运行”。"""
        if self.db.get_state("pet_hidden") == "1":
            return
        pidfile = os.path.join(self.cfg.workspace, "data", "pet.pid")
        if self._pid_alive(pidfile):
            self.db.audit("ui", "pet_already_running")
            return
        try:
            import sys
            py = os.path.join(sys.prefix, "bin", "python")
            if not os.path.exists(py):
                py = sys.executable
            logp = os.path.join(self.cfg.workspace, "logs", "gui_pet.log")
            lf = open(logp, "a")
            p = subprocess.Popen([py, "-m", "localagent.gui", "pet"],
                                 start_new_session=True, stdout=lf, stderr=lf)
            with open(pidfile, "w") as f:
                f.write(str(p.pid))
            self.db.audit("ui", "pet_process_spawned", "", str(p.pid))
        except Exception as e:
            self.db.audit("ui", "pet_spawn_failed", "", str(e)[:120])

    @staticmethod
    def _pid_alive(pidfile):
        try:
            with open(pidfile) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return True
        except (FileNotFoundError, ValueError, ProcessLookupError):
            return False
        except PermissionError:
            return True

    def start_foreground(self):
        """在 GUI 子进程主线程内运行小窗（阻塞）。"""
        if self.db.get_state("pet_hidden") == "1":
            return
        try:
            import webview  # noqa
        except ImportError:
            self.db.audit("ui", "pet_skipped", "", "pywebview not installed")
            return
        import json
        ext = {}
        char_dir = os.path.join(self.cfg.workspace, "assets", "character")
        for st in ("idle", "working", "attention", "error"):
            ext[st] = "gif" if os.path.exists(os.path.join(char_dir, st + ".gif")) else "png"
        html = (HTML.replace("PORT", str(self.port))
                .replace("ASSETS_URL", f"http://127.0.0.1:{self.port}/assets/character/")
                .replace("EXT_MAP", json.dumps(ext)))
        x = self.db.get_state("pet_x")
        y = self.db.get_state("pet_y")
        kwargs = dict(title="LocalAgent", html=html, width=100, height=100,
                      frameless=True, easy_drag=True, transparent=True,
                      on_top=True, resizable=False)
        if x and y:
            try:
                kwargs["x"], kwargs["y"] = int(x), int(y)
            except ValueError:
                pass
        import webview
        try:
            from AppKit import NSApplication, NSImage
            icon_path = os.path.join(self.cfg.workspace, "assets", "app_icon.png")
            if os.path.exists(icon_path):
                nsimg = NSImage.alloc().initWithContentsOfFile_(icon_path)
                if nsimg:
                    NSApplication.sharedApplication().setApplicationIconImage_(nsimg)
        except Exception:
            pass
        api = _Api(self.db, self.port)
        w = webview.create_window(js_api=api, **kwargs)
        api.window = w

        def _save_pos():
            try:
                self.db.set_state("pet_x", str(w.x))
                self.db.set_state("pet_y", str(w.y))
            except Exception:
                pass

        try:
            w.events.moved += _save_pos
        except Exception:
            pass
        webview.start(lambda: None, debug=False)
        _save_pos()

    def start(self):
        self.start_gui_process()

    def open_admin(self):
        webbrowser.open(f"http://127.0.0.1:{self.port}/")
