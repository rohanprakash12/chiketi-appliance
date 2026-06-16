<script>
/* Scale the 1024x600 screen-frame to fill the viewport */
function scaleDisplay() {
  const frame = document.querySelector('.screen-frame');
  if (!frame) return;
  const sx = window.innerWidth / 1024;
  const sy = window.innerHeight / 600;
  const s = Math.max(sx, sy);
  frame.style.transform = 'scale(' + s + ')';
  frame.style.transformOrigin = 'top left';
}
new MutationObserver(scaleDisplay).observe(document.getElementById('display'), {childList: true});
window.addEventListener('resize', scaleDisplay);
</script>

<script>
const API = window.location.origin;
const PANEL_SPEC = __PANEL_SPEC_JSON__;
let metrics = null;
let activeFamily = null, activeVariant = null;
let themeColors = null;
let currentScreenIdx = 0;
let enabledScreens = []; // [{id, name, html, duration}]
let pauseUntil = 0;
let lastRotate = Date.now();
const PAUSE_MS = __PAUSE_S__ * 1000;
const DEFAULT_SCREEN_DURATION = __DEFAULT_SCREEN_DURATION__;
let screenRotation = {}; // {id: {enabled, duration}}
let hostData = null; // {hosts: [...], active_host: "..."}
let lastHostRotate = Date.now();

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

/* ── Shared rendering helpers ── */
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

/* ═══ Screen renderers (identical to control panel) ═══ */

__SCREEN_FUNCTIONS__

/* ── Screen registry for current theme ── */
function getScreenRegistry(c) {
  const isPanel = activeFamily === 'Panel';
  const isVintage = activeFamily === 'Vintage';
  const isCoral = isPanel && activeVariant === 'Coral';
  const isTeal = isPanel && activeVariant === 'Teal';
  let screens;
  if (isTeal) screens = [{id:'screen1',name:'System Stats',fn:panelTealScreen1},{id:'screen2',name:'Clock',fn:panelTealScreen2}];
  else if (isCoral) screens = [{id:'screen1',name:'System Stats',fn:panelCoralScreen1},{id:'screen2',name:'Clock',fn:panelCoralScreen2}];
  else if (isPanel) screens = [{id:'screen1',name:'System Stats',fn:panelGoldScreen1},{id:'screen2',name:'Clock',fn:panelGoldScreen2}];
  else if (isVintage && activeVariant === 'Tubes') screens = [{id:'screen1',name:'System Stats',fn:tubeScreen1},{id:'screen2',name:'Clock',fn:tubeScreen2}];
  else if (isVintage && activeVariant === 'VFD') screens = [{id:'screen1',name:'System Stats',fn:vfdScreen1},{id:'screen2',name:'Clock',fn:vfdScreen2}];
  else if (isVintage) screens = [{id:'screen1',name:'System Stats',fn:scanScreen1},{id:'screen2',name:'Clock',fn:scanScreen2}];
  else screens = [{id:'screen1',name:'System Stats',fn:terminalScreen1},{id:'screen2',name:'AI Monitor',fn:terminalScreen2}];
  screens.push({id:'screen3',name:'Claude Usage',fn:claudeScreen3});
  return screens;
}

function renderDisplay() {
  if (!themeColors || !activeFamily) return;
  const c = themeColors;
  const allScreens = getScreenRegistry(c);
  // Filter to enabled screens
  enabledScreens = allScreens.filter(s => {
    const cfg = screenRotation[s.id];
    return !cfg || cfg.enabled !== false;
  }).map(s => {
    const cfg = screenRotation[s.id];
    return { id: s.id, name: s.name, html: s.fn(c), duration: (cfg && cfg.duration) || DEFAULT_SCREEN_DURATION };
  });
  if (enabledScreens.length === 0) {
    // Fallback: show first screen if all disabled
    enabledScreens = [{ id: allScreens[0].id, name: allScreens[0].name, html: allScreens[0].fn(c), duration: DEFAULT_SCREEN_DURATION }];
  }
  if (currentScreenIdx >= enabledScreens.length) currentScreenIdx = 0;
  document.getElementById('display').innerHTML = enabledScreens[currentScreenIdx].html;
}

