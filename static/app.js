// ==========================================
// Globals & State
// ==========================================
const canvas = document.getElementById('trafficCanvas');
const ctx = canvas.getContext('2d');
const canvasAi = document.getElementById('canvas-ai');
const ctxAi = canvasAi ? canvasAi.getContext('2d') : null;
const fixedCanvas = document.getElementById('fixedCanvas');
const fixedCtx = fixedCanvas ? fixedCanvas.getContext('2d') : null;

// Event Log Drawer toggle (overlays dashboard; no layout push)
function setEventLogOpen(open) {
  const drawer = document.getElementById('event-log-view');
  const btnDash = document.getElementById('btn-nav-dashboard');
  const btnLog = document.getElementById('btn-nav-event-log');
  if (!drawer) return;
  drawer.classList.toggle('open', !!open);
  drawer.setAttribute('aria-hidden', open ? 'false' : 'true');
  btnDash?.classList.toggle('active', !open);
  btnLog?.classList.toggle('active', !!open);
  if (open) {
    EventLogPage.ensureInitialized();
    EventLogPage.render();
  }
}

function toggleEventLogDrawer() {
  const drawer = document.getElementById('event-log-view');
  if (!drawer) return;
  setEventLogOpen(!drawer.classList.contains('open'));
}

let ws = null;
let animFrame = null;
let speed = 5;
let heatmapOn = false;

// WebSocket reconnect state
let wsReconnectAttempts = 0;
let wsReconnectTimer = null;
let wsIsFrozen = false;
const WS_MAX_RECONNECT_DELAY = 30000; // 30 seconds cap
const WS_MAX_ATTEMPTS = 5;

// Data state
let previousLaneState = null;
let targetLaneState = null;
let frameTimestamp = performance.now();
let lastFrameTime = Date.now();
let tickIntervalMs = 200; // speed=5 -> 200ms (1000/5)

let currentPhase = "NS";
let fishSwarmActive = false;
let fishSwarmStartTime = 0;

// Vehicles Array setup
// { id, lane, position, prevPosition, targetPosition, color, type, leaving }
let vehicles = [];
let nextVehicleId = 1;
const vehiclePalette = ['#4FC3F7','#81C784','#FFB74D','#F06292','#CE93D8','#80DEEA','#FFCC02'];

// Flash toggle for emergency vehicles (toggles every 400ms)
let flashToggle = false;
setInterval(() => { flashToggle = !flashToggle; }, 400);

// Chart
let costChart = null;

// Sound Engine (Web Audio API)
const SoundEngine = {
  ctx: null,
  enabled: true,
  _lastAlgorithm: null,

  init() {
    if (!this.ctx) {
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    }
  },

  // Short click on phase switch
  phaseSwitch() {
    if (!this.enabled || !this.ctx) return;
    const o = this.ctx.createOscillator();
    const g = this.ctx.createGain();
    o.connect(g); g.connect(this.ctx.destination);
    o.frequency.setValueAtTime(880, this.ctx.currentTime);
    o.frequency.exponentialRampToValueAtTime(440, this.ctx.currentTime + 0.08);
    g.gain.setValueAtTime(0.15, this.ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, this.ctx.currentTime + 0.1);
    o.start(); o.stop(this.ctx.currentTime + 0.1);
  },

  // Two-tone siren pulse for emergency override
  siren() {
    if (!this.enabled || !this.ctx) return;
    [0, 0.25].forEach(offset => {
      const o = this.ctx.createOscillator();
      const g = this.ctx.createGain();
      o.connect(g); g.connect(this.ctx.destination);
      o.frequency.setValueAtTime(offset === 0 ? 700 : 900, this.ctx.currentTime + offset);
      g.gain.setValueAtTime(0.12, this.ctx.currentTime + offset);
      g.gain.exponentialRampToValueAtTime(0.001, this.ctx.currentTime + offset + 0.22);
      o.start(this.ctx.currentTime + offset);
      o.stop(this.ctx.currentTime + offset + 0.22);
    });
  },

  setEnabled(enabled) {
    this.enabled = enabled;
  }
};

// Session replay buffer
let replayBuffer = [];
let replayMode = false;
let replayFrames = [];
let replayIndex = 0;
let replayInterval = null;
let replaySpeed = 1;
const MAX_REPLAY_BUFFER = 2000;

// Split-screen comparison
let splitMode = false;
let latestNetwork = {};
let activeJunction = 'J1';

// ==========================================
// Initialization & Events
// ==========================================
function init() {
  initChart();

  // Navigation
  document.getElementById('btn-nav-dashboard')?.addEventListener('click', () => setEventLogOpen(false));
  document.getElementById('btn-nav-event-log')?.addEventListener('click', toggleEventLogDrawer);
  document.getElementById('btn-close-event-log')?.addEventListener('click', () => setEventLogOpen(false));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') setEventLogOpen(false);
  });
  
  document.getElementById('btn-start').addEventListener('click', () => {
    // Initialize sound engine on first user gesture (required by browser autoplay policy)
    SoundEngine.init();

    const dur = document.getElementById('input-duration').value;
    fetch('/api/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ duration: parseInt(dur), speed: speed })
    });
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      connect();
    }
  });

  document.getElementById('btn-pause').addEventListener('click', () => fetch('/api/pause', {method: 'POST'}));
  
  document.getElementById('btn-reset').addEventListener('click', () => {
    fetch('/api/reset', {method: 'POST'});
    vehicles = []; // clear cars
    LogStore.clear(); // clear log
  });

  document.getElementById('btn-add-junction-toggle')?.addEventListener('click', () => {
    const form = document.getElementById('add-junction-form');
    if (!form) return;
    form.style.display = form.style.display === 'none' ? 'flex' : 'none';
  });

  document.getElementById('btn-add-junction')?.addEventListener('click', async () => {
    const id = document.getElementById('junction-id')?.value?.trim();
    const col = Number(document.getElementById('junction-col')?.value ?? 1);
    const row = Number(document.getElementById('junction-row')?.value ?? 0);
    const connect_from = document.getElementById('junction-connect-from')?.value?.trim() || 'J1';
    const exit_lane = document.getElementById('junction-exit-lane')?.value || 'E';
    const entry_lane = document.getElementById('junction-entry-lane')?.value || 'W';
    if (!id) return;
    await fetch('/api/junction/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ id, position: [col, row], connect_from, exit_lane, entry_lane })
    });
  });

  document.getElementById('junction-select')?.addEventListener('change', async (e) => {
    const id = e.target.value;
    await fetch('/api/junction/focus', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ id })
    });
  });

  const speedRadios = document.getElementsByName('speed');
  speedRadios.forEach(r => r.addEventListener('change', (e) => {
    speed = parseInt(e.target.value);
    fetch('/api/start', { // restart with new speed
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ duration: 5000, speed: speed })
    });
  }));
  
  document.getElementById('btn-apply-params').addEventListener('click', () => {
    const bw = document.getElementById('param-beam').value;
    const ct = document.getElementById('param-congestion').value;
    fetch('/api/params', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ beam_width: parseInt(bw), congestion_threshold: parseInt(ct) })
    });
  });

  document.getElementById('param-beam').addEventListener('input', e => document.getElementById('lbl-beam').innerText = e.target.value);
  document.getElementById('param-congestion').addEventListener('input', e => document.getElementById('lbl-congestion').innerText = e.target.value);

  // Scenarios
  document.getElementById('btn-inject-emergency').addEventListener('click', () => fetch('/api/inject', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({type: 'emergency', lane: 'W'}) }));
  document.getElementById('btn-inject-block').addEventListener('click', () => fetch('/api/inject', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({type: 'block', lane: 'E'}) }));
  document.getElementById('btn-inject-surge').addEventListener('click', () => fetch('/api/inject', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({type: 'surge'}) }));
  
  document.getElementById('btn-toggle-heatmap').addEventListener('click', () => heatmapOn = !heatmapOn);

  // Theme toggle
  const themeBtn = document.getElementById('btn-theme-toggle');
  if (themeBtn) {
    themeBtn.addEventListener('click', toggleTheme);
  }

  // Mute toggle
  const muteBtn = document.getElementById('btn-mute-toggle');
  if (muteBtn) {
    muteBtn.addEventListener('click', () => {
      SoundEngine.setEnabled(!SoundEngine.enabled);
      muteBtn.innerText = SoundEngine.enabled ? '🔊' : '🔇';
    });
  }

  // Profile dropdown
  const profileSelect = document.getElementById('select-profile');
  if (profileSelect) {
    profileSelect.addEventListener('change', (e) => {
      fetch('/api/profile', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ profile: e.target.value })
      });
    });
  }

  // Split-screen toggle
  const splitBtn = document.getElementById('btn-split-compare');
  if (splitBtn) {
    splitBtn.addEventListener('click', toggleSplitMode);
  }

  // Replay controls
  const replayBtn = document.getElementById('btn-replay');
  if (replayBtn) {
    replayBtn.addEventListener('click', async () => {
      await enterReplayMode();
      startReplayAutoplay();
    });
  }

  const liveBtn = document.getElementById('btn-live');
  if (liveBtn) {
    liveBtn.addEventListener('click', exitReplayMode);
  }

  const scrubInput = document.getElementById('scrub-bar');
  if (scrubInput) {
    scrubInput.addEventListener('input', handleScrub);
  }
  const replayBar = document.getElementById('replayBar');
  if (replayBar) {
    replayBar.addEventListener('input', handleReplayScrub);
  }
  const replaySpeedSelect = document.getElementById('replay-speed-select');
  if (replaySpeedSelect) {
    replaySpeedSelect.addEventListener('change', updateReplaySpeedFromUI);
  }

  // Save chart button
  const saveChartBtn = document.getElementById('btn-save-chart');
  if (saveChartBtn) {
    saveChartBtn.addEventListener('click', saveChartAsPNG);
  }

  // Past runs panel toggle
  const pastRunsBtn = document.getElementById('btn-past-runs');
  if (pastRunsBtn) {
    pastRunsBtn.addEventListener('click', togglePastRunsPanel);
  }

  // Restore theme from localStorage
  const savedTheme = localStorage.getItem('tcTheme');
  if (savedTheme) {
    document.documentElement.dataset.theme = savedTheme;
  }

  // Start render loop
  requestAnimationFrame(gameLoop);
  // Drawer is hidden by default
  setEventLogOpen(false);
  ['replayBar', 'replayCounter', 'replay-speed-select'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
}

