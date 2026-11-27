(function () {
  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function tailer(el, url, isLive, onData) {
    var offset = 0;
    var stick = true;
    el.addEventListener('scroll', function () {
      stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
    });
    function step() {
      fetch(url + (url.indexOf('?') < 0 ? '?' : '&') + 'offset=' + offset)
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.text) {
            if (offset === 0) { el.textContent = ''; }
            el.textContent += d.text;
            if (stick) { el.scrollTop = el.scrollHeight; }
          } else if (offset === 0 && !el.textContent) {
            el.textContent = '(no output yet)';
          }
          offset = d.offset;
          if (onData) { onData(d); }
          if (d.live && isLive()) { setTimeout(step, 1500); }
        })
        .catch(function () { setTimeout(step, 4000); });
    }
    step();
  }

  var consoleEl = qs('#console');
  if (consoleEl) {
    tailer(consoleEl, consoleEl.dataset.tail, function () { return true; });
  }

  var live = qs('#live');
  if (live && live.dataset.live === '1') {
    (function poll() {
      fetch(live.dataset.url).then(function (r) { return r.text(); }).then(function (html) {
        var keep = qsa('details[open]', live).map(function (d) { return d.className; });
        live.innerHTML = html;
        qsa('details', live).forEach(function (d) { if (keep.indexOf(d.className) >= 0 && d.className) { d.open = true; } });
        return fetch(live.dataset.api);
      }).then(function (r) { return r.json(); }).then(function (run) {
        var status = run.result || run.state;
        var names = { no_expected: 'no baseline' };
        var badge = qs('#run-badge');
        if (badge) { badge.textContent = names[status] || status; badge.className = 'badge lg ' + status; }
        var hero = qs('#hero');
        if (hero) { hero.className = 'hero ' + status; }
        if (run.state === 'queued' || run.state === 'running') { setTimeout(poll, 2500); }
        else { location.reload(); }
      }).catch(function () { setTimeout(poll, 5000); });
    })();
  }

  qsa('table.runs tbody tr[data-href]').forEach(function (row) {
    row.addEventListener('click', function (e) {
      if (e.target.closest('a, button, input, select')) { return; }
      location.href = row.dataset.href;
    });
  });

  var tabs = qsa('.tab');
  if (tabs.length) {
    var loaded = {};
    function show(name) {
      if (!qs('#panel-' + name)) { name = 'analysis'; }
      tabs.forEach(function (t) { t.classList.toggle('active', t.dataset.tab === name); });
      qsa('.panel').forEach(function (p) { p.classList.toggle('active', p.id === 'panel-' + name); });
      var tab = qs('.tab[data-tab="' + name + '"]');
      if (tab && tab.dataset.src && !loaded[name]) {
        loaded[name] = true;
        var view = qs('#view-' + name);
        if (tab.dataset.liveTail) {
          tailer(view, tab.dataset.liveTail, function () { return tab.dataset.live === '1'; });
        } else {
          fetch(tab.dataset.src).then(function (r) {
            var truncated = r.headers.get('X-Truncated') === '1';
            var total = r.headers.get('X-Total-Bytes');
            return r.text().then(function (text) {
              view.textContent = text || '(empty)';
              if (truncated) { qs('#note-' + name).textContent = 'showing the first 256 KB of ' + total + ' bytes; download for the rest'; }
            });
          });
        }
      }
    }
    tabs.forEach(function (t) {
      t.addEventListener('click', function (e) { e.preventDefault(); history.replaceState(null, '', '#' + t.dataset.tab); show(t.dataset.tab); });
    });
    var start = (location.hash || '').replace('#', '');
    if (/^h\d+$/.test(start)) { show('diff'); var h = document.getElementById(start); if (h) { h.scrollIntoView(); } }
    else { show(start || 'analysis'); }
  }
})();


// ---- the sidebar on a small screen ---------------------------------------------------------------------------------
(function () {
  var button = document.getElementById('menu-btn');
  var scrim = document.getElementById('scrim');
  if (!button) { return; }
  function set(open) { document.body.classList.toggle('nav-open', open); }
  button.addEventListener('click', function () { set(!document.body.classList.contains('nav-open')); });
  if (scrim) { scrim.addEventListener('click', function () { set(false); }); }
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { set(false); } });
})();

