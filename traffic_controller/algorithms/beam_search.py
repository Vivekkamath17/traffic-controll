"""
beam_search.py
--------------
Beam Search algorithm for peak / congested traffic conditions.

Triggered when:
  - any lane has vehicle_count >= 20, OR
  - total vehicle count > 60

Maintains a beam of the k best states at each time step, pruning
the rest. Looks ahead 10 ticks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from traffic_controller.models import Action, TrafficState
from traffic_controller.utils.cost import cost, heuristic


# ---------------------------------------------------------------------------
# Internal beam node
# ---------------------------------------------------------------------------

@dataclass
class _BeamNode:
    """
    A node in the beam search frontier.

    Attributes
    ----------
    state : TrafficState
        Intersection state at this point in the plan.
    cost_so_far : float
        Cumulative cost from the initial state.
    first_action : str
        The first action in the plan leading to this node
        (used to determine what to return at the root).
    depth : int
        Steps taken from the root.
    """

    state: TrafficState
    cost_so_far: float
    first_action: str
    depth: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_action(state: TrafficState, action_name: str) -> TrafficState:
    """
    Produce the successor state for *action_name*.

    Parameters
    ----------
    state : TrafficState
    action_name : str
        'KEEP_PHASE' or 'SWITCH_PHASE'.

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

    # Green-lane throughput
    for direction in next_state.active_lanes:
        lane = next_state.lanes.get(direction)
        if lane and lane.vehicle_count > 0 and not lane.is_blocked:
            # Higher clearance rate for congested lanes
            clearance = 2 if lane.vehicle_count >= 15 else 1
            lane.vehicle_count = max(0, lane.vehicle_count - clearance)

    # Waiting time accumulates for red lanes
    for direction, lane in next_state.lanes.items():
        if direction not in next_state.active_lanes:
            lane.waiting_time += 1.0

    return next_state


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def beam_search(
    initial_state: TrafficState,
    beam_width: int = 5,
    lookahead: int = 10,
    params: Dict | None = None,
) -> Action:
    """
    Run beam search and return the best immediate action.

    At each time step, all states in the current beam are expanded with
    all valid actions; only the top *beam_width* states (by cost) are
    kept for the next step.

    Parameters
    ----------
    initial_state : TrafficState
        Current intersection snapshot.
    beam_width : int
        Number of states to keep in the beam at each step.
    lookahead : int
        Number of time steps to look ahead.
    params : dict, optional
        Algorithm parameters forwarded to :mod:`cost`.

    Returns
    -------
    Action
        KEEP_PHASE or SWITCH_PHASE (best first action found).
    """
    params = params or {}
    bw = int(params.get("beam_width", beam_width))
    min_phase = int(params.get("min_phase_duration", 15))

    # Initialise beam with both possible first actions
    beam: List[_BeamNode] = []
    for action_name in ("KEEP_PHASE", "SWITCH_PHASE"):
        if action_name == "SWITCH_PHASE" and initial_state.phase_duration < min_phase:
            continue
        next_state = _apply_action(initial_state, action_name)
        node_cost = cost(next_state, params)
        beam.append(
            _BeamNode(
                state=next_state,
                cost_so_far=node_cost,
                first_action=action_name,
                depth=1,
            )
        )

    # If all switches were pruned, default to KEEP
    if not beam:
        return Action.KEEP_PHASE

    # Expand up to *lookahead* steps
    for step in range(1, lookahead):
        candidates: List[_BeamNode] = []

        for node in beam:
            for action_name in ("KEEP_PHASE", "SWITCH_PHASE"):
                if (
                    action_name == "SWITCH_PHASE"
                    and node.state.phase_duration < min_phase
                ):
                    continue

                next_state = _apply_action(node.state, action_name)
                step_cost = cost(next_state, params)
                h = heuristic(next_state, lookahead - step, params)

                candidates.append(
                    _BeamNode(
                        state=next_state,
                        cost_so_far=node.cost_so_far + step_cost + h,
                        first_action=node.first_action,
                        depth=step + 1,
                    )
                )

        # Prune to beam width
        candidates.sort(key=lambda n: n.cost_so_far)
        beam = candidates[:bw]

    # The best node's first action is our recommendation
    best = min(beam, key=lambda n: n.cost_so_far)
    return Action[best.first_action]
