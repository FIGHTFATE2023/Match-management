/* 赛程页实时记分（追分版）：轮询 /api/live，按 rev 只 patch 有变化的卡片。
   - 不整页 reload、不重绘未变化的卡片（与 sync.js 的整页刷新通道互不影响）
   - 卡片显示累计比分、本局目标与承接比分，以及竞猜比例和当前用户的预计得分
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
    paintPrediction(card, d);

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

  function paintPrediction(card, d) {
    // 竞猜预测：红 = 卡片左侧组、蓝 = 卡片右侧组；押注者才显示预期得分
    const pred = q(card, 'predict');
    if (pred) {
      const p = d.predict;
      if (!p || d.state === 'finished') {
        pred.hidden = true;
      } else {
        pred.hidden = false;
        const sum = (p.a_stake || 0) + (p.b_stake || 0);
        const pct = sum ? (p.a_stake / sum * 100) : 50;
        const ba = q(card, 'bar-a');
        const bb = q(card, 'bar-b');
        if (ba) ba.style.width = pct + '%';
        if (bb) bb.style.width = (100 - pct) + '%';
        const bar = q(card, 'predict-bar');
        if (bar) bar.setAttribute('aria-label',
          d.team_a + ' 组竞猜 ' + p.a_stake + ' 分，占 ' + pct.toFixed(1) + '%；' +
          d.team_b + ' 组竞猜 ' + p.b_stake + ' 分，占 ' + (100 - pct).toFixed(1) + '%');
        const la = q(card, 'legend-a');
        const lb = q(card, 'legend-b');
        if (la) la.textContent = d.team_a + ' 组 · 竞猜 ' + p.a_stake + ' 分';
        if (lb) lb.textContent = d.team_b + ' 组 · 竞猜 ' + p.b_stake + ' 分';
        const ph = q(card, 'predict-hint');
        if (ph) {
          if (d.my_pick && d.expected !== null && d.expected !== undefined) {
            ph.hidden = false;
            ph.textContent = '若你押的 ' + d.my_pick + ' 组在本小局先新增十五分，竞猜成功将获得 ' +
              Number(d.expected).toFixed(2) + ' 分';
          } else {
            ph.hidden = true;
          }
        }
      }
    }

    // 竞猜结算：结束后在卡片最下方给押过这一局的人显示结果
    const gr = q(card, 'guess-result');
    if (gr) {
      const s = d.settled;
      if (d.state === 'finished' && s) {
        gr.hidden = false;
        gr.className = 'guess-result ' + (s.win ? 'is-win' : 'is-loss');
        gr.textContent = s.win
          ? '恭喜你竞猜成功，获得 ' + Number(s.gain).toFixed(2) + ' 点竞猜分数'
          : '很遗憾竞猜失败，未获得竞猜分数';
      } else {
        gr.hidden = true;
        gr.className = 'guess-result';
        gr.textContent = '';
      }
    }
  }

  // 每位选手名旁的「本局得分」徽章。按绝对组别计算，与换边无关；双打两人共享。
  // 旧后端缺少 gain 字段时用 score - start 兜底，旧模板缺少占位时动态补建。
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
  // 6 局全部结束改为金色「已完赛」+ 最终累计比分（标签不附净胜分）。
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
    const state = (info && info.state) || (info && info.finished ? 'finished' : 'pending');
    if (state === 'pending') {
      box.hidden = true;
      box.classList.remove('is-running');
      tint(box, tag, score, false);
      if (tag) tag.textContent = '已完赛';
      if (lab) lab.textContent = '累计比分';
      if (score) score.textContent = '';
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
        // 竞猜展示也参与版本比对，比分未变化时仍能更新比例、个人预计得分与结算。
        const displayVersion = JSON.stringify([d.v, d.predict, d.my_pick, d.expected, d.settled]);
        if (versions[ref] !== displayVersion) {
          render(card, d);
          versions[ref] = displayVersion;
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
