/* 赛程页筛选：组别 / 类别勾选 + 姓名搜索，全部在浏览器端即时完成。
   条件写入 URL（teams=A,B & events=混双,男双 & q=姓名），
   sync.js 因数据变化整页刷新后，仍能恢复用户当前的选择。 */
(function () {
  const cards = Array.from(document.querySelectorAll('.game-card'));
  const blocks = Array.from(document.querySelectorAll('.match-panel'));
  if (!cards.length || !blocks.length) return;

  const teamBoxes = Array.from(document.querySelectorAll('[data-filter="team"] input'));
  const eventBoxes = Array.from(document.querySelectorAll('[data-filter="event"] input'));
  const search = document.getElementById('filter-q');
  const count = document.getElementById('filter-count');
  const empty = document.getElementById('filter-empty');
  const clear = document.getElementById('filter-clear');
  const toggle = document.getElementById('filter-toggle');
  const extra = document.getElementById('filter-extra');
  const total = cards.length;
  const teams = new Set();
  const events = new Set();
  let term = '';

  function writeUrl() {
    const params = new URLSearchParams();
    if (teams.size) params.set('teams', Array.from(teams).join(','));
    if (events.size) params.set('events', Array.from(events).join(','));
    if (term.trim()) params.set('q', term.trim());
    const query = params.toString();
    try {
      history.replaceState(null, '', location.pathname + (query ? '?' + query : ''));
    } catch (_) {
      // 个别预览环境（如 file://）不允许改地址栏，忽略即可，不影响筛选
    }
  }

  function apply() {
    const needle = term.trim().toLowerCase();
    let shown = 0;
    cards.forEach(function (card) {
      // 进行中的局已被 live.js 提到顶部「正在进行」区，始终可见且不参与筛选
      if (card.closest('#live-zone')) {
        card.hidden = false;
        shown += 1;
        return;
      }
      const cardTeams = (card.dataset.teams || '').split(' ');
      const okTeam = !teams.size || cardTeams.some(function (team) { return teams.has(team); });
      const okEvent = !events.size || events.has(card.dataset.event || '');
      const okText = !needle || (card.dataset.search || '').toLowerCase().indexOf(needle) !== -1;
      const visible = okTeam && okEvent && okText;
      card.hidden = !visible;
      if (visible) shown += 1;
    });
    blocks.forEach(function (block) {
      const visible = block.querySelectorAll('.game-card:not([hidden])').length;
      block.hidden = visible === 0;
      const label = block.querySelector('.match-count');
      if (label) label.textContent = visible + ' 局';
    });
    count.textContent = shown === total ? total + ' 局' : shown + ' / ' + total + ' 局';
    empty.hidden = shown !== 0;
    writeUrl();
  }

  function bindBoxes(boxes, bucket) {
    boxes.forEach(function (box) {
      box.addEventListener('change', function () {
        if (box.checked) bucket.add(box.value);
        else bucket.delete(box.value);
        apply();
      });
    });
  }

  function restore() {
    const params = new URLSearchParams(location.search);
    // 兼容旧链接 /schedule?team=A
    (params.get('teams') || params.get('team') || '').split(',').forEach(function (value) {
      const box = teamBoxes.find(function (item) { return item.value === value; });
      if (box) {
        box.checked = true;
        teams.add(value);
      }
    });
    (params.get('events') || params.get('event') || '').split(',').forEach(function (value) {
      const box = eventBoxes.find(function (item) { return item.value === value; });
      if (box) {
        box.checked = true;
        events.add(value);
      }
    });
    const keyword = params.get('q') || '';
    if (keyword) {
      search.value = keyword;
      term = keyword;
    }
  }

  bindBoxes(teamBoxes, teams);
  bindBoxes(eventBoxes, events);

  // 筛选栏展开 / 缩小：缩小后只保留「搜索」一行，组别 / 类别随 #filter-extra 隐藏
  if (toggle && extra) {
    toggle.addEventListener('click', function () {
      const willCollapse = !extra.hidden;
      extra.hidden = willCollapse;
      toggle.setAttribute('aria-expanded', String(!willCollapse));
      toggle.textContent = willCollapse ? '展开' : '缩小';
    });
  }

  let timer = null;
  search.addEventListener('input', function () {
    term = search.value;
    clearTimeout(timer);
    timer = setTimeout(apply, 120);
  });
  search.addEventListener('search', function () {
    term = search.value;
    apply();
  });

  clear.addEventListener('click', function () {
    teamBoxes.concat(eventBoxes).forEach(function (box) { box.checked = false; });
    teams.clear();
    events.clear();
    term = '';
    search.value = '';
    apply();
  });

  restore();
  apply();
})();
