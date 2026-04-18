"""
emergency.py
------------
Emergency vehicle override logic.

This module is the first check executed before any search algorithm runs.
If any lane contains an emergency vehicle, the controller immediately
returns EMERGENCY_OVERRIDE and forces a green signal for 30 seconds.
"""

from __future__ import annotations

from traffic_controller.models import Action, TrafficState


# How long (seconds) an emergency override holds the phase.
EMERGENCY_GREEN_DURATION: int = 30


def check_emergency(state: TrafficState) -> tuple[bool, str | None]:
    """
    Detect whether an emergency vehicle is present in any lane.

    Parameters
    ----------
    state : TrafficState
        Current intersection state.

    Returns
    -------
    tuple[bool, str | None]
        (True, lane_direction) if an emergency exists, else (False, None).
        When multiple lanes have emergencies, the one with the most vehicles
        is prioritised.
    """
    emergency_lanes = [
        (direction, lane.vehicle_count)
        for direction, lane in state.lanes.items()
        if lane.has_emergency
    ]

    if not emergency_lanes:
        return False, None

    # Prioritise the lane with the most waiting vehicles
    best_lane = max(emergency_lanes, key=lambda x: x[1])[0]
    return True, best_lane


def get_emergency_phase(lane_direction: str) -> str:
    """
    Return the signal phase that gives green to the specified lane.

    Parameters
    ----------
    lane_direction : str
        Direction key ('N', 'S', 'E', or 'W').

    Returns
    -------
    str
        'NS' if the direction is North or South, else 'EW'.
    """
    return "NS" if lane_direction in ("N", "S") else "EW"


def handle_emergency(
    state: TrafficState,
) -> tuple[Action, str, str]:
    """
    Produce an emergency override action.

    This function should be called after :func:`check_emergency` confirms
    that at least one lane has an emergency vehicle.

    Parameters
    ----------
    state : TrafficState
        Current intersection state (must have at least one emergency lane).

    Returns
    -------
    tuple[Action, str, str]
        (EMERGENCY_OVERRIDE action,
         target phase string,
         human-readable reason string)

    Raises
    ------
    ValueError
        If no emergency is detected in the state (programming error guard).
    """
    found, lane = check_emergency(state)
    if not found or lane is None:
        raise ValueError(
            "handle_emergency called but no emergency lane found in state."
        )

    target_phase = get_emergency_phase(lane)
    reason = (
        f"Emergency vehicle in lane {lane} → forcing {target_phase}-green "
        f"for {EMERGENCY_GREEN_DURATION}s"
    )
    return Action.EMERGENCY_OVERRIDE, target_phase, reason
