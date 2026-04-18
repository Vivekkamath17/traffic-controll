# Smart Adaptive Traffic Signal Controller

A Python simulation of a 4-way traffic intersection that uses **hybrid AI algorithms** to dynamically control signal phases, minimise waiting time, prevent lane starvation, and prioritise emergency vehicles.

---

## Project Structure

```
traffic_controller/
├── main.py                  # Entry point — runs 500-tick simulation
├── models.py                # Core data structures (TrafficState, LaneState, Action)
├── simulator.py             # TrafficSimulator (Poisson arrivals, events)
├── controller.py            # AdaptiveController (algorithm routing + FSO)
├── algorithms/
│   ├── astar.py             # A* Search (normal traffic)
│   ├── beam_search.py       # Beam Search (peak/congested traffic)
│   ├── ao_star.py           # AO* AND-OR tree (blocked lane/accident)
│   ├── bfs.py               # BFS baseline + fixed-timer comparison
│   └── emergency.py         # Emergency vehicle override
├── optimization/
│   └── fish_swarm.py        # Fish Swarm Optimization (parameter tuning)
└── utils/
    ├── cost.py              # Cost function & heuristic
    ├── logger.py            # Decision logger
    └── report.py            # Statistics & comparison report

# Web Application
server.py                    # FastAPI server & WebSocket broadcaster
static/
├── index.html               # Frontend dashboard layout
├── style.css                # Dark mode styling
└── app.js                   # Canvas rendering & UI updates

tests/
├── test_cost.py             # Unit tests for cost function and heuristic
└── test_algorithms.py       # Unit tests for all algorithm modules
```

---

## Requirements

- **Python 3.10+**
- **FastAPI**, **Uvicorn**, **WebSockets**, **NumPy**

Install dependencies:

```bash
pip install fastapi uvicorn[standard] websockets numpy
```

---

## Running the Simulation

You can run the simulation using the new **Web Dashboard**, or the classic **CLI mode**.

### 1. Web Dashboard (Recommended)

From the project root:

```bash
python -m uvicorn server:app --port 8000
```

- Open `http://localhost:8000` in your web browser.
- Click **Start** to run the live canvas animation.
- View real-time charts, inject emergencies, and manually control the simulation speed (1x to 20x).

### 2. Command-Line Mode

From the project root:

```bash
python -m traffic_controller.main
```

The CLI simulation will:
1. Print a **live dashboard** every 10 ticks showing lane status, algorithm used, and cost.
2. Run **Fish Swarm Optimization** every 100 ticks to auto-tune parameters.
3. Print a **final comparison table** after 500 ticks.
4. Export `results.json` and `decision_log.json` to the working directory.

---

## Running the Tests

From the project root:

```bash
# Install pytest if needed
pip install pytest

# Run all tests
python -m pytest tests/ -v
```

---

## Algorithm Overview

| Condition | Algorithm | Notes |
|---|---|---|
| Any emergency vehicle | **Emergency Override** | Immediate 30-second green for affected phase |
| Any blocked lane | **AO\*** | AND-OR tree decomposes blocked vs. accessible lanes |
| `vehicle_count ≥ 20` or total `> 60` | **Beam Search** | Width-5 beam, 10-step lookahead |
| Normal traffic | **A\* Search** | Priority-queue search with admissible heuristic |
| Baseline comparison | **BFS** | Exhaustive depth-5 search, not used for real-time |

---

## Cost Function

```
cost(state) =
    Σ (vehicle_count × waiting_time)        # congestion
  + 10 000 × any_emergency                  # emergency penalty
  + 500 × Σ max(0, waiting_time − 120)      # starvation penalty
  − 200 × vehicles_in_green_lanes           # throughput reward
```

The **heuristic** estimates future cost using:
- Projected vehicle arrivals (Poisson λ × lookahead seconds)
- Starvation lookahead
- Phase-oscillation penalty (discourages rapid switching)

---

## Fish Swarm Optimization

Tunes 6 controller parameters every 100 ticks:

| Parameter | Range | Description |
|---|---|---|
| `min_phase_duration` | 10–25 s | Minimum time before a phase switch is allowed |
| `max_phase_duration` | 30–90 s | Maximum green time before forced switch |
| `emergency_penalty_weight` | 5 000–20 000 | Cost multiplier for emergency flags |
| `starvation_threshold` | 60–180 s | Wait time that triggers starvation penalty |
| `beam_width` | 3–10 | Beam Search width k |
| `congestion_threshold` | 15–25 | Vehicle count that triggers Beam Search |

Three fish behaviours per iteration:
- **Prey** — random local perturbation (exploration)
- **Swarm** — move toward group centre if fitter
- **Follow** — move toward best-known fish

---

## Output Files

| File | Description |
|---|---|
| `results.json` | Final statistics (algorithm usage, wait times, improvement) |
| `decision_log.json` | Full per-tick decision log with reason and cost |

---

## Sample Dashboard Output

```
┌─────────────────────────────────────────────────────┐
│  TICK 140  │  Phase: NS-Green  │  t=35s             │
├───────┬────────┬─────────┬──────────────────────────┤
│ Lane  │ Cars   │ Wait(s) │ Status                   │
├───────┼────────┼─────────┼──────────────────────────┤
│  N    │  12    │  48.2   │ 🟢 GREEN                 │
│  S    │   7    │  31.0   │ 🟢 GREEN                 │
│  E    │  19    │  72.5   │ 🔴 RED                   │
│  W    │   3    │  12.1   │ 🔴 RED   + 🚑            │
├───────────────────────────────────────────────────────┤
│ Algorithm: ASTAR | Cost: 4821.3                      │
└──────────────────────────────────────────────────────┘
```
