/* 裁判台 · 自定义对局：创建、逐分记分、撤销、删除，全部 fetch 静默提交。
   与正式局记分彼此独立；上场人员按所选组别/项目动态生成下拉。 */
(function () {
  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const csrf = csrfMeta ? csrfMeta.content : '';
  const panel = document.getElementById('custom-panel');
  if (!panel || !csrf) return;

  const rosterEl = document.getElementById('custom-roster');
  let roster = {};
  try { roster = JSON.parse(rosterEl ? rosterEl.textContent : '{}') || {}; } catch (e) { roster = {}; }

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body: new URLSearchParams(Object.assign({ csrf: csrf }, body || {}))
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: '网络异常，请重试' }; });
    });
  }

  // ---------- 上场人员下拉：按组别名单 + 项目人数动态生成 ----------
  const teamA = document.getElementById('custom-team-a');
  const teamB = document.getElementById('custom-team-b');
  const eventSel = document.getElementById('custom-event');

  // 各项目每个上场位置的性别：混双第 1 人男、第 2 人女；男双/男单男；女单女。
  function requiredGender(event, index) {
    if (event === '混双') return index === 0 ? '男' : '女';
    if (event === '男双' || event === '男单') return '男';
    if (event === '女单') return '女';
    return '';
  }

  function buildPlayerSelects(container, team, size, prefix, event) {
    if (!container) return;
    container.innerHTML = '';
    const members = roster[team] || [];
    const count = Math.max(1, Number(size) || 1);
    for (let i = 0; i < count; i++) {
      const label = document.createElement('label');
      label.className = 'custom-player-field';
      const title = document.createElement('span');
      title.textContent = count === 1 ? '上场选手' : ('第 ' + (i + 1) + ' 人');
      const sel = document.createElement('select');
      sel.name = prefix;
      sel.required = true;
      const ph = document.createElement('option');
      ph.value = '';
      ph.textContent = '请选择';
      sel.appendChild(ph);
      const want = requiredGender(event, i);
      members.filter(function (m) { return !want || m.gender === want; }).forEach(function (m) {
        const opt = document.createElement('option');
        opt.value = m.name;
        opt.textContent = m.name + '（' + (m.gender || '—') + '）';
        sel.appendChild(opt);
      });
      label.appendChild(title);
      label.appendChild(sel);
      container.appendChild(label);
    }
  }

  function refreshPlayers() {
    const opt = eventSel ? eventSel.selectedOptions[0] : null;
    const size = opt ? opt.dataset.size : 1;
    const event = opt ? opt.value : '';
    buildPlayerSelects(document.getElementById('custom-players-a'), teamA ? teamA.value : '', size, 'players_a', event);
    buildPlayerSelects(document.getElementById('custom-players-b'), teamB ? teamB.value : '', size, 'players_b', event);
  }
  [teamA, teamB, eventSel].forEach(function (el) {
    if (el) el.addEventListener('change', refreshPlayers);
  });
  refreshPlayers();

  // ---------- 创建对局 ----------
  const createForm = document.getElementById('custom-create-form');
  const createMsg = document.getElementById('custom-create-msg');
  function showCreateMsg(text, ok) {
    if (!createMsg) return;
    createMsg.hidden = false;
    createMsg.textContent = text;
    createMsg.className = 'custom-msg ' + (ok ? 'is-ok' : 'is-err');
  }
  if (createForm) {
    createForm.addEventListener('submit', function (e) {
      e.preventDefault();
      const fd = new FormData(createForm);
      const params = new URLSearchParams();
      params.set('csrf', csrf);
      fd.forEach(function (v, k) { if (k !== 'csrf') params.append(k, v); });
      fetch('/referee/custom/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: params
      }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.ok) {
          showCreateMsg(d.message || '已创建', true);
          setTimeout(function () { location.reload(); }, 250);
        } else {
          showCreateMsg(d.error || '创建失败', false);
        }
      }).catch(function () { showCreateMsg('网络异常，请重试', false); });
    });
  }

  // ---------- 记分 / 撤销 / 删除 ----------
  const list = document.getElementById('custom-list');
  function cardOf(btn) { return btn.closest('.custom-card'); }

  function applyResult(card, d) {
    if (!card || !d) return;
    const a = card.querySelector('[data-role="cs-a"]');
    const b = card.querySelector('[data-role="cs-b"]');
    if (a) a.textContent = d.score_a;
    if (b) b.textContent = d.score_b;
    card.setAttribute('data-v', String(d.v || 0));
    card.setAttribute('data-state', d.state || 'running');
    const state = card.querySelector('[data-role="custom-state"]');
    if (state) {
      state.textContent = d.state === 'finished'
        ? ('已结束 · 胜方 ' + (d.winner || '') + ' 组')
        : (d.pending ? '待确认结果' : '进行中');
    }
  }

  if (list) {
    list.addEventListener('click', function (e) {
      const btn = e.target.closest('button');
      if (!btn) return;
      const card = cardOf(btn);
      if (!card) return;
      const id = card.getAttribute('data-cid');

      if (btn.hasAttribute('data-cpoint')) {
        post('/referee/custom/point', { id: id, team: btn.getAttribute('data-cpoint') })
          .then(function (d) {
            if (!d.ok) { alert(d.error || '记分失败'); return; }
            applyResult(card, d);
            if (d.state === 'finished') setTimeout(function () { location.reload(); }, 350);
          });
        return;
      }
      if (btn.hasAttribute('data-cundo')) {
        post('/referee/custom/undo', { id: id }).then(function (d) {
          if (!d.ok) { alert(d.error || '撤销失败'); return; }
          applyResult(card, d);
          setTimeout(function () { location.reload(); }, 250);
        });
        return;
      }
      if (btn.hasAttribute('data-cdelete')) {
        if (!confirm('删除这条自定义对局记录？榜单数据会同步回退。')) return;
        post('/referee/custom/delete', { id: id }).then(function (d) {
          if (!d.ok) { alert(d.error || '删除失败'); return; }
          card.remove();
          if (!list.querySelector('.custom-card')) {
            const empty = document.createElement('p');
            empty.className = 'empty-hint';
            empty.textContent = '暂无自定义对局记录。';
            list.appendChild(empty);
          }
        });
        return;
      }
    });
  }

  // ---------- 轮询 /api/live 同步别人记的自定义对局分数 ----------
  const versions = Object.create(null);
  let polling = false;
  function poll() {
    if (polling || document.visibilityState === 'hidden') return;
    polling = true;
    fetch('/api/live', { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        const custom = (data && data.custom) || {};
        Object.keys(custom).forEach(function (key) {
          const d = custom[key];
          const card = list && list.querySelector('.custom-card[data-cid="' + d.id + '"]');
          if (!card) return;
          if (versions[key] !== d.v) {
            versions[key] = d.v;
            applyResult(card, d);
          }
        });
      })
      .catch(function () { /* 轮询失败静默 */ })
      .then(function () { polling = false; });
  }
  setInterval(poll, 2000);
})();
