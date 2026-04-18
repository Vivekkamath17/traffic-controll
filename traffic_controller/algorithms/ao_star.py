"""
ao_star.py
----------
AO* algorithm for accident / blocked-lane scenarios.

AO* works on an AND-OR tree:
  - OR nodes represent choice points (which action to take).
  - AND nodes represent subproblems that must ALL be solved (e.g. clearing
    each accessible lane independently).

When a lane is blocked, the problem decomposes:
  - Subproblem A: optimise signal for the remaining unblocked lanes.
  - Subproblem B: ensure the blocked lane gets eventual service
    once the blockage clears.

Both must be resolved before the plan is accepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple

from traffic_controller.models import Action, TrafficState
from traffic_controller.utils.cost import cost, heuristic


# ---------------------------------------------------------------------------
# Node types
# ---------------------------------------------------------------------------

class _NodeType(Enum):
    OR = auto()   # Choice node — pick the best child
    AND = auto()  # Conjunction node — all children must be solved


@dataclass
class _AONode:
    """
    Node in the AND-OR tree.

    Attributes
    ----------
    node_type : _NodeType
        Whether this is an OR (choice) or AND (conjunction) node.
    state : TrafficState
        Intersection snapshot at this node.
    action_taken : str
        Action that led to this node from its parent.
    children : list[_AONode]
        Successor nodes expanded from this node.
    solved : bool
        True once the best cost has been propagated back.
    best_cost : float
        The minimum achievable cost from this node downwards.
    depth : int
        Distance from the root.
    """

    node_type: _NodeType
    state: TrafficState
    action_taken: str = "ROOT"
    children: List["_AONode"] = field(default_factory=list)
    solved: bool = False
    best_cost: float = float("inf")
    depth: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_action(state: TrafficState, action_name: str) -> TrafficState:
    """
    Return the successor state for a given action.

    Parameters
    ----------
    state : TrafficState
    action_name : str

    Returns
    -------
    TrafficState
    """
    next_state = state.clone()
    next_state.timestamp += 1

    if action_name == "SWITCH_PHASE":
        next_state.current_phase = state.opposite_phase()
        next_state.phase_duration = 0
    else:
        next_state.phase_duration += 1

    # Clear vehicles in green unblocked lanes
    for direction in next_state.active_lanes:
        lane = next_state.lanes.get(direction)
        if lane and lane.vehicle_count > 0 and not lane.is_blocked:
            lane.vehicle_count = max(0, lane.vehicle_count - 1)

    # Accumulate waiting time for red lanes
    for direction, lane in next_state.lanes.items():
        if direction not in next_state.active_lanes:
            lane.waiting_time += 1.0

    return next_state


def _terminal_cost(state: TrafficState, params: Dict) -> float:
    """
    Leaf-node cost = current cost + heuristic estimate.

    Parameters
    ----------
    state : TrafficState
    params : dict

    Returns
    -------
    float
    """
    return cost(state, params) + heuristic(state, lookahead_seconds=5, params=params)


# ---------------------------------------------------------------------------
# Core AO* logic
# ---------------------------------------------------------------------------

def _build_and_or_tree(
    state: TrafficState,
    depth: int,
    max_depth: int,
    params: Dict,
) -> _AONode:
    """
    Recursively build the AND-OR tree from *state*.

    At each OR node we expand KEEP_PHASE and SWITCH_PHASE children.
    When the state has blocked lanes we introduce an AND node that
    captures both the "replan for active lanes" and "handle blockage"
    sub-problems.

    Parameters
    ----------
    state : TrafficState
    depth : int
    max_depth : int
    params : dict

    Returns
    -------
    _AONode
        Root of the sub-tree.
    """
    # Leaf condition
    if depth >= max_depth:
        node = _AONode(
            node_type=_NodeType.OR,
            state=state,
            depth=depth,
        )
        node.best_cost = _terminal_cost(state, params)
        node.solved = True
        return node

    # If blockage is present, create an AND node
    if state.has_any_blockage:
        and_node = _AONode(
            node_type=_NodeType.AND,
            state=state,
            depth=depth,
        )

        # Sub-problem 1: plan for unblocked lanes (clone with blocks removed)
        unblocked_state = state.clone()
        for lane in unblocked_state.lanes.values():
            lane.is_blocked = False
        sub1 = _build_and_or_tree(unblocked_state, depth + 1, max_depth, params)

        # Sub-problem 2: minimise starvation for blocked lane(s)
        blocked_only = state.clone()
        # Boost waiting time weight for blocked lanes to ensure they are served
        for k, lane in blocked_only.lanes.items():
            if not lane.is_blocked:
                lane.vehicle_count = 0
        sub2 = _build_and_or_tree(blocked_only, depth + 1, max_depth, params)

        and_node.children = [sub1, sub2]
        # AND node cost = sum of subproblem costs
        and_node.best_cost = sub1.best_cost + sub2.best_cost
        and_node.solved = True
        return and_node

    # OR node: expand actions
    or_node = _AONode(
        node_type=_NodeType.OR,
        state=state,
        depth=depth,
    )
    min_phase = int(params.get("min_phase_duration", 15))

    for action_name in ("KEEP_PHASE", "SWITCH_PHASE"):
        if (
            action_name == "SWITCH_PHASE"
            and state.phase_duration < min_phase
        ):
            continue

        child_state = _apply_action(state, action_name)
        child = _build_and_or_tree(child_state, depth + 1, max_depth, params)
        child.action_taken = action_name
        or_node.children.append(child)

    if not or_node.children:
        or_node.best_cost = _terminal_cost(state, params)
        or_node.solved = True
        return or_node

    # OR node cost = min of children's costs
    best_child = min(or_node.children, key=lambda c: c.best_cost)
    or_node.best_cost = best_child.best_cost
    or_node.solved = True
    return or_node


def _extract_best_first_action(root: _AONode) -> str:
    """
    Walk the solved AND-OR tree to find the best first action.

    For OR nodes: follow the child with the lowest cost.
    For AND nodes: both sub-problems must be solved; use sub-problem 1's
    first action (unblocked-lane optimisation takes priority in real time).

    Parameters
    ----------
    root : _AONode

    Returns
    -------
    str
        'KEEP_PHASE' or 'SWITCH_PHASE'.
    """
    node = root
    while node.children:
        if node.node_type == _NodeType.OR:
            node = min(node.children, key=lambda c: c.best_cost)
        else:
            # AND node: take the first sub-problem's first action
            node = node.children[0]

    return node.action_taken if node.action_taken != "ROOT" else "KEEP_PHASE"


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def ao_star(
    initial_state: TrafficState,
    max_depth: int = 6,
    params: Dict | None = None,
) -> Action:
    """
    Run AO* search for blocked-lane conditions.

    Builds an AND-OR tree where blocked-lane sub-problems are decomposed
    into independent AND branches, then extracts the best first action
    from the solved tree.

    Parameters
    ----------
    initial_state : TrafficState
        Current intersection snapshot (must have at least one blocked lane).
    max_depth : int
        Maximum depth of the AND-OR tree.
    params : dict, optional
        Algorithm parameters (forwarded to cost/heuristic).

    Returns
    -------
    Action
        KEEP_PHASE or SWITCH_PHASE.
    """
    params = params or {}
    root = _build_and_or_tree(initial_state, depth=0, max_depth=max_depth, params=params)
    best_action_str = _extract_best_first_action(root)
    return Action[best_action_str]
