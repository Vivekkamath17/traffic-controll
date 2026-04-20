import asyncio
import json
import threading
import time
import uuid
from typing import Optional, Dict, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from traffic_controller.simulator import TrafficSimulator
from traffic_controller.controller import AdaptiveController
from traffic_controller.utils.cost import cost
from traffic_controller.models import Action

# Import database module (will be created)
try:
    from database import save_session, get_sessions, get_session
except ImportError:
    # Placeholder functions if database.py doesn't exist yet
    def save_session(*args, **kwargs): pass
    def get_sessions(): return []
    def get_session(session_id): return None

app = FastAPI(title="Smart Adaptive Traffic Controller")

# Setup CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Globals
sim_lock = asyncio.Lock()
pause_event = asyncio.Event()
pause_event.set()  # Not paused initially
active_connections: set[WebSocket] = set()

# Initialize simulator and controller
simulator = TrafficSimulator(seed=42)
controller = AdaptiveController(simulator_ref=simulator)

# Simulation loop state
simulation_task: Optional[asyncio.Task] = None
is_paused = False
simulation_speed = 1.0

# FSO state
fso_executor_task: Optional[asyncio.Task] = None
fish_swarm_active = False

# Split-screen comparison state
split_mode_enabled = False
fixed_simulator: Optional[TrafficSimulator] = None
fixed_controller: Optional[AdaptiveController] = None

# Session tracking for history
session_frames: list[Dict[str, Any]] = []
current_session_id: Optional[str] = None
session_start_time: Optional[float] = None

# Models
class StartRequest(BaseModel):
    duration: int = 500
    speed: int = 5

class ParamsRequest(BaseModel):
    beam_width: Optional[int] = None
    congestion_threshold: Optional[int] = None
    emergency_penalty: Optional[int] = None
    starvation_threshold: Optional[int] = None

class InjectRequest(BaseModel):
    type: str  # "emergency", "block", "surge", "pedestrian"
    lane: Optional[str] = None

class ProfileRequest(BaseModel):
    profile: str  # "default", "morning_rush", "lunch", "evening_rush", "night"

class SplitRequest(BaseModel):
    enabled: bool

# Routes
@app.get("/", response_class=HTMLResponse)
async def get_index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/event-log", response_class=HTMLResponse)
async def get_event_log_page():
    # Serve the SPA entrypoint; frontend routing handles the view
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/start")
async def start_simulation(req: StartRequest):
    global simulation_task, is_paused, simulation_speed
    async with sim_lock:
        simulation_speed = req.speed
        is_paused = False
        
    if simulation_task is None or simulation_task.done():
        simulation_task = asyncio.create_task(simulation_loop(req.duration))
    return {"status": "started", "session_id": "session_1"}

@app.post("/api/pause")
async def pause_simulation():
    global is_paused
    async with sim_lock:
        is_paused = not is_paused
        if is_paused:
            pause_event.clear()  # Pause
        else:
            pause_event.set()    # Resume
    return {"status": "paused" if is_paused else "resumed"}

@app.post("/api/reset")
async def reset_simulation():
    global simulator, controller, simulation_task, is_paused, current_session_id, session_frames, session_start_time

    # If paused, clear pause event first
    if is_paused:
        pause_event.set()

    # Acquire lock to ensure no tick is running
    async with sim_lock:
        if simulation_task and not simulation_task.done():
            simulation_task.cancel()
            try:
                await simulation_task
            except asyncio.CancelledError:
                pass

        # Save current session if exists
        if current_session_id and session_frames:
            await save_session_to_db()

        simulator = TrafficSimulator(seed=42)
        controller = AdaptiveController(simulator_ref=simulator)
        is_paused = False
        simulation_task = None
        current_session_id = None
        session_frames = []
        session_start_time = None

    return {"status": "reset"}

async def save_session_to_db():
    """Save the current session to the database."""
    global current_session_id, session_frames, session_start_time, simulator, controller

    if not current_session_id or not session_start_time:
        return

    stats = simulator.generate_report()
    duration = time.time() - session_start_time

    session_data = {
        "id": current_session_id,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(session_start_time)),
        "duration": int(duration),
        "avg_wait_astar": stats.get("recent_avg_wait", 0),
        "avg_wait_beam": stats.get("recent_avg_wait", 0),  # Same for now
        "avg_wait_fixed": stats.get("fixed_timer_avg_wait", 0),
        "total_served": stats.get("total_vehicles_served", 0),
        "emergency_overrides": stats.get("emergency_override_count", 0),
        "profile": getattr(simulator, '_current_profile', 'default'),
        "results_json": json.dumps({
            "frames": session_frames,
            "final_stats": stats
        })
    }

    # Run in executor to avoid blocking
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, save_session, current_session_id, session_data)