// ==========================================
// Theme Toggle
// ==========================================
function toggleTheme() {
  const currentTheme = document.documentElement.dataset.theme || 'dark';
  const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = newTheme;
  localStorage.setItem('tcTheme', newTheme);
  // Redraw static road layer on next frame
}

// ==========================================
// Session Replay / Scrub Bar
// ==========================================
function updateScrubBar() {
  const scrubInput = document.getElementById('replayBar');
  const tickDisplay = document.getElementById('replay-tick-display');
  const replayCounter = document.getElementById('replayCounter');
  if (!scrubInput) return;
  scrubInput.max = Math.max(0, replayFrames.length - 1);
  scrubInput.value = String(replayIndex);
  if (tickDisplay && replayFrames.length > 0) {
    const frame = replayFrames[replayIndex] || replayFrames[0];
    if (frame) tickDisplay.innerText = `Tick: ${frame.tick}`;
  }
  if (replayCounter) {
    replayCounter.innerText = `${replayFrames.length ? replayIndex : 0} / ${Math.max(0, replayFrames.length - 1)}`;
  }
}

async function enterReplayMode() {
  clearInterval(replayInterval);
  replayInterval = null;
  replayMode = true;
  const response = await fetch('/api/replay/frames/range?start=0&end=2000');
  replayFrames = await response.json();
  replayIndex = 0;
  ['replayBar', 'replayCounter', 'replay-speed-select'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.style.display = '';
  });
  updateScrubBar();
  renderReplayFrame(0);
}

function exitReplayMode() {
  clearInterval(replayInterval);
  replayInterval = null;
  replayMode = false;
  ['replayBar', 'replayCounter', 'replay-speed-select'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
}

function renderReplayFrame(index) {
  if (!replayFrames.length) return;
  replayIndex = Math.max(0, Math.min(index, replayFrames.length - 1));
  const frame = replayFrames[replayIndex];
  handleFrame(frame, { isReplay: true });
  const replayLogContainer = document.getElementById('event-log-items');
  if (replayLogContainer) {
    replayLogContainer.innerHTML = `<div class="log-line">${frame.log || ''}</div>`;
  }
  updateScrubBar();
}

function startReplayAutoplay() {
  if (!replayMode || replayFrames.length === 0) return;
  clearInterval(replayInterval);
  replayInterval = setInterval(() => {
    replayIndex += 1;
    if (replayIndex >= replayFrames.length) {
      clearInterval(replayInterval);
      replayInterval = null;
      return;
    }
    renderReplayFrame(replayIndex);
  }, 1000 / (replaySpeed * 5));
}

function handleReplayScrub(e) {
  if (!replayMode) return;
  clearInterval(replayInterval);
  replayInterval = null;
  replayIndex = parseInt(e.target.value, 10) || 0;
  renderReplayFrame(replayIndex);
}

function updateReplaySpeedFromUI() {
  const sel = document.getElementById('replay-speed-select');
  replaySpeed = sel ? parseFloat(sel.value || '1') : 1;
  if (replayInterval) {
    startReplayAutoplay();
  }
}

function handleScrub(e) {
  handleReplayScrub(e);
}

function updateLightsFromFrame(frame) {
  currentPhase = frame.phase;
  splitMode = !!frame.split_mode;
}

// ==========================================
// Split-Screen Comparison
// ==========================================
function toggleSplitMode() {
  splitMode = !splitMode;
  const btn = document.getElementById('btn-split-compare');
  const container = document.getElementById('split-container');
  const comparison = document.getElementById('split-comparison');

  if (splitMode) {
    btn?.classList.add('active');
    if (container) container.style.display = 'flex';
    if (comparison) comparison.style.display = 'block';
    // Enable split mode on server
    fetch('/api/split', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ enabled: true })
    });
  } else {
    btn?.classList.remove('active');
    if (container) container.style.display = 'none';
    if (comparison) comparison.style.display = 'none';
    fetch('/api/split', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ enabled: false })
    });
  }
}

function updateSplitComparison(frame) {
  if (!frame.split_mode || !frame.fixed) return;

  const aiWait = frame.stats?.avg_wait_astar || frame.stats?.avg_wait_current || 0;
  const fixedWait = frame.fixed?.avg_wait || 0;

  const improvementEl = document.getElementById('improvement-badge');
  const adaptiveWaitEl = document.getElementById('split-adaptive-wait');
  const fixedWaitEl = document.getElementById('split-fixed-wait');
  if (improvementEl && fixedWait > 0) {
    const improvement = ((fixedWait - aiWait) / fixedWait * 100);
    improvementEl.innerText = `${improvement.toFixed(1)}%`;
    improvementEl.style.backgroundColor = improvement >= 0 ? '#00e676' : '#ff1744';
    improvementEl.style.color = improvement >= 0 ? '#000' : '#fff';
  }
  if (adaptiveWaitEl) adaptiveWaitEl.innerText = `${aiWait.toFixed(1)}s`;
  if (fixedWaitEl) fixedWaitEl.innerText = `${fixedWait.toFixed(1)}s`;

  // Update comparison bar in metrics panel
  const barFixed = document.getElementById('bar-split-fixed');
  const valFixed = document.getElementById('val-split-fixed');
  if (barFixed && valFixed) {
    const maxBar = Math.max(aiWait, fixedWait, 60);
    valFixed.innerText = fixedWait.toFixed(1) + 's';
    barFixed.style.width = `${(fixedWait / maxBar) * 100}%`;
  }
}

