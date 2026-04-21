import asyncio
import copy
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

from traffic_controller.simulator import TrafficSimulator, FixedTimerSimulator
from traffic_controller.network import IntersectionNetwork
from traffic_controller.controller import AdaptiveController
from traffic_controller.utils.cost import cost
from traffic_controller.utils import cost as cost_module
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

# Initialize network, simulator and controller
network = IntersectionNetwork()
main_sim = TrafficSimulator(seed=42, grid_position=(0, 0))
network.add_junction("J1", main_sim)
simulator = main_sim  # backward-compatible alias
controller = AdaptiveController(simulator_ref=simulator)
active_junction_id = "J1"

# Simulation loop state
simulation_task: Optional[asyncio.Task] = None
is_paused = False
simulation_speed = 1.0

# FSO state
fso_executor_task: Optional[asyncio.Task] = None
fish_swarm_active = False

# Split-screen comparison state
split_mode: bool = False
fixed_sim: Optional[FixedTimerSimulator] = None

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
    type: str  # "emergency", "block", "surge"
    lane: Optional[str] = None

class ProfileRequest(BaseModel):
    profile: str  # "default", "morning_rush", "lunch", "evening_rush", "night"

class SplitRequest(BaseModel):
    enabled: bool

class SpeedRequest(BaseModel):
    speed: int = 5

class AddJunctionRequest(BaseModel):
    id: str
    position: list[int]
    connect_from: str
    exit_lane: str
    entry_lane: str

class FocusJunctionRequest(BaseModel):
    id: str

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

    # Cancel any running simulation first
    if simulation_task and not simulation_task.done():
        simulation_task.cancel()
        try:
            await simulation_task
        except asyncio.CancelledError:
            pass
        simulation_task = None

    async def pre_warm(n_ticks=50):
        for _ in range(n_ticks):
            focus_sim = network.junctions[active_junction_id]
            s = focus_sim.get_state()
            a = controller.decide(s)
            prev = copy.deepcopy(s)
            target_phase = None
            if a == Action.EMERGENCY_OVERRIDE:
                for lane_id, lane in s.lanes.items():
                    if lane.has_emergency:
                        target_phase = "NS" if lane_id in ("N", "S") else "EW"
                        break
            focus_sim.apply_action(a, target_phase)
            network.tick_all()
            new = focus_sim.get_state()
            controller.record_outcome(prev, new, False)

    async with sim_lock:
        simulation_speed = req.speed
        is_paused = False
        pause_event.set()  # Ensure not paused when starting
        await pre_warm()

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
    global simulator, controller, simulation_task, is_paused, current_session_id, session_frames, session_start_time, fixed_sim, split_mode, network, main_sim, active_junction_id

    # Always unblock pause_event so any waiting coroutine can exit
    pause_event.set()

    # Cancel running simulation without holding the lock (avoids deadlock)
    if simulation_task and not simulation_task.done():
        simulation_task.cancel()
        try:
            await simulation_task
        except asyncio.CancelledError:
            pass

    async with sim_lock:
        # Save current session if exists
        if current_session_id and session_frames:
            await save_session_to_db()

        network = IntersectionNetwork()
        main_sim = TrafficSimulator(seed=42, grid_position=(0, 0))
        network.add_junction("J1", main_sim)
        simulator = main_sim
        active_junction_id = "J1"
        controller = AdaptiveController(simulator_ref=simulator)
        is_paused = False
        simulation_task = None
        current_session_id = None
        session_frames = []
        session_start_time = None
        fixed_sim = None
        split_mode = False

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
        focus_sim = network.junctions.get(active_junction_id, simulator)
        stats = focus_sim.generate_report()
    return stats

@app.post("/api/inject")
async def inject_scenario(req: InjectRequest):
    async with sim_lock:
        focus_sim = network.junctions.get(active_junction_id, simulator)
        if req.type == "surge":
            for lane in focus_sim._state.lanes.values():
                addable = min(15, 30 - lane.vehicle_count)
                lane.vehicle_count += addable
                lane.intent_counts["straight"] += addable
        elif req.type == "emergency" and req.lane:
            if req.lane in focus_sim._state.lanes:
                focus_sim._state.lanes[req.lane].has_emergency = True
                focus_sim.emergency_active_lane = req.lane
                focus_sim.emergency_ticks_active = 0
        elif req.type == "block" and req.lane:
            if req.lane in focus_sim._state.lanes:
                focus_sim._state.lanes[req.lane].is_blocked = True
                focus_sim.block_duration[req.lane] = 40
        else:
            raise HTTPException(status_code=400, detail=f"Unknown inject type: {req.type}")
    return {"status": "injected", "type": req.type}

@app.post("/api/profile")
async def set_profile(req: ProfileRequest):
    """Set the traffic profile (time-of-day traffic patterns)."""
    async with sim_lock:
        network.junctions.get(active_junction_id, simulator).set_profile(req.profile)
    return {"status": "profile_set", "profile": req.profile}

