/* 选择经后端确认后高亮；同一卡片串行提交，避免慢请求覆盖新选择。 */
(function () {
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const feedback = document.getElementById('guess-feedback');
  async function submit(url, body, root) {
    if (root.dataset.busy === '1' || root.dataset.locked === '1') return;
    root.dataset.busy = '1';
    try {
      const response = await fetch(url, {
        method: 'POST', credentials: 'same-origin',
        headers: {'X-Requested-With': 'XMLHttpRequest'},
        body: new URLSearchParams(Object.assign({csrf: csrf}, body))
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || '提交失败，请稍后重试');
      feedback.textContent = '竞猜已保存';
      if (body.ref) {
        root.querySelectorAll('.game-side').forEach(function (side) {
          const on = side.dataset.team === data.pick;
          side.classList.toggle('is-picked', on);
          side.setAttribute('aria-pressed', String(on));
        });
      } else {
        root.querySelectorAll('.champion-card').forEach(function (card) {
          const on = card.dataset.pick === data.pick;
          card.classList.toggle('is-picked', on);
          card.setAttribute('aria-pressed', String(on));
        });
      }
    } catch (error) {
      feedback.textContent = error.message || '网络异常，请稍后重试';
    } finally {
      root.dataset.busy = '0';
      document.dispatchEvent(new Event('guess-updated'));
    }
  }
  const champion = document.querySelector('.champion-grid');
  function choose(target) {
    const champ = target.closest('.champion-card');
    if (champ) return submit('/guess/champion', {pick: champ.dataset.pick}, champion);
    const side = target.closest('.game-side[data-ref]');
    if (!side) return;
    const cancel = target.closest('.side-cancel');
    submit(cancel ? '/guess/game/cancel' : '/guess/game',
           cancel ? {ref: side.dataset.ref} : {ref: side.dataset.ref, pick: side.dataset.team},
           side.closest('.game-card'));
  }
  document.addEventListener('click', function (event) { choose(event.target); });
  document.addEventListener('keydown', function (event) {
    if ((event.key === 'Enter' || event.key === ' ') &&
        event.target.closest('.champion-card, .game-side[data-ref]')) {
      event.preventDefault(); choose(event.target);
    }
  });
  // ---------- 小局竞猜：对局搜索（关键词过滤，复用 data-search） ----------
  (function guessSearch() {
    const root = document.getElementById("match-list");
    if (!root) return;
    const cards = Array.from(root.querySelectorAll(".game-card"));
    const blocks = Array.from(root.querySelectorAll(".match-panel"));
    const input = document.getElementById("guess-q");
    const clearBtn = document.getElementById("guess-clear");
    const empty = document.getElementById("guess-empty");
    const count = document.getElementById("guess-count");
    if (!cards.length || !input) return;
    const total = cards.length;
    let term = "";
    function apply() {
      const needle = term.trim().toLowerCase();
      let shown = 0;
      cards.forEach(function (card) {
        const ok = !needle || (card.dataset.search || "").toLowerCase().indexOf(needle) !== -1;
        card.hidden = !ok;
        if (ok) shown += 1;
      });
      blocks.forEach(function (block) {
        const vis = block.querySelectorAll(".game-card:not([hidden])").length;
        block.hidden = vis === 0;
        const label = block.querySelector(".match-count");
        if (label) label.textContent = vis + " 局";
      });
      if (count) count.textContent = shown === total ? ("全部 " + total + " 局") : (shown + " / " + total + " 局");
      if (empty) empty.hidden = shown !== 0;
    }
    let timer = null;
    input.addEventListener("input", function () {
      term = input.value;
      clearTimeout(timer);
      timer = setTimeout(apply, 120);
    });
    input.addEventListener("search", function () { term = input.value; apply(); });
    if (clearBtn) clearBtn.addEventListener("click", function () { term = ""; input.value = ""; apply(); });
    apply();
  })();
})();
