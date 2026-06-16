
(function(){
"use strict";

var STEPS = ['welcome','add-server','test','servers','finish'];
var state = {
  step: 0,
  hosts: [],
  currentHost: { name:'', host:'', user:'', port:22 },
  sshKey: null,
  theme: 'Panel/Gold',
  themes: null,
  testResult: null,
  testPassword: '',
};

function $(sel, ctx) { return (ctx||document).querySelector(sel); }
function $$(sel, ctx) { return Array.from((ctx||document).querySelectorAll(sel)); }

/* ── Progress dots with connecting lines ── */
function renderProgress() {
  var el = document.getElementById('progress');
  var html = '';
  for (var i = 0; i < STEPS.length; i++) {
    var cls = 'dot';
    if (i < state.step) cls += ' done';
    if (i === state.step) cls += ' active';
    html += '<div class="' + cls + '"></div>';
    if (i < STEPS.length - 1) {
      html += '<div class="dot-line' + (i < state.step ? ' done' : '') + '"></div>';
    }
  }
  el.innerHTML = html;
}

/* ── Step rendering ── */
function goTo(n) {
  state.step = Math.max(0, Math.min(STEPS.length-1, n));
  renderProgress();
  renderStep();
}

function renderStep() {
  var container = document.getElementById('steps');
  var stepName = STEPS[state.step];
  var renderers = {
    'welcome': renderWelcome,
    'add-server': renderAddServer,
    'test': renderTest,
    'servers': renderServers,
    'finish': renderFinish,
  };
  container.innerHTML = '<div class="step active">' + renderers[stepName]() + '</div>';
  bindStep(stepName);
}

/* ── Step 0: Welcome ── */
function renderWelcome() {
  return '' +
    '<div style="padding:2.5rem 0;text-align:center;">' +
      '<h1>CHIKETI<br>APPLIANCE</h1>' +
      '<p class="subtitle" style="margin-top:1rem;font-size:0.95rem;color:var(--text-dim);">Remote System Monitor</p>' +
      '<p class="subtitle" style="margin-top:1.5rem;">' +
        'Monitor your Linux servers from a single dashboard.<br>' +
        'No software needed on your servers &mdash; just SSH access.' +
      '</p>' +
      '<p class="desc">' +
        'This wizard will connect to your servers, set up SSH keys,<br>' +
        'and configure the dashboard theme.' +
      '</p>' +
      '<button class="btn btn-big" id="btn-start">Get Started</button>' +
    '</div>';
}

/* ── Step 1: Add Server ── */
function renderAddServer() {
  var h = state.currentHost;
  var filled = h.name && h.host && h.user;
  return '' +
    '<h2>Add Server</h2>' +
    '<div class="card">' +
      '<label for="srv-name">Friendly Name</label>' +
      '<input type="text" id="srv-name" placeholder="my-server" value="' + esc(h.name) + '">' +
      '<label for="srv-host">Host / IP Address</label>' +
      '<input type="text" id="srv-host" placeholder="192.168.1.50" value="' + esc(h.host) + '">' +
      '<div class="field-row" style="margin-top:0.85rem;">' +
        '<div class="field-col">' +
          '<label for="srv-user" style="margin-top:0;">Username</label>' +
          '<input type="text" id="srv-user" placeholder="rohan" value="' + esc(h.user) + '">' +
        '</div>' +
        '<div class="field-col-sm">' +
          '<label for="srv-port" style="margin-top:0;">Port</label>' +
          '<input type="number" id="srv-port" value="' + h.port + '" min="1" max="65535">' +
        '</div>' +
      '</div>' +
      '<label for="srv-password" style="margin-top:0.85rem;">SSH Password</label>' +
      '<input type="password" id="srv-password" placeholder="Server password (used once to set up key)" value="' + esc(state.testPassword) + '">' +
      '<p class="hint" style="margin-top:0.25rem;">The password is used once to copy the SSH key. It is never stored.</p>' +
    '</div>' +
    '<button class="btn" id="btn-to-test" ' + (filled ? '' : 'disabled') + '>Connect</button>' +
    (state.hosts.length > 0 ? '<button class="btn btn-secondary" id="btn-add-cancel" style="margin-top:0.5rem;">Cancel</button>' : '');
}

/* ── Step 2: SSH Key ── */
function renderSSHKey() {
  if (!state.sshKey) {
    return '' +
      '<h2>SSH Key</h2>' +
      '<div class="card">' +
        '<div class="status-msg"><div class="spinner"></div><br>Loading SSH key...</div>' +
      '</div>';
  }
  var k = state.sshKey;
  var genNote = k.generated
    ? '<p class="note">A new SSH key was generated for this appliance at <code>' + esc(k.key_path) + '</code></p>'
    : '';
  var cmdText = "echo '" + k.public_key + "' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys";
  return '' +
    '<h2>SSH Key</h2>' +
    '<div class="card">' +
      '<p style="font-size:0.85rem;color:var(--text-dim);margin-bottom:0.75rem;">' +
        'Add this key to your server\'s <code>~/.ssh/authorized_keys</code>:' +
      '</p>' +
      '<div class="copy-wrap">' +
        '<textarea id="pubkey-area" rows="3" readonly>' + esc(k.public_key) + '</textarea>' +
        '<button class="copy-btn" id="btn-copy-key">COPY</button>' +
      '</div>' +
      genNote +
      '<p style="font-size:0.82rem;color:var(--text-dim);margin-top:1rem;">Run this on your server:</p>' +
      '<div class="copy-wrap" style="margin-top:0.3rem;">' +
        '<textarea id="cmd-area" rows="2" readonly>' + esc(cmdText) + '</textarea>' +
        '<button class="copy-btn" id="btn-copy-cmd">COPY</button>' +
      '</div>' +
    '</div>' +
    '<button class="btn" id="btn-key-done">I\'ve Added the Key</button>' +
    '<div class="card" style="margin-top:0.75rem;">' +
      '<button class="expand-toggle" id="btn-expand-pw">' +
        '<span class="arrow">&#9654;</span>' +
        'Or enter password for one-time key setup' +
      '</button>' +
      '<div class="expand-content" id="pw-section">' +
        '<p class="hint" style="margin-top:0.25rem;margin-bottom:0.5rem;">' +
          'Enter the server password to automatically copy the key via SSH. The password is not stored.' +
        '</p>' +
        '<label for="ssh-password" style="margin-top:0;">Server Password</label>' +
        '<input type="password" id="ssh-password" placeholder="Enter server password" value="' + esc(state.testPassword) + '">' +
        '<button class="btn btn-sm" id="btn-auto-copy" style="margin-top:0.6rem;width:100%;">Copy Key Automatically</button>' +
      '</div>' +
    '</div>';
}

/* ── Step 2: Test Connection ── */
function renderTest() {
  var statusHTML = '';
  var phase = state.testPhase || '';
  if (state.testResult === null) {
    statusHTML = '<div class="status-msg"><div class="spinner"></div><br>' + (phase || 'Setting up connection...') + '</div>';
  } else if (state.testResult === 'loading') {
    statusHTML = '<div class="status-msg"><div class="spinner"></div><br>' + (phase || 'Connecting...') + '</div>';
  } else if (state.testResult.success) {
    statusHTML = '' +
      '<div class="status-msg success">' +
        '<span class="check-anim">&#10003;</span><br>' +
        'Connected successfully!<br>' +
        '<span style="font-size:0.85rem;color:var(--text-dim);display:inline-block;margin-top:0.5rem;">' +
          'Hostname: <strong style="color:var(--green);">' + esc(state.testResult.hostname) + '</strong><br>' +
          'Uptime: ' + esc(state.testResult.uptime || 'N/A') +
        '</span>' +
      '</div>';
  } else {
    statusHTML = '' +
      '<div class="status-msg error">' +
        '<span class="x-anim">&#10007;</span><br>' +
        'Connection failed<br>' +
        '<span style="font-size:0.82rem;display:inline-block;margin-top:0.35rem;">' + esc(state.testResult.error) + '</span>' +
      '</div>';
  }
  var isLoading = state.testResult === null || state.testResult === 'loading';
  var canProceed = state.testResult && state.testResult !== 'loading' && state.testResult.success;
  return '' +
    '<h2>Connecting</h2>' +
    '<div class="card">' +
      '<p style="font-size:0.85rem;color:var(--text-dim);margin-bottom:0.75rem;">' +
        'Server: <strong style="color:var(--text);">' + esc(state.currentHost.name) + '</strong> &mdash; ' +
        '<span style="color:var(--text-muted);">' + esc(state.currentHost.user) + '@' + esc(state.currentHost.host) + ':' + state.currentHost.port + '</span>' +
      '</p>' +
      statusHTML +
      ((!isLoading && !canProceed) ? '<button class="btn" id="btn-test">Retry</button>' : '') +
    '</div>' +
    '<div class="btn-row">' +
      '<button class="btn btn-secondary" id="btn-test-back">Back</button>' +
      '<button class="btn" id="btn-test-next" ' + (canProceed ? '' : 'disabled') + '>Add Server</button>' +
    '</div>';
}

/* ── Step 4: Server List ── */
function renderServers() {
  var listHTML = '';
  if (state.hosts.length === 0) {
    listHTML = '<p class="status-msg" style="color:var(--text-dim);">No servers added yet.</p>';
  } else {
    var items = '';
    for (var i = 0; i < state.hosts.length; i++) {
      var h = state.hosts[i];
      items += '' +
        '<li class="host-item">' +
          '<div class="host-dot"></div>' +
          '<div class="host-info">' +
            '<div class="host-name">' + esc(h.name) + '</div>' +
            '<div class="host-addr">' + esc(h.user) + '@' + esc(h.host) + ':' + h.port + '</div>' +
          '</div>' +
          '<button class="btn btn-sm btn-danger" data-remove="' + esc(h.name) + '">&times;</button>' +
        '</li>';
    }
    listHTML = '<ul class="host-list">' + items + '</ul>';
  }
  return '' +
    '<h2>Servers (' + state.hosts.length + ')</h2>' +
    '<div class="card">' + listHTML + '</div>' +
    '<button class="btn btn-secondary" id="btn-add-another" style="margin-top:0;">+ Add Another Server</button>' +
    '<button class="btn" id="btn-to-finish" ' + (state.hosts.length === 0 ? 'disabled' : '') + '>Finish Setup</button>';
}

/* ── Step 5: Theme Picker ── */
function renderTheme() {
  if (!state.themes) {
    return '' +
      '<h2>Choose Theme</h2>' +
      '<div class="card">' +
        '<div class="status-msg"><div class="spinner"></div><br>Loading themes...</div>' +
      '</div>';
  }
  var familiesHTML = '';
  var families = state.themes.families;
  var famKeys = Object.keys(families);
  for (var f = 0; f < famKeys.length; f++) {
    var fam = famKeys[f];
    var variants = families[fam];
    var swatchesHTML = '';
    var vKeys = Object.keys(variants);
    for (var v = 0; v < vKeys.length; v++) {
      var vname = vKeys[v];
      var vdata = variants[vname];
      var fullName = fam + '/' + vname;
      var sel = state.theme === fullName ? ' selected' : '';
      swatchesHTML += '' +
        '<div class="swatch-wrap">' +
          '<div class="swatch' + sel + '" data-theme="' + esc(fullName) + '">' +
            '<div class="swatch-inner">' +
              '<div class="swatch-bg" style="background:' + vdata.background + ';"></div>' +
              '<div class="swatch-bg" style="background:' + vdata.panel + ';"></div>' +
              '<div class="swatch-accent" style="background:' + vdata.primary + ';"></div>' +
            '</div>' +
            '<div class="swatch-tooltip">' + esc(fam + '/' + vname) + '</div>' +
          '</div>' +
          '<span class="swatch-label">' + esc(vname) + '</span>' +
        '</div>';
    }
    familiesHTML += '' +
      '<div>' +
        '<div class="theme-family-name">' + esc(fam) + '</div>' +
        '<div class="theme-swatches">' + swatchesHTML + '</div>' +
      '</div>';
  }
  return '' +
    '<h2>Choose Theme</h2>' +
    '<div class="card">' +
      '<div class="theme-families">' + familiesHTML + '</div>' +
    '</div>' +
    '<p style="text-align:center;font-size:0.82rem;color:var(--text-dim);margin-bottom:0.25rem;">' +
      'Selected: <strong style="color:var(--green);">' + esc(state.theme) + '</strong>' +
    '</p>' +
    '<div class="btn-row">' +
      '<button class="btn btn-secondary" id="btn-theme-back">Back</button>' +
      '<button class="btn" id="btn-to-finish">Finish Setup</button>' +
    '</div>';
}

/* ── Step 6: Finish ── */
function renderFinish() {
  var hostNames = '';
  for (var i = 0; i < state.hosts.length; i++) {
    if (i > 0) hostNames += ', ';
    hostNames += state.hosts[i].name;
  }
  var serverWord = state.hosts.length === 1 ? 'server' : 'servers';
  return '' +
    '<h2>Ready to Go</h2>' +
    '<p class="summary-text">' +
      'Setting up <strong style="color:var(--green);">' + state.hosts.length + ' ' + serverWord + '</strong> ' +
      'with theme <strong style="color:var(--green);">' + esc(state.theme) + '</strong>' +
    '</p>' +
    '<div class="card">' +
      '<div class="summary-row">' +
        '<span class="summary-label">Servers</span>' +
        '<span class="summary-value">' + state.hosts.length + '</span>' +
      '</div>' +
      '<div class="summary-row">' +
        '<span class="summary-label">Hosts</span>' +
        '<span class="summary-value">' + esc(hostNames) + '</span>' +
      '</div>' +
      '<div class="summary-row">' +
        '<span class="summary-label">Theme</span>' +
        '<span class="summary-value">' + esc(state.theme) + '</span>' +
      '</div>' +
    '</div>' +
    '<button class="btn btn-big" id="btn-finish">Start Monitoring</button>' +
    '<button class="btn btn-secondary" id="btn-finish-back" style="margin-top:0.5rem;">Back</button>';
}

/* ── Event binding ── */
function bindStep(stepName) {
  switch(stepName) {
    case 'welcome':
      on('btn-start', function() { goTo(1); });
      break;

    case 'add-server':
      var nameEl = document.getElementById('srv-name');
      var hostEl = document.getElementById('srv-host');
      var userEl = document.getElementById('srv-user');
      var portEl = document.getElementById('srv-port');
      var nextBtn = document.getElementById('btn-to-test');

      function checkFields() {
        var n = nameEl ? nameEl.value.trim() : '';
        var h = hostEl ? hostEl.value.trim() : '';
        var u = userEl ? userEl.value.trim() : '';
        if (nextBtn) nextBtn.disabled = !(n && h && u);
        /* Keep state in sync as user types */
        state.currentHost.name = n;
        state.currentHost.host = h;
        state.currentHost.user = u;
        state.currentHost.port = parseInt((portEl ? portEl.value : '22'), 10) || 22;
      }

      if (nameEl) nameEl.addEventListener('input', checkFields);
      if (hostEl) hostEl.addEventListener('input', checkFields);
      if (userEl) userEl.addEventListener('input', checkFields);
      if (portEl) portEl.addEventListener('input', checkFields);

      on('btn-to-test', function() {
        var name = val('srv-name'), host = val('srv-host'), user = val('srv-user');
        var port = parseInt($('#srv-port').value, 10) || 22;
        var pw = val('srv-password');
        if (!name || !host || !user) return;
        state.currentHost = { name:name, host:host, user:user, port:port };
        state.testPassword = pw;
        state.testResult = null;
        state.testPhase = '';
        goTo(2);
        setTimeout(function() { doFullConnect(); }, 100);
      });
      on('btn-add-cancel', function() { goTo(3); });
      break;

    case 'test':
      on('btn-test', function() {
        state.testResult = null;
        state.testPhase = '';
        renderStep();
        setTimeout(function() { doFullConnect(); }, 100);
      });
      on('btn-test-back', function() { goTo(1); });
      on('btn-test-next', function() { addHost(); });
      break;

    case 'servers':
      on('btn-add-another', function() {
        state.currentHost = { name:'', host:'', user:'', port:22 };
        state.testResult = null;
        state.testPassword = '';
        goTo(1);
      });
      on('btn-to-finish', function() {
        goTo(4);
      });
      $$('[data-remove]').forEach(function(btn) {
        btn.addEventListener('click', function() { removeHost(btn.dataset.remove); });
      });
      break;

    case 'theme':
      $$('.swatch').forEach(function(s) {
        s.addEventListener('click', function() {
          state.theme = s.dataset.theme;
          renderStep();
        });
      });
      on('btn-theme-back', function() { goTo(3); });
      on('btn-to-finish', function() { goTo(5); });
      break;

    case 'finish':
      on('btn-finish', doFinish);
      on('btn-finish-back', function() { goTo(3); });
      break;
  }
}

/* ── Helpers ── */
function on(id, fn) {
  var el = document.getElementById(id);
  if (el) el.addEventListener('click', fn);
}
function val(id) { var el = document.getElementById(id); return el ? el.value.trim() : ''; }
function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function copyText(id, btnId) {
  var el = document.getElementById(id);
  if (!el) return;
  el.select();
  navigator.clipboard.writeText(el.value).catch(function() { document.execCommand('copy'); });
  var btn = btnId ? document.getElementById(btnId) : el.parentElement.querySelector('.copy-btn');
  if (btn) {
    btn.textContent = 'COPIED';
    btn.classList.add('copied');
    setTimeout(function() { btn.textContent = 'COPY'; btn.classList.remove('copied'); }, 2000);
  }
}

function api(method, path, body) {
  var opts = { method: method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  return fetch(path, opts).then(function(r) {
    if (!r.ok) {
      return r.text().then(function(text) {
        try { return JSON.parse(text); } catch(e) {}
        return { success: false, error: 'Server error: ' + r.status };
      });
    }
    return r.json();
  });
}

/* ── API calls ── */
function fetchSSHKey() {
  if (state.sshKey) return;
  api('GET', '/api/setup/ssh-key').then(function(data) {
    state.sshKey = data;
  }).catch(function(e) {
    state.sshKey = { public_key: 'Error loading key: ' + e.message, key_path: '', generated: false };
  }).then(function() {
    if (STEPS[state.step] === 'ssh-key') renderStep();
  });
}

function fetchThemes() {
  if (state.themes) { console.log('[setup] themes already cached'); renderStep(); return; }
  console.log('[setup] fetching themes...');
  var xhr = new XMLHttpRequest();
  xhr.open('GET', '/api/setup/themes', true);
  xhr.onload = function() {
    console.log('[setup] themes xhr status:', xhr.status);
    if (xhr.status === 200) {
      try {
        state.themes = JSON.parse(xhr.responseText);
        console.log('[setup] themes parsed OK, families:', Object.keys(state.themes.families || {}).length);
      } catch(e) {
        console.error('[setup] themes parse error:', e);
        state.themes = { families: {} };
      }
    } else {
      console.error('[setup] themes bad status:', xhr.status);
      state.themes = { families: {} };
    }
    console.log('[setup] calling renderStep after themes load');
    renderStep();
  };
  xhr.onerror = function() {
    console.error('[setup] themes xhr error');
    state.themes = { families: {} };
    renderStep();
  };
  xhr.send();
}

function doFullConnect() {
  var h = state.currentHost;
  var pw = state.testPassword;

  /* Step 1: If password provided, copy SSH key first */
  if (pw) {
    state.testPhase = 'Copying SSH key to ' + h.host + '...';
    state.testResult = 'loading';
    renderStep();

    api('POST', '/api/setup/copy-key', {
      host: h.host, user: h.user, port: h.port, password: pw
    }).then(function(data) {
      if (!data.success) {
        state.testResult = { success: false, error: 'Key copy failed: ' + (data.error || 'Unknown error') };
        renderStep();
        return;
      }
      state.testPassword = '';
      /* Step 2: Test with key (no password) */
      doTestConnection();
    }).catch(function(e) {
      state.testResult = { success: false, error: 'Key copy error: ' + e.message };
      renderStep();
    });
  } else {
    /* No password — try key-based auth directly */
    doTestConnection();
  }
}

function doTestConnection() {
  var h = state.currentHost;
  state.testPhase = 'Testing connection to ' + h.host + '...';
  state.testResult = 'loading';
  renderStep();

  api('POST', '/api/setup/test-connection', {
    host: h.host, user: h.user, port: h.port
  }).then(function(data) {
    state.testResult = data;
  }).catch(function(e) {
    state.testResult = { success: false, error: e.message };
  }).then(function() {
    state.testPhase = '';
    renderStep();
  });
}

function addHost() {
  api('POST', '/api/setup/add-host', state.currentHost).then(function(data) {
    if (data.success) {
      state.hosts = data.hosts;
      goTo(4);
    } else {
      alert(data.error || 'Failed to add host');
    }
  }).catch(function(e) {
    alert('Error: ' + e.message);
  });
}

function removeHost(name) {
  api('POST', '/api/setup/remove-host', { name: name }).then(function(data) {
    if (data.success) {
      state.hosts = data.hosts;
      renderStep();
    }
  }).catch(function(e) {
    alert('Error: ' + e.message);
  });
}

function doFinish() {
  var btn = document.getElementById('btn-finish');
  if (btn) btn.disabled = true;
  var overlay = document.getElementById('overlay');
  var msg = document.getElementById('overlay-msg');
  overlay.classList.add('show');
  msg.textContent = 'Saving configuration...';
  api('POST', '/api/setup/finish', { theme: state.theme }).then(function(data) {
    if (data.success) {
      msg.textContent = 'Setup complete! Loading dashboard...';
      setTimeout(function() { window.location.href = '/'; }, 2000);
    } else {
      overlay.classList.remove('show');
      alert(data.error || 'Setup failed');
      if (btn) btn.disabled = false;
    }
  }).catch(function(e) {
    overlay.classList.remove('show');
    alert('Error: ' + e.message);
    if (btn) btn.disabled = false;
  });
}

/* ── Init ── */
goTo(0);

})();