@app.post("/api/speed")
async def update_speed(req: SpeedRequest):
    """Live-update the simulation speed without restarting."""
    global simulation_speed
    simulation_speed = max(1, req.speed)
    return {"status": "speed_updated", "speed": simulation_speed}

@app.post("/api/split")
async def toggle_split(req: SplitRequest):
    """Toggle split-screen comparison mode."""
    global split_mode, fixed_sim
    async with sim_lock:
        split_mode = req.enabled
        if req.enabled is True:
            fixed_sim = FixedTimerSimulator()
            raw_state = network.junctions.get(active_junction_id, simulator).get_raw_state()
            for lane_id, lane in raw_state.lanes.items():
                fixed_sim.state.lanes[lane_id].vehicle_count = lane.vehicle_count
                fixed_sim.state.lanes[lane_id].waiting_time = lane.waiting_time
            fixed_sim.state.current_phase = raw_state.current_phase
        else:
            split_mode = False
            fixed_sim = None
    return {"status": "split_mode", "enabled": split_mode}

@app.post("/api/junction/add")
async def add_junction(req: AddJunctionRequest):
    global network
    jid = req.id.strip()
    if not jid:
        raise HTTPException(status_code=400, detail="junction id required")
    if jid in network.junctions:
        raise HTTPException(status_code=400, detail="junction already exists")
    if len(req.position) != 2:
        raise HTTPException(status_code=400, detail="position must be [col,row]")
    async with sim_lock:
        new_sim = TrafficSimulator(seed=42, grid_position=(req.position[0], req.position[1]))
        network.add_junction(jid, new_sim)
        network.connect(req.connect_from, req.exit_lane, jid, req.entry_lane)
    return {"junction_id": jid, "total_junctions": len(network.junctions)}

@app.post("/api/junction/focus")
async def focus_junction(req: FocusJunctionRequest):
    global active_junction_id
    jid = req.id.strip()
    if jid not in network.junctions:
        raise HTTPException(status_code=404, detail="unknown junction")
    active_junction_id = jid
    return {"active_junction": active_junction_id}

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

@app.get("/api/replay/frames")
async def replay_frames_info():
    max_tick = 0
    if session_frames:
        max_tick = max((f.get("tick", 0) for f in session_frames), default=0)
    return {"total": len(session_frames), "duration_ticks": max_tick}

@app.get("/api/replay/frame/{index}")
async def replay_frame(index: int):
    if index < 0 or index >= len(session_frames):
        raise HTTPException(status_code=404, detail="Frame index out of range")
    return session_frames[index]

@app.get("/api/replay/frames/range")
async def replay_frames_range(start: int = 0, end: int = 100):
    start = max(0, start)
    end = max(start, end)
    return session_frames[start:end]

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
                  profile='default', fixed_data=None):
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
        "lane_intents": {
            lane_id: dict(lane.intent_counts)
            for lane_id, lane in state.lanes.items()
        },
        "exiting_vehicles": [
            {"from": v.entry_lane, "to": v.exit_lane, "intent": v.intent.value}
            for v in network.junctions.get(active_junction_id, simulator).exiting_vehicles
        ],
        "stats": {
            "avg_wait_current": round(avg_current, 1),
            "avg_wait_astar": round(avg_current, 1),
            "avg_wait_fixed": stats.get("fixed_timer_avg_wait", 0),
            "total_served": stats.get("total_vehicles_served", 0),
            "emergency_overrides": stats.get("emergency_override_count", 0),
        },
        "fish_swarm_active": fso_active,
        "log": reason,
        "profile": profile,
        "rl": {
            "q_table_size": len(controller.rl_agent.q_table),
            "epsilon": round(controller.rl_agent.epsilon, 4),
            "total_updates": controller.rl_agent.total_updates,
            "training_phase": "exploring" if controller.rl_agent.epsilon > 0.3 else "exploiting",
        },
        "live_params": {
            "beam_width": controller.params.get("beam_width", 5),
            "congestion_threshold": controller.params.get("congestion_threshold", 20),
            "emergency_penalty": cost_module._weights["emergency_penalty"],
        },
        "split_mode": split_mode,
        "fixed": fixed_data or {
            "phase": None,
            "lanes": {},
            "avg_wait": 0,
        },
        "network": network.get_network_state(),
        "active_junction": active_junction_id,
        "junction_count": len(network.junctions),
    }

    return frame

async def run_fso_optimized():
    """Run FSO in thread pool and return updated params."""
    global controller
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, controller.optimize_params)

