// 入力欄のクリアボタンと、カテゴリ選択時の段階表示の更新。
document.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-clear]');
  if (!button) return;
  const input = document.getElementById(button.dataset.clear);
  if (!input) return;
  input.value = '';
  input.focus();
});

document.addEventListener('change', (event) => {
  const select = event.target.closest('select[data-refresh]');
  if (!select || !select.form) return;
  // form.submit() は押されたボタンの値を送らないため、操作の種類を隠し項目で渡す。
  const action = document.createElement('input');
  action.type = 'hidden';
  action.name = 'action';
  action.value = 'refresh';
  select.form.appendChild(action);
  select.form.submit();
});
