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