function renderFixedCanvas(frame) {
  if (!fixedCtx || !fixedCanvas) return;
  if (!frame.split_mode || !frame.fixed || !frame.fixed.lanes) {
    fixedCanvas.style.display = 'none';
    return;
  }
  fixedCanvas.style.display = 'block';
  fixedCtx.clearRect(0, 0, fixedCanvas.width, fixedCanvas.height);
  drawRoads(fixedCtx, frame.fixed.lanes);
  drawLights(fixedCtx, frame.fixed.phase, frame.fixed.lanes);
  drawVehiclesForState(fixedCtx, frame.fixed.lanes, frame.fixed.phase);
}

function renderAdaptiveSplitCanvas(frame) {
  if (!ctxAi || !canvasAi) return;
  if (!frame.split_mode) {
    canvasAi.style.display = 'none';
    return;
  }
  canvasAi.style.display = 'block';
  ctxAi.clearRect(0, 0, canvasAi.width, canvasAi.height);
  drawRoads(ctxAi, frame.lanes);
  drawLights(ctxAi, frame.phase, frame.lanes);
  drawVehiclesForState(ctxAi, frame.lanes, frame.phase);
}

function drawVehiclesForState(renderCtx, lanes, phase) {
  if (!lanes) return;
  const configs = {
    N: { x: 225, y0: 25, dx: 0, dy: 16, vertical: true },
    S: { x: 275, y0: 475, dx: 0, dy: -16, vertical: true },
    E: { x: 475, y0: 225, dx: -16, dy: 0, vertical: false },
    W: { x: 25, y0: 275, dx: 16, dy: 0, vertical: false },
  };
  ["N", "S", "E", "W"].forEach((dir) => {
    const lane = lanes[dir];
    if (!lane) return;
    const cfg = configs[dir];
    const isGreen = phase === "NS" ? (dir === "N" || dir === "S") : (dir === "E" || dir === "W");
    for (let i = 0; i < Math.min(12, lane.count || 0); i++) {
      const x = cfg.x + cfg.dx * i;
      const y = cfg.y0 + cfg.dy * i;
      renderCtx.fillStyle = lane.blocked ? "#ff9800" : (isGreen ? "#4FC3F7" : "#8080aa");
      if (cfg.vertical) {
        renderCtx.fillRect(x - 4, y - 7, 8, 14);
      } else {
        renderCtx.fillRect(x - 7, y - 4, 14, 8);
      }
    }
  });
}

// ==========================================
// Chart Export
// ==========================================
function saveChartAsPNG() {
  if (!costChart) return;
  const url = costChart.toBase64Image();
  const link = document.createElement('a');
  link.href = url;
  link.download = 'waiting_times.png';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

// ==========================================
// Past Runs Panel
// ==========================================
async function togglePastRunsPanel() {
  const panel = document.getElementById('past-runs-panel');
  if (!panel) return;

  const isVisible = panel.style.display !== 'none';
  if (isVisible) {
    panel.style.display = 'none';
  } else {
    panel.style.display = 'block';
    await loadPastRuns();
  }
}

async function loadPastRuns() {
  try {
    const response = await fetch('/api/history');
    const sessions = await response.json();
    renderPastRunsTable(sessions);
  } catch (e) {
    console.error('Failed to load past runs:', e);
  }
}

function renderPastRunsTable(sessions) {
  const tbody = document.getElementById('past-runs-table-body');
  if (!tbody) return;

  tbody.innerHTML = '';
  sessions.forEach(session => {
    const row = document.createElement('tr');
    const improvement = session.avg_wait_fixed > 0
      ? ((session.avg_wait_fixed - session.avg_wait_astar) / session.avg_wait_fixed * 100).toFixed(1)
      : '0.0';

    row.innerHTML = `
      <td>${new Date(session.started_at).toLocaleString()}</td>
      <td>${session.duration}s</td>
      <td>${session.avg_wait_astar?.toFixed(1) || '0.0'}s</td>
      <td>${session.avg_wait_fixed?.toFixed(1) || '0.0'}s</td>
      <td>${improvement}%</td>
      <td>${session.total_served}</td>
      <td><button class="btn btn-sm" onclick="loadSessionReplay('${session.id}')">▶ Replay</button></td>
    `;
    tbody.appendChild(row);
  });
}

async function loadSessionReplay(sessionId) {
  try {
    const response = await fetch(`/api/history/${sessionId}`);
    const session = await response.json();
    if (session.results_json) {
      const results = JSON.parse(session.results_json);
      if (results.frames) {
        replayBuffer = results.frames;
        enterReplayMode();
        document.getElementById('scrub-bar').value = 0;
        handleScrub({ target: { value: 0 } });
      }
    }
  } catch (e) {
    console.error('Failed to load session:', e);
  }
}

// ==========================================
// WebSocket Management
// ==========================================
function updateWsStatusBanner() {
  let banner = document.getElementById('ws-status');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'ws-status';
    banner.style.cssText = `
      position: fixed; top: 0; left: 0; right: 0; z-index: 10000;
      padding: 8px; text-align: center; font-weight: 600;
      transition: all 0.3s ease;
    `;
    document.body.prepend(banner);
  }

  if (ws && ws.readyState === WebSocket.OPEN) {
    // Connected - remove banner
    banner.style.display = 'none';
    wsIsFrozen = false;
  } else if (wsReconnectAttempts > 0 && wsReconnectAttempts < WS_MAX_ATTEMPTS) {
    // Reconnecting - yellow
    banner.style.display = 'block';
    banner.style.backgroundColor = '#ffd600';
    banner.style.color = '#000';
    banner.innerText = `Reconnecting... (attempt ${wsReconnectAttempts})`;
    wsIsFrozen = true;
  } else if (wsReconnectAttempts >= WS_MAX_ATTEMPTS) {
    // Failed - red
    banner.style.display = 'block';
    banner.style.backgroundColor = '#ff1744';
    banner.style.color = '#fff';
    banner.innerText = 'Disconnected — server offline';
    wsIsFrozen = true;
  } else {
    banner.style.display = 'none';
  }
}

function connect() {
  // Clear any existing reconnect timer
  if (wsReconnectTimer) {
    clearTimeout(wsReconnectTimer);
    wsReconnectTimer = null;
  }

  ws = new WebSocket(`ws://${location.host}/ws`);

  ws.onopen = () => {
    document.getElementById('disconnected-banner').classList.add('hidden');
    wsReconnectAttempts = 0; // Reset attempt counter on success
    updateWsStatusBanner();
  };

  ws.onclose = () => {
    document.getElementById('disconnected-banner').classList.remove('hidden');

    // Increment attempt counter
    wsReconnectAttempts++;
    updateWsStatusBanner();

    if (wsReconnectAttempts >= WS_MAX_ATTEMPTS) {
      // Max attempts reached, show disconnected state
      wsIsFrozen = true;
      return;
    }

    // Exponential backoff: 1s, 2s, 4s, 8s, 16s... capped at 30s
    const delay = Math.min(1000 * Math.pow(2, wsReconnectAttempts - 1), WS_MAX_RECONNECT_DELAY);

    wsReconnectTimer = setTimeout(() => {
      connect();
    }, delay);
  };

  ws.onerror = () => {
    ws.close();
  };

  ws.onmessage = (e) => handleFrame(JSON.parse(e.data));
}

