"""
astar.py
--------
A* Search algorithm for optimal signal phase sequencing.

Used when traffic is light-to-moderate:
  - max vehicle count across all lanes < 20
  - no emergency vehicles
  - no blocked lanes

A* expands states in a priority queue ordered by f(n) = g(n) + h(n),
where g(n) is the accumulated cost and h(n) is the admissible heuristic.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from traffic_controller.models import Action, TrafficState
from traffic_controller.utils.cost import cost, heuristic


# ---------------------------------------------------------------------------
# Search node
# ---------------------------------------------------------------------------

@dataclass(order=True)
class _Node:
    """
    Internal A* search node.

    Attributes
    ----------
    f : float
        f(n) = g(n) + h(n), used as the priority queue key.
    g : float
        Accumulated cost from the start node to this node.
    state : TrafficState
        Intersection state at this node (excluded from ordering).
    actions : list
        Sequence of actions taken to reach this state.
    depth : int
        Steps taken from the root.
    """

    f: float
    g: float
    state: TrafficState = field(compare=False)
    actions: List[str] = field(compare=False, default_factory=list)
    depth: int = field(compare=False, default=0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_action(state: TrafficState, action_name: str) -> TrafficState:
    """
    Return a new state produced by applying *action_name* to *state*.

    Parameters
    ----------
    state : TrafficState
        Parent state.
    action_name : str
        'KEEP_PHASE' or 'SWITCH_PHASE'.

    Returns
    -------
    TrafficState
        New (cloned and modified) state.
    """
    next_state = state.clone()
    next_state.timestamp += 1

    if action_name == "SWITCH_PHASE":
        next_state.current_phase = state.opposite_phase()
        next_state.phase_duration = 0
    else:  # KEEP_PHASE
        next_state.phase_duration += 1

    # Simulate minimal vehicle progression: vehicles in green lanes decrement slowly
    for direction in next_state.active_lanes:
        lane = next_state.lanes.get(direction)
        if lane and lane.vehicle_count > 0 and not lane.is_blocked:
            lane.vehicle_count = max(0, lane.vehicle_count - 1)

    # Waiting time accumulates for red lanes
    for direction, lane in next_state.lanes.items():
        if direction not in next_state.active_lanes:
            lane.waiting_time += 1.0

    return next_state


def _state_key(state: TrafficState) -> Tuple:
    """
    Create a hashable key for visited-state detection.

    Parameters
    ----------
    state : TrafficState

    Returns
    -------
    tuple
        (phase, phase_duration, tuple of vehicle counts per lane)
    """
    counts = tuple(
        state.lanes[d].vehicle_count for d in sorted(state.lanes)
    )
    return (state.current_phase, min(state.phase_duration, 60), counts)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def astar_search(
    initial_state: TrafficState,
    max_depth: int = 10,
    params: Dict | None = None,
) -> Action:
    """
    Run A* search and return the best immediate action.

    The search explores possible phase sequences up to *max_depth* steps,
    using the composite cost function as g(n) and the heuristic as h(n).

    Parameters
    ----------
    initial_state : TrafficState
        Current intersection snapshot.
    max_depth : int
        Maximum lookahead depth (time steps).
    params : dict, optional
        Algorithm parameters forwarded to cost/heuristic functions.

    Returns
    -------
    Action
        KEEP_PHASE or SWITCH_PHASE (whichever minimises projected cost).
    """
    params = params or {}

    # Build the root node
    root_g = cost(initial_state, params)
    root_h = heuristic(initial_state, max_depth, params)
    root = _Node(
        f=root_g + root_h,
        g=root_g,
        state=initial_state.clone(),
        actions=[],
        depth=0,
    )

    heap: List[_Node] = [root]
    visited: dict[Tuple, float] = {}
    best_action: str = "KEEP_PHASE"
    best_cost: float = float("inf")

    while heap:
        node = heapq.heappop(heap)

        state_key = _state_key(node.state)
        if state_key in visited and visited[state_key] <= node.g:
            continue
        visited[state_key] = node.g

        # Track the best terminal node found so far
        if node.f < best_cost:
            best_cost = node.f
            best_action = node.actions[0] if node.actions else "KEEP_PHASE"

        if node.depth >= max_depth:
            continue

        # Expand children
        for action_name in ("KEEP_PHASE", "SWITCH_PHASE"):
            # Respect minimum phase duration
            min_phase = params.get("min_phase_duration", 15)
            if (
                action_name == "SWITCH_PHASE"
                and node.state.phase_duration < min_phase
            ):
                continue

            next_state = _apply_action(node.state, action_name)
            child_g = node.g + cost(next_state, params)
            child_h = heuristic(next_state, max_depth - node.depth - 1, params)
            child_f = child_g + child_h

            child_actions = node.actions + [action_name]
            child = _Node(
                f=child_f,
                g=child_g,
                state=next_state,
                actions=child_actions,
                depth=node.depth + 1,
            )
            heapq.heappush(heap, child)

    return Action[best_action]
