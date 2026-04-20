# Smart Adaptive Traffic Signal Controller

A Python simulation of a 4-way traffic intersection that uses **hybrid AI algorithms** to dynamically control signal phases, minimise waiting time, prevent lane starvation, and prioritise emergency vehicles.

![Traffic Controller Demo](demo.gif)  <!-- Record your own with any screen-to-GIF tool -->

## Architecture

```
User Request → AdaptiveController.select_algorithm()
    → Emergency? → EmergencyOverride
    → Blocked?   → AO* Search
    → Congested? → Beam Search
    → Normal      → A* Search
All paths → apply_action() → TrafficSimulator.tick()
Every 100 ticks → FishSwarm.run() → update params
```

---

## Project Structure

```
traffic_controller/
├── main.py                  # Entry point — runs 500-tick simulation
├── models.py                # Core data structures (TrafficState, LaneState, Action)
├── simulator.py             # TrafficSimulator (Poisson arrivals, events, profiles)
├── controller.py            # AdaptiveController (algorithm routing + FSO)
├── algorithms/
│   ├── astar.py             # A* Search (normal traffic)
│   ├── beam_search.py       # Beam Search (peak/congested traffic)
│   ├── ao_star.py           # AO* AND-OR tree (blocked lane/accident)
│   ├── bfs.py               # BFS baseline + fixed-timer comparison
│   └── emergency.py         # Emergency vehicle override
├── optimization/
│   └── fish_swarm.py        # Fish Swarm Optimization (parameter tuning)
├── utils/
    ├── cost.py              # Cost function & heuristic
    ├── logger.py            # Decision logger
    └── report.py            # Statistics & comparison report

# Web Application
server.py                    # FastAPI server & WebSocket broadcaster
database.py                  # SQLite session history
static/
├── index.html               # Frontend dashboard layout
├── style.css                # Dark/Light theme styling
└── app.js                   # Canvas rendering, UI updates, sound effects

tests/
├── test_cost.py             # Unit tests for cost function and heuristic
├── test_astar.py            # Unit tests for A* algorithm
├── test_beam_search.py      # Unit tests for Beam Search
├── test_ao_star.py          # Unit tests for AO*
├── test_fish_swarm.py       # Unit tests for Fish Swarm Optimization
└── test_simulator.py        # Unit tests for TrafficSimulator

# Docker
docker-compose.yml           # Docker Compose configuration
Dockerfile                   # Docker image definition
.dockerignore                # Docker ignore patterns
```

---

## Quick Start

### Local Development (3 commands)

```bash
pip install -r requirements.txt
uvicorn server:app --reload --port 8000
# open http://localhost:8000
```

### Docker Start (2 commands)

```bash
docker-compose up --build
# open http://localhost:8000
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
- **New Features**:
  - 🌙/☀️ **Dark/Light theme toggle** in header
  - 🔊/🔇 **Sound effects** (phase switch, emergency siren)
  - 🌡️ **Heatmap overlay** showing congestion by lane
  - ⏮️ **Session replay** with scrub bar
  - ⚡ **Split-screen comparison** (Adaptive AI vs Fixed Timer)
  - 📊 **Export chart as PNG**
  - 📋 **Past runs history** panel

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

### 3. Running Tests

```bash
pytest tests/ -v --tb=short
```

---

## Algorithm Summary

| Algorithm | When used | Complexity | Tunable params |
|-----------|-----------|------------|----------------|
| **Emergency Override** | Any lane has `has_emergency=True` | O(1) | `EMERGENCY_GREEN_DURATION=30s` |
| **AO\*** | Any lane `is_blocked=True` | O(b^d) | `max_depth=6` |
| **Beam Search** | `vehicle_count ≥ 20` or total `> 60` | O(k·b·d) | `beam_width`, `lookahead=10` |
| **A\*** | Default (light/moderate traffic) | O(b^d) | `max_depth=10` |
| **BFS** | Baseline comparison only | O(b^d) | `max_depth=5` |

---

## Performance Results

| Mode | Avg Wait | vs Fixed Timer |
|------|----------|----------------|
| Adaptive AI (A*/Beam/AO*) | ~25-35s | **~40-50% faster** |
| Fixed Timer (30s) | ~45-60s | Baseline |

*Results from 500-tick simulations with default traffic profile*

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

## Requirements

```
fastapi
uvicorn[standard]
websockets
numpy
pytest
```

Install with:
```bash
pip install -r requirements.txt
```

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