function maybeApplyLiveParams(frame) {
  if (!frame.live_params) return;
  const beam = document.getElementById('param-beam');
  const thr = document.getElementById('param-congestion');
  const lblBeam = document.getElementById('lbl-beam');
  const lblThr = document.getElementById('lbl-congestion');

  if (beam && document.activeElement !== beam && Number.isFinite(frame.live_params.beam_width)) {
    beam.value = String(frame.live_params.beam_width);
    if (lblBeam) lblBeam.innerText = beam.value;
  }
  if (thr && document.activeElement !== thr && Number.isFinite(frame.live_params.congestion_threshold)) {
    thr.value = String(frame.live_params.congestion_threshold);
    if (lblThr) lblThr.innerText = thr.value;
  }
}

function handleFrame(frame, options = {}) {
  const isReplay = !!options.isReplay;
  // In replay mode, ignore live frames
  if (replayMode && !isReplay) return;

  previousLaneState = targetLaneState ? JSON.parse(JSON.stringify(targetLaneState)) : null;
  targetLaneState = frame.lanes;
  frameTimestamp = performance.now();
  lastFrameTime = Date.now();
  currentPhase = frame.phase;
  latestNetwork = frame.network || {};
  activeJunction = frame.active_junction || 'J1';

  // Update tick interval based on current speed setting
  tickIntervalMs = 1000 / speed;

  // Add to local replay buffer for fallback
  if (!isReplay) {
    replayBuffer.push(JSON.parse(JSON.stringify(frame)));
    if (replayBuffer.length > MAX_REPLAY_BUFFER) replayBuffer.shift();
  }

  // Handle sounds
  if (frame.action === 'SWITCH_PHASE' || frame.action === 'SWITCH') {
    SoundEngine.phaseSwitch();
  }
  if (frame.algorithm === 'EMERGENCY' && SoundEngine._lastAlgorithm !== 'EMERGENCY') {
    SoundEngine.siren();
  }
  SoundEngine._lastAlgorithm = frame.algorithm;
  
  if (frame.fish_swarm_active !== fishSwarmActive) {
    fishSwarmActive = frame.fish_swarm_active;
    const badge = document.getElementById('fish-swarm-badge');
    if (fishSwarmActive) {
      badge.classList.remove('hidden');
      fishSwarmStartTime = performance.now();
    } else {
      badge.classList.add('hidden');
    }
  }

  maybeApplyLiveParams(frame);
  updateMetrics(frame);
  if (!isReplay) {
    updateLog(frame);
  }
  syncVehicles(frame.lanes, frame.phase, frame.lane_intents || {}, frame.exiting_vehicles || []);
  updateChart(frame);
  renderAdaptiveSplitCanvas(frame);
  renderFixedCanvas(frame);

  if (splitMode) {
    document.getElementById('split-container')?.style.setProperty('display', 'flex');
    document.getElementById('split-comparison')?.style.setProperty('display', 'block');
  }
}

// ==========================================
// Canvas Rendering Loop
// ==========================================
const clamp = (val, min, max) => Math.max(min, Math.min(max, val));
const lerp = (a, b, t) => a + (b - a) * t;

function gameLoop(now) {
  // If frozen (disconnected), just redraw the last known state without advancing
  if (wsIsFrozen) {
    drawRoads(ctx, targetLaneState);
    if (heatmapOn) drawHeatmap();
    drawLights(ctx, currentPhase, targetLaneState);
    drawFrozenVehicles();
    requestAnimationFrame(gameLoop);
    return;
  }

  ctx.clearRect(0, 0, 500, 500);
  drawRoads(ctx, targetLaneState);
  if (heatmapOn) drawHeatmap();
  drawLights(ctx, currentPhase, targetLaneState);

  // Calculate interpolation alpha based on time since last frame
  let alpha = 1;
  if (targetLaneState) {
    const elapsed = Date.now() - lastFrameTime;
    alpha = clamp(elapsed / tickIntervalMs, 0, 1);
  }

  moveAndDrawVehicles(alpha);
  drawNetworkMap();

  if (fishSwarmActive && (now - fishSwarmStartTime < 1500)) {
    drawFishSwarmDots(now);
  }

  if (replayMode) {
    ctx.fillStyle = 'rgba(255, 200, 0, 0.08)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#ffd600';
    ctx.font = 'bold 13px monospace';
    ctx.fillText(`⏪ REPLAY  ${replayIndex} / ${replayFrames.length}`, 10, 20);
  }

  requestAnimationFrame(gameLoop);
}

function drawFrozenVehicles() {
  // Draw vehicles at their last known positions when disconnected
  vehicles.forEach(v => {
    if (v.position > 0) {
      drawVehicle(v, 1);
    }
  });
}

function drawRoads(renderCtx = ctx, laneState = targetLaneState) {
  renderCtx.fillStyle = '#2d2d2d';
  // Vertical
  renderCtx.fillRect(200, 0, 100, 500);
  // Horizontal
  renderCtx.fillRect(0, 200, 500, 100);
  
  // Sidewalks
  renderCtx.fillStyle = '#3a3a3a';
  renderCtx.fillRect(190, 0, 10, 200); renderCtx.fillRect(300, 0, 10, 200); // N
  renderCtx.fillRect(190, 300, 10, 200); renderCtx.fillRect(300, 300, 10, 200); // S
  renderCtx.fillRect(0, 190, 200, 10); renderCtx.fillRect(0, 300, 200, 10); // W
  renderCtx.fillRect(300, 190, 200, 10); renderCtx.fillRect(300, 300, 200, 10); // E

  // Corners
  renderCtx.beginPath(); renderCtx.arc(200, 200, 10, 0, Math.PI*2); renderCtx.fill();
  renderCtx.beginPath(); renderCtx.arc(300, 200, 10, 0, Math.PI*2); renderCtx.fill();
  renderCtx.beginPath(); renderCtx.arc(200, 300, 10, 0, Math.PI*2); renderCtx.fill();
  renderCtx.beginPath(); renderCtx.arc(300, 300, 10, 0, Math.PI*2); renderCtx.fill();

  // Markings
  renderCtx.strokeStyle = '#ffffff';
  renderCtx.lineWidth = 2;
  renderCtx.setLineDash([10, 15]);
  // N
  renderCtx.beginPath(); renderCtx.moveTo(250, 0); renderCtx.lineTo(250, 200); renderCtx.stroke();
  // S
  renderCtx.beginPath(); renderCtx.moveTo(250, 300); renderCtx.lineTo(250, 500); renderCtx.stroke();
  // E
  renderCtx.beginPath(); renderCtx.moveTo(300, 250); renderCtx.lineTo(500, 250); renderCtx.stroke();
  // W
  renderCtx.beginPath(); renderCtx.moveTo(0, 250); renderCtx.lineTo(200, 250); renderCtx.stroke();
  renderCtx.setLineDash([]);

  // Blocked lane overlays / markers
  if (laneState) {
    const blockedOverlays = {
      N: { x: 210, y: 0, w: 80, h: 200, markX: 235, markY: 170 },
      S: { x: 210, y: 300, w: 80, h: 200, markX: 235, markY: 330 },
      E: { x: 300, y: 210, w: 200, h: 80, markX: 330, markY: 235 },
      W: { x: 0, y: 210, w: 200, h: 80, markX: 170, markY: 235 },
    };
    Object.keys(blockedOverlays).forEach((dir) => {
      if (!laneState[dir]?.blocked) return;
      const o = blockedOverlays[dir];
      renderCtx.fillStyle = 'rgba(255, 140, 0, 0.3)';
      renderCtx.fillRect(o.x, o.y, o.w, o.h);
      renderCtx.strokeStyle = '#ff9800';
      renderCtx.lineWidth = 3;
      renderCtx.beginPath();
      renderCtx.moveTo(o.markX - 10, o.markY - 10);
      renderCtx.lineTo(o.markX + 10, o.markY + 10);
      renderCtx.moveTo(o.markX + 10, o.markY - 10);
      renderCtx.lineTo(o.markX - 10, o.markY + 10);
      renderCtx.stroke();
      renderCtx.font = '18px Inter';
      renderCtx.fillText('🚧', o.markX + 12, o.markY + 6);
    });
  }
}

