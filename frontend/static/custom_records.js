/* 赛程页 · 自定义对局记录局部刷新：与正式局 /api/live 共用一次请求，
   按 cid 比对比分/状态，只 patch 变化的行，避免整页 reload。 */
(function () {
  const panel = document.getElementById('custom-record-panel');
  if (!panel) return;
  let busy = false;

  function apply(row, d) {
    const score = row.querySelector('[data-role="cr-score"]');
    if (score) score.textContent = d.score_a + ' : ' + d.score_b;
    const state = row.querySelector('[data-role="cr-state"]');
    if (state) {
      if (d.state === 'running') {
        state.innerHTML = '<span class="is-live">进行中</span>';
      } else {
        state.innerHTML = '<span class="is-done">' + (d.winner || '') + ' 组胜</span>';
      }
    }
    row.setAttribute('data-v', String(d.v || 0));
  }

  function poll() {
    if (busy || document.visibilityState === 'hidden') return;
    busy = true;
    fetch('/api/live', { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        const custom = (data && data.custom) || {};
        Object.keys(custom).forEach(function (key) {
          const d = custom[key];
          const row = panel.querySelector('.custom-record-row[data-cid="' + d.id + '"]');
          if (!row) return;
          if (row.getAttribute('data-v') !== String(d.v)) apply(row, d);
        });
      })
      .catch(function () { /* 静默 */ })
      .then(function () { busy = false; });
  }
  setInterval(poll, 2000);
})();