/* ── Host bar rendering ── */
function renderHostBar() {
  const bar = document.getElementById('host-bar');
  if (!hostData || !hostData.hosts || hostData.hosts.length <= 1) {
    bar.style.display = 'none';
    return;
  }
  bar.style.display = 'flex';
  bar.innerHTML = hostData.hosts.map(h => {
    const isActive = h.name === hostData.active_host;
    const cls = 'host-btn' + (isActive ? ' active' : '') + (!h.online ? ' offline' : '');
    return `<button class="${cls}" onclick="switchHost('${h.name}')">${h.name}${!h.online ? ' \u2718' : ''}</button>`;
  }).join('');
}

async function switchHost(name) {
  try {
    const res = await fetch(API + '/api/host/' + encodeURIComponent(name), { method: 'POST' });
    if (res.ok) { poll(); }
  } catch(e) {}
}

function cycleHost() {
  if (!hostData || !hostData.hosts || hostData.hosts.length <= 1) return;
  const names = hostData.hosts.map(h => h.name);
  const idx = names.indexOf(hostData.active_host);
  const next = names[(idx + 1) % names.length];
  switchHost(next);
}

/* ── Polling ── */
async function poll() {
  try {
    const [tr, mr, dr, hr] = await Promise.all([
      fetch(API + '/api/themes'),
      fetch(API + '/api/metrics'),
      fetch(API + '/api/display'),
      fetch(API + '/api/hosts'),
    ]);
    const themeData = await tr.json();
    metrics = await mr.json();
    const displayData = await dr.json();
    hostData = await hr.json();

    // Apply per-screen rotation config
    if (displayData.screen_rotation) screenRotation = displayData.screen_rotation;

    const newFamily = themeData.active_family;
    const newVariant = themeData.active_variant;
    if (newFamily !== activeFamily || newVariant !== activeVariant) {
      activeFamily = newFamily;
      activeVariant = newVariant;
      currentScreenIdx = 0;
      lastRotate = Date.now();
    }
    themeColors = (themeData.families[activeFamily] || {})[activeVariant];

    renderDisplay();
    renderHostBar();
  } catch(e) { /* retry next poll */ }
}

/* ── Auto-rotate (per-screen durations + host rotation) ── */
function tick() {
  const now = Date.now();
  if (enabledScreens.length > 1 && now > pauseUntil) {
    const currentDuration = (enabledScreens[currentScreenIdx] || {}).duration || DEFAULT_SCREEN_DURATION;
    if (now - lastRotate >= currentDuration * 1000) {
      currentScreenIdx = (currentScreenIdx + 1) % enabledScreens.length;
      lastRotate = now;
      renderDisplay();
    }
  }
  /* Host auto-rotate: cycle hosts every HOST_ROTATE_S seconds (if enabled via config) */
  if (hostData && hostData.hosts && hostData.hosts.length > 1) {
    const hostRotateS = hostData.host_rotate_interval || 0;
    if (hostRotateS > 0 && now - lastHostRotate >= hostRotateS * 1000) {
      lastHostRotate = now;
      cycleHost();
    }
  }
  requestAnimationFrame(tick);
}

/* ── Keyboard shortcuts ── */
document.addEventListener('keydown', (e) => {
  const n = enabledScreens.length || 1;
  if (e.key >= '1' && e.key <= '9') { currentScreenIdx = Math.min(parseInt(e.key) - 1, n - 1); pauseUntil = Date.now() + PAUSE_MS; lastRotate = Date.now(); renderDisplay(); }
  else if (e.key === ' ') { e.preventDefault(); currentScreenIdx = (currentScreenIdx + 1) % n; pauseUntil = Date.now() + PAUSE_MS; lastRotate = Date.now(); renderDisplay(); }
  else if (e.key === 'h' || e.key === 'H') { cycleHost(); }
  else if (e.key === 'Escape') { window.close(); }
});

/* ── Start ── */
poll();
setInterval(poll, 2500);
requestAnimationFrame(tick);
</script>