/* Run: node tests/test_live_predictions.cjs. Exercises the actual polling renderer. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element() {
  const classes = new Set();
  return {
    hidden: true, style: {}, textContent: '', className: '', attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    classList: { toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); } },
  };
}

(async function () {
  const roles = Object.fromEntries(['predict', 'predict-bar', 'bar-a', 'bar-b',
    'legend-a', 'legend-b', 'predict-hint', 'guess-result'].map(name => [name, element()]));
  const card = element();
  card.querySelector = selector => {
    const role = selector.match(/^\[data-role="([^"]+)"\]$/);
    return role ? roles[role[1]] || null : null;
  };
  let item = { v: 1, state: 'running', team_a: 'A', team_b: 'B',
    score_a: 15, score_b: 5, start_a: 15, start_b: 5,
    predict: { a_stake: 3, b_stake: 1 }, my_pick: 'A', expected: 1.5, settled: null };
  let poll;
  const context = {
    document: { visibilityState: 'visible', getElementById: () => null,
      querySelector: selector => selector === '.game-card[data-ref="1-2"]' ? card : null,
      addEventListener() {} },
    window: { addEventListener() {} },
    fetch: async () => ({ json: async () => ({ items: { '1-2': item } }) }),
    setInterval: callback => { poll = callback; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../frontend/static/live.js'), 'utf8'), context);
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  assert.equal(roles.predict.hidden, false);
  assert.equal(roles['bar-a'].style.width, '75%');
  assert.equal(roles['bar-b'].style.width, '25%');
  assert.match(roles['legend-a'].textContent, /A 组 · 竞猜 3 分/);
  assert.match(roles['predict-hint'].textContent, /本小局先新增十五分.*1\.50/);

  // Same score revision, changed prediction data: the card must still repaint.
  item = { ...item, predict: { a_stake: 0, b_stake: 1 }, my_pick: 'B', expected: 1 };
  poll(); await flush();
  assert.equal(roles['bar-a'].style.width, '0%');
  assert.equal(roles['bar-b'].style.width, '100%');
  assert.match(roles['predict-hint'].textContent, /B 组.*1\.00/);

  // Observers see the ratio without personal rewards or a result.
  item = { ...item, my_pick: null, expected: null };
  poll(); await flush();
  assert.equal(roles.predict.hidden, false);
  assert.equal(roles['predict-hint'].hidden, true);
  assert.equal(roles['guess-result'].hidden, true);

  item = { ...item, state: 'finished', settled: { win: true, gain: 1.5 } };
  poll(); await flush();
  assert.equal(roles.predict.hidden, true);
  assert.equal(roles['guess-result'].hidden, false);
  assert.match(roles['guess-result'].textContent, /竞猜成功.*1\.50/);

  item = { ...item, settled: { win: false, gain: 0 } };
  poll(); await flush();
  assert.match(roles['guess-result'].textContent, /竞猜失败/);
  assert.match(roles['guess-result'].className, /is-loss/);

  item = { ...item, state: 'running', predict: null, settled: null };
  poll(); await flush();
  assert.equal(roles.predict.hidden, true);
  assert.equal(roles['guess-result'].hidden, true);
  console.log('Live prediction renderer: all checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
