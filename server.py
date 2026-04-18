import asyncio
import json
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from traffic_controller.simulator import TrafficSimulator
from traffic_controller.controller import AdaptiveController
from traffic_controller.utils.cost import cost

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
active_connections: set[WebSocket] = set()

# Initialize simulator and controller
simulator = TrafficSimulator(seed=42)
controller = AdaptiveController(simulator_ref=simulator)

# Simulation loop state
simulation_task: Optional[asyncio.Task] = None
is_paused = False
simulation_speed = 1.0

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
    type: str # "emergency", "block", "surge"
    lane: Optional[str] = None

# Routes
@app.get("/", response_class=HTMLResponse)
async def get_index():
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
    return {"status": "paused" if is_paused else "resumed"}

@app.post("/api/reset")
async def reset_simulation():
    global simulator, controller, simulation_task, is_paused
    if simulation_task and not simulation_task.done():
        simulation_task.cancel()
        
    async with sim_lock:
        simulator = TrafficSimulator(seed=42)
        controller = AdaptiveController(simulator_ref=simulator)
        is_paused = False
        simulation_task = None
    return {"status": "reset"}

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
    return {"status": "injected"}

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

def _create_frame(state, action, effective_algo, cost_val, fso_active, reason, stats, avg_current):
    lanes = {}
    for d, lane in state.lanes.items():
        lanes[d] = {
            "count": lane.vehicle_count,
            "wait": round(lane.waiting_time, 1),
            "emergency": lane.has_emergency,
            "blocked": lane.is_blocked,
            "green": d in state.active_lanes
        }
    
    return {
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
        "log": reason
    }

async def simulation_loop(duration: int):
    global simulator, controller, is_paused, simulation_speed
    loop = asyncio.get_event_loop()
    
    for _ in range(duration):
        if is_paused:
            # Wait for unpause
            while is_paused:
                await asyncio.sleep(0.1)
                
        async with sim_lock:
            simulator.tick()
            state = simulator.get_state()
            
        # Offload heavy algorithmic CPU work
        action, target_phase, effective_algo = await loop.run_in_executor(
            None, controller.decide, state
        )
        cost_val = await loop.run_in_executor(None, cost, state, controller.params)
        
        fso_active = False
        if state.timestamp > 0 and state.timestamp % controller._fso_interval == 0:
            fso_active = True
            await loop.run_in_executor(None, controller.optimize_params)
        
        async with sim_lock:
            simulator.apply_action(action, target_phase)
            frame_state = simulator.get_state()
            stats = simulator.generate_report()
            avg_current = simulator._current_avg_wait()
            params = controller.params
            
        # Logging payload
        log_msg = f"[{state.timestamp:04d}] {effective_algo} -> {action.name} cost={round(cost_val, 1) if cost_val else 0}"
        
        # Identify emergencies/blocks
        if action.name == "EMERGENCY_OVERRIDE":
            log_msg = f"[{state.timestamp:04d}] 🚑 EMERGENCY override -> {target_phase or state.current_phase}"
        elif effective_algo == "AO_STAR":
            log_msg = f"[{state.timestamp:04d}] 🚧 AO* -> Blocked lane replanning (cost={round(cost_val, 1)})"

        frame = _create_frame(frame_state, action, effective_algo, cost_val, fso_active, log_msg, stats, avg_current)
        await broadcast(frame)
        
        if fso_active:
            fso_log = f"[{state.timestamp:04d}] 🐟 Fish Swarm optimized: beam={params.get('beam_width', 5)}, threshold={params.get('congestion_threshold', 20)}"
            frame["log"] = fso_log
            await broadcast(frame)
            
        # Tick delay
        await asyncio.sleep(1.0 / simulation_speed)
