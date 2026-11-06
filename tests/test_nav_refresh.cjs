/**
 * 回归：切到排班/交接时「先渲染、后后台刷新」，不再阻塞等 /api/state。
 * 用法：node tests/test_nav_refresh.cjs
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const handlers = {};
const context = vm.createContext({
  console, crypto: require('node:crypto').webcrypto, structuredClone, URLSearchParams,
  CustomEvent: class CustomEvent { constructor(type, init = {}) { this.type = type; this.detail = init.detail; } },
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0,
  location: { hash: '#home' }, history: { replaceState() {} },
  document: {
    styleSheets: [], documentElement: { dataset: {} },
    addEventListener() {}, dispatchEvent() { return true; },
    querySelector: (s) => (s === '#layout-switch' ? { addEventListener() {} } : null),
    querySelectorAll: () => [],
  },
  window: { addEventListener: (name, fn) => { handlers[name] = fn; }, scrollTo() {}, innerWidth: 1280 },
  assert, handlers,
});

const source = fs.readFileSync('app/static/app.js', 'utf8').replace(/^init\(\);\r?$/m, '');
vm.runInContext(source, context);

const tick = () => new Promise((r) => setImmediate(r));

(async () => {
  vm.runInContext(`
    let renders = 0, apiCalls = 0, pending = null;
    globalThis.__stats = () => ({ renders, apiCalls });
    globalThis.__resolve = (value) => pending && pending(value);
    globalThis.__resetPending = () => { pending = null; };
    render = () => { renders++; };
    toast = () => {};
    closeModal = () => {};
    stopHomeDrag = () => {};
    rememberEditor = () => {};
    api = () => { apiCalls++; return new Promise((res) => { pending = res; }); };
    S.boot = { logged_in: true, demo: false };
    S.data = {
      me: { id: 'm1', name: '测试', caps: ['can_register'] },
      records: [], shifts: [], members: [], messages: [], todos: [], handoffs: [],
      mail: [], swaps: [], public_swaps: [], assigned: [], slots: [], settings: {},
      server_time: '2026-11-06T10:00:00+08:00',
    };
    S.loadedAt = Date.now();
    location.hash = '#schedule';
  `, context);

  // 场景 1：数据刚拉过（15 秒内）→ 直接渲染，不发请求
  await vm.runInContext('handlers.hashchange()', context);
  await tick();
  let stats = vm.runInContext('__stats()', context);
  assert.equal(stats.renders, 1, '切换页应先渲染一次');
  assert.equal(stats.apiCalls, 0, '数据新鲜时不该再请求 /api/state');

  // 场景 2：数据陈旧 → 先渲染，再后台拉；拉回来内容没变就不二次渲染
  vm.runInContext(`
    S.loadedAt = 0;
    location.hash = '#messages';
  `, context);
  const running = vm.runInContext('handlers.hashchange()', context);
  await tick();
  stats = vm.runInContext('__stats()', context);
  assert.equal(stats.renders, 2, '陈旧数据也要先渲染，不能等网络');
  assert.equal(stats.apiCalls, 1, '陈旧数据应发起一次后台刷新');

  vm.runInContext('__resolve(JSON.parse(JSON.stringify(S.data)))', context);
  await running;
  await tick();
  stats = vm.runInContext('__stats()', context);
  assert.equal(stats.renders, 2, '刷新结果没变化时不该重复渲染');

  // 场景 3：后台刷新拿回新数据 → 补一次渲染
  vm.runInContext(`
    S.loadedAt = 0;
    __resetPending();
    location.hash = '#schedule';
  `, context);
  const running2 = vm.runInContext('handlers.hashchange()', context);
  await tick();
  vm.runInContext(`
    const next = JSON.parse(JSON.stringify(S.data));
    next.messages = [{ id: 'x' }];
    __resolve(next);
  `, context);
  await running2;
  await tick();
  stats = vm.runInContext('__stats()', context);
  assert.equal(stats.renders, 4, '新数据回来应再渲染一次（3=首次，4=更新后）');

  console.log('PASS: 切页先渲染、数据新鲜不请求、后台刷新无变化不重绘、有变化补渲染');
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
