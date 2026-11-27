// The add / edit repository form.
//  - the clone folder is looked at while you type (read-only, on the server) and fills in what can be guessed: the name,
//    the key, the Python, the kind of test suite, and the branches found in the clone;
//  - the test suite type is chosen from cards, and only its own settings are shown;
//  - branches are added from the clone's own list or typed, and removed with a confirmation when they were saved;
//  - a sentence under the form says what Save will create, and the step links at the top show what is filled in.
(function () {
  var form = document.getElementById('repo-form');
  if (!form) { return; }
  var creating = form.dataset.creating === '1';
  var touched = {};
  var auto = {};

  function byId(id) { return document.getElementById(id); }
  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text) { node.textContent = text; }
    return node;
  }

  // ---- the kind of test suite -------------------------------------------------------------------------------------
  var radios = form.querySelectorAll('input[name=adapter]');
  function chosenKind() {
    var radio = form.querySelector('input[name=adapter]:checked');
    return radio ? radio.value : '';
  }
  function applySuite() {
    var kind = chosenKind();
    document.querySelectorAll('.adapter-options').forEach(function (box) { box.hidden = box.dataset.adapter !== kind; });
    form.querySelectorAll('.suite-card').forEach(function (card) { card.classList.toggle('selected', card.dataset.kind === kind); });
  }
  radios.forEach(function (radio) {
    radio.addEventListener('change', function () { touched.adapter = true; applySuite(); refresh(); });
  });
  applySuite();

  // ---- the branch rows --------------------------------------------------------------------------------------------
  var table = byId('branch-rows');
  var template = byId('branch-template');
  var addButton = byId('add-branch');
  var total = byId('id_branch-TOTAL_FORMS');
  var body = table ? table.querySelector('tbody') : null;

  function liveRows() {
    return body ? Array.prototype.filter.call(body.querySelectorAll('tr.branch-row'), function (row) {
      return !row.hidden && !row.classList.contains('removed');
    }) : [];
  }
  function rowName(row) {
    var box = row.querySelector('input[type=text]');
    return box ? box.value.trim() : '';
  }
  function namedRows() { return liveRows().filter(function (row) { return rowName(row); }); }

  function addRow(name, nightly) {
    var index = parseInt(total.value, 10);
    var holder = document.createElement('tbody');
    holder.innerHTML = template.innerHTML.replace(/__prefix__/g, index);
    var row = holder.querySelector('tr');
    body.appendChild(row);
    total.value = index + 1;
    var box = row.querySelector('input[type=text]');
    if (name && box) { box.value = name; }
    var switchBox = row.querySelector('input[type=checkbox][name$="-scheduled"]');
    if (switchBox && nightly !== undefined) { switchBox.checked = nightly; }
    if (!name && box) { box.focus(); }
    return row;
  }
  function strike(row, removed) {
    row.classList.toggle('removed', removed);
    var flag = row.querySelector('input[type=checkbox][name$="-DELETE"]');
    if (flag) { flag.checked = removed; }
    var button = row.querySelector('.remove-branch');
    if (button) { button.textContent = removed ? 'Undo' : 'Remove'; }
  }
  function dropRow(row) {
    if (row.dataset.existing === '1') { strike(row, true); return; }
    row.querySelectorAll('input[type=text]').forEach(function (box) { box.value = ''; });
    row.hidden = true;
  }
  function rowFor(name) {
    return namedRows().filter(function (row) { return rowName(row) === name; })[0];
  }

  if (body && template && addButton && total) {
    addButton.addEventListener('click', function () { addRow(); refresh(); });
    if (!body.querySelector('tr.branch-row')) { addRow(); }
    body.addEventListener('click', function (event) {
      var button = event.target.closest('.remove-branch');
      if (!button) { return; }
      var row = button.closest('tr');
      if (row.classList.contains('removed')) { strike(row, false); refresh(); return; }
      if (row.dataset.existing !== '1') { dropRow(row); refresh(); return; }
      window.ciConfirm({
        title: 'Remove branch ' + rowName(row) + '?',
        text: 'It is removed when you press Save. Nothing is lost before that.',
        items: ['Its nightly and on-request schedulers are removed from Buildbot.',
                'Nobody can pick it for a new run any more.',
                'Its runs and results stay on disk and in the history.'],
        ok: 'Yes, remove it',
        tone: 'warn'
      }, function () { strike(row, true); refresh(); });
    });
  }

  // ---- the branches found in the clone -----------------------------------------------------------------------------
  var chipBox = byId('branch-chips');
  var chipHolder = byId('branch-suggest');
  var found = [];
  var drawn = null;
  function drawChips() {
    if (!chipBox) { return; }
    var signature = found.join('\n');
    if (signature !== drawn) {                                       // rebuild only when the list changed: a click must never hit a button that was just replaced
      drawn = signature;
      chipBox.textContent = '';
      found.forEach(function (name) {
        var chip = element('button', 'chip-button', name);
        chip.type = 'button';
        chip.addEventListener('click', function () {
          var row = rowFor(name);
          if (row) { dropRow(row); return refresh(); }
          var empty = liveRows().filter(function (r) { return !rowName(r); })[0];
          if (empty) { empty.querySelector('input[type=text]').value = name; } else { addRow(name, false); }
          refresh();
        });
        chipBox.appendChild(chip);
      });
    }
    chipBox.querySelectorAll('button').forEach(function (chip) {
      var on = !!rowFor(chip.textContent);
      chip.classList.toggle('on', on);
      chip.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    if (chipHolder) { chipHolder.hidden = !found.length; }
  }

  // ---- looking at the clone folder ---------------------------------------------------------------------------------
  var folder = byId('id_repo_dir');
  var python = byId('id_python');
  var keyBox = byId('id_key');
  var nameBox = byId('id_name');
  var checkoutBox = byId('id_checkout_name');
  var probeList = byId('probe');
  var pythonLine = byId('python-check');
  var suiteWhy = byId('suite-why');
  var url = form.dataset.probeUrl;
  var tokenBox = form.querySelector('input[name=csrfmiddlewaretoken]');
  var sequence = 0;
  if (creating && python) { auto.python = python.value; }          // the default python3 may be replaced by a better guess
  var timer = null;
  var state = { folder: null, git: null };

  function fill(box, field, value) {
    if (!box || box.disabled || !value) { return; }
    if (!box.value || box.value === auto[field]) { box.value = value; auto[field] = value; }
  }
  function line(level, text) {
    var item = element('li', level);
    item.appendChild(element('span', 'dot'));
    item.appendChild(document.createTextNode(text));
    return item;
  }

  function show(answer) {
    state.folder = answer.folder.ok;
    state.git = answer.git.ok;
    probeList.textContent = '';
    if (folder.value.trim()) {
      probeList.appendChild(line(answer.folder.ok ? 'ok' : 'bad', answer.folder.ok ? 'Folder found: ' + answer.folder.text : answer.folder.text));
      if (answer.git.text) { probeList.appendChild(line(answer.git.ok ? 'ok' : 'bad', answer.git.text)); }
      if (answer.git.ok !== null && answer.folder.ok && answer.git.ok !== false) {
        if (answer.branches.length) {
          var shown = answer.branches.slice(0, 8).join(', ') + (answer.branches.length > 8 ? ', …' : '');
          probeList.appendChild(line('ok', answer.branches.length + (answer.branches.length === 1 ? ' branch' : ' branches') + ' known to the clone: ' + shown + ' (as of its last fetch).'));
        } else {
          probeList.appendChild(line('warn', 'No branches known yet. Run git fetch in the clone, or type the branch names below.'));
        }
      }
    }
    probeList.hidden = !probeList.children.length;

    if (answer.python.text) {
      pythonLine.hidden = false;
      pythonLine.className = 'probe-line ' + (answer.python.ok ? 'ok' : 'bad');
      pythonLine.textContent = answer.python.text;
    } else {
      pythonLine.hidden = true;
    }

    found = answer.branches || [];
    drawChips();
    var suggest = answer.suggest || {};
    if (creating) {
      fill(keyBox, 'key', suggest.key);
      fill(nameBox, 'name', suggest.name);
      fill(checkoutBox, 'checkout', suggest.checkout_name);
      if (answer.default_branch && !namedRows().length) {
        var empty = liveRows()[0];
        if (empty) { empty.querySelector('input[type=text]').value = answer.default_branch; var nightly = empty.querySelector('input[name$="-scheduled"]'); if (nightly) { nightly.checked = true; } }
      }
    }
    if (python && suggest.python && (!python.value || python.value === auto.python)) { python.value = suggest.python; auto.python = suggest.python; ask(); }
    if (suggest.adapter) {
      if (creating && !touched.adapter) {
        var radio = form.querySelector('input[name=adapter][value="' + suggest.adapter + '"]');
        if (radio) { radio.checked = true; applySuite(); }
      }
      form.querySelectorAll('.suite-card').forEach(function (card) {
        card.querySelector('.suite-tag').hidden = card.dataset.kind !== suggest.adapter;
      });
      if (suiteWhy) { suiteWhy.hidden = !suggest.adapter_why; suiteWhy.textContent = suggest.adapter_why || ''; }
    }
    refresh();
  }

  function ask() {
    if (!url || !folder || !tokenBox) { return; }
    var mine = ++sequence;
    var data = new FormData();
    data.set('repo_dir', folder.value);
    data.set('python', python ? python.value : '');
    fetch(url, { method: 'POST', body: data, credentials: 'same-origin', headers: { 'X-CSRFToken': tokenBox.value } })
      .then(function (reply) { return reply.json(); })
      .then(function (answer) { if (mine === sequence) { show(answer); } })
      .catch(function () { probeList.hidden = true; });
  }
  function later() { clearTimeout(timer); timer = setTimeout(ask, 450); }
  if (folder) { folder.addEventListener('input', later); }
  if (python) { python.addEventListener('input', later); }

  // ---- what Save will do, and which steps are filled in ------------------------------------------------------------
  var summaryLine = byId('form-summary');
  function plural(count, one, many) { return count + ' ' + (count === 1 ? one : many); }

  function refresh() {
    var card = form.querySelector('.suite-card.selected .suite-name');
    var rows = namedRows();
    var nightly = rows.filter(function (row) {
      var box = row.querySelector('input[name$="-scheduled"]');
      return box && box.checked;
    }).length;
    var workers = parseInt((byId('id_worker_count') || {}).value, 10) || 0;
    var title = ((nameBox && nameBox.value) || (keyBox && keyBox.value) || '').trim();
    if (summaryLine) {
      summaryLine.textContent = (title ? title + ': ' : 'This repository: ') + (card ? card.textContent + ', ' : '') +
        plural(rows.length, 'branch', 'branches') + (rows.length ? ' (' + nightly + ' tested every night)' : '') + ', ' + plural(workers, 'worker', 'workers') + '.';
    }
    var done = {
      code: !!(folder && folder.value.trim() && python && python.value.trim() && keyBox && keyBox.value.trim() && nameBox && nameBox.value.trim()),
      tests: !!chosenKind(),
      branches: rows.length > 0,
      workers: workers > 0
    };
    var attention = { code: state.folder === false || state.git === false };
    document.querySelectorAll('.step-link').forEach(function (link) {
      var step = link.dataset.step;
      link.classList.toggle('done', !!done[step] && !attention[step]);
      link.classList.toggle('attention', !!attention[step]);
    });
    drawChips();
  }

  form.addEventListener('input', refresh);
  form.addEventListener('change', refresh);
  refresh();
  if (folder && folder.value.trim()) { ask(); }
})();
