// ==========================================
// Globals & State
// ==========================================
const canvas = document.getElementById('trafficCanvas');
const ctx = canvas.getContext('2d');

let ws = null;
let retryDelay = 500;
let animFrame = null;
let speed = 5;
let heatmapOn = false;

// Data state
let previousLaneState = null;
let targetLaneState = null;
let frameTimestamp = performance.now();

let currentPhase = "NS";
let fishSwarmActive = false;
let fishSwarmStartTime = 0;

// Vehicles Array setup
// { id, lane, position, speed, color, type, targetPos, previousPos }
let vehicles = [];
let nextVehicleId = 1;
const vehiclePalette = ['#4FC3F7','#81C784','#FFB74D','#F06292','#CE93D8','#80DEEA','#FFCC02'];

// Chart
let costChart = null;

// ==========================================
// Initialization & Events
// ==========================================
function init() {
  initChart();
  
  document.getElementById('btn-start').addEventListener('click', () => {
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
    document.getElementById('event-log').innerHTML = ''; // clear log
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

  // Start render loop
  requestAnimationFrame(gameLoop);
}

// ==========================================
// WebSocket Management
// ==========================================
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => {
    document.getElementById('disconnected-banner').classList.add('hidden');
    retryDelay = 500;
  };
  ws.onclose = () => {
    document.getElementById('disconnected-banner').classList.remove('hidden');
    setTimeout(connect, Math.min(retryDelay *= 2, 10000));
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (e) => handleFrame(JSON.parse(e.data));
}

function handleFrame(frame) {
  previousLaneState = targetLaneState ? JSON.parse(JSON.stringify(targetLaneState)) : null;
  targetLaneState = frame.lanes;
  frameTimestamp = performance.now();
  currentPhase = frame.phase;
  
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

  updateMetrics(frame);
  updateLog(frame);
  syncVehicles(frame.lanes, frame.phase);
  updateChart(frame);
}

// ==========================================
// Canvas Rendering Loop
// ==========================================
const clamp = (val, min, max) => Math.max(min, Math.min(max, val));
const lerp = (a, b, t) => a + (b - a) * t;

function gameLoop(now) {
  ctx.clearRect(0, 0, 500, 500);
  drawRoads();
  if (heatmapOn) drawHeatmap();
  drawLights();
  
  
  let t = 1;
  if (targetLaneState) {
    t = clamp((now - frameTimestamp) / (1000 / speed), 0, 1);
  }

  moveAndDrawVehicles(t);
  
  if (fishSwarmActive && (now - fishSwarmStartTime < 1500)) {
    drawFishSwarmDots(now);
  }

  requestAnimationFrame(gameLoop);
}

function drawRoads() {
  ctx.fillStyle = '#2d2d2d';
  // Vertical
  ctx.fillRect(200, 0, 100, 500);
  // Horizontal
  ctx.fillRect(0, 200, 500, 100);
  
  // Sidewalks
  ctx.fillStyle = '#3a3a3a';
  ctx.fillRect(190, 0, 10, 200); ctx.fillRect(300, 0, 10, 200); // N
  ctx.fillRect(190, 300, 10, 200); ctx.fillRect(300, 300, 10, 200); // S
  ctx.fillRect(0, 190, 200, 10); ctx.fillRect(0, 300, 200, 10); // W
  ctx.fillRect(300, 190, 200, 10); ctx.fillRect(300, 300, 200, 10); // E

  // Corners
  ctx.beginPath(); ctx.arc(200, 200, 10, 0, Math.PI*2); ctx.fill();
  ctx.beginPath(); ctx.arc(300, 200, 10, 0, Math.PI*2); ctx.fill();
  ctx.beginPath(); ctx.arc(200, 300, 10, 0, Math.PI*2); ctx.fill();
  ctx.beginPath(); ctx.arc(300, 300, 10, 0, Math.PI*2); ctx.fill();

  // Markings
  ctx.strokeStyle = '#ffffff';
  ctx.lineWidth = 2;
  ctx.setLineDash([10, 15]);
  // N
  ctx.beginPath(); ctx.moveTo(250, 0); ctx.lineTo(250, 200); ctx.stroke();
  // S
  ctx.beginPath(); ctx.moveTo(250, 300); ctx.lineTo(250, 500); ctx.stroke();
  // E
  ctx.beginPath(); ctx.moveTo(300, 250); ctx.lineTo(500, 250); ctx.stroke();
  // W
  ctx.beginPath(); ctx.moveTo(0, 250); ctx.lineTo(200, 250); ctx.stroke();
  ctx.setLineDash([]);
}

function drawHeatmap() {
  if (!targetLaneState) return;
  const colors = (count) => {
    if (count <= 5) return 'rgba(0, 255, 0, 0.15)';
    if (count <= 15) return 'rgba(255, 165, 0, 0.2)';
    return 'rgba(255, 0, 0, 0.25)';
  };
  
  // N: x=200..300, y=0..200
  ctx.fillStyle = colors(targetLaneState.N.count); ctx.fillRect(200, 0, 100, 200);
  // S: x=200..300, y=300..500
  ctx.fillStyle = colors(targetLaneState.S.count); ctx.fillRect(200, 300, 100, 200);
  // W: x=0..200, y=200..300
  ctx.fillStyle = colors(targetLaneState.W.count); ctx.fillRect(0, 200, 200, 100);
  // E: x=300..500, y=200..300
  ctx.fillStyle = colors(targetLaneState.E.count); ctx.fillRect(300, 200, 200, 100);
}

function drawLights() {
  const nsGreen = currentPhase === "NS";
  
  const drawLight = (gx, gy, isGreen, hasEmergency) => {
    ctx.fillStyle = '#111';
    ctx.beginPath(); ctx.roundRect(gx-8, gy-20, 16, 40, 4); ctx.fill();
    ctx.strokeStyle = '#000'; ctx.stroke();
    
    // Red 
    ctx.beginPath(); ctx.arc(gx, gy-12, 4, 0, Math.PI*2);
    ctx.fillStyle = isGreen ? '#400' : (hasEmergency && (Date.now()%500<250) ? '#400' : '#f00');
    if (!isGreen) { ctx.shadowColor='#f00'; ctx.shadowBlur=10; }
    ctx.fill(); ctx.shadowBlur=0;
    
    // Yellow flash for emergency
    ctx.beginPath(); ctx.arc(gx, gy, 4, 0, Math.PI*2);
    ctx.fillStyle = (!isGreen && hasEmergency && (Date.now()%500<250)) ? '#ff0' : '#440';
    if (!isGreen && hasEmergency && (Date.now()%500<250)) { ctx.shadowColor='#ff0'; ctx.shadowBlur=10; }
    ctx.fill(); ctx.shadowBlur=0;

    // Green
    ctx.beginPath(); ctx.arc(gx, gy+12, 4, 0, Math.PI*2);
    ctx.fillStyle = isGreen ? '#0f0' : '#040';
    if (isGreen) { ctx.shadowColor='#0f0'; ctx.shadowBlur=10; }
    ctx.fill(); ctx.shadowBlur=0;
  };

  const nsEmergN = targetLaneState?.N?.emergency || false;
  const nsEmergS = targetLaneState?.S?.emergency || false;
  const ewEmergE = targetLaneState?.E?.emergency || false;
  const ewEmergW = targetLaneState?.W?.emergency || false;

  drawLight(215, 155, nsGreen, nsEmergN); // N
  drawLight(265, 335, nsGreen, nsEmergS); // S
  drawLight(335, 215, !nsGreen, ewEmergE); // E
  drawLight(155, 265, !nsGreen, ewEmergW); // W
}

function syncVehicles(lanes, phase) {
  // Add missing vehicles
  ['N', 'S', 'E', 'W'].forEach(dir => {
    const laneInfo = lanes[dir];
    let laneVehicles = vehicles.filter(v => v.lane === dir && !v.leaving);
    
    // if emergency, ensure one emergency vehicle at the back
    if (laneInfo.emergency) {
      if (!laneVehicles.some(v => v.type === 'emergency')) {
        vehicles.push({
          id: nextVehicleId++, lane: dir, position: 0, previousPos: 0, targetPos: 0,
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
      vehicles.push({
        id: nextVehicleId++, lane: dir, position: Math.random()*-0.5, previousPos: Math.random()*-0.5, targetPos: 0,
        color: vehiclePalette[Math.floor(Math.random()*vehiclePalette.length)], type: 'car', leaving: false
      });
      laneVehicles = vehicles.filter(v => v.lane === dir && !v.leaving);
    }
    
    while (laneVehicles.length > laneInfo.count) {
      // Front vehicles leave
      laneVehicles.sort((a,b) => b.position - a.position);
      laneVehicles[0].leaving = true;
      laneVehicles.shift();
    }
    
    // Assign proper target positions for queueing
    laneVehicles.sort((a,b) => b.position - a.position);
    
    const isGreen = phase === 'NS' ? (dir === 'N' || dir === 'S') : (dir === 'E' || dir === 'W');
    
    laneVehicles.forEach((v, idx) => {
      v.previousPos = v.position;
      if (isGreen) {
        v.targetPos = 1.0; // move past intersection
        if (v.position > 0.8) v.leaving = true; 
      } else {
         // stack up based on idx
         const stopLine = 0.85;
         const spacing = 0.12; 
         v.targetPos = Math.max(0.0, stopLine - idx * spacing);
      }
    });
  });
}

function moveAndDrawVehicles(t) {
  // Move
  for (let i = vehicles.length - 1; i >= 0; i--) {
    const v = vehicles[i];
    
    if (v.leaving) {
      v.position += 0.05 * speed; // Exit speed
    } else {
      v.position = lerp(v.previousPos, Math.max(v.targetPos, v.previousPos), t);
    }
    
    if (v.position > 1.2) {
      vehicles.splice(i, 1); // remove
    } else {
      drawVehicle(v);
    }
  }
}

function drawVehicle(v) {
  if (v.position <= 0) return; // not entered yet
  
  let x, y, w, h;
  const cx = 250, cy = 250;
  const isVert = v.lane === 'N' || v.lane === 'S';
  
  if (isVert) { w = 10; h = 20; }
  else { w = 20; h = 10; }
  
  if (v.lane === 'N') {
    x = 225 - w/2; y = lerp(-20, cy, v.position);
  } else if (v.lane === 'S') {
    x = 275 - w/2; y = lerp(520, cy, v.position);
  } else if (v.lane === 'E') {
    x = lerp(520, cx, v.position); y = 225 - h/2;
  } else if (v.lane === 'W') {
    x = lerp(-20, cx, v.position); y = 275 - h/2;
  }
  
  ctx.fillStyle = v.color;
  ctx.beginPath();
  if (isVert) ctx.roundRect(x, y, w, h, 3);
  else ctx.roundRect(x, y, w, h, 3);
  ctx.fill();
  
  if (v.type === 'emergency') {
    ctx.fillStyle = (Date.now()%500<250) ? '#fff' : '#00f';
    ctx.beginPath(); ctx.arc(x+w/2, y+h/2, 3, 0, Math.PI*2); ctx.fill();
  }
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
  document.getElementById('tick-counter').innerText = `TICK ${frame.tick}`;
  document.getElementById('phase-timer').innerText = `Phase: ${frame.phase}-Green | ${frame.phase_timer}s active`;
  
  const badge = document.getElementById('algo-badge');
  badge.innerText = frame.algorithm;
  badge.className = `algorithm-badge badge-${frame.algorithm.toLowerCase()}`;
  
  const tbody = document.getElementById('lane-table-body');
  tbody.innerHTML = '';
  ['N', 'S', 'E', 'W'].forEach(dir => {
    const info = frame.lanes[dir];
    const tr = document.createElement('tr');
    let statusText = info.green ? '🟢 GREEN' : '🔴 RED';
    if (info.emergency) statusText += ' 🚑';
    if (info.blocked) statusText = '🚧 BLOCKED';
    
    tr.innerHTML = `
      <td>${dir}</td>
      <td>${info.count}</td>
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
}

function updateLog(frame) {
  if (!frame.log) return;
  const logDiv = document.getElementById('event-log');
  const line = document.createElement('div');
  line.className = 'log-line';
  
  if (frame.log.includes('SWITCH')) line.classList.add('yellow');
  if (frame.log.includes('EMERGENCY')) line.classList.add('red');
  if (frame.log.includes('Fish Swarm')) line.classList.add('cyan');
  if (frame.log.includes('AO*') || frame.log.includes('Blocked')) line.classList.add('orange');
  
  line.innerText = frame.log;
  logDiv.appendChild(line);
  
  while (logDiv.childElementCount > 200) {
    logDiv.removeChild(logDiv.firstChild);
  }
  logDiv.scrollTop = logDiv.scrollHeight;
}

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
