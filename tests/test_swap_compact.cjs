/**
 * 回归：换班请求页改成「一条三行、说明折叠、已结束收起」。
 * 用法：node tests/test_swap_compact.cjs
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const context = vm.createContext({
  console,
  crypto: require('node:crypto').webcrypto,
  structuredClone,
  URLSearchParams,
  CustomEvent: class CustomEvent { constructor(type, init = {}) { this.type = type; this.detail = init.detail; } },
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  location: { hash: '#messages' },
  history: { replaceState() {} },
  document: {
    styleSheets: [],
    documentElement: { dataset: {} },
    addEventListener() {},
    dispatchEvent() { return true; },
    querySelector: (s) => (s === '#layout-switch' ? { addEventListener() {} } : null),
    querySelectorAll: () => [],
  },
  window: { addEventListener() {}, scrollTo() {}, innerWidth: 1280 },
  assert,
});

vm.runInContext(fs.readFileSync('app/static/app.js', 'utf8').replace(/^init\(\);\r?$/m, ''), context);

vm.runInContext(`
  S.boot = { logged_in: true, demo: true, mail_configured: false };
  S.data = {
    me: { id: 'm1', name: '甲', caps: ['can_register'] },
    settings: { public_swap_mail_mode: 'test', share_schedule: true },
    slots: [
      { name: '五六节', start: '14:00', end: '16:00' },
      { name: '一二节', start: '08:00', end: '10:00' },
    ],
    server_time: '2026-11-06T10:00:00+08:00',
    members: [{ id: 'm1', name: '甲' }, { id: 'm2', name: '乙' }],
    public_swaps: [
      { id: 'p1', requester_id: 'm2', requester_name: '乙', state: 'open', date: '2026-11-03', slot: 0, shift_id: 's1', reason: '测试说明一',
        offers: [{ id: 'o1', member_id: 'm1', member_name: '甲', state: 'active', reply_kind: 'cover' }] },
      { id: 'p2', requester_id: 'm1', requester_name: '甲', state: 'matched', date: '2026-11-03', slot: 0, shift_id: 's2', reason: '', offers: [] },
      { id: 'p3', requester_id: 'm1', requester_name: '甲', state: 'canceled', date: '2026-11-03', slot: 0, shift_id: 's3', reason: '', offers: [] },
    ],
    swaps: [
      { id: 'w1', requester_id: 'm2', requester_name: '乙', recipient_id: 'm1', recipient_name: '甲', state: 'pending',
        date: '2026-11-03', slot: 0, reason: '说明A', created_at: '2026-11-06T09:00:00+08:00' },
      { id: 'w2', requester_id: 'm1', requester_name: '甲', recipient_id: 'm2', recipient_name: '乙', state: 'accepted',
        date: '2026-11-04', slot: 1, target_shift_id: 't1', target_date: '2026-11-05', target_slot: 0, reason: '',
        decided_at: '2026-11-06T08:00:00+08:00' },
    ],
    messages: [], records: [], shifts: [], todos: [], handoffs: [], mail: [], assigned: [], historical_swaps: [],
  };
  S.params = {};
  S.page = 'messages';
  globalThis.__pub = publicSwapList();
  globalThis.__req = swapRequests();
`, context);

const pub = vm.runInContext('__pub', context);
const req = vm.runInContext('__req', context);

assert.match(pub, /swap-item/, '公开求换班卡片应使用紧凑布局');
assert.match(pub, /已结束 2 条/, '已结束的公开换班应折叠成一组');
assert.doesNotMatch(pub, /需要换出的班次/, '每张卡片重复的「需要换出的班次」应去掉');
assert.match(pub, /说明与通知/, '说明应折叠在 details 里');
assert.equal((pub.match(/class="swap-item"/g) || []).length, 3, '三条公开换班都应渲染');

assert.match(req, /swap-item/, '个人换班卡片应使用紧凑布局');
assert.match(req, /已结束 1 条/, '已处理的个人换班应折叠');
assert.match(req, /接受换班/, '待处理请求仍要保留接受按钮');
assert.match(req, /查看值班表/, '已接受的请求保留值班表入口');
assert.match(req, /swap-detail/, '说明与邮件状态应折叠');

console.log('PASS: 换班页紧凑化（重复标签去掉、说明折叠、已结束收起、按钮保留）');
