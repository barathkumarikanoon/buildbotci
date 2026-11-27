(function () {
  var form = document.getElementById('runform');
  if (!form) { return; }
  var filter = document.getElementById('filter');
  var count = document.getElementById('count');
  var picker = document.getElementById('picker');
  var submit = document.getElementById('submit');
  var hint = document.getElementById('submit-hint');
  var summary = document.getElementById('summary');
  var big = document.getElementById('sum-big');
  var sub = document.getElementById('sum-sub');
  var boxes = Array.prototype.slice.call(form.querySelectorAll('input[name="tests"]'));
  var idleHint = hint ? hint.textContent : '';

  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }
  function mode() { return form.querySelector('input[name="mode"]:checked').value; }
  function setMode(value) { form.querySelector('input[name="mode"][value="' + value + '"]').checked = true; }

  function refresh() {
    var chosen = boxes.filter(function (b) { return b.checked; });
    var modules = 0;
    count.textContent = chosen.length + ' selected of ' + boxes.length;
    Array.prototype.forEach.call(form.querySelectorAll('details.module'), function (d) {
      var mine = Array.prototype.slice.call(d.querySelectorAll('input[name="tests"]'));
      var t = d.querySelector('.mod-toggle');
      var on = mine.filter(function (b) { return b.checked; }).length;
      var badge = d.querySelector('.mod-count');
      t.checked = on === mine.length && on > 0;
      t.indeterminate = on > 0 && on < mine.length;
      badge.textContent = on + ' / ' + mine.length;
      badge.classList.toggle('some', on > 0);
      if (on > 0) { modules += 1; }
    });
    var everything = mode() === 'all';
    picker.classList.toggle('dim', everything);
    var tests = everything ? parseInt(summary.dataset.all, 10) : chosen.length;
    var mods = everything ? parseInt(summary.dataset.modules, 10) : modules;
    big.textContent = plural(tests, 'test');
    sub.textContent = tests ? 'in ' + plural(mods, 'module') + (everything ? ', the whole branch.' : '.') : 'Nothing selected yet.';
    var blocked = !everything && chosen.length === 0;
    submit.disabled = blocked || submit.dataset.configured !== '1';
    if (hint) { hint.textContent = blocked ? 'Select at least one test, or switch to Everything.' : idleHint; }
  }

  function visible(box) { return box.closest('label.test').style.display !== 'none'; }

  filter.addEventListener('input', function () {
    var q = filter.value.trim().toLowerCase();
    boxes.forEach(function (b) {
      var l = b.closest('label.test');
      l.style.display = !q || l.dataset.name.toLowerCase().indexOf(q) >= 0 ? '' : 'none';
    });
    Array.prototype.forEach.call(form.querySelectorAll('details.module'), function (d) {
      var any = Array.prototype.some.call(d.querySelectorAll('label.test'), function (l) { return l.style.display !== 'none'; });
      d.style.display = any ? '' : 'none';
      if (q && any) { d.open = true; }
    });
  });

  form.addEventListener('change', function (e) {
    if (e.target.classList.contains('mod-toggle')) {
      var d = e.target.closest('details.module');
      Array.prototype.forEach.call(d.querySelectorAll('input[name="tests"]'), function (b) {
        if (visible(b)) { b.checked = e.target.checked; }
      });
      setMode('explicit');
    }
    if (e.target.name === 'tests') { setMode('explicit'); }
    refresh();
  });
  document.querySelectorAll('.mod-toggle').forEach(function (t) {
    t.addEventListener('click', function (e) { e.stopPropagation(); });
  });

  document.getElementById('sel-visible').addEventListener('click', function () {
    boxes.forEach(function (b) { if (visible(b)) { b.checked = true; } });
    setMode('explicit');
    refresh();
  });
  document.getElementById('sel-none').addEventListener('click', function () {
    boxes.forEach(function (b) { b.checked = false; });
    refresh();
  });
  var toggle = document.getElementById('toggle-modules');
  if (toggle) {
    toggle.addEventListener('click', function () {
      var open = toggle.dataset.open !== '1';
      Array.prototype.forEach.call(form.querySelectorAll('details.module'), function (d) { if (d.style.display !== 'none') { d.open = open; } });
      toggle.dataset.open = open ? '1' : '0';
      toggle.textContent = open ? 'Collapse all' : 'Expand all';
    });
  }
  refresh();
})();
