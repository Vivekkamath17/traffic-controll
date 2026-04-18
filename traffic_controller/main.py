"""
main.py
-------
Entry point for the Smart Adaptive Traffic Signal Controller simulation.

Runs a 500-tick simulation, printing a live dashboard every 10 ticks,
then prints a final comparison report and exports results.json.

Usage
-----
    python -m traffic_controller.main
  or:
    python main.py          (from the traffic_controller/ directory)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Force UTF-8 on Windows consoles (PowerShell / cmd default to cp1252)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Allow running directly without installing the package
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from traffic_controller.algorithms.bfs import bfs_baseline, fixed_timer_avg_wait
from traffic_controller.controller import AdaptiveController
from traffic_controller.models import Action
from traffic_controller.simulator import TrafficSimulator
from traffic_controller.utils.cost import cost as compute_cost
from traffic_controller.utils.report import ReportGenerator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOTAL_TICKS: int = 500
DASHBOARD_INTERVAL: int = 10      # Print every N ticks
RESULTS_PATH: Path = Path("results.json")


# ---------------------------------------------------------------------------
# Dashboard rendering
# ---------------------------------------------------------------------------

def _lane_status(direction: str, phase: str, has_emergency: bool, is_blocked: bool) -> str:
    """Build the ASCII status cell for a lane row."""
    in_green = direction in list(phase)
    signal = "[GREEN]" if in_green else "[RED]  "

    extras = []
    if has_emergency:
        extras.append("[EMG]")
    if is_blocked:
        extras.append("[BLK]")

    suffix = " " + " ".join(extras) if extras else ""
    return f"{signal}{suffix}"


def print_dashboard(state, algorithm: str, tick_cost: float) -> None:
    """
    Print the live traffic dashboard to stdout.

    Parameters
    ----------
    state : TrafficState
    algorithm : str
    tick_cost : float
    """
    phase = state.current_phase
    dur   = state.phase_duration
    W     = 58      # total table width

    phase_label = "NS-Green" if phase == "NS" else "EW-Green"

    def row(left, right=""):
        print(f"+{left}+{right}" if right else f"+{left}+")

    bar  = "-" * (W - 2)
    hdr  = f"  TICK {state.timestamp:<4}  |  Phase: {phase_label:<9}  |  t={dur:<3}s  "
    cols = f" {'Lane':<5} | {'Cars':<6} | {'Wait(s)':<8} | {'Status':<19}"
    div  = f"-{'-'*5}-+-{'-'*6}-+-{'-'*8}-+-{'-'*19}-"

    print("+" + bar + "+")
    print(f"|{hdr:<{W-2}}|")
    print("+" + div + "+")
    print(f"|{cols:<{W-2}}|")
    print("+" + div + "+")

    for direction in ["N", "S", "E", "W"]:
        lane   = state.lanes[direction]
        status = _lane_status(direction, phase, lane.has_emergency, lane.is_blocked)
        cell   = f" {direction:<5} | {lane.vehicle_count:<6} | {lane.waiting_time:>7.1f}  | {status:<19}"
        print(f"|{cell:<{W-2}}|")

    algo_line = f"  Algorithm: {algorithm} | Cost: {tick_cost:,.1f}"
    print("+" + "-" * (W - 2) + "+")
    print(f"|{algo_line:<{W-2}}|")
    print("+" + "-" * (W - 2) + "+")
    print()


# ---------------------------------------------------------------------------
# Main simulation loop
# ---------------------------------------------------------------------------

def run_simulation(total_ticks: int = TOTAL_TICKS) -> None:
    """
    Execute the full simulation and print results.

    Parameters
    ----------
    total_ticks : int
        Number of simulation steps.
    """
    print("\n" + "=" * 55)
    print("  Smart Adaptive Traffic Signal Controller")
    print(f"  Simulation: {total_ticks} ticks")
    print("=" * 55 + "\n")

    sim = TrafficSimulator(seed=42)
    controller = AdaptiveController(simulator_ref=sim)
    reporter = ReportGenerator(tick_count=total_ticks)

    bfs_total_cost: float = 0.0
    bfs_samples: int = 0

    start_wall = time.time()

    for tick in range(1, total_ticks + 1):

        # -------------------------------------------------------------
        # 1. Advance environment
        # -------------------------------------------------------------
        prev_served = sim.generate_report()["total_vehicles_served"]
        sim.tick()
        state = sim.get_state()
        curr_served = sim.generate_report()["total_vehicles_served"]
        tick_served = curr_served - prev_served

        # -------------------------------------------------------------
        # 2. Controller decides
        # -------------------------------------------------------------
        action, target_phase, algorithm = controller.decide(state)

        # -------------------------------------------------------------
        # 3. Apply action
        # -------------------------------------------------------------
        sim.apply_action(action, target_phase)

        # Track emergency overrides for reporter
        if action == Action.EMERGENCY_OVERRIDE:
            reporter.record_emergency()

        # -------------------------------------------------------------
        # 4. BFS baseline sample (every 20 ticks to avoid slowdown)
        # -------------------------------------------------------------
        if tick % 20 == 0:
            _, bfs_cost = bfs_baseline(state, max_depth=4)
            bfs_total_cost += bfs_cost
            bfs_samples += 1

        # -------------------------------------------------------------
        # 5. Fixed-timer baseline
        # -------------------------------------------------------------
        fixed_avg = fixed_timer_avg_wait(state, phase_duration=30)
        reporter.record_fixed_timer(fixed_avg)

        # -------------------------------------------------------------
        # 6. Record statistics
        # -------------------------------------------------------------
        # Per-tick incremental wait (vehicles × 1 s for red lanes, 0 for green).
        # This is a bounded, comparable metric that works across both adaptive
        # and fixed-timer baselines without cumulative drift.
        active = list(state.current_phase)   # e.g. ['N', 'S'] for 'NS'
        lane_waits = {
            d: float(lane.vehicle_count) if d not in active else 0.0
            for d, lane in state.lanes.items()
        }
        vehicles_cleared = sim.generate_report()["total_vehicles_served"]

        tick_cost = compute_cost(state, controller.params)

        reporter.record_tick(
            algorithm=algorithm,
            lane_waits=lane_waits,
            vehicles_served=tick_served,
        )

        # -------------------------------------------------------------
        # 7. FSO parameter optimization every 100 ticks
        # -------------------------------------------------------------
        optimized = controller.maybe_optimize(tick)
        if optimized:
            print(f"  [FSO] Parameters updated at tick {tick}")

        # -------------------------------------------------------------
        # 8. Live dashboard every DASHBOARD_INTERVAL ticks
        # -------------------------------------------------------------
        if tick % DASHBOARD_INTERVAL == 0:
            print_dashboard(state, algorithm, tick_cost)

    # -----------------------------------------------------------------
    # Final report
    # -----------------------------------------------------------------
    elapsed = time.time() - start_wall
    sim_report = sim.generate_report()

    stats = reporter.generate(
        algorithm_usage=controller.logger.algorithm_usage()
    )
    # Inject BFS average if we have samples
    if bfs_samples > 0:
        bfs_avg = round(bfs_total_cost / bfs_samples / 1000, 2)  # normalise
        stats["per_algorithm_avg_wait_sec"]["BFS"] = bfs_avg

    reporter.print_comparison_table(stats)

    print(f"  Wall-clock time: {elapsed:.1f}s for {total_ticks} ticks")
    print(f"  Vehicles served: {sim_report['total_vehicles_served']}")
    print(f"  Emergency overrides: {sim_report['emergency_override_count']}\n")

    # Export JSON results
    reporter.export_json(RESULTS_PATH, stats)

    # Export decision log
    log_path = Path("decision_log.json")
    controller.logger.export_json(log_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_simulation()
