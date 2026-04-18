"""
bfs.py
------
Breadth-First Search — baseline comparison only.

BFS exhaustively explores all possible phase sequences up to a fixed
depth without any heuristic guidance. It is intentionally inefficient
and should NOT be used for real-time decisions.

Purpose: generate a baseline "optimal-without-heuristic" cost for
         comparison reports (simulates a naive fixed-timer controller).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from traffic_controller.models import Action, TrafficState
from traffic_controller.utils.cost import cost


# ---------------------------------------------------------------------------
# BFS node
# ---------------------------------------------------------------------------

@dataclass
class _BFSNode:
    """
    A node in the BFS frontier.

    Attributes
    ----------
    state : TrafficState
        Snapshot at this node.
    actions : list[str]
        Action sequence taken to reach this node.
    total_cost : float
        Accumulated cost from root to this node.
    depth : int
        Distance from the root.
    """

    state: TrafficState
    actions: List[str] = field(default_factory=list)
    total_cost: float = 0.0
    depth: int = 0


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _apply_action(state: TrafficState, action_name: str) -> TrafficState:
    """
    Return the successor state for a given action.

    Parameters
    ----------
    state : TrafficState
    action_name : str

    Returns
    -------
    TrafficState
    """
    next_state = state.clone()
    next_state.timestamp += 1

    if action_name == "SWITCH_PHASE":
        next_state.current_phase = state.opposite_phase()
        next_state.phase_duration = 0
    else:
        next_state.phase_duration += 1

    for direction in next_state.active_lanes:
        lane = next_state.lanes.get(direction)
        if lane and lane.vehicle_count > 0 and not lane.is_blocked:
            lane.vehicle_count = max(0, lane.vehicle_count - 1)

    for direction, lane in next_state.lanes.items():
        if direction not in next_state.active_lanes:
            lane.waiting_time += 1.0

    return next_state


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def bfs_baseline(
    initial_state: TrafficState,
    max_depth: int = 5,
    params: Dict | None = None,
) -> Tuple[Action, float]:
    """
    Run BFS up to *max_depth* and return the best action and its cost.

    All possible phase sequences up to *max_depth* steps are explored.
    The sequence with the minimum TOTAL accumulated cost is selected.

    NOTE: This function is for **baseline comparison only**.
          It is not suitable for real-time use due to exponential blowup.

    Parameters
    ----------
    initial_state : TrafficState
        Current intersection snapshot.
    max_depth : int
        Maximum depth to explore (default 5).
    params : dict, optional
        Forwarded to the cost function.

    Returns
    -------
    tuple[Action, float]
        (best_action, best_total_cost) — the first action of the cheapest
        sequence found, and its associated cumulative cost.
    """
    params = params or {}
    root_cost = cost(initial_state, params)
    root = _BFSNode(
        state=initial_state.clone(),
        actions=[],
        total_cost=root_cost,
        depth=0,
    )

    queue: deque[_BFSNode] = deque([root])
    best_action: str = "KEEP_PHASE"
    best_total_cost: float = float("inf")

    while queue:
        node = queue.popleft()

        # Track best terminal node
        if node.total_cost < best_total_cost:
            best_total_cost = node.total_cost
            best_action = node.actions[0] if node.actions else "KEEP_PHASE"

        if node.depth >= max_depth:
            continue

        # Expand children (no pruning — this is BFS)
        for action_name in ("KEEP_PHASE", "SWITCH_PHASE"):
            child_state = _apply_action(node.state, action_name)
            child_cost = node.total_cost + cost(child_state, params)
            queue.append(
                _BFSNode(
                    state=child_state,
                    actions=node.actions + [action_name],
                    total_cost=child_cost,
                    depth=node.depth + 1,
                )
            )

    return Action[best_action], best_total_cost


def fixed_timer_avg_wait(
    initial_state: TrafficState,
    phase_duration: int = 30,
) -> float:
    """
    Simulate a fixed-timer controller and return average per-tick incremental wait.

    The fixed-timer alternates phases every *phase_duration* seconds
    regardless of traffic conditions.  The metric returned is the mean
    number of vehicles waiting per lane per tick (vehicles * 1 second),
    which is the same scale used by the adaptive controller's per-tick
    wait tracking.

    Parameters
    ----------
    initial_state : TrafficState
        Starting state.
    phase_duration : int
        Seconds each phase is held before switching.

    Returns
    -------
    float
        Average per-tick per-lane red-vehicle count over a full cycle.
    """
    state = initial_state.clone()
    total_red_vehicle_seconds = 0.0
    ticks = 0

    # Simulate one full cycle (two phases)
    for _ in range(phase_duration * 2):
        # Count vehicles waiting in red lanes this tick
        for direction, lane in state.lanes.items():
            if direction not in state.active_lanes:
                total_red_vehicle_seconds += lane.vehicle_count

        state.phase_duration += 1
        if state.phase_duration >= phase_duration:
            state.current_phase = state.opposite_phase()
            state.phase_duration = 0

        ticks += 1

    num_lanes = len(state.lanes) or 1
    return total_red_vehicle_seconds / (ticks * num_lanes)