// ---- warnings: every risky action asks first, in a dialog ----------------------------------------------------------
// A form (or its submit button) with data-confirm opens the dialog before it is sent:
//   data-confirm="what happens"  data-confirm-title="Title"  data-confirm-items="one|two|three"
//   data-confirm-ok="Button text"  data-confirm-tone="danger|warn"
// window.ciConfirm({title, text, items, ok, tone}, callback) opens the same dialog from script.
(function () {
  var dialog = document.getElementById('confirm-dialog');
  if (!dialog || typeof dialog.showModal !== 'function') { return; }
  var title = document.getElementById('confirm-title');
  var text = document.getElementById('confirm-text');
  var items = document.getElementById('confirm-items');
  var yes = document.getElementById('confirm-yes');
  var no = document.getElementById('confirm-no');
  var pending = null;

  function ask(options, done) {
    title.textContent = (options.tone === 'warn' ? '\u26A0 ' : '\u26A0 ') + (options.title || 'Are you sure?');
    text.textContent = options.text || '';
    text.hidden = !options.text;
    items.textContent = '';
    (options.items || []).forEach(function (line) {
      var li = document.createElement('li');
      li.textContent = line;
      items.appendChild(li);
    });
    items.hidden = !(options.items || []).length;
    yes.textContent = options.ok || 'Confirm';
    yes.className = 'btn ' + (options.tone === 'warn' ? 'warn' : 'danger');
    dialog.className = 'warn-dialog ' + (options.tone === 'warn' ? 'tone-warn' : 'tone-danger');
    pending = done;
    dialog.showModal();
    no.focus();
  }
  window.ciConfirm = ask;

  yes.addEventListener('click', function () { var done = pending; pending = null; dialog.close(); if (done) { done(); } });
  no.addEventListener('click', function () { pending = null; dialog.close(); });
  dialog.addEventListener('cancel', function () { pending = null; });

  var passed = new WeakSet();
  document.addEventListener('submit', function (event) {
    var form = event.target;
    var source = (event.submitter && event.submitter.dataset.confirm) ? event.submitter : form;
    if (!source.dataset || !source.dataset.confirm || passed.has(form)) { return; }
    event.preventDefault();
    ask({
      title: source.dataset.confirmTitle,
      text: source.dataset.confirm,
      items: source.dataset.confirmItems ? source.dataset.confirmItems.split('|') : [],
      ok: source.dataset.confirmOk,
      tone: source.dataset.confirmTone
    }, function () {
      passed.add(form);
      if (event.submitter && event.submitter.name) {
        var hidden = document.createElement('input');
        hidden.type = 'hidden'; hidden.name = event.submitter.name; hidden.value = event.submitter.value;
        form.appendChild(hidden);
      }
      form.submit();
    });
  }, true);

  // a dialog that opens by a button: <button data-open-dialog="id">
  document.addEventListener('click', function (event) {
    var opener = event.target.closest('[data-open-dialog]');
    if (opener) {
      var target = document.getElementById(opener.dataset.openDialog);
      if (target && target.showModal) { event.preventDefault(); target.showModal(); }
    }
    var closer = event.target.closest('[data-close-dialog]');
    if (closer) { var box = closer.closest('dialog'); if (box) { box.close(); } }
  });
})();


// ---- the colour theme picker ---------------------------------------------------------------------------------------
(function () {
  var buttons = Array.prototype.slice.call(document.querySelectorAll('[data-theme-choice]'));
  if (!buttons.length) { return; }
  function show() {
    var now = document.documentElement.getAttribute('data-theme');
    buttons.forEach(function (b) { b.setAttribute('aria-checked', b.dataset.themeChoice === now ? 'true' : 'false'); });
  }
  buttons.forEach(function (b) {
    b.addEventListener('click', function () {
      document.documentElement.setAttribute('data-theme', b.dataset.themeChoice);
      try { localStorage.setItem('ci-theme', b.dataset.themeChoice); } catch (e) {}
      show();
    });
  });
  show();
})();
