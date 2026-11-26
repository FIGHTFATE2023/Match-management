/* 四队长确认进度与全站冠军庆祝；仅展示已保存的最终结果。 */
(function () {
  const dialog = document.getElementById('champion-dialog');
  if (!dialog) return;
  const form = document.getElementById('tournament-confirm-form');
  const panel = document.getElementById('tournament-end');
  const feedback = document.getElementById('tournament-feedback');
  let latest = null;
  let polling = false;
  let submitting = false;
  let shown = '';
  let returnFocus = null;
  let activeResult = '';

  function dismissalKey(result) {
    return 'champion-dismissed:' + dialog.dataset.viewer + ':' + result;
  }
  function persistDismissal() {
    if (!activeResult) return;
    try { localStorage.setItem(dismissalKey(activeResult), '1'); } catch (_) {}
    if (latest && latest.finalized_at === activeResult) latest.celebration_dismissed = true;
    fetch('/tournament/dismiss', {
      method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: {'X-Requested-With': 'XMLHttpRequest'},
      body: new URLSearchParams({csrf: dialog.dataset.csrf, finalized_at: activeResult}),
    }).catch(function () { /* 本机记录仍可阻止刷新、切页后的重复展示。 */ });
  }

  function openCelebration(data, manual) {
    if (!data || !data.finalized || !data.champions.length || dialog.open) return;
    const key = data.finalized_at;
    if (!manual) {
      if (shown === key || data.celebration_dismissed) return;
      try { if (localStorage.getItem(dismissalKey(key))) return; } catch (_) {}
    }
    dialog.querySelector('[data-champion-winner]').textContent = data.champions.map(t => t + ' 组').join('、');
    dialog.querySelector('#champion-dialog-title').textContent = data.champions.length > 1 ? '本次比赛并列冠军是' : '比赛冠军是';
    const rosters = dialog.querySelector('[data-champion-rosters]');
    rosters.replaceChildren();
    data.champions.forEach(function (team) {
      const group = document.createElement('section');
      if (data.champions.length > 1) {
        const heading = document.createElement('h3'); heading.textContent = team + ' 组'; group.appendChild(heading);
      }
      const list = document.createElement('ul');
      (data.champion_rosters[team] || []).forEach(function (name) {
        const member = document.createElement('li'); member.textContent = name; list.appendChild(member);
      });
      group.appendChild(list); rosters.appendChild(group);
    });
    const confetti = dialog.querySelector('.champion-confetti');
    confetti.replaceChildren();
    for (let i = 0; i < 24; i += 1) {
      const piece = document.createElement('i');
      piece.style.setProperty('--x', ((i * 43) % 100) + '%');
      piece.style.setProperty('--delay', (i % 6) * 0.12 + 's');
      piece.style.setProperty('--rotate', (i * 31) + 'deg');
      confetti.appendChild(piece);
    }
    returnFocus = document.activeElement;
    dialog.showModal();
    activeResult = key;
    shown = key;
  }
  dialog.querySelectorAll('[data-champion-close]').forEach(function (button) {
    button.addEventListener('click', function () { dialog.close(); });
  });
  dialog.addEventListener('close', function () {
    persistDismissal();
    if (returnFocus && returnFocus.isConnected) returnFocus.focus();
  });
  dialog.querySelectorAll('a[href]').forEach(function (link) {
    link.addEventListener('click', persistDismissal);
  });
  document.querySelectorAll('[data-champion-open]').forEach(function (button) {
    button.addEventListener('click', function () { openCelebration(latest, true); });
  });

  function paint(data) {
    latest = data;
    if (panel && data.finalized && panel.dataset.finalized !== '1') {
      // 先加载结算后的榜单，再展示弹窗，避免看到旧竞猜分数。
      location.reload(); return;
    }
    document.querySelectorAll('[data-tournament-progress]').forEach(function (label) {
      label.textContent = data.finalized ? '比赛已结束' : data.confirmed_count + ' / 4 组已确认';
    });
    document.querySelectorAll('[data-confirm-team]').forEach(function (row) {
      const confirmed = data.confirmed_teams.includes(row.dataset.confirmTeam);
      row.classList.toggle('is-confirmed', confirmed);
      row.querySelector('span').textContent = confirmed ? '已确认' : '等待队长确认';
    });
    if (form) {
      form.hidden = data.finalized;
      const confirmed = data.confirmed_teams.includes(form.dataset.team);
      const button = form.querySelector('button');
      button.disabled = submitting || confirmed;
      button.textContent = confirmed ? '本组已确认' : (submitting ? '正在确认…' : '比赛结束确认');
    }
    openCelebration(data, false);
  }

  async function check() {
    if (polling || document.visibilityState === 'hidden') return;
    polling = true;
    try {
      const response = await fetch('/api/tournament', {cache: 'no-store', credentials: 'same-origin',
        headers: {'ngrok-skip-browser-warning': 'true'}});
      if (response.ok && !response.redirected) paint(await response.json());
    } catch (_) { /* 临时断网时保留当前状态，下次重试。 */ }
    finally { polling = false; }
  }
  if (form) form.addEventListener('submit', async function (event) {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    form.querySelector('button').disabled = true;
    form.querySelector('button').textContent = '正在确认…';
    try {
      const response = await fetch(form.action, {method: 'POST', credentials: 'same-origin',
        headers: {'X-Requested-With': 'XMLHttpRequest'}, body: new FormData(form)});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || '确认失败，请稍后重试');
      feedback.textContent = data.finalized ? '四组已确认，比赛结束！' : '本组已确认，等待其他队长。';
    } catch (error) { feedback.textContent = error.message || '网络异常，请稍后重试'; }
    finally {
      submitting = false;
      form.querySelector('button').disabled = false;
      form.querySelector('button').textContent = '比赛结束确认';
      if (latest) paint(latest);
      await check();
    }
  });
  check();
  setInterval(check, 5000);
  window.addEventListener('focus', check);
  document.addEventListener('visibilitychange', check);
})();