// Heatmap alpha values for smooth lerp
const heatmapAlphas = { N: 0.12, S: 0.12, E: 0.12, W: 0.12 };
const heatmapTargetAlphas = { N: 0.12, S: 0.12, E: 0.12, W: 0.12 };

function drawHeatmap() {
  if (!targetLaneState) return;

  // Define arm rectangles (x, y, w, h) for each lane
  const arms = {
    N: { x: 210, y: 0, w: 80, h: 200 },
    S: { x: 210, y: 300, w: 80, h: 200 },
    E: { x: 300, y: 210, w: 200, h: 80 },
    W: { x: 0, y: 210, w: 200, h: 80 }
  };

  // Smoothly lerp alpha values toward target
  ['N', 'S', 'E', 'W'].forEach(dir => {
    const current = heatmapAlphas[dir];
    const target = heatmapTargetAlphas[dir];
    heatmapAlphas[dir] = current + (target - current) * 0.08;
  });

  // Draw heatmap for each lane
  ['N', 'S', 'E', 'W'].forEach(dir => {
    const count = targetLaneState[dir].count;
    const arm = arms[dir];

    // Determine color based on count
    let color;
    let targetAlpha;
    if (count <= 5) {
      color = '0, 230, 118'; // green
      targetAlpha = 0.12;
    } else if (count <= 15) {
      color = '255, 214, 0'; // yellow
      targetAlpha = 0.20;
    } else if (count <= 25) {
      color = '255, 111, 0'; // orange
      targetAlpha = 0.28;
    } else {
      color = '255, 23, 68'; // red
      targetAlpha = 0.35;
    }

    // Update target alpha for smooth transition
    heatmapTargetAlphas[dir] = targetAlpha;

    // Draw with current lerped alpha
    ctx.fillStyle = `rgba(${color}, ${heatmapAlphas[dir]})`;
    ctx.fillRect(arm.x, arm.y, arm.w, arm.h);
  });
}

function drawLights(renderCtx = ctx, phase = currentPhase, laneState = targetLaneState) {
  const nsGreen = phase === "NS";
  
  const drawLight = (gx, gy, isGreen, hasEmergency) => {
    renderCtx.fillStyle = '#111';
    renderCtx.beginPath(); renderCtx.roundRect(gx-8, gy-20, 16, 40, 4); renderCtx.fill();
    renderCtx.strokeStyle = '#000'; renderCtx.stroke();
    
    // Red 
    renderCtx.beginPath(); renderCtx.arc(gx, gy-12, 4, 0, Math.PI*2);
    renderCtx.fillStyle = isGreen ? '#400' : (hasEmergency && (Date.now()%500<250) ? '#400' : '#f00');
    if (!isGreen) { renderCtx.shadowColor='#f00'; renderCtx.shadowBlur=10; }
    renderCtx.fill(); renderCtx.shadowBlur=0;
    
    // Yellow flash for emergency
    renderCtx.beginPath(); renderCtx.arc(gx, gy, 4, 0, Math.PI*2);
    renderCtx.fillStyle = (!isGreen && hasEmergency && (Date.now()%500<250)) ? '#ff0' : '#440';
    if (!isGreen && hasEmergency && (Date.now()%500<250)) { renderCtx.shadowColor='#ff0'; renderCtx.shadowBlur=10; }
    renderCtx.fill(); renderCtx.shadowBlur=0;

    // Green
    renderCtx.beginPath(); renderCtx.arc(gx, gy+12, 4, 0, Math.PI*2);
    renderCtx.fillStyle = isGreen ? '#0f0' : '#040';
    if (isGreen) { renderCtx.shadowColor='#0f0'; renderCtx.shadowBlur=10; }
    renderCtx.fill(); renderCtx.shadowBlur=0;
  };

  const nsEmergN = laneState?.N?.emergency || false;
  const nsEmergS = laneState?.S?.emergency || false;
  const ewEmergE = laneState?.E?.emergency || false;
  const ewEmergW = laneState?.W?.emergency || false;

  drawLight(215, 155, nsGreen, nsEmergN); // N
  drawLight(265, 335, nsGreen, nsEmergS); // S
  drawLight(335, 215, !nsGreen, ewEmergE); // E
  drawLight(155, 265, !nsGreen, ewEmergW); // W
}

function getExitLane(lane, intent) {
  const key = `${lane}:${intent}`;
  const map = {
    'N:straight': 'S', 'N:turn_left': 'E', 'N:turn_right': 'W',
    'S:straight': 'N', 'S:turn_left': 'W', 'S:turn_right': 'E',
    'E:straight': 'W', 'E:turn_left': 'S', 'E:turn_right': 'N',
    'W:straight': 'E', 'W:turn_left': 'N', 'W:turn_right': 'S'
  };
  return map[key] || 'S';
}

function getTurnPath(lane, intent) {
  const ENTRY = { N:{x:230,y:200}, S:{x:270,y:300}, E:{x:300,y:230}, W:{x:200,y:270} };
  const EXIT  = { N:{x:230,y:100}, S:{x:270,y:400}, E:{x:400,y:230}, W:{x:100,y:270} };
  const entry = ENTRY[lane];
  const exitDir = getExitLane(lane, intent);
  const exit = EXIT[exitDir];
  if (!entry || !exit) return [];
  if (intent === 'straight') {
    return [entry, {x: (entry.x + exit.x) / 2, y: (entry.y + exit.y) / 2}, exit];
  }
  const R = 40;
  const ctrl = {
    x: entry.x + (exit.x - entry.x) * 0.3 + (intent === 'turn_right' ? R : -R),
    y: entry.y + (exit.y - entry.y) * 0.3 + (intent === 'turn_right' ? R : -R)
  };
  return [entry, ctrl, exit];
}