async def simulation_loop(duration: int):
    global simulator, controller, is_paused, simulation_speed, fish_swarm_active
    global current_session_id, session_frames, session_start_time
    global fixed_sim, split_mode, active_junction_id

    loop = asyncio.get_event_loop()

    # Initialize session tracking
    current_session_id = str(uuid.uuid4())
    session_frames = []
    session_start_time = time.time()

    for tick in range(duration):
        # ==== PAUSE HANDLING ====
        # Wait until resume. Use a short-sleep polling approach so
        # CancelledError propagates cleanly on reset.
        while is_paused:
            await asyncio.sleep(0.05)

        # Before tick
        focus_sim = network.junctions.get(active_junction_id, simulator)
        prev_state = focus_sim.get_state_copy()

        # Decide and apply
        action = await loop.run_in_executor(
            None, controller.decide, prev_state
        )
        target_phase = None
        effective_algo = "Q_LEARNING"
        if action == Action.EMERGENCY_OVERRIDE:
            effective_algo = "EMERGENCY"
            for lane_id, lane in prev_state.lanes.items():
                if lane.has_emergency:
                    target_phase = "NS" if lane_id in ("N", "S") else "EW"
                    break
        async with sim_lock:
            focus_sim.apply_action(action, target_phase)
            arrivals_by_junction = network.tick_all()
            this_tick_arrivals = arrivals_by_junction.get(active_junction_id, {})

        # After tick — get new state
        new_state = focus_sim.get_state()

        # Check if emergency just cleared this tick
        emergency_cleared = (
            any(l.has_emergency for l in prev_state.lanes.values())
            and not any(l.has_emergency for l in new_state.lanes.values())
        )

        # Record outcome so Q-agent can learn
        controller.record_outcome(prev_state, new_state, emergency_cleared)
        cost_val = await loop.run_in_executor(None, cost, new_state, controller.params)

        # Handle FSO in thread pool (non-blocking)
        fso_active = False
        if tick > 0 and tick % controller._fso_interval == 0:
            fish_swarm_active = True
            fso_active = True
            # Run FSO in background task so broadcast continues at full speed
            asyncio.create_task(run_fso_async())

        async with sim_lock:
            frame_state = focus_sim.get_state()
            stats = focus_sim.generate_report()
            avg_current = focus_sim._current_avg_wait()
            params = controller.params
            current_profile = getattr(focus_sim, '_current_profile', 'default')

        # Handle fixed timer simulator for split-screen comparison
        fixed_data = None
        if split_mode and fixed_sim:
            fixed_sim.tick(this_tick_arrivals or {})
            f_state = fixed_sim.state
            fixed_data = {
                "lanes": {d: {
                    "count": lane.vehicle_count,
                    "wait": round(lane.waiting_time, 1),
                    "green": d in fixed_sim._get_green_lane_ids()
                } for d, lane in f_state.lanes.items()},
                "phase": f_state.current_phase,
                "avg_wait": round(fixed_sim.get_avg_wait(), 2)
            }

        # Logging payload
        log_msg = f"[{new_state.timestamp:04d}] {effective_algo} -> {action.name} cost={round(cost_val, 1) if cost_val else 0}"

        # Identify emergencies/blocks
        if action.name == "EMERGENCY_OVERRIDE":
            log_msg = f"[{new_state.timestamp:04d}] 🚑 EMERGENCY override -> {target_phase or new_state.current_phase}"
        elif effective_algo == "AO_STAR":
            log_msg = f"[{new_state.timestamp:04d}] 🚧 AO* -> Blocked lane replanning (cost={round(cost_val, 1)})"
        frame = _create_frame(
            frame_state, action, effective_algo, cost_val, fso_active, log_msg,
            stats, avg_current, current_profile,
            fixed_data=fixed_data
        )

        # Store frame for replay
        session_frames.append(frame)
        if len(session_frames) > 2000:
            session_frames.pop(0)

        await broadcast(frame)

        if fso_active:
            fso_log = f"[{new_state.timestamp:04d}] 🐟 Fish Swarm optimized: beam={params.get('beam_width', 5)}, threshold={params.get('congestion_threshold', 20)}"
            frame["log"] = fso_log
            await broadcast(frame)

        # Tick delay — 1 second divided by speed multiplier
        # speed=1 → 1.0s/tick, speed=5 → 0.2s/tick, speed=20 → 0.05s/tick
        await asyncio.sleep(1.0 / max(1, simulation_speed))

    # Save session on completion
    await save_session_to_db()

async def run_fso_async():
    """Run FSO in background and update flag when done."""
    global fish_swarm_active, controller
    try:
        result = await run_fso_optimized()
        if result:
            async with sim_lock:
                controller.apply_fish_swarm_result(result)
                state = simulator.get_state()
                stats = simulator.generate_report()
                avg_current = simulator._current_avg_wait()
            log_entry = (
                f"🐟 FSO applied: beam={result.get('beam_width')}, "
                f"threshold={result.get('congestion_threshold')}"
            )
            frame = _create_frame(
                state,
                Action.KEEP_PHASE,
                "FSO",
                cost(state, controller.params),
                True,
                log_entry,
                stats,
                avg_current,
                getattr(simulator, "_current_profile", "default"),
            )
            await broadcast(frame)
    finally:
        fish_swarm_active = False
