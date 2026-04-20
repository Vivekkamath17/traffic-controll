"""
simulator.py
------------
Traffic Simulator — the environment the controller acts within.

Advances the intersection state one tick at a time:
  - Poisson vehicle arrivals (λ = 0.3 per lane per tick, configurable).
  - Vehicle throughput for green lanes.
  - Random emergency and blockage events.
  - Fixed-timer baseline tracking for comparison reports.
"""

from __future__ import annotations

import random
from typing import Dict, List, Optional

import numpy as np

from traffic_controller.models import Action, LaneState, TrafficState


# ---------------------------------------------------------------------------
# Default simulation parameters
# ---------------------------------------------------------------------------

DEFAULT_ARRIVAL_LAMBDA: float = 0.3   # Poisson λ per lane per tick
EMERGENCY_PROB: float = 0.002         # P(emergency appears in a lane per tick)
BLOCKAGE_PROB: float = 0.001          # P(blockage appears in a lane per tick)
EMERGENCY_CLEAR_PROB: float = 0.05    # P(emergency clears per tick)
BLOCKAGE_CLEAR_PROB: float = 0.02     # P(blockage clears per tick)
MAX_VEHICLES: int = 30

# Traffic profiles (time-of-day patterns)
TRAFFIC_PROFILES = {
    "morning_rush": {"N": 0.7, "S": 0.2, "E": 0.5, "W": 0.3},
    "lunch": {"N": 0.35, "S": 0.35, "E": 0.35, "W": 0.35},
    "evening_rush": {"N": 0.2, "S": 0.7, "E": 0.3, "W": 0.5},
    "night": {"N": 0.08, "S": 0.08, "E": 0.08, "W": 0.08},
    "default": {"N": 0.3, "S": 0.3, "E": 0.3, "W": 0.3},
}