function syncVehicles(lanes, phase, laneIntents = {}, exitingVehicles = []) {
  // Add missing vehicles with prevPosition/targetPosition for interpolation
  const exitingByLane = exitingVehicles.reduce((acc, v) => {
    const key = v.from;
    if (!acc[key]) acc[key] = [];
    acc[key].push(v);
    return acc;
  }, {});

  ['N', 'S', 'E', 'W'].forEach(dir => {
    const laneInfo = lanes[dir];
    let laneVehicles = vehicles.filter(v => v.lane === dir && !v.leaving);

    // if emergency, ensure one emergency vehicle at the back
    if (laneInfo.emergency) {
      if (!laneVehicles.some(v => v.type === 'emergency')) {
        vehicles.push({
          id: nextVehicleId++, lane: dir,
          position: 0, prevPosition: 0, targetPosition: 0,
          color: '#f00', type: 'emergency', leaving: false
        });
        laneVehicles = vehicles.filter(v => v.lane === dir && !v.leaving);
      }
    } else {
      // remove emergency if cleared
      const evIdx = vehicles.findIndex(v => v.lane === dir && v.type === 'emergency');
      if (evIdx !== -1) {
        vehicles[evIdx].type = 'car';
        vehicles[evIdx].color = vehiclePalette[Math.floor(Math.random()*vehiclePalette.length)];
      }
    }

    while (laneVehicles.length < laneInfo.count) {
      const startPos = Math.random() * -0.5;
      vehicles.push({
        id: nextVehicleId++, lane: dir,
        position: startPos, prevPosition: startPos, targetPosition: 0,
        color: vehiclePalette[Math.floor(Math.random()*vehiclePalette.length)],
        type: 'car', leaving: false,
        intent: 'straight',
        exitLane: getExitLane(dir, 'straight'),
        exitPosition: 0,
        phase: 'approaching',
        pathPoints: []
      });
      laneVehicles = vehicles.filter(v => v.lane === dir && !v.leaving);
    }

    while (laneVehicles.length > laneInfo.count) {
      // Front vehicles leave
      laneVehicles.sort((a,b) => b.position - a.position);
      laneVehicles[0].leaving = true;
      const out = (exitingByLane[dir] || []).shift();
      laneVehicles[0].intent = out?.intent || laneVehicles[0].intent || 'straight';
      laneVehicles[0].exitLane = out?.to || getExitLane(dir, laneVehicles[0].intent);
      laneVehicles[0].phase = laneVehicles[0].intent === 'straight' ? 'exiting' : 'turning';
      laneVehicles[0].exitPosition = 0;
      laneVehicles[0].pathPoints = getTurnPath(dir, laneVehicles[0].intent);
      laneVehicles.shift();
    }

    // Assign proper target positions for queueing
    laneVehicles.sort((a,b) => b.position - a.position);

    const isGreen = phase === 'NS' ? (dir === 'N' || dir === 'S') : (dir === 'E' || dir === 'W');

    laneVehicles.forEach((v, idx) => {
      // Store current position as previous before setting new target
      v.prevPosition = v.position;
      if (laneInfo.blocked) {
        v.targetPosition = v.position; // blocked lanes do not move forward
        return;
      }
      if (isGreen) {
        v.targetPosition = 1.0; // move past intersection
        if (v.position > 0.8) {
          const out = (exitingByLane[dir] || []).shift();
          v.leaving = true;
          v.intent = out?.intent || v.intent || 'straight';
          v.exitLane = out?.to || getExitLane(dir, v.intent);
          v.phase = v.intent === 'straight' ? 'exiting' : 'turning';
          v.exitPosition = 0;
          v.pathPoints = getTurnPath(dir, v.intent);
        }
      } else {
         // stack up based on idx
         const stopLine = 0.85;
         const spacing = 0.12;
         v.targetPosition = Math.max(0.0, stopLine - idx * spacing);
      }
    });
  });
}

function moveAndDrawVehicles(alpha) {
  // Move vehicles with interpolation
  for (let i = vehicles.length - 1; i >= 0; i--) {
    const v = vehicles[i];
    const laneBlocked = !!targetLaneState?.[v.lane]?.blocked;

    if (laneBlocked) {
      v.position = v.prevPosition;
    } else if (v.phase === 'turning') {
      v.exitPosition = Math.min(1, (v.exitPosition || 0) + 0.06 * speed);
      if (v.exitPosition >= 1) v.phase = 'exiting';
    } else if (v.leaving) {
      v.position += 0.05 * speed; // Exit speed
    } else {
      // Linear interpolation between prev and target position
      v.position = lerp(v.prevPosition, v.targetPosition, alpha);
    }

    if (v.position > 1.2) {
      vehicles.splice(i, 1); // remove
    } else {
      drawVehicle(v, alpha);
    }
  }
}

function drawVehicle(v, alpha = 1) {
  if (v.position <= 0) return; // not entered yet

  let vx, vy; // vehicle center position on canvas
  const cx = 250, cy = 250;

  // Calculate vehicle center position based on lane and position
  if (v.lane === 'N') {
    vx = 225; vy = lerp(-20, cy, v.position);
  } else if (v.lane === 'S') {
    vx = 275; vy = lerp(520, cy, v.position);
  } else if (v.lane === 'E') {
    vx = lerp(520, cx, v.position); vy = 225;
  } else if (v.lane === 'W') {
    vx = lerp(-20, cx, v.position); vy = 275;
  } else {
    return;
  }

  if (v.phase === 'turning' && Array.isArray(v.pathPoints) && v.pathPoints.length === 3) {
    const [p0, p1, p2] = v.pathPoints;
    const t = Math.max(0, Math.min(1, v.exitPosition || 0));
    const prevT = Math.max(0, t - 0.03);
    const bx = (1-t)*(1-t)*p0.x + 2*(1-t)*t*p1.x + t*t*p2.x;
    const by = (1-t)*(1-t)*p0.y + 2*(1-t)*t*p1.y + t*t*p2.y;
    const prevBx = (1-prevT)*(1-prevT)*p0.x + 2*(1-prevT)*prevT*p1.x + prevT*prevT*p2.x;
    const prevBy = (1-prevT)*(1-prevT)*p0.y + 2*(1-prevT)*prevT*p1.y + prevT*prevT*p2.y;
    vx = bx;
    vy = by;
    const angle = Math.atan2(by - prevBy, bx - prevBx);
    ctx.save();
    ctx.translate(vx, vy);
    ctx.rotate(angle);
    ctx.fillStyle = v.color;
    ctx.beginPath();
    ctx.roundRect(-10, -5, 20, 10, 3);
    ctx.fill();
    ctx.restore();
    return;
  }

  // Vehicle dimensions
  const w = 20, h = 10; // horizontal base dimensions

  ctx.save();
  ctx.translate(vx, vy);

  // Apply rotation based on lane direction
  // N → Math.PI (travelling south toward intersection)
  // S → 0
  // E → -Math.PI / 2
  // W → Math.PI / 2
  if (v.lane === 'N') {
    ctx.rotate(Math.PI);
  } else if (v.lane === 'S') {
    ctx.rotate(0);
  } else if (v.lane === 'E') {
    ctx.rotate(-Math.PI / 2);
  } else if (v.lane === 'W') {
    ctx.rotate(Math.PI / 2);
  }

  // Draw vehicle rectangle centered at (0, 0) after rotation
  ctx.fillStyle = v.color;
  ctx.beginPath();
  ctx.roundRect(-w/2, -h/2, w, h, 3);
  ctx.fill();

  // Emergency vehicles get flashing red/white alternating fill
  if (v.type === 'emergency') {
    // Toggle between red and white every 400ms
    ctx.fillStyle = flashToggle ? '#ff1744' : '#ffffff';
    ctx.beginPath();
    ctx.roundRect(-w/2 + 2, -h/2 + 2, w - 4, h - 4, 2);
    ctx.fill();
  }

  ctx.restore();
}

function drawFishSwarmDots(now) {
  const elapsed = now - fishSwarmStartTime;
  const alpha = 1.0 - (elapsed / 1500);
  ctx.fillStyle = `rgba(0, 229, 255, ${alpha})`;
  
  for (let i=0; i<20; i++) {
    const angle = (now / 200) + i * Math.PI*2/20;
    const r = 100 * (1 - elapsed/1500);
    const px = 250 + Math.cos(angle)*r;
    const py = 250 + Math.sin(angle)*r;
    ctx.beginPath(); ctx.arc(px, py, 3, 0, Math.PI*2); ctx.fill();
  }
}

