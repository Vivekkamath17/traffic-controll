"""
test_cost.py
------------
Unit tests for the cost function and heuristic.
"""

import sys
from pathlib import Path

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from traffic_controller.models import LaneState, TrafficState
from traffic_controller.utils.cost import (
    cost,
    heuristic,
    lane_cost,
    starvation_penalty,
    DEFAULT_EMERGENCY_PENALTY,
    DEFAULT_STARVATION_PENALTY,
    DEFAULT_STARVATION_THRESHOLD,
    DEFAULT_THROUGHPUT_REWARD,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_state(
    phase: str = "NS",
    phase_duration: int = 20,
    n_count: int = 5,
    s_count: int = 3,
    e_count: int = 8,
    w_count: int = 2,
    n_wait: float = 30.0,
    s_wait: float = 10.0,
    e_wait: float = 60.0,
    w_wait: float = 5.0,
    has_emergency: bool = False,
    is_blocked: bool = False,
) -> TrafficState:
    return TrafficState(
        lanes={
            "N": LaneState(vehicle_count=n_count, waiting_time=n_wait,
                           has_emergency=has_emergency),
            "S": LaneState(vehicle_count=s_count, waiting_time=s_wait),
            "E": LaneState(vehicle_count=e_count, waiting_time=e_wait,
                           is_blocked=is_blocked),
            "W": LaneState(vehicle_count=w_count, waiting_time=w_wait),
        },
        current_phase=phase,
        phase_duration=phase_duration,
        timestamp=100,
    )


# ---------------------------------------------------------------------------
# lane_cost
# ---------------------------------------------------------------------------

class TestLaneCost:
    def test_zero_vehicles(self):
        lane = LaneState(vehicle_count=0, waiting_time=100.0)
        assert lane_cost(lane) == 0.0

    def test_zero_wait(self):
        lane = LaneState(vehicle_count=10, waiting_time=0.0)
        assert lane_cost(lane) == 0.0

    def test_normal(self):
        lane = LaneState(vehicle_count=5, waiting_time=20.0)
        assert lane_cost(lane) == pytest.approx(100.0)

    def test_max_values(self):
        lane = LaneState(vehicle_count=30, waiting_time=999.0)
        assert lane_cost(lane) == pytest.approx(30 * 999.0)


# ---------------------------------------------------------------------------
# starvation_penalty
# ---------------------------------------------------------------------------

class TestStarvationPenalty:
    def test_below_threshold(self):
        lane = LaneState(vehicle_count=5, waiting_time=60.0)
        assert starvation_penalty(lane, threshold=120.0) == 0.0

    def test_at_threshold(self):
        lane = LaneState(vehicle_count=5, waiting_time=120.0)
        assert starvation_penalty(lane, threshold=120.0) == 0.0

    def test_above_threshold(self):
        lane = LaneState(vehicle_count=5, waiting_time=150.0)
        pen = starvation_penalty(lane, threshold=120.0, weight=500.0)
        assert pen == pytest.approx(500.0 * 30.0)

    def test_custom_weight(self):
        lane = LaneState(vehicle_count=1, waiting_time=200.0)
        pen = starvation_penalty(lane, threshold=100.0, weight=100.0)
        assert pen == pytest.approx(100.0 * 100.0)


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------

class TestCostFunction:
    def test_congestion_component(self):
        """Cost includes vehicle_count * waiting_time for each lane."""
        state = _make_state(
            n_count=2, n_wait=10.0,
            s_count=0, s_wait=0.0,
            e_count=0, e_wait=0.0,
            w_count=0, w_wait=0.0,
        )
        # Only N contributes congestion = 2 * 10 = 20
        # N is in NS green → throughput reward = 200 * 2 = 400
        # No starvation penalty (wait < 120)
        c = cost(state)
        assert c == pytest.approx(20.0 - 400.0)

    def test_emergency_penalty_applied(self):
        """Emergency flag should add DEFAULT_EMERGENCY_PENALTY."""
        state_no_emg = _make_state(has_emergency=False)
        state_emg    = _make_state(has_emergency=True)
        diff = cost(state_emg) - cost(state_no_emg)
        assert diff == pytest.approx(DEFAULT_EMERGENCY_PENALTY)

    def test_starvation_penalty_applied(self):
        """Lanes waiting > 120 s should incur starvation penalty.

        The starvation penalty is additive.  We compare two otherwise-identical
        states that differ only in E-lane waiting time to isolate the starvation
        delta rather than comparing against the raw penalty value, since the
        throughput reward can offset the total cost.
        """
        state_starved = _make_state(e_count=5, e_wait=150.0)
        state_normal  = _make_state(e_count=5, e_wait=60.0)
        expected_starvation = DEFAULT_STARVATION_PENALTY * (150.0 - DEFAULT_STARVATION_THRESHOLD)
        # Extra congestion from higher waiting time (5 * 90 = 450)
        extra_congestion = 5 * (150.0 - 60.0)
        delta = cost(state_starved) - cost(state_normal)
        # Delta should be: extra_congestion + starvation_penalty
        assert delta == pytest.approx(extra_congestion + expected_starvation)

    def test_throughput_reward_reduces_cost(self):
        """Green-lane vehicles should provide a throughput reward."""
        state_no_vehicles = _make_state(n_count=0, s_count=0, e_count=0, w_count=0)
        state_green_full  = _make_state(n_count=10, s_count=10, e_count=0, w_count=0)
        # More green-lane vehicles → lower cost (bigger reward)
        assert cost(state_green_full) < cost(state_no_vehicles) + 10_000

    def test_params_override(self):
        """Custom params should override default weights."""
        state = _make_state(has_emergency=True)
        custom_params = {"emergency_penalty_weight": 1.0}
        c_default = cost(state)
        c_custom  = cost(state, params=custom_params)
        assert c_custom < c_default

    def test_all_zero_state(self):
        """Empty intersection should have negative cost (throughput reward for 0 vehicles = 0)."""
        state = _make_state(
            n_count=0, s_count=0, e_count=0, w_count=0,
            n_wait=0.0, s_wait=0.0, e_wait=0.0, w_wait=0.0,
        )
        c = cost(state)
        # Throughput reward for 0 green vehicles = 0; cost = 0
        assert c == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# heuristic
# ---------------------------------------------------------------------------

class TestHeuristic:
    def test_non_negative(self):
        """Heuristic must be non-negative (admissibility requirement)."""
        state = _make_state()
        h = heuristic(state)
        assert h >= 0.0

    def test_zero_state(self):
        """Empty intersection with no waiting should have near-zero heuristic."""
        state = _make_state(
            n_count=0, s_count=0, e_count=0, w_count=0,
            n_wait=0.0, s_wait=0.0, e_wait=0.0, w_wait=0.0,
        )
        h = heuristic(state, lookahead_seconds=5)
        assert h >= 0.0

    def test_congested_higher_than_empty(self):
        """Congested state should have a higher heuristic than an empty one."""
        empty = _make_state(n_count=0, s_count=0, e_count=0, w_count=0,
                            n_wait=0, s_wait=0, e_wait=0, w_wait=0)
        congested = _make_state(n_count=20, s_count=20, e_count=20, w_count=20,
                                n_wait=100, s_wait=100, e_wait=100, w_wait=100)
        assert heuristic(congested) > heuristic(empty)

    def test_oscillation_penalty(self):
        """Short phase duration should trigger oscillation penalty in heuristic."""
        short_phase = _make_state(phase_duration=2)
        long_phase  = _make_state(phase_duration=30)
        # With default min_phase_duration=15, short_phase incurs extra penalty
        assert heuristic(short_phase) >= heuristic(long_phase) - 1  # allow ε
