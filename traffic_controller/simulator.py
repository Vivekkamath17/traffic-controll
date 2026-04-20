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

import copy
import random
from typing import Dict, List, Optional

import numpy as np

from traffic_controller.models import (
    Action,
    LaneState,
    TrafficState,
    VehicleIntent,
    VehicleRecord,
    get_exit_lane,
)


# ---------------------------------------------------------------------------
# Default simulation parameters
# ---------------------------------------------------------------------------

DEFAULT_ARRIVAL_LAMBDA: float = 0.3   # Poisson λ per lane per tick
EMERGENCY_PROB: float = 0.002         # P(emergency appears once per tick)
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
        grid_position: tuple = (0, 0),
    ) -> None:
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)

        self._arrival_lambda = arrival_lambda
        self._tick: int = 0
        self.grid_position: tuple = grid_position

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

        # Emergency lifecycle guards
        self.emergency_cooldown_remaining: int = 0
        self.emergency_active_lane: Optional[str] = None
        self.emergency_ticks_active: int = 0
        self.EMERGENCY_DURATION: int = 30
        self.emergency_just_cleared: bool = False

        # Block duration tracking
        self.block_duration: Dict[str, int] = {}
        self.exiting_vehicles: List[VehicleRecord] = []

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

    def tick(self) -> Dict[str, int]:
        """
        Advance the simulation by one second.

        Actions performed each tick
        ---------------------------
        1. **Arrivals**: Poisson-distributed vehicles added to each lane.
        2. **Throughput**: Vehicles depart green lanes probabilistically.
        3. **Waiting**: Red-lane vehicles accumulate waiting time.
        4. **Events**: Random emergencies and blockages are triggered/cleared.
        5. **Fixed-timer baseline**: Parallel state advanced for comparison.
        """
        self._tick += 1
        state = self._state
        self.emergency_just_cleared = False
        self.exiting_vehicles = []

        # 1. Vehicle arrivals (Poisson) - use profile-specific lambdas
        arrivals_by_lane: Dict[str, int] = {}
        intent_options = list(VehicleIntent)
        intent_weights = [0.5, 0.25, 0.25]
        for direction, lane in state.lanes.items():
            arrival_lambda = self._arrival_lambdas.get(direction, self._arrival_lambda)
            arrivals = np.random.poisson(arrival_lambda)
            arrivals_by_lane[direction] = int(arrivals)
            for _ in range(int(arrivals)):
                if lane.vehicle_count >= MAX_VEHICLES:
                    break
                intent = random.choices(intent_options, weights=intent_weights)[0]
                lane.intent_counts[intent.value] += 1
                lane.vehicle_count += 1

        # 2. Throughput for green lanes
        for direction in state.active_lanes:
            lane = state.lanes[direction]
            if lane.vehicle_count > 0 and not lane.is_blocked:
                # 1–3 vehicles clear per green tick (uniform)
                cleared = random.randint(1, min(3, lane.vehicle_count))
                served_count = 0
                for i in range(cleared):
                    chosen_intent = self._pop_served_intent(lane)
                    if chosen_intent is None:
                        break
                    lane.vehicle_count = max(0, lane.vehicle_count - 1)
                    served_count += 1
                    self.exiting_vehicles.append(
                        VehicleRecord(
                            id=f"{direction}_{self._tick}_{i}",
                            entry_lane=direction,
                            intent=chosen_intent,
                            exit_lane=get_exit_lane(direction, chosen_intent),
                        )
                    )
                self._total_vehicles_served += served_count
                # Waiting time partial reset proportional to clearance
                lane.waiting_time = max(0.0, lane.waiting_time - served_count * 0.5)

        # 3. Waiting time accumulation for red lanes
        for direction, lane in state.lanes.items():
            if direction not in state.active_lanes:
                if lane.vehicle_count > 0:
                    lane.waiting_time += 1.0

        # 4a. Emergency events (single active emergency at a time)
        if self.emergency_cooldown_remaining > 0:
            self.emergency_cooldown_remaining -= 1
        elif self.emergency_active_lane is None:
            if random.random() < EMERGENCY_PROB:
                chosen_lane = random.choice(self.DIRECTIONS)
                state.lanes[chosen_lane].has_emergency = True
                self.emergency_active_lane = chosen_lane

        # 4b. Blockage events
        for direction, lane in state.lanes.items():
            if lane.is_blocked:
                # Blocked lane traffic piles up and gets extra wait penalty
                if random.random() < 0.4:
                    lane.vehicle_count = min(lane.vehicle_count + 1, MAX_VEHICLES)
                lane.waiting_time += 1.0
            else:
                if random.random() < BLOCKAGE_PROB:
                    lane.is_blocked = True
                    self.block_duration[direction] = 40

        # Decay block timers and clear blocked lanes automatically
        for lane_id in list(self.block_duration.keys()):
            self.block_duration[lane_id] -= 1
            if self.block_duration[lane_id] <= 0:
                if lane_id in state.lanes:
                    state.lanes[lane_id].is_blocked = False
                del self.block_duration[lane_id]

        # 5. Fixed-timer baseline tracking
        self._tick_fixed_timer()

        # Record average wait for FSO fitness
        avg_wait = self._current_avg_wait()
        self._recent_avg_waits.append(avg_wait)
        if len(self._recent_avg_waits) > 100:
            self._recent_avg_waits.pop(0)

        state.timestamp = self._tick
        state.phase_duration += 1
        return arrivals_by_lane

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

    @property
    def state(self) -> TrafficState:
        return self._state

    def get_state_copy(self) -> TrafficState:
        return copy.deepcopy(self._state)

    def inject_arrival(self, lane_id: str, count: int) -> None:
        """Add vehicles arriving from a connected junction."""
        lane = self._state.lanes[lane_id]
        addable = min(count, MAX_VEHICLES - lane.vehicle_count)
        for _ in range(addable):
            lane.intent_counts[VehicleIntent.STRAIGHT.value] += 1
        lane.vehicle_count = min(lane.vehicle_count + addable, MAX_VEHICLES)

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
            self.emergency_ticks_active = 0

        elif action == Action.EMERGENCY_OVERRIDE:
            if target_phase is None:
                # Default: give NS green (safe fallback)
                target_phase = "NS"
            if state.current_phase != target_phase:
                state.current_phase = target_phase
                state.phase_duration = 0
            self._emergency_override_ticks.append(self._tick)
            self.emergency_ticks_active += 1
            if self.emergency_ticks_active >= self.EMERGENCY_DURATION:
                if self.emergency_active_lane and self.emergency_active_lane in state.lanes:
                    state.lanes[self.emergency_active_lane].has_emergency = False
                self.emergency_active_lane = None
                self.emergency_ticks_active = 0
                self.emergency_cooldown_remaining = 50
                self.emergency_just_cleared = True

        # KEEP_PHASE: phase_duration already incremented in tick()
        if action != Action.EMERGENCY_OVERRIDE:
            self.emergency_ticks_active = 0

    # ------------------------------------------------------------------
    # Report helpers
    # ------------------------------------------------------------------

    def generate_report(self) -> Dict:
        """
        Return a statistics dictionary for the simulation so far.
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
        """Fitness function for the Fish Swarm Optimiser."""
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
        total_wait = 0.0
        for direction, lane in self._state.lanes.items():
            if direction not in list(self._fixed_phase):
                total_wait += lane.waiting_time

        self._fixed_timer_wait_history.append(
            total_wait / max(1, len(self._state.lanes))
        )

        self._fixed_phase_timer += 1
        if self._fixed_phase_timer >= self._fixed_phase_duration:
            self._fixed_phase = "EW" if self._fixed_phase == "NS" else "NS"
            self._fixed_phase_timer = 0

    @staticmethod
    def _pop_served_intent(lane: LaneState) -> Optional[VehicleIntent]:
        if sum(lane.intent_counts.values()) <= 0 and lane.vehicle_count > 0:
            lane.intent_counts["straight"] = lane.vehicle_count
        # Clear straights first, then right turns, then left turns
        if lane.intent_counts["straight"] > 0:
            lane.intent_counts["straight"] -= 1
            return VehicleIntent.STRAIGHT
        if lane.intent_counts["turn_right"] > 0:
            lane.intent_counts["turn_right"] -= 1
            return VehicleIntent.TURN_RIGHT
        if lane.intent_counts["turn_left"] > 0:
            lane.intent_counts["turn_left"] -= 1
            return VehicleIntent.TURN_LEFT
        return None


class FixedTimerSimulator:
    """Dumb fixed-timer baseline — switches phase every 30 ticks."""

    CYCLE = 30

    def __init__(self):
        self.state = TrafficState(
            lanes={d: LaneState(arrival_rate=DEFAULT_ARRIVAL_LAMBDA) for d in ["N", "S", "E", "W"]},
            current_phase="NS",
            phase_duration=0,
            timestamp=0,
        )
        self.tick_count = 0
        self.total_wait = 0.0
        self.fixed_cycle = 30

    def tick(self, vehicle_arrivals: dict):
        """
        Accept same vehicle arrivals as main simulator for fair comparison.
        vehicle_arrivals: {'N': int, 'S': int, 'E': int, 'W': int}
        """
        self.tick_count += 1
        self.state.timestamp = self.tick_count
        self.state.phase_duration += 1
        for lane_id, count in vehicle_arrivals.items():
            self.state.lanes[lane_id].vehicle_count = min(self.state.lanes[lane_id].vehicle_count + count, 30)
        if self.tick_count % self.fixed_cycle == 0:
            self.state.current_phase = "EW" if self.state.current_phase == "NS" else "NS"
            self.state.phase_duration = 0
        ticks_into_phase = self.tick_count % self.fixed_cycle
        if ticks_into_phase < 25:
            self._clear_green_lanes()
        for lane_id, lane in self.state.lanes.items():
            if lane_id not in self._get_green_lane_ids():
                lane.waiting_time += 1.0
                self.total_wait += lane.vehicle_count

    def _get_green_lane_ids(self) -> list:
        return ["N", "S"] if self.state.current_phase == "NS" else ["E", "W"]

    def _clear_green_lanes(self):
        for lid in self._get_green_lane_ids():
            cleared = min(self.state.lanes[lid].vehicle_count, 3)
            self.state.lanes[lid].vehicle_count -= cleared
            self.state.lanes[lid].waiting_time = max(0, self.state.lanes[lid].waiting_time - cleared * 2)

    def get_avg_wait(self) -> float:
        return self.total_wait / max(self.tick_count, 1)
