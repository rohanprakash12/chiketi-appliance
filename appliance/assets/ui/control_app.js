
const API = window.location.origin;
const PANEL_SPEC = __PANEL_SPEC_JSON__;
let currentData = null, metrics = null;
let selectedFamily = null, selectedVariant = null;

/* ── Data helpers ── */
function m(key) {
  if (!metrics || !metrics[key]) return { value: null, available: false, unit: '', extra: {} };
  return metrics[key];
}
function mv(key, suffix) {
  const d = m(key);
  if (!d.available) return 'N/A';
  return suffix ? d.value + suffix : String(d.value);
}

function cleanModel() {
  const d = m('llama.model');
  if (!d.available) return '--';
  return String(d.value).replace(/\.gguf$/i, '').replace(/[-_]Q\d[A-Z0-9_]*$/i, '').replace(/_/g, ' ').replace(/-$/, '');
}

function switchTab(tab) {
  document.querySelectorAll('.main-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.toggle('active', t.id === 'tab-' + tab));
}

async function loadThemes() {
  try {
    const [tr, mr] = await Promise.all([
      fetch(API + '/api/themes'), fetch(API + '/api/metrics')
    ]);
    currentData = await tr.json();
    metrics = await mr.json();
    if (!selectedFamily) selectedFamily = currentData.active_family;
    if (!selectedVariant) selectedVariant = currentData.active_variant;
    renderCategoryDropdown();
    renderVariantRow();
    renderScreens();
    renderScreenRotationUI();
    setStatus('Connected', true);
  } catch(e) { setStatus('Connection failed', false); }
}

function renderCategoryDropdown() {
  const sel = document.getElementById('categorySelect');
  sel.innerHTML = '';
  for (const fam of Object.keys(currentData.families)) {
    const opt = document.createElement('option');
    opt.value = fam;
    opt.textContent = fam;
    if (fam === selectedFamily) opt.selected = true;
    sel.appendChild(opt);
  }
  sel.onchange = () => {
    selectedFamily = sel.value;
    selectedVariant = Object.keys(currentData.families[selectedFamily])[0];
    renderVariantRow();
    renderScreens();
  };
}

function renderVariantRow() {
  const el = document.getElementById('variantRow'); el.innerHTML = '';
  const variants = currentData.families[selectedFamily] || {};
  const isActive = selectedFamily === currentData.active_family;
  for (const [name, c] of Object.entries(variants)) {
    const btn = document.createElement('button');
    const active = name === selectedVariant;
    const live = isActive && name === currentData.active_variant;
    btn.className = 'variant-btn' + (active ? ' active' : '');
    btn.style.borderColor = active ? c.primary : '#333';
    btn.style.color = active ? c.primary : '#888';
    btn.innerHTML = `<span class="variant-dot" style="background:${c.primary}"></span>${name}` +
      (live ? ' <span style="font-size:10px;color:#666">(live)</span>' : '');
    btn.onclick = () => {
      selectedVariant = name;
      selectTheme(selectedFamily, name);
      renderVariantRow();
      renderScreens();
    };
    el.appendChild(btn);
  }
}

/* ═══════════════════════════════════════
   Shared rendering helpers
   ═══════════════════════════════════════ */
function tBar(c, pct) {
  if (pct == null) return '';
  pct = Math.max(0, Math.min(100, pct));
  const filled = Math.round(pct / 5), empty = 20 - filled;
  return `<span class="t-bar"><span style="color:${c.primary}">${'\u2588'.repeat(filled)}</span><span style="color:${c.primary};opacity:0.2">${'\u2591'.repeat(empty)}</span></span>`;
}
function tPanel(c, title, rows) {
  return `<div class="t-panel" style="background:${c.panel};border:1px solid ${c.border}">` +
    `<div class="t-title" style="color:${c.header}">\u2500\u2500[ ${title} ]</div>${rows}</div>`;
}
function tRow(c, label, bar, val, color) {
  color = color || c.primary;
  return `<div class="t-row"><span class="t-label" style="color:${c.primary}">${label}</span>` +
    (bar || '') + `<span class="t-val" style="color:${color}">${val}</span></div>`;
}

const GOLD = PANEL_SPEC.colors.gold;
const AMBER = PANEL_SPEC.colors.amber;
const GREEN = PANEL_SPEC.colors.green;
const TEAL = PANEL_SPEC.colors.teal;
function _thermColor(t) {
  if (t >= 90) return PANEL_SPEC.colors.thermOrange || '#FF7700';
  if (t >= 70) return PANEL_SPEC.colors.thermYellow || '#DDCC00';
  if (t >= 50) return PANEL_SPEC.colors.thermGreen || '#22BB44';
  return PANEL_SPEC.colors.thermBlue || '#2288DD';
}
function lPanel(titleLeft, color, body, titleRight) {
  const right = titleRight ? `<span>${titleRight}</span>` : '';
  return `<div class="l-panel" style="border:2px solid ${color}">` +
    `<div class="l-titlebar" style="background:${color};display:flex;justify-content:space-between;align-items:center">`+
    `<span>${titleLeft}</span>${right}</div>` +
    `<div class="l-body">${body}</div></div>`;
}
function lStat(label, val, color) {
  return `<div class="l-stat"><span class="l-stat-label">${label}</span>` +
    `<span class="l-stat-val" style="color:${color}">${val}</span></div>`;
}
function lBar(color, pct) {
  if (pct == null) return '';
  return `<div class="l-bar"><div class="l-bar-fill" style="width:${Math.max(0,Math.min(100,pct))}%;background:${color}"></div></div>`;
}

__SCREEN_FUNCTIONS__


function getScreenRegistry(c) {
  const isPanel = selectedFamily === 'Panel';
  const isVintage = selectedFamily === 'Vintage';
  const isCoral = isPanel && selectedVariant === 'Coral';
  const isTeal = isPanel && selectedVariant === 'Teal';
  let screens;
  if (isTeal) screens = [{id:'screen1',name:'System Stats',fn:panelTealScreen1},{id:'screen2',name:'Clock',fn:panelTealScreen2}];
  else if (isCoral) screens = [{id:'screen1',name:'System Stats',fn:panelCoralScreen1},{id:'screen2',name:'Clock',fn:panelCoralScreen2}];
  else if (isPanel) screens = [{id:'screen1',name:'System Stats',fn:panelGoldScreen1},{id:'screen2',name:'Clock',fn:panelGoldScreen2}];
  else if (isVintage && selectedVariant === 'Tubes') screens = [{id:'screen1',name:'System Stats',fn:tubeScreen1},{id:'screen2',name:'Clock',fn:tubeScreen2}];
  else if (isVintage && selectedVariant === 'VFD') screens = [{id:'screen1',name:'System Stats',fn:vfdScreen1},{id:'screen2',name:'Clock',fn:vfdScreen2}];
  else if (isVintage) screens = [{id:'screen1',name:'System Stats',fn:scanScreen1},{id:'screen2',name:'Clock',fn:scanScreen2}];
  else screens = [{id:'screen1',name:'System Stats',fn:terminalScreen1},{id:'screen2',name:'AI Monitor',fn:terminalScreen2}];
  screens.push({id:'screen3',name:'Claude Usage',fn:claudeScreen3});
  return screens;
}

function renderScreens() {
  const el = document.getElementById('screens'); el.innerHTML = '';
  const c = (currentData.families[selectedFamily] || {})[selectedVariant];
  if (!c) return;
  const screens = getScreenRegistry(c);
  for (const s of screens) {
    const div = document.createElement('div');
    div.innerHTML = `<div class="screen-label">${s.name}</div>${s.fn(c)}`;
    el.appendChild(div);
  }
}

async function selectTheme(family, variant) {
  try {
    const res = await fetch(API + '/api/theme/' + family + '/' + variant, { method: 'POST' });
    if (res.ok) { await loadThemes(); setStatus('Theme: ' + family + '/' + variant, true); }
  } catch(e) { setStatus('Failed to set theme', false); }
}

function setStatus(msg, ok) {
  const el = document.getElementById('status');
  el.textContent = msg;
  el.className = 'status' + (ok ? ' ok' : '');
}

loadThemes();

// ── Host management ──
let _hostData = null;

async function loadHosts() {
  try {
    const res = await fetch(API + '/api/hosts');
    _hostData = await res.json();
    renderHostList();
  } catch(e) {}
}

function renderHostList() {
  const el = document.getElementById('hostList');
  if (!el || !_hostData) return;
  el.innerHTML = '';
  if (!_hostData.hosts || _hostData.hosts.length === 0) {
    el.innerHTML = '<div style="color:#666;font-size:12px">No hosts configured</div>';
    return;
  }
  for (const h of _hostData.hosts) {
    const isActive = h.name === _hostData.active_host;
    const card = document.createElement('div');
    card.className = 'host-card' + (isActive ? ' active-host' : '');
    card.style.position = 'relative';
    const mainArea = document.createElement('div');
    mainArea.style.cssText = 'display:flex;align-items:center;gap:8px;flex:1;cursor:pointer';
    mainArea.onclick = () => cpSwitchHost(h.name);
    mainArea.innerHTML =
      `<div class="host-dot ${h.online ? 'online' : 'offline'}"></div>` +
      `<span class="host-name">${h.name}</span>` +
      (h.latency_ms != null ? `<span class="host-latency">${h.latency_ms}ms</span>` : '') +
      (isActive ? '<span class="host-active-tag">active</span>' : '');
    card.appendChild(mainArea);
    const removeBtn = document.createElement('button');
    removeBtn.textContent = '\u00d7';
    removeBtn.title = 'Remove host';
    removeBtn.style.cssText = 'background:none;border:1px solid transparent;color:#666;font-size:16px;cursor:pointer;padding:2px 6px;border-radius:3px;line-height:1;transition:all 0.2s';
    removeBtn.onmouseenter = () => { removeBtn.style.color='#f44'; removeBtn.style.borderColor='#f44'; };
    removeBtn.onmouseleave = () => { removeBtn.style.color='#666'; removeBtn.style.borderColor='transparent'; };
    removeBtn.onclick = (e) => { e.stopPropagation(); cpRemoveHost(h.name); };
    card.appendChild(removeBtn);
    el.appendChild(card);
  }
}

async function cpTestHost() {
  const host = document.getElementById('newHostAddr').value.trim();
  const user = document.getElementById('newHostUser').value.trim();
  const port = parseInt(document.getElementById('newHostPort').value) || 22;
  if (!host || !user) { setHostStatus('Host and username required', false); return; }
  setHostStatus('Testing connection...', null);
  try {
    const res = await fetch(API + '/api/setup/test-connection', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({host, user, port})
    });
    const data = await res.json();
    if (data.success) {
      setHostStatus('Connected! Hostname: ' + data.hostname, true);
    } else {
      setHostStatus('Failed: ' + data.error, false);
    }
  } catch(e) { setHostStatus('Error: ' + e, false); }
}

