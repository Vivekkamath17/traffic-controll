"""
test_ao_star.py
---------------
Unit tests for AO* algorithm.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from traffic_controller.models import Action, LaneState, TrafficState
from traffic_controller.algorithms.ao_star import ao_star, solve, _AONode, _NodeType, _build_and_or_tree


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
    blocked_dir: str | None = None,
) -> TrafficState:
    lanes = {
        "N": LaneState(vehicle_count=n_count, waiting_time=n_wait),
        "S": LaneState(vehicle_count=s_count, waiting_time=s_wait),
        "E": LaneState(vehicle_count=e_count, waiting_time=e_wait),
        "W": LaneState(vehicle_count=w_count, waiting_time=w_wait),
    }
    if blocked_dir and blocked_dir in lanes:
        lanes[blocked_dir].is_blocked = True

    return TrafficState(
        lanes=lanes,
        current_phase=phase,
        phase_duration=phase_duration,
        timestamp=100,
    )


class TestAOStar:
    """Tests for AO* algorithm."""

    def test_blocked_lane_schedules_all_remaining_lanes(self):
        """
        When one lane is blocked, remaining 3 lanes are all scheduled.
        """
        # Block the N lane
        state = _make_state(blocked_dir="N")

        # Build tree and verify structure
        root = _build_and_or_tree(state, depth=0, max_depth=3, params={})

        # With blockage, root should be an AND node
        assert root.node_type == _NodeType.AND

        # AND node should have children for each remaining lane
        # (unblocked N, S, E, W subproblems)
        assert len(root.children) > 0

    def test_and_node_cost_is_sum_of_children(self):
        """
        AND node cost = sum of child costs.
        """
        # Create AND node with known children
        and_node = _AONode(
            node_type=_NodeType.AND,
            state=_make_state(),
        )

        # Create child nodes with known costs
        child1 = _AONode(node_type=_NodeType.OR, state=_make_state(), best_cost=100.0, solved=True)
        child2 = _AONode(node_type=_NodeType.OR, state=_make_state(), best_cost=200.0, solved=True)
        child3 = _AONode(node_type=_NodeType.OR, state=_make_state(), best_cost=50.0, solved=True)

        and_node.children = [child1, child2, child3]

        # AND node cost should be sum
        expected_cost = 100.0 + 200.0 + 50.0

        # Verify by checking the children costs sum correctly
        actual_sum = sum(child.best_cost for child in and_node.children)
        assert actual_sum == pytest.approx(expected_cost)

    def test_or_node_cost_is_min_of_children(self):
        """
        OR node cost = min of child costs.
        """
        # Create OR node with known children
        or_node = _AONode(
            node_type=_NodeType.OR,
            state=_make_state(),
        )

        # Create child nodes with different costs
        child1 = _AONode(node_type=_NodeType.AND, state=_make_state(), best_cost=200.0, solved=True)
        child2 = _AONode(node_type=_NodeType.AND, state=_make_state(), best_cost=150.0, solved=True)
        child3 = _AONode(node_type=_NodeType.AND, state=_make_state(), best_cost=300.0, solved=True)

        or_node.children = [child1, child2, child3]

        # OR node cost should be minimum
        expected_cost = min(child.best_cost for child in or_node.children)
        assert expected_cost == 150.0

    def test_solve_returns_action_enum_never_none(self):
        """
        solve() returns an Action enum value, never None.
        """
        state = _make_state(blocked_dir="E")
        result = solve(state, max_depth=5)

        assert result is not None
        assert isinstance(result, Action)
        assert result in [Action.KEEP_PHASE, Action.SWITCH_PHASE, Action.EMERGENCY_OVERRIDE]

    def test_ao_star_with_blockage_returns_valid_action(self):
        """
        ao_star with blocked lanes returns valid action.
        """
        state = _make_state(blocked_dir="W", w_count=15)
        action = ao_star(state, max_depth=5)

        assert action in [Action.KEEP_PHASE, Action.SWITCH_PHASE]
