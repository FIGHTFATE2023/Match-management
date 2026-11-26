/* node tests/test_tournament.cjs: exercise the actual celebration polling script. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element() {
  return {
    children: [], dataset: {}, listeners: {}, textContent: '',
    style: { setProperty() {} }, classList: { toggle() {} },
    appendChild(child) { this.children.push(child); },
    replaceChildren() { this.children = []; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
  };
}

(async function () {
  const dialog = element();
  dialog.dataset.viewer = 'test';
  let shows = 0;
  let focusRestored = 0;
  const selectors = ['[data-champion-winner]', '#champion-dialog-title',
    '[data-champion-rosters]', '.champion-confetti'];
  const parts = Object.fromEntries(selectors.map(selector => [selector, element()]));
  const close = element();
  const replay = element();
  const progress = element();
  const teams = ['A', 'B', 'C', 'D'].map(team => {
    const row = element(); row.dataset.confirmTeam = team;
    row.label = element(); row.querySelector = () => row.label; return row;
  });
  dialog.querySelector = selector => parts[selector];
  dialog.querySelectorAll = selector => selector === '[data-champion-close]' ? [close] : [];
  dialog.showModal = () => { dialog.open = true; shows += 1; };
  dialog.close = () => { dialog.open = false; dialog.listeners.close(); };
  const memory = new Map();
  let poll;
  let data = { finalized: false, confirmed_count: 1, confirmed_teams: ['A'], champions: [], champion_rosters: {} };
  const context = {
    document: { visibilityState: 'visible',
      getElementById: id => id === 'champion-dialog' ? dialog : null,
      querySelectorAll: selector => ({ '[data-champion-open]': [replay],
        '[data-tournament-progress]': [progress], '[data-confirm-team]': teams })[selector] || [],
      createElement: element, addEventListener() {},
      activeElement: { isConnected: true, focus() { focusRestored += 1; } },
    },
    window: { addEventListener() {} }, location: { reload() { throw Error('Unexpected reload'); } },
    fetch: async (url) => {
      if (url === '/tournament/dismiss') data = { ...data, celebration_dismissed: true };
      return { ok: true, redirected: false, json: async () => data };
    },
    URLSearchParams,
    localStorage: { getItem: key => memory.get(key), setItem: (key, value) => memory.set(key, value) },
    setInterval: callback => { poll = callback; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../frontend/static/tournament.js'), 'utf8'), context);
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  assert.equal(progress.textContent, '1 / 4 组已确认');
  assert.equal(teams[0].label.textContent, '已确认');
  assert.equal(teams[1].label.textContent, '等待队长确认');
  assert.equal(shows, 0);

  data = { finalized: true, finalized_at: 'test-result', confirmed_count: 4,
    confirmed_teams: ['A', 'B', 'C', 'D'], champions: ['A'], champion_rosters: { A: ['队员一', '队员二'] } };
  poll(); await flush();
  assert.equal(shows, 1);
  assert.equal(parts['[data-champion-winner]'].textContent, 'A 组');
  const list = parts['[data-champion-rosters]'].children[0].children[0];
  assert.deepEqual(list.children.map(child => child.textContent), ['队员一', '队员二']);
  assert.equal(parts['.champion-confetti'].children.length, 24);
  close.listeners.click();
  assert.equal(focusRestored, 1);
  poll(); await flush();
  assert.equal(shows, 1, 'Polling must not reopen a dismissed result');
  replay.listeners.click();
  assert.equal(shows, 2, 'Manual replay remains available');
  close.listeners.click();

  data = { ...data, finalized_at: 'tie-result', champions: ['A', 'B'],
    celebration_dismissed: false, champion_rosters: { A: ['甲'], B: ['乙'] } };
  poll(); await flush();
  assert.match(parts['#champion-dialog-title'].textContent, /并列冠军/);
  assert.equal(parts['[data-champion-winner]'].textContent, 'A 组、B 组');
  assert.equal(parts['[data-champion-rosters]'].children.length, 2);
  close.listeners.click();
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../frontend/static/tournament.js'), 'utf8'), context);
  await flush();
  assert.equal(shows, 3, 'Entering another page must not reopen a dismissed result');
  poll(); await flush();
  assert.equal(shows, 3, 'Polling must not reopen a dismissed result');
  memory.clear();
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../frontend/static/tournament.js'), 'utf8'), context);
  await flush();
  assert.equal(shows, 3, 'Server acknowledgement must suppress the result even without local storage');
  dialog.dataset.viewer = 'another-user';
  data = { ...data, celebration_dismissed: false };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../frontend/static/tournament.js'), 'utf8'), context);
  await flush();
  assert.equal(shows, 4, 'A different user still receives their first celebration');
  console.log('Tournament celebration: all checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