class TrafficSimulator:
    """
    Discrete-time simulation of a 4-way signalised intersection.

    Parameters
    ----------
    seed : int, optional
        Random seed for reproducibility.
    arrival_lambda : float
        Poisson arrival rate (vehicles per lane per tick).
    """

    # Direction keys for the four approach lanes
    DIRECTIONS: List[str] = ["N", "S", "E", "W"]

    def __init__(
        self,
        seed: int | None = None,
        arrival_lambda: float = DEFAULT_ARRIVAL_LAMBDA,
    ) -> None:
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)

        self._arrival_lambda = arrival_lambda
        self._tick: int = 0

        # Build initial state with empty lanes
        self._state: TrafficState = TrafficState(
            lanes={
                d: LaneState(arrival_rate=arrival_lambda)
                for d in self.DIRECTIONS
            },
            current_phase="NS",
            phase_duration=0,
            timestamp=0,
        )

        # Statistics accumulators
        self._total_vehicles_served: int = 0
        self._emergency_override_ticks: List[int] = []

        # Fixed-timer baseline accumulator
        self._fixed_phase: str = "NS"
        self._fixed_phase_timer: int = 0
        self._fixed_phase_duration: int = 30  # seconds per phase
        self._fixed_timer_wait_history: List[float] = []

        # History for Fish Swarm fitness evaluation
        self._recent_avg_waits: List[float] = []  # last 100 ticks

        # Pedestrian handling
        self._pedestrian_request_prob: float = 0.003  # per tick
        self._pedestrian_phase_active: bool = False
        self._pedestrian_phase_timer: int = 0
        self._pedestrian_phase_duration: int = 8  # ticks

        # Current traffic profile
        self._current_profile: str = "default"
        self._arrival_lambdas: dict[str, float] = TRAFFIC_PROFILES["default"].copy()

    # ------------------------------------------------------------------
    # Core simulation step
    # ------------------------------------------------------------------

    def set_profile(self, profile: str) -> None:
        """
        Change the traffic profile (time-of-day traffic patterns).

        Parameters
        ----------
        profile : str
            One of: "morning_rush", "lunch", "evening_rush", "night", "default"

        Raises
        ------
        ValueError
            If profile name is not recognized.
        """
        if profile not in TRAFFIC_PROFILES:
            raise ValueError(f"Unknown profile: {profile}. Valid profiles: {list(TRAFFIC_PROFILES.keys())}")

        self._current_profile = profile
        self._arrival_lambdas = TRAFFIC_PROFILES[profile].copy()

        # Update lane arrival rates
        for direction, lane in self._state.lanes.items():
            if direction in self._arrival_lambdas:
                lane.arrival_rate = self._arrival_lambdas[direction]

    def tick(self) -> None:
        """
        Advance the simulation by one second.

        Actions performed each tick
        ---------------------------
        1. **Arrivals**: Poisson-distributed vehicles added to each lane.
        2. **Throughput**: Vehicles depart green lanes probabilistically.
        3. **Waiting**: Red-lane vehicles accumulate waiting time.
        4. **Events**: Random emergencies and blockages are triggered/cleared.
        5. **Pedestrian**: Handle pedestrian crossing phases.
        6. **Fixed-timer baseline**: Parallel state advanced for comparison.
        """
        self._tick += 1
        state = self._state

        # Handle pedestrian phase
        if self._pedestrian_phase_active:
            self._pedestrian_phase_timer += 1
            if self._pedestrian_phase_timer >= self._pedestrian_phase_duration:
                self._pedestrian_phase_active = False
                self._pedestrian_phase_timer = 0
                state.pedestrian_waiting = False
            # During pedestrian phase, no new vehicles clear
            # Just accumulate waiting time
            for direction, lane in state.lanes.items():
                if lane.vehicle_count > 0:
                    lane.waiting_time += 0.5  # Reduced waiting accumulation during ped phase
            state.timestamp = self._tick
            state.phase_duration += 1
            return

        # Check for pedestrian request
        if not state.pedestrian_waiting and not self._pedestrian_phase_active:
            if random.random() < self._pedestrian_request_prob:
                state.pedestrian_waiting = True

        # 1. Vehicle arrivals (Poisson) - use profile-specific lambdas
        for direction, lane in state.lanes.items():
            arrival_lambda = self._arrival_lambdas.get(direction, self._arrival_lambda)
            arrivals = np.random.poisson(arrival_lambda)
            lane.vehicle_count = min(MAX_VEHICLES, lane.vehicle_count + arrivals)

        # 2. Throughput for green lanes
        for direction in state.active_lanes:
            lane = state.lanes[direction]
            if lane.vehicle_count > 0 and not lane.is_blocked:
                # 1–3 vehicles clear per green tick (uniform)
                cleared = random.randint(1, min(3, lane.vehicle_count))
                lane.vehicle_count = max(0, lane.vehicle_count - cleared)
                self._total_vehicles_served += cleared
                # Waiting time partial reset proportional to clearance
                lane.waiting_time = max(0.0, lane.waiting_time - cleared * 0.5)

        # 3. Waiting time accumulation for red lanes
        for direction, lane in state.lanes.items():
            if direction not in state.active_lanes:
                if lane.vehicle_count > 0:
                    lane.waiting_time += 1.0

        # 4a. Emergency events
        for direction, lane in state.lanes.items():
            if lane.has_emergency:
                if random.random() < EMERGENCY_CLEAR_PROB:
                    lane.has_emergency = False
            else:
                if random.random() < EMERGENCY_PROB:
                    lane.has_emergency = True

        # 4b. Blockage events
        for direction, lane in state.lanes.items():
            if lane.is_blocked:
                if random.random() < BLOCKAGE_CLEAR_PROB:
                    lane.is_blocked = False
            else:
                if random.random() < BLOCKAGE_PROB:
                    lane.is_blocked = True

        # 5. Apply pedestrian phase if requested and phase duration > 60s
        if state.pedestrian_waiting and state.phase_duration > 60 and not self._pedestrian_phase_active:
            self._pedestrian_phase_active = True
            self._pedestrian_phase_timer = 0
            # All vehicle signals go to red during pedestrian phase
            # This is handled by the controller returning PEDESTRIAN_PHASE

        # 6. Fixed-timer baseline tracking
        self._tick_fixed_timer()

        # Record average wait for FSO fitness
        avg_wait = self._current_avg_wait()
        self._recent_avg_waits.append(avg_wait)
        if len(self._recent_avg_waits) > 100:
            self._recent_avg_waits.pop(0)

        state.timestamp = self._tick
        state.phase_duration += 1

    # ------------------------------------------------------------------
    # State access
    # ------------------------------------------------------------------

    def get_state(self) -> TrafficState:
        """
        Return a snapshot of the current intersection state.

        Returns
        -------
        TrafficState
            A deep copy of the internal state.
        """
        return self._state.clone()

    def get_raw_state(self) -> TrafficState:
        """Return the internal state reference (avoid cloning overhead)."""
        return self._state

    # ------------------------------------------------------------------
    # Action application
    # ------------------------------------------------------------------

    def apply_action(self, action: Action, target_phase: str | None = None) -> None:
        """
        Update the signal phase according to the controller's decision.

        Parameters
        ----------
        action : Action
            The action chosen by the controller.
        target_phase : str, optional
            Required for EMERGENCY_OVERRIDE — specifies which phase to force.
        """
        state = self._state

        if action == Action.SWITCH_PHASE:
            state.current_phase = state.opposite_phase()
            state.phase_duration = 0

        elif action == Action.EMERGENCY_OVERRIDE:
            if target_phase is None:
                # Default: give NS green (safe fallback)
                target_phase = "NS"
            if state.current_phase != target_phase:
                state.current_phase = target_phase
                state.phase_duration = 0
            self._emergency_override_ticks.append(self._tick)

        elif action == Action.PEDESTRIAN_PHASE:
            # Start pedestrian crossing phase
            self._pedestrian_phase_active = True
            self._pedestrian_phase_timer = 0
            state.pedestrian_waiting = True
            # Reset phase duration for tracking
            state.phase_duration = 0

        # KEEP_PHASE: phase_duration already incremented in tick()

    # ------------------------------------------------------------------
    # Report helpers
    # ------------------------------------------------------------------

    def generate_report(self) -> Dict:
        """
        Return a statistics dictionary for the simulation so far.

        Returns
        -------
        dict
            - average_wait_per_lane: dict[str, float]
            - total_vehicles_served: int
            - emergency_override_count: int
            - fixed_timer_avg_wait: float
            - recent_avg_wait: float  (last 100 ticks)
        """
        avg_per_lane = {
            d: round(self._state.lanes[d].waiting_time, 2)
            for d in self.DIRECTIONS
        }
        fixed_avg = (
            sum(self._fixed_timer_wait_history) / len(self._fixed_timer_wait_history)
            if self._fixed_timer_wait_history
            else 0.0
        )
        return {
            "average_wait_per_lane": avg_per_lane,
            "total_vehicles_served": self._total_vehicles_served,
            "emergency_override_count": len(self._emergency_override_ticks),
            "fixed_timer_avg_wait": round(fixed_avg, 2),
            "recent_avg_wait": round(
                sum(self._recent_avg_waits) / max(1, len(self._recent_avg_waits)), 2
            ),
        }

    def fso_fitness(self, params: Dict) -> float:
        """
        Fitness function for the Fish Swarm Optimiser.

        Returns negative average waiting time over the last 100 ticks.
        Higher (less negative) = better.

        Parameters
        ----------
        params : dict
            Controller parameters (currently unused in the simulator;
            fitness is based on observed history).

        Returns
        -------
        float
        """
        if not self._recent_avg_waits:
            return 0.0
        return -sum(self._recent_avg_waits) / len(self._recent_avg_waits)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _current_avg_wait(self) -> float:
        """Compute mean waiting time across all lanes."""
        waits = [lane.waiting_time for lane in self._state.lanes.values()]
        return sum(waits) / len(waits) if waits else 0.0

    def _tick_fixed_timer(self) -> None:
        """Advance the parallel fixed-timer baseline and record wait."""
        # Accumulate wait for red lanes under fixed timer
        total_wait = 0.0
        for direction, lane in self._state.lanes.items():
            if direction not in list(self._fixed_phase):
                total_wait += lane.waiting_time

        self._fixed_timer_wait_history.append(
            total_wait / max(1, len(self._state.lanes))
        )

        # Advance fixed-timer clock
        self._fixed_phase_timer += 1
        if self._fixed_phase_timer >= self._fixed_phase_duration:
            self._fixed_phase = "EW" if self._fixed_phase == "NS" else "NS"
            self._fixed_phase_timer = 0