async function cpCopyKey() {
  const host = document.getElementById('newHostAddr').value.trim();
  const user = document.getElementById('newHostUser').value.trim();
  const password = document.getElementById('newHostPassword').value;
  const port = parseInt(document.getElementById('newHostPort').value) || 22;
  if (!host || !user) { setHostStatus('Host and username required', false); return; }
  if (!password) { setHostStatus('Password required to copy SSH key', false); return; }
  setHostStatus('Copying SSH key...', null);
  try {
    const res = await fetch(API + '/api/setup/copy-key', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({host, user, port, password})
    });
    const data = await res.json();
    if (data.success) {
      setHostStatus('Key copied! You can now test without a password.', true);
      document.getElementById('newHostPassword').value = '';
    } else {
      setHostStatus('Failed: ' + data.error, false);
    }
  } catch(e) { setHostStatus('Error: ' + e, false); }
}

async function cpAddHost() {
  const name = document.getElementById('newHostName').value.trim();
  const host = document.getElementById('newHostAddr').value.trim();
  const user = document.getElementById('newHostUser').value.trim();
  const port = parseInt(document.getElementById('newHostPort').value) || 22;
  if (!name || !host || !user) { setHostStatus('All fields required', false); return; }
  try {
    const res = await fetch(API + '/api/setup/add-host', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name, host, user, port})
    });
    const data = await res.json();
    if (data.success) {
      setHostStatus('Host added!', true);
      document.getElementById('newHostName').value = '';
      document.getElementById('newHostAddr').value = '';
      document.getElementById('newHostUser').value = '';
      document.getElementById('newHostPassword').value = '';
      document.getElementById('newHostPort').value = '22';
      await loadHosts();
    } else {
      setHostStatus(data.error || 'Failed to add host', false);
    }
  } catch(e) { setHostStatus('Error: ' + e, false); }
}

