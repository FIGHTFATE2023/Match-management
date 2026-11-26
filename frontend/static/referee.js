/* 裁判台（追分版）：开赛、逐分记分、回退、换边对调、本局结束确认。
   全部走 fetch 静默提交；加分始终按按钮上的 data-team（绝对组别），
   换边对调只改变显示顺序，绝不会把分数记到错的组。
   结束确认后自动跳到下一局开赛页，比分由后端跨局继承。 */
(function () {
  const meta = document.querySelector('meta[name="csrf-token"]');
  const csrf = meta ? meta.content : '';
  if (!csrf) return;

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body: new URLSearchParams(Object.assign({ csrf: csrf }, body))
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: '网络异常，请重试' }; });
    });
  }

  // ---------- 顺序锁：上一局结束后才显示「场地序号 + 确认开始」 ----------
  // 兼容后端尚未重启的旧进程：模板可能拿不到 can_start，这里直接按列表里
  // 各局的真实状态推导，避免跳局开赛被误触发。新后端下此处结果幂等。
  function applyStartGates() {
    document.querySelectorAll('.ref-group').forEach(function (group) {
      const rows = Array.prototype.slice.call(group.querySelectorAll('.ref-row'));
      function rowNo(row) {
        const el = row.querySelector('.ref-no');
        const m = el && String(el.textContent).match(/\d+/);
        return m ? Number(m[0]) : 0;
      }
      let runningNo = 0;
      rows.forEach(function (row) {
        if (row.classList.contains('is-running')) runningNo = rowNo(row);
      });
      rows.forEach(function (row) {
        const form = row.querySelector('.js-start-form');
        const hint = row.querySelector('.js-lock-hint');
        if (!form) return;
        const no = rowNo(row);
        let prevFinished = true;
        if (no > 1) {
          const prev = rows.filter(function (r) { return rowNo(r) === no - 1; })[0];
          prevFinished = !!prev && prev.classList.contains('is-finished');
        }
        const canStart = !runningNo && prevFinished;
        form.hidden = !canStart;
        if (hint) {
          hint.hidden = canStart;
          if (!canStart) {
            hint.textContent = runningNo
              ? ('本场次第 ' + runningNo + ' 局进行中')
              : ('请先结束第 ' + (no - 1) + ' 局');
          }
        }
      });
    });
  }
  applyStartGates();

  // 面板中的开赛入口跟随列表对应局：进 ?ref= 未解锁局时也看不到开赛按钮
  (function syncPanelGate() {
    const panel = document.getElementById('scoring-panel');
    if (!panel) return;
    const panelForm = panel.querySelector('.js-start-form');
    if (!panelForm) return;
    const listForm = document.querySelector(
      '.ref-row .js-start-form[data-ref="' + panel.dataset.ref + '"]'
    );
    if (!listForm) return;
    panelForm.hidden = listForm.hidden;
    const panelHint = panel.querySelector('.js-lock-hint');
    const listHint = listForm.parentNode.querySelector('.js-lock-hint');
    if (panelHint && listHint) {
      panelHint.hidden = !listForm.hidden;
      panelHint.textContent = '这一局尚未解锁：' + listHint.textContent +
        '。按顺序完成上一局后，再回到这里开赛。';
    }
  })();

  // ---------- 选择一局并开赛（列表内 + 记分面板内两种表单） ----------
  document.querySelectorAll('.js-start-form').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      const input = form.querySelector('input[name="court"]');
      const btn = form.querySelector('button[type="submit"]');
      const court = input ? input.value.trim() : '';
      if (!court) { if (input) input.focus(); return; }
      if (btn) btn.disabled = true;
      post('/referee/start', { ref: form.dataset.ref, court: court }).then(function (res) {
        if (res && res.ok) {
          location.href = '/referee?ref=' + encodeURIComponent(form.dataset.ref);
        } else {
          if (btn) btn.disabled = false;
          window.alert((res && res.error) || '开赛失败，请重试');
        }
      });
    });
  });

  // ---------- 记分面板 ----------
  const panel = document.getElementById('scoring-panel');
  if (!panel) return;
  const ref = panel.dataset.ref;
  const isCustom = panel.dataset.isCustom === '1';
  const customId = panel.dataset.customId || '';
  const target = Number(panel.dataset.target || 15);
  const switchAt = Number(panel.dataset.switchAt || 8);
  const scoreA = document.getElementById('score-a');
  const scoreB = document.getElementById('score-b');
  const hint = document.getElementById('score-hint');
  const serveText = document.querySelector('[data-role="serve-hint"] [data-role="serve-text"]');
  const buttons = document.getElementById('score-buttons');
  const undoBtn = document.getElementById('undo-btn');
  // 自定义对局不设换边，但达到目标分同样需要二次确认；两种模式共用结束弹窗。
  const switchModal = isCustom ? null : document.getElementById('switch-modal');
  const finishModal = document.getElementById('finish-modal');
  // 已结束的自定义局没有大比分按钮，但「回退一分」必须仍可用；
  // 只有记分面板与回退按钮都不存在时才退出（正式局已结束为纯只读）。
  if (!scoreA || !scoreB || (!buttons && !undoBtn)) return;
  if (finishModal) {
    const backBtn = document.getElementById('finish-back');
    if (backBtn) backBtn.textContent = '返回并回退一分';
  }
  if (isCustom && finishModal) {
    const title = document.getElementById('fn-title');
    const okBtn = document.getElementById('finish-confirm');
    if (title) title.textContent = '确认对局结果';
    if (okBtn) okBtn.textContent = '确认结束并计入榜单';
  }

  // 统一记分面板按模式选择接口：正式局走 /referee/score|undo，自定义局走 /referee/custom/*。
  function scoreUrl() { return isCustom ? '/referee/custom/point' : '/referee/score'; }
  function undoUrl() { return isCustom ? '/referee/custom/undo' : '/referee/undo'; }
  function scoringBody(extra) {
    return isCustom ? Object.assign({ id: customId }, extra || {})
                    : Object.assign({ ref: ref }, extra || {});
  }

  function endText(a, b, t) {
    if (isCustom) {
      return '当前比分 ' + a + ' : ' + b + '，有一方达到本局目标 ' + t +
        ' 分。确认后本局结束并计入榜单；如需改分，返回将自动回退一分。';
    }
    return '当前累计比分 ' + a + ' : ' + b + '，有一方达到本局目标 ' + t +
      ' 分。确认后本局结束，下一局将继承这个比分继续追分；如需改分，返回将自动回退一分。';
  }
  function switchText(t) {
    return '有一方累计分达到本局换边点 ' + t +
      ' 分，请提示双方换边。确认后，裁判界面的左右两侧将完成对调。';
  }

  // 重新进入裁判台时，恢复尚未确认的换边 / 结束判定（换边优先）
  (function restorePending() {
    if (panel.dataset.needSwitch === '1' && switchModal) {
      const st = document.getElementById('switch-text');
      if (st) st.textContent = switchText(switchAt);
      switchModal.hidden = false;
    } else if (panel.dataset.reachedEnd === '1' && finishModal) {
      const text = document.getElementById('finish-text');
      if (text) text.textContent = endText(scoreA.textContent, scoreB.textContent, target);
      finishModal.hidden = false;
    }
  })();

  let busy = false;
  let lastAction = 0;
  function lock() {
    const now = Date.now();
    if (busy || now - lastAction < 300) return false;  // 300ms 防连点
    lastAction = now;
    busy = true;
    return true;
  }
  function modalOpen(m) { return !!(m && !m.hidden); }
  function blocked() { return modalOpen(switchModal) || modalOpen(finishModal); }

  function apply(res, okText) {
    busy = false;
    if (!res || !res.ok) {
      if (hint) hint.textContent = (res && res.error) || '操作失败，请重试';
      return;
    }
    if (scoreA) scoreA.textContent = res.score_a;
    if (scoreB) scoreB.textContent = res.score_b;
    // 发球 / 换边提示随最近两分实时刷新（自定义对局不设此提示）。
    if (!isCustom && serveText && res.serve && res.serve.text) serveText.textContent = res.serve.text;
    if (hint) hint.textContent = okText;
    if (isCustom) {
      // 达标后只进入待确认：确认才结束入榜，返回后可继续回退重记。
      if (res.reached_end) {
        const text = document.getElementById('finish-text');
        if (text) text.textContent = endText(res.score_a, res.score_b, res.target || target);
        if (finishModal) finishModal.hidden = false;
        if (hint) hint.textContent = '比分已达目标，请确认结束或返回修改';
      }
      return;
    }
    if (res.need_switch && switchModal) {
      const st = document.getElementById('switch-text');
      if (st) st.textContent = switchText(res.switch_at || switchAt);
      switchModal.hidden = false;
    } else if (res.reached_end && finishModal) {
      const text = document.getElementById('finish-text');
      if (text) text.textContent = endText(res.score_a, res.score_b, res.target || target);
      finishModal.hidden = false;
    }
  }

  document.querySelectorAll('.score-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (blocked() || !lock()) return;
      if (hint) hint.textContent = '正在记录…';
      post(scoreUrl(), scoringBody({ team: btn.dataset.team }))
        .then(function (res) { apply(res, '已加 1 分'); });
    });
  });

  if (undoBtn) undoBtn.addEventListener('click', function () {
    if (blocked() || !lock()) return;
    if (hint) hint.textContent = '正在回退…';
    post(undoUrl(), scoringBody())
      .then(function (res) {
        apply(res, '已回退一分');
        // 自定义对局回退后清除待确认状态，刷新同步榜单与按钮状态。
        if (isCustom && res && res.ok) {
          setTimeout(function () { location.reload(); }, 400);
        }
      });
  });

  // 达到本局换边点：确认换边 -> 标记一次 + 左右大按钮对调（自定义模式不参与）
  const swConfirm = isCustom ? null : document.getElementById('switch-confirm');
  if (swConfirm) swConfirm.addEventListener('click', function () {
    post('/referee/switch', { ref: ref }).then(function (res) {
      if (res && res.ok) {
        if (buttons) buttons.classList.add('is-swapped');
        switchModal.hidden = true;
        if (hint) hint.textContent = '已换边，界面左右完成对调';
      } else if (hint) {
        hint.textContent = (res && res.error) || '换边确认失败';
      }
    });
  });

  // 达到本局目标：二次确认。正式局跳下一局；自定义局确认后才结束并计入榜单。
  const fnConfirm = document.getElementById('finish-confirm');
  if (fnConfirm) fnConfirm.addEventListener('click', function () {
    if (!lock()) return;
    const url = isCustom ? '/referee/custom/finish' : '/referee/finish';
    const body = isCustom ? scoringBody() : { ref: ref };
    post(url, body).then(function (res) {
      if (res && res.ok) {
        if (isCustom) {
          if (finishModal) finishModal.hidden = true;
          if (hint) hint.textContent = '本局结束，已计入榜单';
          setTimeout(function () { location.reload(); }, 350);
        } else {
          location.href = '/referee?ref=' +
            encodeURIComponent(res.next_ref || ref);
        }
      } else {
        busy = false;
        if (hint) hint.textContent = (res && res.error) || '结束失败';
      }
    });
  });
  const fnBack = document.getElementById('finish-back');
  if (fnBack) fnBack.addEventListener('click', function () {
    if (finishModal) finishModal.hidden = true;
    // 正式局与自定义局一致：点「返回」即自动回退最后一分，退出
    // 「已达标未确认」状态，避免误加让累计分越过本局目标分。
    if (hint) hint.textContent = '正在回退最后一分…';
    post(undoUrl(), scoringBody()).then(function (res) {
      if (res && res.ok) {
        apply(res, '已返回并自动回退一分，可继续记分');
        setTimeout(function () { location.reload(); }, 300);
      } else {
        if (hint) hint.textContent = (res && res.error) || '回退失败，请手动回退一分';
      }
    });
  });
})();
