/* 内部展示页：锁定页面缩放，避免误触让布局错位或需要左右滑动查看。
   网页无法关闭浏览器菜单/设置里的「页面缩放」，这里拦截页面内的手势与快捷键。 */
(function () {
  'use strict';

  function stop(event) {
    event.preventDefault();
  }

  // iOS Safari 10+ 会忽略 viewport 的 user-scalable=no，需拦截其专有手势事件
  ['gesturestart', 'gesturechange', 'gestureend'].forEach(function (type) {
    document.addEventListener(type, stop, { passive: false });
  });

  // 双指及以上 = 捏合缩放；单指保持正常纵向滚动
  document.addEventListener('touchmove', function (event) {
    if (event.touches && event.touches.length > 1) stop(event);
  }, { passive: false });

  // Ctrl / Cmd + 滚轮、触控板捏合（浏览器以 ctrl+wheel 形式派发）
  document.addEventListener('wheel', function (event) {
    if (event.ctrlKey || event.metaKey) stop(event);
  }, { passive: false });

  // Ctrl / Cmd + 加号、减号、0（放大 / 缩小 / 还原）
  document.addEventListener('keydown', function (event) {
    if (!(event.ctrlKey || event.metaKey)) return;
    var key = event.key;
    if (key === '+' || key === '=' || key === '-' || key === '_' ||
        key === '0' || key === 'Add' || key === 'Subtract') {
      stop(event);
    }
  }, { passive: false });

  // 部分移动浏览器横竖屏切换时会自动放大文字
  var root = document.documentElement;
  root.style.webkitTextSizeAdjust = '100%';
  root.style.textSizeAdjust = '100%';
})();
