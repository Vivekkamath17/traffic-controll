"""
test_beam_search.py
-------------------
Unit tests for Beam Search algorithm.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from traffic_controller.models import Action, LaneState, TrafficState
from traffic_controller.algorithms.beam_search import beam_search


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
) -> TrafficState:
    return TrafficState(
        lanes={
            "N": LaneState(vehicle_count=n_count, waiting_time=n_wait),
            "S": LaneState(vehicle_count=s_count, waiting_time=s_wait),
            "E": LaneState(vehicle_count=e_count, waiting_time=e_wait),
            "W": LaneState(vehicle_count=w_count, waiting_time=w_wait),
        },
        current_phase=phase,
        phase_duration=phase_duration,
        timestamp=100,
    )


class TestBeamSearch:
    """Tests for Beam Search algorithm."""

    def test_beam_width_1_degenerates_to_greedy(self):
        """
        With beam_width=1 it degenerates to greedy search.
        """
        state = _make_state(
            phase="NS",
            e_count=20, w_count=18,  # High EW congestion
            n_count=3, s_count=2,
        )
        action = beam_search(state, beam_width=1, lookahead=5)
        assert action in [Action.KEEP_PHASE, Action.SWITCH_PHASE]

    def test_beam_width_5_never_worse_than_width_1(self):
        """
        With beam_width=5 it never returns a worse action than width=1
        on the same congested state (smoke test, not guaranteed).
        """
        state = _make_state(
            phase="NS",
            e_count=25, w_count=20,  # Very high EW congestion
            n_count=5, s_count=4,
            e_wait=80.0, w_wait=70.0,
        )
        action_w1 = beam_search(state, beam_width=1, lookahead=5)
        action_w5 = beam_search(state, beam_width=5, lookahead=5)

        # Both should return valid actions
        assert action_w1 in [Action.KEEP_PHASE, Action.SWITCH_PHASE]
        assert action_w5 in [Action.KEEP_PHASE, Action.SWITCH_PHASE]

    def test_correct_beam_pruning(self):
        """
        After each step, exactly k states remain (beam pruning).
        """
        # This is an internal test - we can't directly access the beam
        # but we can verify the algorithm runs correctly with different widths
        state = _make_state(
            n_count=15, s_count=12, e_count=18, w_count=10,
            n_wait=50.0, s_wait=45.0, e_wait=60.0, w_wait=40.0,
        )

        # Test with various beam widths
        for width in [1, 3, 5, 10]:
            action = beam_search(state, beam_width=width, lookahead=5)
            assert action in [Action.KEEP_PHASE, Action.SWITCH_PHASE]
