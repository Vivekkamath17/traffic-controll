"""
models.py
---------
Core data structures for the Smart Adaptive Traffic Signal Controller.

Defines:
    - LaneState: per-lane traffic conditions
    - TrafficState: full intersection snapshot
    - Action: enumeration of controller decisions
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict


class Action(Enum):
    """
    Enumeration of all possible actions the controller can take.

    Attributes
    ----------
    KEEP_PHASE : auto
        Maintain the current signal phase.
    SWITCH_PHASE : auto
        Toggle the active phase between NS and EW.
    EMERGENCY_OVERRIDE : auto
        Force green for a lane that contains an emergency vehicle.
    """

    KEEP_PHASE = auto()
    SWITCH_PHASE = auto()
    EMERGENCY_OVERRIDE = auto()


@dataclass
class LaneState:
    """
    Snapshot of a single approach lane at the intersection.

    Parameters
    ----------
    vehicle_count : int
        Number of vehicles currently queued in this lane (0–30).
    waiting_time : float
        Cumulative seconds that vehicles in this lane have been waiting.
    has_emergency : bool
        True when an ambulance or fire-truck is detected in the lane.
    is_blocked : bool
        True when an accident or roadblock prevents movement.
    arrival_rate : float
        Estimated vehicles arriving per second (used by heuristic).
    """

    vehicle_count: int = 0
    waiting_time: float = 0.0
    has_emergency: bool = False
    is_blocked: bool = False
    arrival_rate: float = 0.3  # Poisson lambda default

    def __post_init__(self) -> None:
        """Validate that vehicle_count stays in the allowed range."""
        if not (0 <= self.vehicle_count <= 30):
            raise ValueError(
                f"vehicle_count must be 0-30, got {self.vehicle_count}"
            )

    def clone(self) -> "LaneState":
        """Return a shallow copy of this lane state."""
        return LaneState(
            vehicle_count=self.vehicle_count,
            waiting_time=self.waiting_time,
            has_emergency=self.has_emergency,
            is_blocked=self.is_blocked,
            arrival_rate=self.arrival_rate,
        )


@dataclass
class TrafficState:
    """
    Complete snapshot of the intersection at a single simulation tick.

    Parameters
    ----------
    lanes : Dict[str, LaneState]
        Mapping from direction key ('N', 'S', 'E', 'W') to lane state.
    current_phase : str
        Active signal phase: 'NS' (North-South green) or 'EW' (East-West green).
    phase_duration : int
        Number of seconds the current phase has been active.
    timestamp : int
        Simulation tick counter (seconds since start).
    """

    lanes: Dict[str, LaneState] = field(default_factory=dict)
    current_phase: str = "NS"
    phase_duration: int = 0
    timestamp: int = 0

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @property
    def total_vehicles(self) -> int:
        """Total number of queued vehicles across all lanes."""
        return sum(lane.vehicle_count for lane in self.lanes.values())

    @property
    def active_lanes(self) -> list[str]:
        """Direction keys whose lanes are currently green."""
        return list(self.current_phase)  # e.g. ['N', 'S'] for 'NS'

    @property
    def has_any_emergency(self) -> bool:
        """True if at least one lane contains an emergency vehicle."""
        return any(lane.has_emergency for lane in self.lanes.values())

    @property
    def has_any_blockage(self) -> bool:
        """True if at least one lane is blocked."""
        return any(lane.is_blocked for lane in self.lanes.values())

    def emergency_lanes(self) -> list[str]:
        """Return direction keys of lanes that have emergency vehicles."""
        return [k for k, v in self.lanes.items() if v.has_emergency]

    def blocked_lanes(self) -> list[str]:
        """Return direction keys of lanes that are blocked."""
        return [k for k, v in self.lanes.items() if v.is_blocked]

    def opposite_phase(self) -> str:
        """Return the phase that is NOT currently active."""
        return "EW" if self.current_phase == "NS" else "NS"

    def clone(self) -> "TrafficState":
        """Return a deep copy of this traffic state."""
        return TrafficState(
            lanes={k: v.clone() for k, v in self.lanes.items()},
            current_phase=self.current_phase,
            phase_duration=self.phase_duration,
            timestamp=self.timestamp,
        )

    def __repr__(self) -> str:
        counts = {k: v.vehicle_count for k, v in self.lanes.items()}
        return (
            f"TrafficState(tick={self.timestamp}, phase={self.current_phase}, "
            f"phase_dur={self.phase_duration}s, vehicles={counts})"
        )
