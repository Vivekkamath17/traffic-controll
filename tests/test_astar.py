"""
test_astar.py
-------------
Unit tests for A* search algorithm.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from traffic_controller.models import Action, LaneState, TrafficState
from traffic_controller.algorithms.astar import astar_search


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


class TestAStarSearch:
    """Tests for A* search algorithm."""

    def test_returns_keep_phase_when_ns_high_and_green(self):
        """
        Returns KEEP_PHASE when NS has high count and is currently green.
        """
        # NS phase with high N/S counts - should keep to clear them
        state = _make_state(
            phase="NS",
            n_count=15, s_count=12,  # High counts on green lanes
            e_count=3, w_count=2,    # Low counts on red lanes
            n_wait=50.0, s_wait=40.0,
            e_wait=10.0, w_wait=5.0,
        )
        action = astar_search(state, max_depth=5)
        assert action == Action.KEEP_PHASE

    def test_returns_switch_phase_when_idle_phase_accumulated(self):
        """
        Returns SWITCH_PHASE when idle phase has accumulated more vehicles.
        """
        # NS phase with low N/S counts but high E/W counts
        state = _make_state(
            phase="NS",
            n_count=3, s_count=2,    # Low counts on green lanes
            e_count=15, w_count=12,  # High counts on red lanes
            n_wait=10.0, s_wait=5.0,
            e_wait=60.0, w_wait=55.0,
        )
        action = astar_search(state, max_depth=5)
        # Should switch to clear the congested EW lanes
        assert action == Action.SWITCH_PHASE

    def test_never_returns_emergency_override(self):
        """
        Never returns EMERGENCY_OVERRIDE (handled upstream).
        """
        state = _make_state()
        action = astar_search(state, max_depth=5)
        assert action != Action.EMERGENCY_OVERRIDE

    def test_runs_under_100ms_for_depth_5(self):
        """
        Runs in under 100ms for depth=5 (performance assertion).
        """
        state = _make_state(
            n_count=10, s_count=8, e_count=12, w_count=6,
            n_wait=40.0, s_wait=35.0, e_wait=50.0, w_wait=30.0,
        )

        start = time.perf_counter()
        action = astar_search(state, max_depth=5)
        elapsed = (time.perf_counter() - start) * 1000  # Convert to ms

        assert elapsed < 100.0, f"A* took {elapsed:.2f}ms, expected < 100ms"
        assert action in [Action.KEEP_PHASE, Action.SWITCH_PHASE]