// ==========================================
// UI Updates
// ==========================================
function updateMetrics(frame) {
  // Update replay tick display if in replay mode
  const tickDisplay = replayMode ? `TICK ${frame.tick} (REPLAY)` : `TICK ${frame.tick}`;
  document.getElementById('tick-counter').innerText = tickDisplay;
  document.getElementById('phase-timer').innerText = `Phase: ${frame.phase}-Green | ${frame.phase_timer}s active`;

  const badge = document.getElementById('algo-badge');
  if (frame.algorithm === 'EMERGENCY') {
    badge.innerText = 'EMERGENCY';
    badge.className = 'algorithm-badge badge-emergency';
    badge.style.backgroundColor = '';
    badge.style.color = '';
  } else if (frame.algorithm === 'AO_STAR') {
    badge.innerText = 'AO*';
    badge.className = 'algorithm-badge badge-ao_star';
    badge.style.backgroundColor = '';
    badge.style.color = '';
  } else {
    badge.innerText = 'Q-Learning';
    badge.className = 'algorithm-badge';
    badge.style.backgroundColor = '#00e5ff';
    badge.style.color = '#001018';
  }

  const rlStatsLine = document.getElementById('rl-stats-line');
  if (rlStatsLine && frame.rl) {
    rlStatsLine.innerText = `Q-states: ${frame.rl.q_table_size} | ε: ${frame.rl.epsilon} | Phase: ${frame.rl.training_phase}`;
  }

  // Update split-screen comparison if active
  if (splitMode && frame.fixed) {
    updateSplitComparison(frame);
  }

  const tbody = document.getElementById('lane-table-body');
  tbody.innerHTML = '';
  ['N', 'S', 'E', 'W'].forEach(dir => {
    const info = frame.lanes[dir];
    const intents = frame.lane_intents?.[dir] || { straight: 0, turn_left: 0, turn_right: 0 };
    const tr = document.createElement('tr');
    let statusText = info.green ? '🟢 GREEN' : '🔴 RED';
    if (info.emergency) statusText += ' 🚑';
    if (info.blocked) statusText = '<span style="background:#ff9800;color:#111;padding:2px 8px;border-radius:999px;font-weight:700;">BLOCKED</span>';
    
    tr.innerHTML = `
      <td>${dir}</td>
      <td>${info.count} cars | ↑${intents.straight || 0} ←${intents.turn_left || 0} →${intents.turn_right || 0}</td>
      <td>${info.wait.toFixed(1)}</td>
      <td>${statusText}</td>
    `;
    tbody.appendChild(tr);
  });
  
  const valCurrent = frame.stats.avg_wait_current;
  const valFixed = frame.stats.avg_wait_fixed;
  const maxBar = Math.max(valCurrent, valFixed, 60);
  
  document.getElementById('val-astar').innerText = valCurrent.toFixed(1) + 's';
  document.getElementById('bar-astar').style.width = `${(valCurrent / maxBar) * 100}%`;
  
  document.getElementById('val-fixed').innerText = valFixed.toFixed(1) + 's';
  document.getElementById('bar-fixed').style.width = `${(valFixed / maxBar) * 100}%`;

  syncJunctionSelect(frame);
}

function syncJunctionSelect(frame) {
  const select = document.getElementById('junction-select');
  if (!select || !frame.network) return;
  const ids = Object.keys(frame.network);
  ids.forEach((id) => {
    if (![...select.options].some(opt => opt.value === id)) {
      const opt = document.createElement('option');
      opt.value = id;
      opt.textContent = id;
      select.appendChild(opt);
    }
  });
  if (frame.active_junction) select.value = frame.active_junction;
}

function drawNetworkMap() {
  if (!latestNetwork || Object.keys(latestNetwork).length <= 1) return;
  const mapX = 360, mapY = 10, cellSize = 30, pad = 4;
  ctx.fillStyle = 'rgba(0,0,0,0.5)';
  ctx.fillRect(mapX - pad, mapY - pad, 120 + pad * 2, 80 + pad * 2);

  for (const [jid, jstate] of Object.entries(latestNetwork)) {
    const [col, row] = jstate.position || [0, 0];
    const x = mapX + col * cellSize;
    const y = mapY + row * cellSize;
    ctx.strokeStyle = jid === activeJunction ? '#00e676' : '#666';
    ctx.lineWidth = jid === activeJunction ? 2 : 1;
    ctx.strokeRect(x, y, cellSize - 4, cellSize - 4);
    ctx.fillStyle = jstate.phase === 'NS' ? '#1a7a3a' : '#1a3a7a';
    ctx.fillRect(x + 1, y + 1, cellSize - 6, cellSize - 6);
    ctx.fillStyle = '#fff';
    ctx.font = '8px monospace';
    ctx.fillText(jid, x + 4, y + 14);
  }
  ctx.strokeStyle = '#888';
  ctx.lineWidth = 0.5;
  for (const [jidA, a] of Object.entries(latestNetwork)) {
    for (const [jidB, b] of Object.entries(latestNetwork)) {
      if (jidA >= jidB) continue;
      const [ac, ar] = a.position || [0, 0];
      const [bc, br] = b.position || [0, 0];
      if (Math.abs(ac - bc) + Math.abs(ar - br) === 1) {
        ctx.beginPath();
        ctx.moveTo(mapX + ac * cellSize + 13, mapY + ar * cellSize + 13);
        ctx.lineTo(mapX + bc * cellSize + 13, mapY + br * cellSize + 13);
        ctx.stroke();
      }
    }
  }
}

// ==========================================
// Event Log Global Store (persists across navigation)
// ==========================================
const LogStore = (() => {
  /** @type {{id:number, tick:number|null, algorithm:string|null, action:string|null, cost:number|null, raw:string, ts:number}[]} */
  let items = [];
  /** @type {Set<() => void>} */
  const listeners = new Set();
  let nextId = 1;

  function parseTick(raw) {
    const m = raw.match(/\[(\d+)\]/);
    return m ? parseInt(m[1], 10) : null;
  }

  function parseCost(raw) {
    const m = raw.match(/cost\s*=\s*([0-9]+(?:\.[0-9]+)?)/i);
    return m ? parseFloat(m[1]) : null;
  }

  function parseAlgoAction(raw) {
    // Expected: [tick] ALGO → ACTION cost=VALUE
    // Fallbacks handle older formats too.
    const arrowSplit = raw.split('→').map(s => s.trim());
    if (arrowSplit.length >= 2) {
      const left = arrowSplit[0]; // "[tick] ALGO"
      const right = arrowSplit[1]; // "ACTION cost=..."
      const algo = left.replace(/\[\d+\]/, '').trim().split(/\s+/)[0] || null;
      const action = right.split(/\s+/)[0] || null;
      return { algorithm: algo, action };
    }
    return { algorithm: null, action: null };
  }

  function notify() {
    listeners.forEach(fn => {
      try { fn(); } catch (e) { console.error(e); }
    });
  }

  return {
    getAll() { return items; },
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    clear() { items = []; notify(); },
    addFromFrame(frame) {
      if (!frame?.log) return;
      const raw = String(frame.log);
      const parsed = parseAlgoAction(raw);
      items.push({
        id: nextId++,
        tick: frame.tick ?? parseTick(raw),
        algorithm: frame.algorithm ?? parsed.algorithm,
        action: frame.action ?? parsed.action,
        cost: parseCost(raw),
        raw,
        ts: Date.now()
      });
      // keep memory bounded; UI supports lots, but we still cap
      const MAX_ITEMS = 50000;
      if (items.length > MAX_ITEMS) items = items.slice(items.length - MAX_ITEMS);
      notify();
    }
  };
})();

function updateLog(frame) {
  LogStore.addFromFrame(frame);
}

