/* 分步登录：身份选择 -> 老师选项 / 选手姓名输入（纯前端切换，校验交给后端） */
(function () {
  const card = document.querySelector('.login-card');
  const steps = {
    role: document.getElementById('step-role'),
    teacher: document.getElementById('step-teacher'),
    student: document.getElementById('step-student')
  };

  function show(name) {
    const target = steps[name] ? name : 'role';
    Object.keys(steps).forEach(function (key) {
      if (steps[key]) steps[key].hidden = key !== target;
    });
    if (target === 'student') {
      const input = document.getElementById('student-name');
      if (input) input.focus();
    }
  }

  document.querySelectorAll('[data-next]').forEach(function (button) {
    button.addEventListener('click', function () { show(button.dataset.next); });
  });
  document.querySelectorAll('[data-back]').forEach(function (button) {
    button.addEventListener('click', function () { show('role'); });
  });

  show(card ? card.dataset.initial : 'role');
})();
