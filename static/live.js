/* 赛程页实时记分（追分版）：轮询 /api/live，按 rev 只 patch 有变化的卡片。
   - 不整页 reload、不重绘未变化的卡片（与 sync.js 的整页刷新通道互不影响）
   - 卡片显示本场次累计比分、本局目标分与承接比分；不再有任何竞猜信息
   - 进行中的局移动到页面顶部「正在进行」区，结束后移回原场次原位
   - 卡片始终按 data-team-a / data-team-b 的绝对组别渲染，不受裁判换边影响 */
(function () {
  const zone = document.getElementById('live-zone');
  const grid = document.getElementById('live-grid');
  const label = document.getElementById('live-count');
  // 无 live-zone（如全部未发布）时仍渲染自定义卡片的比分，只是不搬移位置。
  const canMove = !!(zone && grid);

  const versions = Object.create(null);       // ref -> 展示版本号
  const matchVersions = Object.create(null);  // matchId -> 场次状态版本号
  const homes = Object.create(null);          // ref -> 原位置 { parent, next }
  let pending = false;

  function q(card, role) {
    return card.querySelector('[data-role="' + role + '"]');
  }
  function esc(value) {
    return String(value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function render(card, d) {
    const state = q(card, 'state');
    if (state) {
      state.hidden = false;
      // 自定义对局结束后直接标注胜方，正式局沿用「已结束」。
      state.textContent = d.state === 'running' ? '正在进行中'
        : (d.custom && d.winner ? (d.winner + ' 组胜') : '已结束');
      state.className = 'game-state ' + (d.state === 'running' ? 'is-live' : 'is-done');
    }
    card.classList.toggle('is-live', d.state === 'running');
    card.classList.toggle('is-done', d.state === 'finished');

    // 进行中的比赛：显示执裁裁判与场地；自定义对局不设换边/场地序号。
    const meta = q(card, 'live-meta');
    if (meta) {
      if (d.state === 'running') {
        meta.hidden = false;
        if (d.custom) {
          meta.innerHTML = '<span>裁判：' + esc(d.referee || '—') + '</span>' +
            '<span>从 0:0 起计分</span>';
        } else {
          meta.innerHTML = '<span>裁判：' + esc(d.referee || '—') + '</span>' +
            '<span>场地：' + esc(d.court_input || '—') + '</span>';
        }
      } else {
        meta.hidden = true;
        meta.textContent = '';
      }
    }

    const vs = q(card, 'vs');
    if (vs) {
      vs.textContent = d.state === 'finished'
        ? (d.score_a + ' : ' + d.score_b) : 'VS';
      vs.classList.toggle('is-final', d.state === 'finished');
    }

    // 选手名字旁标注本局得分（本局累计分 − 承接分）；双打两人共享同一分数。
    // 只有已开赛（进行中 / 已结束）的局会进入 render，未开赛局占位保持 hidden。
    paintGains(card, d);

    const strip = q(card, 'score-strip');
    if (strip) strip.hidden = false;
    const total = q(card, 'score-total');
    if (total) total.hidden = d.state === 'finished';
    const sa = q(card, 'score-a');
    const sb = q(card, 'score-b');
    if (sa) sa.textContent = d.score_a;
    if (sb) sb.textContent = d.score_b;

    // 累计比分 + 本局目标分 + 从上一局承接的比分
    const tgt = q(card, 'score-target');
    if (tgt) {
      let txt = (d.custom ? '目标 ' : '本局目标 ') + d.target + ' 分';
      if (d.custom) txt += d.win_by_two ? ' · 赛点连得两分' : ' · 先到先胜';
      if (d.start_a || d.start_b) {
        txt += ' · 承接 ' + d.start_a + ':' + d.start_b;
      }
      if (d.state === 'finished') txt += ' · ' + (d.custom ? '已计入榜单' : '本局已结束');
      tgt.textContent = txt;
    }

    // 逐回合长条：两行 = 两组，每列只有得分方显示「得分后的累计分」
    const rally = q(card, 'rally');
    if (rally) {
      let ca = '';
      let cb = '';
      (d.rallies || []).forEach(function (r) {
        if (r.team === d.team_a) {
          ca += '<span class="rally-cell has-point">' + r.a + '</span>';
          cb += '<span class="rally-cell"></span>';
        } else {
          ca += '<span class="rally-cell"></span>';
          cb += '<span class="rally-cell has-point">' + r.b + '</span>';
        }
      });
      rally.innerHTML =
        '<div class="rally-line"><span class="rally-team">' + esc(d.team_a) +
        '</span><div class="rally-cells">' + ca + '</div></div>' +
        '<div class="rally-line"><span class="rally-team">' + esc(d.team_b) +
        '</span><div class="rally-cells">' + cb + '</div></div>';
    }
  }

  // 每位选手名旁的「本局得分」徽章。按绝对组别计算，与换边无关；双打两人共享。
  // 容错：旧后端可能还没有 gain_a/gain_b 字段，旧模板也可能没有占位节点，
  // 因此这里用 score - start 兜底计算，并在缺少占位时动态补建节点，
  // 保证只更新静态文件、不重启后端也能正确显示。
  function paintGains(card, d) {
    const groups = [
      {
        side: card.querySelector('.game-side:not(.game-side-right)'),
        value: d.gain_a != null ? d.gain_a : (d.score_a || 0) - (d.start_a || 0),
      },
      {
        side: card.querySelector('.game-side-right'),
        value: d.gain_b != null ? d.gain_b : (d.score_b || 0) - (d.start_b || 0),
      },
    ];
    groups.forEach(function (group) {
      if (!group.side) return;
      group.side.querySelectorAll('.side-player:not(.is-missing)').forEach(function (player) {
        let badge = player.querySelector('[data-role="player-gain"]');
        if (!badge) {
          badge = document.createElement('span');
          badge.className = 'player-gain';
          badge.setAttribute('data-role', 'player-gain');
          player.appendChild(badge);
        }
        badge.hidden = false;
        badge.textContent = '+' + Math.max(0, Number(group.value) || 0);
      });
    });
  }

  function moveToLive(card, ref) {
    if (!canMove || card.parentNode === grid) return;
    if (!homes[ref]) {
      homes[ref] = { parent: card.parentNode, next: card.nextSibling };
    }
    grid.appendChild(card);
    card.hidden = false;
  }

  function moveHome(card, ref) {
    if (!canMove) return;
    const h = homes[ref];
    if (!h || card.parentNode === h.parent) return;
    if (h.next && h.next.parentNode === h.parent) h.parent.insertBefore(card, h.next);
    else h.parent.appendChild(card);
  }

  // 场次卡片：未开赛隐藏；已开赛未打完显示绿色「正在进行中」+ 当前累计比分；
  // 6 局全部结束改为金色「已完赛」+ 最终累计比分 + 净胜分。
  // 进行中的绿色直接写成内联样式，避免浏览器/CDN 缓存旧 CSS 时退回金色。
  const RUNNING_BG = 'rgba(72, 199, 116, .12)';
  const RUNNING_BORDER = 'rgba(72, 199, 116, .42)';
  const RUNNING_FG = '#7ee2a8';

  function tint(box, tag, score, running) {
    box.style.background = running ? RUNNING_BG : '';
    box.style.borderColor = running ? RUNNING_BORDER : '';
    if (tag) tag.style.color = running ? RUNNING_FG : '';
    if (score) score.style.color = running ? RUNNING_FG : '';
  }

  function renderMatchResult(panel, info) {
    const box = panel.querySelector('[data-role="match-result"]');
    if (!box) return;
    const tag = box.querySelector('[data-role="match-tag"]');
    const lab = box.querySelector('[data-role="finish-label"]');
    const score = box.querySelector('[data-role="finish-score"]');
    const net = box.querySelector('[data-role="finish-net"]');
    const state = (info && info.state) || (info && info.finished ? 'finished' : 'pending');
    if (state === 'pending') {
      box.hidden = true;
      box.classList.remove('is-running');
      tint(box, tag, score, false);
      if (tag) tag.textContent = '已完赛';
      if (lab) lab.textContent = '累计比分';
      if (score) score.textContent = '';
      if (net) { net.hidden = true; net.textContent = ''; }
      box.removeAttribute('title');
      return;
    }
    const running = state === 'running';
    box.hidden = false;
    box.classList.toggle('is-running', running);
    tint(box, tag, score, running);
    if (tag) tag.textContent = running ? '正在进行中' : '已完赛';
    if (lab) lab.textContent = running ? '当前累计比分' : '最终累计比分';
    if (score) {
      score.textContent = running
        ? (info.text || '0:0')
        : (info.final_a + ':' + info.final_b);
    }
    if (net) {
      if (!running && info.net !== null && info.net !== undefined) {
        net.hidden = false;
        net.textContent = '净胜分 ' + (info.net > 0 ? '+' : '') + info.net;
      } else {
        net.hidden = true;
        net.textContent = '';
      }
    }
    if (info.title) box.title = info.title;
    else box.removeAttribute('title');
  }

  function check() {
    if (pending || document.visibilityState === 'hidden') return;
    pending = true;
    fetch('/api/live', {
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { 'ngrok-skip-browser-warning': 'true' }
    }).then(function (r) { return r.json(); }).then(function (data) {
      // 自定义对局与正式局共用同一渲染管线：键 'c<id>' 不会与正式局 ref 冲突。
      const items = Object.assign({}, (data && data.items) || {}, (data && data.custom) || {});
      let liveCount = 0;
      let customLiveCount = 0;
      Object.keys(items).forEach(function (ref) {
        const d = items[ref];
        const card = document.querySelector('.game-card[data-ref="' + ref + '"]');
        if (!card) return;
        if (d.state === 'running') {
          moveToLive(card, ref);
          liveCount += 1;
          if (d.custom) customLiveCount += 1;
        } else moveHome(card, ref);
        if (versions[ref] !== d.v) {   // 只更新有变化的卡片
          render(card, d);
          versions[ref] = d.v;
        }
      });
      const matchInfo = (data && data.matches) || {};
      Object.keys(matchInfo).forEach(function (mid) {
        const info = matchInfo[mid];
        const panel = document.querySelector('.match-panel[data-match-id="' + mid + '"]');
        if (!panel) return;
        const state = info.state || (info.finished ? 'finished' : 'pending');
        const v = String(info.v || 0) + '|' + state;
        if (matchVersions[mid] === v) return;   // 只更新有变化的场次
        renderMatchResult(panel, info);
        matchVersions[mid] = v;
      });
      if (zone) zone.hidden = liveCount === 0;
      if (label) {
        label.textContent = liveCount + ' 局' +
          (customLiveCount ? '（含 ' + customLiveCount + ' 场自定义）' : '');
      }
      pending = false;
    }).catch(function () { pending = false; });
  }

  check();
  setInterval(check, 2000);
  window.addEventListener('focus', check);
  document.addEventListener('visibilitychange', check);
})();