async function cpRemoveHost(name) {
  if (!confirm('Remove host "' + name + '"?')) return;
  try {
    const res = await fetch(API + '/api/setup/remove-host', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name})
    });
    const data = await res.json();
    if (data.success) {
      setStatus('Host removed', true);
      await loadHosts();
    } else { setStatus(data.error || 'Failed', false); }
  } catch(e) { setStatus('Error', false); }
}

function setHostStatus(msg, ok) {
  const el = document.getElementById('hostActionStatus');
  if (!el) return;
  el.textContent = msg;
  el.style.color = ok === null ? '#999' : ok ? '#0f0' : '#f44';
}

async function cpSwitchHost(name) {
  try {
    const res = await fetch(API + '/api/host/' + encodeURIComponent(name), { method: 'POST' });
    if (res.ok) {
      await loadHosts();
      await loadThemes();
      setStatus('Switched to ' + name, true);
    } else { setStatus('Failed to switch host', false); }
  } catch(e) { setStatus('Error switching host', false); }
}

loadHosts();
// Refresh host status every 5 seconds
setInterval(loadHosts, 5000);

// Settings
let _outputsCache = [];
async function loadSettings() {
  try {
    const res = await fetch(API + '/api/display');
    const data = await res.json();
    _outputsCache = (data.outputs || []).filter(o => o.connected);
    populateOutputs(_outputsCache, data.current_output);
    document.getElementById('brightnessSlider').value = data.brightness || 1.0;
    document.getElementById('brightnessVal').textContent = (data.brightness || 1.0).toFixed(1);
    _serverScreenRotation = data.screen_rotation || {};
    renderScreenRotationUI();
    updatePowerToggle(data.display_on || false);
    updateResDisplay();
    updatePreviewAspectRatio(data.width || 1024, data.height || 600);
  } catch(e) {}
}
function populateOutputs(outputs, current) {
  const sel = document.getElementById('outputSelect');
  sel.innerHTML = '';
  outputs.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o.name;
    opt.textContent = o.name + (o.resolution ? ' (' + o.resolution + ')' : '');
    if (o.name === current) opt.selected = true;
    sel.appendChild(opt);
  });
  updateResDisplay();
}
function getSelectedResolution() {
  const name = document.getElementById('outputSelect').value;
  const o = _outputsCache.find(x => x.name === name);
  return o && o.resolution ? o.resolution : null;
}
function updateResDisplay() {
  const res = getSelectedResolution();
  document.getElementById('resDisplay').textContent = res || 'auto';
}
function parseResolution(res) {
  if (!res) return null;
  const m = res.match(/^(\d+)x(\d+)/);
  return m ? { w: parseInt(m[1]), h: parseInt(m[2]) } : null;
}
let _serverScreenRotation = {};
function renderScreenRotationUI() {
  const el = document.getElementById('screenRotationList');
  el.innerHTML = '';
  const c = (currentData && currentData.families[selectedFamily] || {})[selectedVariant];
  if (!c) return;
  const screens = getScreenRegistry(c);
  for (const s of screens) {
    const cfg = _serverScreenRotation[s.id] || { enabled: true, duration: 10 };
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:10px;padding:4px 0';
    row.innerHTML =
      `<label style="display:flex;align-items:center;gap:6px;color:#ccc;flex:1;cursor:pointer">` +
        `<input type="checkbox" data-screen="${s.id}" class="sr-enable" ${cfg.enabled ? 'checked' : ''} style="accent-color:#0f0;width:16px;height:16px">` +
        `${s.name}` +
      `</label>` +
      `<input type="number" data-screen="${s.id}" class="sr-duration" value="${cfg.duration}" min="3" max="600" ` +
        `style="width:60px;background:#111;border:1px solid #333;color:#0f0;padding:4px 6px;border-radius:4px;font-size:13px;text-align:center">` +
      `<span style="color:#666;font-size:12px">sec</span>`;
    el.appendChild(row);
  }
}
function getScreenRotationFromUI() {
  const result = {};
  document.querySelectorAll('.sr-enable').forEach(cb => {
    const id = cb.dataset.screen;
    const dur = document.querySelector(`.sr-duration[data-screen="${id}"]`);
    result[id] = { enabled: cb.checked, duration: parseInt(dur.value) || 10 };
  });
  return result;
}
let _displayOn = false;
function updatePowerToggle(isOn) {
  _displayOn = isOn;
  const toggle = document.getElementById('powerToggle');
  const knob = toggle.firstElementChild;
  const label = document.getElementById('powerLabel');
  if (isOn) {
    toggle.style.background = '#00aa44';
    knob.style.left = '22px';
    knob.style.background = '#fff';
    label.textContent = 'ON';
    label.style.color = '#00ff41';
  } else {
    toggle.style.background = '#333';
    knob.style.left = '2px';
    knob.style.background = '#888';
    label.textContent = 'OFF';
    label.style.color = '#888';
  }
}
document.getElementById('powerToggle').addEventListener('click', async function() {
  const newState = !_displayOn;
  try {
    const res = await fetch(API + '/api/display', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ display_on: newState }),
    });
    if (res.ok) {
      const data = await res.json();
      updatePowerToggle(data.display_on);
    }
  } catch(e) {}
});
function updatePreviewAspectRatio(w, h) {
  document.querySelectorAll('.screen-frame').forEach(f => {
    f.style.aspectRatio = w + ' / ' + h;
  });
}
document.getElementById('outputSelect').addEventListener('change', updateResDisplay);
document.getElementById('brightnessSlider').addEventListener('input', function() {
  document.getElementById('brightnessVal').textContent = parseFloat(this.value).toFixed(1);
});
document.getElementById('scanDisplays').addEventListener('click', async function() {
  try {
    const res = await fetch(API + '/api/display');
    const data = await res.json();
    _outputsCache = (data.outputs || []).filter(o => o.connected);
    populateOutputs(_outputsCache, data.current_output);
    document.getElementById('settingsStatus').textContent = 'Scanned ' + _outputsCache.length + ' connected';
    document.getElementById('settingsStatus').style.color = '#00ff41';
  } catch(e) {}
});
document.getElementById('applySettings').addEventListener('click', async function() {
  const dims = parseResolution(getSelectedResolution());
  const body = {
    output: document.getElementById('outputSelect').value,
    brightness: parseFloat(document.getElementById('brightnessSlider').value),
    screen_rotation: getScreenRotationFromUI(),
  };
  if (dims) { body.width = dims.w; body.height = dims.h; }
  try {
    const res = await fetch(API + '/api/display', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (res.ok) {
      const data = await res.json();
      updatePreviewAspectRatio(data.width, data.height);
      document.getElementById('settingsStatus').textContent = 'Settings applied';
      document.getElementById('settingsStatus').style.color = '#00ff41';
    } else {
      document.getElementById('settingsStatus').textContent = 'Failed to apply';
      document.getElementById('settingsStatus').style.color = '#ff4444';
    }
  } catch(e) {
    document.getElementById('settingsStatus').textContent = 'Error';
    document.getElementById('settingsStatus').style.color = '#ff4444';
  }
});
loadSettings();

// Refresh metrics every 3 seconds
setInterval(async () => {
  try {
    const res = await fetch(API + '/api/metrics');
    metrics = await res.json();
    renderScreens();
  } catch(e) {}
}, 3000);
