/* 导航「查看赛程」的绿色提示点：有进行中比赛时显示，纯图形无文字。
   轻量轮询 /api/live/status，只切换圆点显隐，不刷新页面、不干扰记分。 */
(function () {
  const dots = Array.from(document.querySelectorAll('[data-role="live-dot"]'));
  if (!dots.length) return;
  const INTERVAL = 3000;
  let pending = false;

  function apply(live) {
    dots.forEach(function (dot) { dot.hidden = !live; });
  }

  function check() {
    if (pending || document.visibilityState === 'hidden') return;
    pending = true;
    fetch('/api/live/status', {
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { 'ngrok-skip-browser-warning': 'true' }
    }).then(function (r) { return r.json(); }).then(function (data) {
      apply(!!(data && data.live));
      pending = false;
    }).catch(function () { pending = false; });
  }

  check();
  setInterval(check, INTERVAL);
  window.addEventListener('focus', check);
  document.addEventListener('visibilitychange', check);
})();
