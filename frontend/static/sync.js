/* 数据变动自动刷新。
   版本号由 SQLite 触发器维护：名单、安排或各账号验证码的任何写入（含外部直接改库）
   都会递增，前端轮询到变化即重载当前页，保证显示内容始终与数据库一致。
   正在填写表单时暂缓刷新，避免丢失未提交的内容。 */
(function () {
  const INTERVAL = 2000;
  let revision = null;
  let pending = false;

  function editing() {
    const inputs = document.querySelectorAll(
      'main input[type="text"], main input[type="password"]'
    );
    for (const input of inputs) {
      if (input === document.activeElement || input.value.trim() !== '') return true;
    }
    // 正在搜索时也暂缓刷新，避免重载打断输入（搜索词已写入 URL，刷新后可恢复）
    const search = document.querySelector('main input[type="search"]');
    if (search && search === document.activeElement) return true;
    // 调整人员安排展开后，同样暂缓刷新，避免下拉选择被重载打断
    if (document.querySelector('main .lineup-editor:not([hidden])')) return true;
    return !!document.querySelector('main details[open]');
  }

  function banner(show) {
    let el = document.getElementById('sync-banner');
    if (!show) {
      if (el) el.remove();
      return;
    }
    if (el) return;
    el = document.createElement('div');
    el.id = 'sync-banner';
    el.className = 'sync-banner';
    el.innerHTML = '数据已更新，完成当前编辑后自动刷新　' +
      '<button type="button" id="sync-now">立即刷新</button>';
    document.body.appendChild(el);
    document.getElementById('sync-now').addEventListener('click', function () {
      location.reload();
    });
  }

  async function check() {
    if (document.visibilityState === 'hidden') return;
    try {
      const res = await fetch('/api/revision', {
        cache: 'no-store',
        credentials: 'same-origin',
        // ngrok 免费版会拦截未带此头的请求并显示警告页；对其他环境无影响
        headers: { 'ngrok-skip-browser-warning': 'true' }
      });
      // 会话失效时 fetch 会跟随到登录页，需整页重载展示登录界面
      if (res.redirected) {
        location.reload();
        return;
      }
      // 429 / 5xx 等瞬时错误只跳过本轮，绝不能整页刷新：
      // 隧道并发超限（Quick Tunnel 200 in-flight）时，所有人同时 reload
      // 会把请求量再放大一轮，形成刷新风暴，甚至把整页刷成 429 错误页。
      if (!res.ok) return;
      const data = await res.json();
      if (typeof data.revision !== 'number') return;
      if (revision === null) {
        revision = data.revision;
        return;
      }
      if (data.revision === revision) {
        if (pending && !editing()) location.reload();
        return;
      }
      revision = data.revision;
      if (editing()) {
        pending = true;
        banner(true);
      } else {
        pending = false;
        banner(false);
        location.reload();
      }
    } catch (_) {
      // 网络抖动或服务重启：保持当前页面，下一轮自动重试
    }
  }

  check();
  setInterval(check, INTERVAL);
  window.addEventListener('focus', check);
  document.addEventListener('visibilitychange', check);
})();
