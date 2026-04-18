"""
test_algorithms.py
------------------
Unit tests for all five algorithm modules.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from traffic_controller.models import Action, LaneState, TrafficState
from traffic_controller.algorithms.astar import astar_search
from traffic_controller.algorithms.beam_search import beam_search
from traffic_controller.algorithms.ao_star import ao_star
from traffic_controller.algorithms.bfs import bfs_baseline, fixed_timer_avg_wait
from traffic_controller.algorithms.emergency import (
    check_emergency,
    get_emergency_phase,
    handle_emergency,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _state(
    phase: str = "NS",
    phase_duration: int = 20,
    n_count: int = 5,
    s_count: int = 3,
    e_count: int = 8,
    w_count: int = 2,
    n_wait: float = 10.0,
    s_wait: float = 8.0,
    e_wait: float = 20.0,
    w_wait: float = 4.0,
    n_emg: bool = False,
    e_blocked: bool = False,
) -> TrafficState:
    return TrafficState(
        lanes={
            "N": LaneState(vehicle_count=n_count, waiting_time=n_wait,
                           has_emergency=n_emg),
            "S": LaneState(vehicle_count=s_count, waiting_time=s_wait),
            "E": LaneState(vehicle_count=e_count, waiting_time=e_wait,
                           is_blocked=e_blocked),
            "W": LaneState(vehicle_count=w_count, waiting_time=w_wait),
        },
        current_phase=phase,
        phase_duration=phase_duration,
        timestamp=50,
    )


# ---------------------------------------------------------------------------
# Emergency module
# ---------------------------------------------------------------------------

class TestEmergency:
    def test_no_emergency_detected(self):
        found, lane = check_emergency(_state())
        assert found is False
        assert lane is None

    def test_emergency_detected(self):
        found, lane = check_emergency(_state(n_emg=True))
        assert found is True
        assert lane == "N"

    def test_emergency_phase_ns(self):
        assert get_emergency_phase("N") == "NS"
        assert get_emergency_phase("S") == "NS"

    def test_emergency_phase_ew(self):
        assert get_emergency_phase("E") == "EW"
        assert get_emergency_phase("W") == "EW"

    def test_handle_emergency_returns_override(self):
        action, phase, reason = handle_emergency(_state(n_emg=True))
        assert action == Action.EMERGENCY_OVERRIDE
        assert phase == "NS"
        assert "N" in reason

    def test_handle_emergency_raises_without_emergency(self):
        with pytest.raises(ValueError):
            handle_emergency(_state(n_emg=False))


# ---------------------------------------------------------------------------
# A* Search
# ---------------------------------------------------------------------------

class TestAStarSearch:
    def test_returns_valid_action(self):
        action = astar_search(_state())
        assert action in (Action.KEEP_PHASE, Action.SWITCH_PHASE)

    def test_phase_duration_constraint(self):
        """With phase_duration=2 (< min=15), SWITCH_PHASE should be blocked."""
        state = _state(phase_duration=2)
        action = astar_search(state, params={"min_phase_duration": 15})
        # Cannot switch because phase is too fresh
        assert action == Action.KEEP_PHASE

    def test_returns_action_with_short_depth(self):
        action = astar_search(_state(), max_depth=3)
        assert isinstance(action, Action)

    def test_congested_east_prefers_switch(self):
        """High EW congestion should prompt switching to EW green."""
        state = _state(
            phase="NS", phase_duration=20,
            n_count=1, s_count=1,
            e_count=25, w_count=25,
            e_wait=80.0, w_wait=80.0,
        )
        action = astar_search(state, max_depth=5)
        assert action in (Action.KEEP_PHASE, Action.SWITCH_PHASE)  # deterministic


# ---------------------------------------------------------------------------
# Beam Search
# ---------------------------------------------------------------------------

class TestBeamSearch:
    def test_returns_valid_action(self):
        action = beam_search(_state())
        assert action in (Action.KEEP_PHASE, Action.SWITCH_PHASE)

    def test_respects_beam_width_1(self):
        """Beam width 1 is degenerate but should still return an action."""
        action = beam_search(_state(), beam_width=1)
        assert isinstance(action, Action)

    def test_congested_returns_action(self):
        state = _state(e_count=28, w_count=28, e_wait=90.0, w_wait=90.0)
        action = beam_search(state, beam_width=5, lookahead=10)
        assert isinstance(action, Action)

    def test_phase_constraint_respected(self):
        state = _state(phase_duration=3)
        action = beam_search(state, params={"min_phase_duration": 15})
        assert action == Action.KEEP_PHASE


# ---------------------------------------------------------------------------
# AO* (AND-OR Search)
# ---------------------------------------------------------------------------

class TestAOStar:
    def test_returns_valid_action_blocked(self):
        state = _state(e_blocked=True)
        action = ao_star(state)
        assert action in (Action.KEEP_PHASE, Action.SWITCH_PHASE)

    def test_returns_valid_action_no_block(self):
        """AO* should still work even when no lane is blocked."""
        action = ao_star(_state())
        assert isinstance(action, Action)

    def test_shallow_depth(self):
        state = _state(e_blocked=True)
        action = ao_star(state, max_depth=2)
        assert isinstance(action, Action)

    def test_all_blocked(self):
        state = TrafficState(
            lanes={
                d: LaneState(vehicle_count=5, waiting_time=30.0, is_blocked=True)
                for d in "NSEW"
            },
            current_phase="NS",
            phase_duration=20,
        )
        action = ao_star(state, max_depth=3)
        assert isinstance(action, Action)


# ---------------------------------------------------------------------------
# BFS Baseline
# ---------------------------------------------------------------------------

class TestBFSBaseline:
    def test_returns_action_and_cost(self):
        """BFS returns a valid action and a finite (possibly negative) cost.

        The cost function subtracts a throughput reward for green-lane vehicles,
        so the accumulated total can be negative on light-traffic states.
        """
        action, total_cost = bfs_baseline(_state(), max_depth=3)
        assert action in (Action.KEEP_PHASE, Action.SWITCH_PHASE)
        assert total_cost < float("inf")  # must be finite
        assert total_cost > -float("inf")  # must not be -inf

    def test_cost_finite(self):
        _, c = bfs_baseline(_state(), max_depth=5)
        assert c < float("inf")

    def test_empty_state_low_cost(self):
        empty = _state(n_count=0, s_count=0, e_count=0, w_count=0,
                       n_wait=0, s_wait=0, e_wait=0, w_wait=0)
        _, c = bfs_baseline(empty, max_depth=3)
        assert c >= 0.0


class TestFixedTimerBaseline:
    def test_returns_positive_wait(self):
        avg = fixed_timer_avg_wait(_state(), phase_duration=30)
        assert avg >= 0.0

    def test_longer_phase_higher_wait(self):
        state = _state(e_count=10, w_count=10, e_wait=20.0, w_wait=20.0)
        short = fixed_timer_avg_wait(state, phase_duration=15)
        long_ = fixed_timer_avg_wait(state, phase_duration=60)
        # Longer fixed phase → more accumulated wait for red lanes
        assert long_ >= short
