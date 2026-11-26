/* 后台通用确认：编辑表单提交前确认、删除/启停按钮点击前确认。 */
(function () {
  'use strict';

  document.addEventListener('submit', function (event) {
    var form = event.target;
    var message = form.getAttribute && form.getAttribute('data-confirm');
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });

  document.addEventListener('click', function (event) {
    var target = event.target;
    var button = target && target.closest ? target.closest('[data-confirm-click]') : null;
    if (button) {
      var message = button.getAttribute('data-confirm-click');
      if (message && !window.confirm(message)) {
        event.preventDefault();
      }
    }
  });

  // 操作成功后提示会随页面刷新显示，几秒后自动淡出，保持界面整洁。
  var notices = document.querySelectorAll('.flash');
  if (notices.length) {
    window.setTimeout(function () {
      notices.forEach(function (node) { node.style.opacity = '0'; });
      window.setTimeout(function () {
        notices.forEach(function (node) { if (node.parentNode) node.parentNode.removeChild(node); });
      }, 400);
    }, 4200);
  }
})();
