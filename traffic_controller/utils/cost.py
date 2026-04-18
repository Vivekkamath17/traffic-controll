"""
cost.py
-------
Cost function and heuristic used by A* and other search algorithms.

All scoring is framed as *minimisation*: lower cost = better state.
"""

from __future__ import annotations
from typing import Dict, List

from traffic_controller.models import TrafficState, LaneState


# ---------------------------------------------------------------------------
# Tuneable weights (can be overridden by Fish Swarm parameters)
# ---------------------------------------------------------------------------
DEFAULT_EMERGENCY_PENALTY: float = 10_000.0
DEFAULT_STARVATION_THRESHOLD: float = 120.0   # seconds before penalty applies
DEFAULT_STARVATION_PENALTY: float = 500.0
DEFAULT_THROUGHPUT_REWARD: float = 200.0


def lane_cost(lane: LaneState) -> float:
    """
    Compute the raw congestion cost contribution from a single lane.

    Parameters
    ----------
    lane : LaneState
        The lane to evaluate.

    Returns
    -------
    float
        vehicle_count * waiting_time  (congestion product).
    """
    return lane.vehicle_count * lane.waiting_time


def starvation_penalty(
    lane: LaneState,
    threshold: float = DEFAULT_STARVATION_THRESHOLD,
    weight: float = DEFAULT_STARVATION_PENALTY,
) -> float:
    """
    Extra penalty applied when a lane's wait exceeds the starvation threshold.

    Parameters
    ----------
    lane : LaneState
        Lane to check.
    threshold : float
        Seconds of waiting beyond which starvation is declared.
    weight : float
        Multiplier for the excess waiting time.

    Returns
    -------
    float
        weight * max(0, waiting_time - threshold)
    """
    excess = max(0.0, lane.waiting_time - threshold)
    return weight * excess


def cost(
    state: TrafficState,
    params: Dict[str, float] | None = None,
) -> float:
    """
    Evaluate the total cost of a traffic state.

    Cost formula
    ------------
    cost = Σ (vehicle_count_i * waiting_time_i)          # congestion
           + 10000 * any_emergency                        # emergency penalty
           + 500 * Σ max(0, waiting_time_i - 120)         # starvation penalty
           - 200 * vehicles_in_green_lanes                # throughput reward

    Parameters
    ----------
    state : TrafficState
        The intersection state to evaluate.
    params : dict, optional
        Override default weights.  Keys expected:
            'emergency_penalty_weight', 'starvation_threshold',
            'starvation_penalty_weight', 'throughput_reward'.

    Returns
    -------
    float
        Composite cost value (lower = better).
    """
    p = params or {}
    emg_pen = p.get("emergency_penalty_weight", DEFAULT_EMERGENCY_PENALTY)
    starv_thresh = p.get("starvation_threshold", DEFAULT_STARVATION_THRESHOLD)
    starv_pen = p.get("starvation_penalty_weight", DEFAULT_STARVATION_PENALTY)
    thru_rew = p.get("throughput_reward", DEFAULT_THROUGHPUT_REWARD)

    # --- Congestion cost ---
    congestion = sum(lane_cost(lane) for lane in state.lanes.values())

    # --- Emergency penalty ---
    emergency = emg_pen if state.has_any_emergency else 0.0

    # --- Starvation penalty ---
    starvation = sum(
        starvation_penalty(lane, starv_thresh, starv_pen)
        for lane in state.lanes.values()
    )

    # --- Throughput reward ---
    green_vehicles = sum(
        state.lanes[d].vehicle_count
        for d in state.active_lanes
        if d in state.lanes
    )
    throughput = thru_rew * green_vehicles

    return congestion + emergency + starvation - throughput


def heuristic(
    state: TrafficState,
    lookahead_seconds: int = 10,
    params: Dict[str, float] | None = None,
) -> float:
    """
    Estimate future cost from the current state (admissible lower-bound).

    Heuristic components
    --------------------
    1. Projected congestion: vehicles * (current_wait + estimated_future_wait)
       where future_wait = arrival_rate * lookahead_seconds.
    2. Time-since-last-switch penalty: discourages rapid phase oscillation
       by adding a small cost proportional to how little time has elapsed
       since the last switch (minimum phase time compliance).
    3. Average historical waiting time boost for lanes not yet served.

    Parameters
    ----------
    state : TrafficState
        Current intersection state.
    lookahead_seconds : int
        How many seconds ahead to project.
    params : dict, optional
        Algorithm parameters that may contain 'min_phase_duration'.

    Returns
    -------
    float
        Non-negative heuristic estimate (admissible for A*).
    """
    p = params or {}
    min_phase = p.get("min_phase_duration", 15)

    h = 0.0
    for direction, lane in state.lanes.items():
        # Projected future waiting time
        future_wait = lane.arrival_rate * lookahead_seconds
        projected_vehicles = lane.vehicle_count + int(future_wait)

        h += projected_vehicles * (lane.waiting_time + future_wait * 0.5)

        # Starvation lookahead
        if lane.waiting_time + future_wait > DEFAULT_STARVATION_THRESHOLD:
            h += DEFAULT_STARVATION_PENALTY * future_wait

    # Oscillation penalty: penalise if phase switches happen too fast
    if state.phase_duration < min_phase:
        oscillation_cost = (min_phase - state.phase_duration) * 50.0
        h += oscillation_cost

    return max(0.0, h)


def evaluate_action_cost(
    state: TrafficState,
    action_name: str,
    params: Dict[str, float] | None = None,
) -> float:
    """
    Quickly estimate cost after applying a given action label.

    Used by beam search and AO* to score candidate moves without
    fully simulating a transition.

    Parameters
    ----------
    state : TrafficState
        Current state.
    action_name : str
        One of 'KEEP_PHASE', 'SWITCH_PHASE', 'EMERGENCY_OVERRIDE'.
    params : dict, optional
        Algorithm parameters.

    Returns
    -------
    float
        Estimated cost after action.
    """
    projected = state.clone()

    if action_name == "SWITCH_PHASE":
        projected.current_phase = state.opposite_phase()
        projected.phase_duration = 0
    elif action_name == "KEEP_PHASE":
        projected.phase_duration += 1
    # EMERGENCY_OVERRIDE handled upstream; treat similarly to SWITCH_PHASE

    return cost(projected, params)
