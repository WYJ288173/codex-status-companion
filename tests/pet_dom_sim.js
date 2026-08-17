// pet 悬浮窗 JS 行为仿真：stub DOM 后执行真实脚本，验证点击不再弹气泡覆盖图标
const fs = require('fs');

function makeEl(id) {
  return {
    id, style: { display: 'none', background: '' }, dataset: {},
    textContent: '', src: '', className: '', innerHTML: '',
    _listeners: {},
    addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
    dispatch(ev, e) { (this._listeners[ev] || []).forEach(fn => fn(e || { clientX: 0, clientY: 0, stopPropagation() {}, target: this })); },
    contains() { return false; },
  };
}

const els = {};
['img', 'dot', 'badge', 'toast', 'bubble', 'menu', 'wrap'].forEach(id => els[id] = makeEl(id));
els.wrap.contains = () => true;

const fetchCalls = [];
global.fetch = (url) => {
  fetchCalls.push(String(url));
  if (String(url).includes('/api/state')) {
    return Promise.resolve({ json: () => Promise.resolve({
      status: 'attention', pending: [{ severity: 'P2', summary: 'x' }],
      ding_conn: 'dws_polling', last_run: 'g failed 08-17', today_alerts: 3,
      toast: '', toast_ts: '', last_report: '', paused: false, writes_disabled: false,
    }) });
  }
  return Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
};
global.window = { pywebview: undefined };
global.document = {
  getElementById: (id) => els[id],
  _listeners: {},
  addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
};
global.setInterval = () => 0;   // 不跑轮询定时器
global.setTimeout = (fn) => { setImmediate(fn); return 0; };  // toast 立即收起，便于断言

const js = fs.readFileSync(process.argv[2], 'utf-8');
eval(js);

function assert(name, cond) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name);
  if (!cond) process.exit(1);
}

setTimeout === null; // noop
(async () => {
  // 等首次 tick 完成（脚本末尾 tick() 是 async），toast 收起
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));

  // 1. 单击 wrap：不得有任何内容显示（气泡已整体移除）
  els.wrap.dispatch('click');
  assert('单击后无任何内容显示', els.toast.style.display === 'none' && els.menu.style.display === 'none');

  // 2. 悬停不再有任何响应（mouseenter/mouseleave 监听已移除）
  els.wrap.dispatch('mouseenter');
  assert('悬停无响应', els.toast.style.display === 'none' && els.menu.style.display === 'none');
  els.wrap.dispatch('mouseleave');

  // 3. 连续多次单击也不得显示任何内容
  for (let i = 0; i < 5; i++) els.wrap.dispatch('click');
  assert('连续单击后仍无内容显示', els.toast.style.display === 'none' && els.menu.style.display === 'none');

  // 4. 点击 toast 只产生 /api/open 请求（窗内无导航）
  els.toast.dispatch('click');
  assert('toast 点击走 /api/open', fetchCalls.some(u => u.includes('/api/open?path=%2Falerts')));

  // 5. 源码无气泡/固定残留
  assert('无 bubble 残留', !js.includes('bubble'));
  assert('无 pinned 残留', !js.includes('pinned'));
  console.log('PET_DOM_SIM_OK');
})();
