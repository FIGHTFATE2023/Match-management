/* 竞猜页实时锁定与结果；榜单按数据变化刷新。 */
(function () {
  let busy = false;
  let signature = null;
  function lockChampion() {
    const grid = document.querySelector('.champion-grid');
    if (!grid) return;
    grid.dataset.locked = '1';
    grid.classList.add('is-locked');
    grid.querySelectorAll('.champion-card').forEach(function (card) {
      card.setAttribute('aria-disabled', 'true'); card.tabIndex = -1;
    });
    ['champion-count', 'champion-hint-open'].forEach(function (role) {
      document.querySelector('[data-role="' + role + '"]').hidden = true;
    });
    ['champion-lock-tag', 'champion-hint-locked'].forEach(function (role) {
      document.querySelector('[data-role="' + role + '"]').hidden = false;
    });
    const chosen = grid.querySelector('.is-picked');
    document.querySelector('[data-role="champion-pick-text"]').textContent = chosen ? chosen.dataset.pick + ' 组' : '未选择';
  }
  async function check() {
    if (busy || document.visibilityState === 'hidden') return;
    busy = true;
    try {
      const response = await fetch('/api/guess', {cache: 'no-store', credentials: 'same-origin',
        headers: {'ngrok-skip-browser-warning': 'true'}});
      if (!response.ok || response.redirected) return;
      const data = await response.json();
      if (location.pathname === '/standings') {
        const next = JSON.stringify(data.standings);
        if (signature !== null && signature !== next) location.reload();
        signature = next;
        return;
      }
      if (data.champion_locked) lockChampion();
      Object.keys(data.games).forEach(function (ref) {
        const result = data.games[ref];
        const card = document.querySelector('.game-card[data-ref="' + ref + '"]');
        if (!card) return;
        card.dataset.locked = '1'; card.classList.add('is-locked');
        card.querySelectorAll('.game-side').forEach(function (side) {
          side.tabIndex = -1; side.setAttribute('aria-disabled', 'true');
          const picked = side.dataset.team === result.my_pick;
          side.classList.toggle('is-picked', picked); side.setAttribute('aria-pressed', String(picked));
        });
        let tag = card.querySelector('.game-state');
        if (!tag) {
          tag = document.createElement('span'); tag.className = 'game-state is-locked';
          card.querySelector('.game-card-head').appendChild(tag);
        }
        tag.textContent = '竞猜已锁定';
        const label = card.querySelector('[data-role="guess-result"]');
        label.classList.toggle('is-win', !!(result.settled && result.settled.win));
        label.classList.toggle('is-loss', !!(result.settled && !result.settled.win));
        let text = '本局得分 ' + result.gain_a + ':' + result.gain_b + ' · ';
        if (result.settled) {
          text += (result.settled.win ? '预测成功 +' : '预测未中 +') + result.settled.gain.toFixed(2) + ' 分';
        } else if (result.winner) {
          text += result.winner + ' 组率先获得十五分' + (result.state === 'finished' ? '' : '，待裁判确认结束');
        } else text += '等待一方本局获得十五分';
        label.textContent = text;
      });
    } catch (_) { /* 暂时断网时保留页面，下一轮重试。 */ }
    finally { busy = false; }
  }
  check();
  setInterval(check, 3000);
  window.addEventListener('focus', check);
  document.addEventListener('visibilitychange', check);
  document.addEventListener('guess-updated', check);
})();