// ==========================================
// Event Log Page UI (filters/search/autoscroll/virtualized rendering)
// ==========================================
const EventLogPage = (() => {
  let initialized = false;

  // UI state
  let autoScroll = true;
  let search = '';
  let algoAstar = true;
  let algoBeam = true;
  let actionKeep = true;
  let actionSwitch = true;

  // Virtualization config
  const LINE_HEIGHT = 20; // px (approx; keeps perf stable)
  const OVERSCAN = 12;

  function isOpen() {
    const drawer = document.getElementById('event-log-view');
    return !!drawer && drawer.classList.contains('open');
  }

  function getEls() {
    return {
      viewport: document.getElementById('event-log-viewport'),
      items: document.getElementById('event-log-items'),
      top: document.getElementById('event-log-spacer-top'),
      bottom: document.getElementById('event-log-spacer-bottom'),
      toggleAuto: document.getElementById('toggle-autoscroll'),
      clearBtn: document.getElementById('btn-clear-logs'),
      dlJson: document.getElementById('btn-download-json'),
      dlCsv: document.getElementById('btn-download-csv'),
      fAstar: document.getElementById('filter-algo-astar'),
      fBeam: document.getElementById('filter-algo-beam'),
      fKeep: document.getElementById('filter-action-keep'),
      fSwitch: document.getElementById('filter-action-switch'),
      inputSearch: document.getElementById('input-log-search')
    };
  }

  function matchesFilters(item) {
    const algo = (item.algorithm || '').toUpperCase();
    const action = (item.action || '').toUpperCase();
    const raw = (item.raw || '').toUpperCase();

    const algoOk =
      (algoAstar && (algo === 'ASTAR' || raw.includes('ASTAR'))) ||
      (algoBeam && (algo === 'BEAM' || raw.includes('BEAM')));
    if (!algoOk) return false;

    const actionOk =
      (actionKeep && (action === 'KEEP_PHASE' || raw.includes('KEEP_PHASE'))) ||
      (actionSwitch && (action === 'SWITCH_PHASE' || raw.includes('SWITCH_PHASE') || raw.includes('SWITCH')));
    if (!actionOk) return false;

    if (search.trim()) {
      const s = search.trim().toUpperCase();
      if (!(raw.includes(s) || String(item.tick ?? '').includes(s))) return false;
    }

    return true;
  }

  function classifyLine(raw) {
    const classes = ['log-line'];
    if (raw.includes('SWITCH')) classes.push('yellow');
    if (raw.includes('EMERGENCY')) classes.push('red');
    if (raw.includes('Fish Swarm')) classes.push('cyan');
    if (raw.includes('AO*') || raw.includes('Blocked')) classes.push('orange');
    return classes.join(' ');
  }

  function downloadText(filename, text) {
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  function toCSV(rows) {
    const header = ['tick', 'algorithm', 'action', 'cost', 'timestamp', 'raw'];
    const escape = (v) => {
      const s = String(v ?? '');
      if (s.includes('"') || s.includes(',') || s.includes('\n')) return `"${s.replace(/"/g, '""')}"`;
      return s;
    };
    const lines = [header.join(',')];
    rows.forEach(r => {
      lines.push([
        escape(r.tick),
        escape(r.algorithm),
        escape(r.action),
        escape(r.cost),
        escape(new Date(r.ts).toISOString()),
        escape(r.raw)
      ].join(','));
    });
    return lines.join('\n');
  }

  function isNearBottom(viewport) {
    const threshold = 24;
    return viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - threshold;
  }

  function renderVirtualized(filtered) {
    const els = getEls();
    if (!els.viewport || !els.items || !els.top || !els.bottom) return;

    const viewport = els.viewport;
    const wasNearBottom = isNearBottom(viewport);

    const total = filtered.length;
    const visibleCount = Math.max(1, Math.ceil(viewport.clientHeight / LINE_HEIGHT));
    const start = Math.max(0, Math.floor(viewport.scrollTop / LINE_HEIGHT) - OVERSCAN);
    const end = Math.min(total, start + visibleCount + OVERSCAN * 2);

    els.top.style.height = `${start * LINE_HEIGHT}px`;
    els.bottom.style.height = `${(total - end) * LINE_HEIGHT}px`;

    // Render window
    els.items.innerHTML = '';
    for (let i = start; i < end; i++) {
      const item = filtered[i];
      const div = document.createElement('div');
      div.className = classifyLine(item.raw);
      div.innerText = item.raw;
      els.items.appendChild(div);
    }

    // Auto-scroll only if enabled and user wasn't reading older content
    if (autoScroll && wasNearBottom) {
      viewport.scrollTop = viewport.scrollHeight;
    }
  }

  function getFilteredItems() {
    const all = LogStore.getAll();
    if (!all.length) return [];
    const out = [];
    for (const it of all) {
      if (matchesFilters(it)) out.push(it);
    }
    return out;
  }

  function render() {
    if (!isOpen()) return;
    const els = getEls();
    if (!els.viewport) return;
    renderVirtualized(getFilteredItems());
  }

  function ensureInitialized() {
    if (initialized) return;
    initialized = true;

    const els = getEls();
    if (!els.viewport) return;

    els.toggleAuto?.addEventListener('change', (e) => {
      autoScroll = !!e.target.checked;
      render();
    });
    els.clearBtn?.addEventListener('click', () => LogStore.clear());
    els.inputSearch?.addEventListener('input', (e) => {
      search = e.target.value || '';
      render();
    });
    els.fAstar?.addEventListener('change', (e) => { algoAstar = !!e.target.checked; render(); });
    els.fBeam?.addEventListener('change', (e) => { algoBeam = !!e.target.checked; render(); });
    els.fKeep?.addEventListener('change', (e) => { actionKeep = !!e.target.checked; render(); });
    els.fSwitch?.addEventListener('change', (e) => { actionSwitch = !!e.target.checked; render(); });

    els.viewport.addEventListener('scroll', () => {
      if (!isOpen()) return;
      // If user scrolls up, stop autoscroll; if they scroll back down, re-enable.
      const nearBottom = isNearBottom(els.viewport);
      if (!nearBottom && autoScroll) {
        autoScroll = false;
        if (els.toggleAuto) els.toggleAuto.checked = false;
      } else if (nearBottom && !autoScroll) {
        // keep user's preference; only re-enable if toggle is on
      }
      render();
    });

    els.dlJson?.addEventListener('click', () => {
      const rows = getFilteredItems();
      downloadText(`event-log-${Date.now()}.json`, JSON.stringify(rows, null, 2));
    });

    els.dlCsv?.addEventListener('click', () => {
      const rows = getFilteredItems();
      downloadText(`event-log-${Date.now()}.csv`, toCSV(rows));
    });

    LogStore.subscribe(() => {
      if (isOpen()) render();
    });
  }

  return { ensureInitialized, render };
})();

// ==========================================
// Chart.js Setup
// ==========================================
function initChart() {
  const ctxChart = document.getElementById('costChart').getContext('2d');
  costChart = new Chart(ctxChart, {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        { label: 'Current Avg Wait', data: [], borderColor: '#2979ff', borderWidth: 2, pointRadius: 0, tension: 0.2 },
        { label: 'Fixed Avg Wait', data: [], borderColor: '#8080aa', borderWidth: 2, pointRadius: 0, borderDash: [5,5], tension: 0.2 }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { labels: { color: '#e0e0ff' } } },
      scales: {
        x: { display: false },
        y: { grid: { color: '#2a2a4a' }, ticks: { color: '#8080aa' } }
      }
    }
  });
}

function updateChart(frame) {
  if (!costChart) return;
  
  costChart.data.labels.push(frame.tick);
  costChart.data.datasets[0].data.push(frame.stats.avg_wait_current);
  costChart.data.datasets[1].data.push(frame.stats.avg_wait_fixed);
  
  if (costChart.data.labels.length > 60) {
    costChart.data.labels.shift();
    costChart.data.datasets[0].data.shift();
    costChart.data.datasets[1].data.shift();
  }
  
  costChart.update('none');
}

// Start
window.addEventListener('load', init);
