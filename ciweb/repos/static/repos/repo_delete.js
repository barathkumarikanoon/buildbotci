// Asks for a last confirmation, in a dialog that lists what is going to be deleted for the options that are ticked.
// Without JavaScript the form is simply submitted; the server insists on the typed key either way.
(function () {
  var form = document.getElementById('delete-form');
  var dialog = document.getElementById('delete-dialog');
  if (!form || !dialog || typeof dialog.showModal !== 'function') { return; }
  var confirmed = false;

  function ticked(name) {
    var box = form.elements[name];
    return !!(box && box.checked);
  }

  function refresh() {
    dialog.querySelectorAll('[data-when]').forEach(function (item) {
      item.hidden = !ticked(item.getAttribute('data-when'));
    });
    dialog.querySelectorAll('[data-when-not]').forEach(function (item) {
      item.hidden = ticked(item.getAttribute('data-when-not'));
    });
  }

  form.addEventListener('submit', function (event) {
    if (confirmed) { return; }
    event.preventDefault();
    if (form.elements.confirm.value.trim() !== form.getAttribute('data-key')) {
      form.elements.confirm.setCustomValidity('Type ' + form.getAttribute('data-key') + ' to confirm.');
      form.elements.confirm.reportValidity();
      return;
    }
    form.elements.confirm.setCustomValidity('');
    refresh();
    dialog.showModal();
  });
  form.elements.confirm.addEventListener('input', function () { form.elements.confirm.setCustomValidity(''); });

  document.getElementById('delete-cancel').addEventListener('click', function () { dialog.close(); });
  document.getElementById('delete-go').addEventListener('click', function () {
    confirmed = true;
    dialog.close();
    form.submit();
  });
})();