@app.post("/api/params")
async def update_params(req: ParamsRequest):
    async with sim_lock:
        if req.beam_width is not None:
            controller.params["beam_width"] = req.beam_width
        if req.congestion_threshold is not None:
            controller.params["congestion_threshold"] = req.congestion_threshold
        if req.emergency_penalty is not None:
            controller.params["emergency_penalty"] = req.emergency_penalty
        if req.starvation_threshold is not None:
            controller.params["max_phase_duration"] = req.starvation_threshold
    return {"status": "updated", "params": controller.params}

@app.get("/api/stats")
async def get_stats():
    async with sim_lock:
        stats = simulator.generate_report()
    return stats

@app.post("/api/inject")
async def inject_scenario(req: InjectRequest):
    async with sim_lock:
        if req.type == "surge":
            for lane in simulator._state.lanes.values():
                lane.vehicle_count = min(30, lane.vehicle_count + 15)
        elif req.type == "emergency" and req.lane:
            if req.lane in simulator._state.lanes:
                simulator._state.lanes[req.lane].has_emergency = True
        elif req.type == "block" and req.lane:
            if req.lane in simulator._state.lanes:
                simulator._state.lanes[req.lane].is_blocked = True
        elif req.type == "pedestrian":
            simulator._state.pedestrian_waiting = True
            # Also handle fixed simulator if in split mode
            if fixed_simulator:
                fixed_simulator._state.pedestrian_waiting = True
        else:
            raise HTTPException(status_code=400, detail=f"Unknown inject type: {req.type}")
    return {"status": "injected", "type": req.type}

@app.post("/api/profile")
async def set_profile(req: ProfileRequest):
    """Set the traffic profile (time-of-day traffic patterns)."""
    async with sim_lock:
        simulator.set_profile(req.profile)
        if fixed_simulator:
            fixed_simulator.set_profile(req.profile)
    return {"status": "profile_set", "profile": req.profile}

@app.post("/api/split")
async def toggle_split(req: SplitRequest):
    """Toggle split-screen comparison mode."""
    global split_mode_enabled, fixed_simulator, fixed_controller
    async with sim_lock:
        split_mode_enabled = req.enabled
        if req.enabled:
            # Create fixed timer simulator
            fixed_simulator = TrafficSimulator(seed=42)
            fixed_controller = AdaptiveController(simulator_ref=fixed_simulator)
            # Copy current state
            fixed_simulator._state = simulator.get_state()
        else:
            fixed_simulator = None
            fixed_controller = None
    return {"status": "split_mode", "enabled": split_mode_enabled}

@app.get("/api/history")
async def get_history():
    """Get list of all past simulation sessions."""
    sessions = get_sessions()
    return sessions

@app.get("/api/history/{session_id}")
async def get_history_detail(session_id: str):
    """Get full details of a specific session."""
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session

# Websocket Management
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    try:
        # Keep connection open
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.discard(websocket)
    except Exception:
        active_connections.discard(websocket)

async def broadcast(frame: dict):
    global active_connections
    dead = set()
    for ws in list(active_connections):
        try:
            await ws.send_json(frame)
        except Exception:
            dead.add(ws)
    active_connections -= dead

def _create_frame(state, action, effective_algo, cost_val, fso_active, reason, stats, avg_current,
                  profile='default', pedestrian_waiting=False, fixed_data=None):
    lanes = {}
    for d, lane in state.lanes.items():
        lanes[d] = {
            "count": lane.vehicle_count,
            "wait": round(lane.waiting_time, 1),
            "emergency": lane.has_emergency,
            "blocked": lane.is_blocked,
            "green": d in state.active_lanes
        }

    frame = {
        "tick": state.timestamp,
        "phase": state.current_phase,
        "phase_timer": state.phase_duration,
        "algorithm": effective_algo,
        "cost": round(cost_val, 1) if cost_val else 0.0,
        "action": action.name if action else "KEEP",
        "lanes": lanes,
        "stats": {
            "avg_wait_current": round(avg_current, 1),
            "avg_wait_fixed": stats.get("fixed_timer_avg_wait", 0),
            "total_served": stats.get("total_vehicles_served", 0),
            "emergency_overrides": stats.get("emergency_override_count", 0),
        },
        "fish_swarm_active": fso_active,
        "log": reason,
        "profile": profile,
        "pedestrian_waiting": pedestrian_waiting or action.name == "PEDESTRIAN_PHASE"
    }

    # Include fixed timer data for split-screen mode
    if fixed_data:
        frame["fixed"] = fixed_data

    return frame

async def run_fso_optimized():
    """Run FSO in thread pool and return updated params."""
    global controller
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, controller.optimize_params)

async def simulation_loop(duration: int):
    global simulator, controller, is_paused, simulation_speed, fish_swarm_active
    global current_session_id, session_frames, session_start_time
    global fixed_simulator, fixed_controller, split_mode_enabled

    loop = asyncio.get_event_loop()

    # Initialize session tracking
    current_session_id = str(uuid.uuid4())
    session_frames = []
    session_start_time = time.time()

    for tick in range(duration):
        # Wait if paused (using Event for cleaner async handling)
        if is_paused:
            await pause_event.wait()

        # Acquire lock for tick processing
        async with sim_lock:
            simulator.tick()
            state = simulator.get_state()

        # Offload heavy algorithmic CPU work to thread pool
        action, target_phase, effective_algo = await loop.run_in_executor(
            None, controller.decide, state
        )
        cost_val = await loop.run_in_executor(None, cost, state, controller.params)

        # Handle FSO in thread pool (non-blocking)
        fso_active = False
        if tick > 0 and tick % controller._fso_interval == 0:
            fish_swarm_active = True
            fso_active = True
            # Run FSO in background task so broadcast continues at full speed
            asyncio.create_task(run_fso_async())

        async with sim_lock:
            simulator.apply_action(action, target_phase)
            frame_state = simulator.get_state()
            stats = simulator.generate_report()
            avg_current = simulator._current_avg_wait()
            params = controller.params
            current_profile = getattr(simulator, '_current_profile', 'default')

        # Handle fixed timer simulator for split-screen comparison
        fixed_data = None
        if split_mode_enabled and fixed_simulator:
            # Run fixed timer tick
            fixed_simulator.tick()
            f_state = fixed_simulator.get_state()
            # Fixed timer simply alternates every 30 seconds
            if f_state.phase_duration >= 30:
                fixed_simulator.apply_action(Action.SWITCH_PHASE, None)
            fixed_stats = fixed_simulator.generate_report()
            fixed_data = {
                "lanes": {d: {
                    "count": lane.vehicle_count,
                    "wait": round(lane.waiting_time, 1),
                    "emergency": lane.has_emergency,
                    "blocked": lane.is_blocked,
                    "green": d in f_state.active_lanes
                } for d, lane in f_state.lanes.items()},
                "phase": f_state.current_phase,
                "avg_wait": fixed_stats.get("recent_avg_wait", 0)
            }

        # Logging payload
        log_msg = f"[{state.timestamp:04d}] {effective_algo} -> {action.name} cost={round(cost_val, 1) if cost_val else 0}"

        # Identify emergencies/blocks
        if action.name == "EMERGENCY_OVERRIDE":
            log_msg = f"[{state.timestamp:04d}] 🚑 EMERGENCY override -> {target_phase or state.current_phase}"
        elif effective_algo == "AO_STAR":
            log_msg = f"[{state.timestamp:04d}] 🚧 AO* -> Blocked lane replanning (cost={round(cost_val, 1)})"
        elif action.name == "PEDESTRIAN_PHASE":
            log_msg = f"[{state.timestamp:04d}] 🚶 Pedestrian crossing phase"

        frame = _create_frame(
            frame_state, action, effective_algo, cost_val, fso_active, log_msg,
            stats, avg_current, current_profile, pedestrian_waiting=state.pedestrian_waiting,
            fixed_data=fixed_data
        )

        # Store frame for replay
        session_frames.append(frame)
        if len(session_frames) > 2000:
            session_frames.pop(0)

        await broadcast(frame)

        if fso_active:
            fso_log = f"[{state.timestamp:04d}] 🐟 Fish Swarm optimized: beam={params.get('beam_width', 5)}, threshold={params.get('congestion_threshold', 20)}"
            frame["log"] = fso_log
            await broadcast(frame)

        # Tick delay
        await asyncio.sleep(1.0 / simulation_speed)

    # Save session on completion
    await save_session_to_db()

async def run_fso_async():
    """Run FSO in background and update flag when done."""
    global fish_swarm_active, controller
    try:
        await run_fso_optimized()
    finally:
        fish_swarm_active = False
