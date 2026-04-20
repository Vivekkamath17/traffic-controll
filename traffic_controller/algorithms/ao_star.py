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

Cost Backpropagation Rules:
  - AND nodes: cost = SUM of costs of ALL child subtrees (every subproblem
    must be solved). Propagate this sum upward on every node expansion.
  - OR nodes: cost = MIN of child costs (choose the best action).
  - After expanding a node, walk back up the tree via parent pointers and
    recompute costs bottom-up until the root cost stabilises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple, Set

from traffic_controller.models import Action, TrafficState
from traffic_controller.utils.cost import cost, heuristic


# ---------------------------------------------------------------------------
# Node types
# ---------------------------------------------------------------------------

class _NodeType(Enum):
    """Enumeration of node types in the AND-OR tree."""
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
    parent : _AONode | None
        Parent node in the tree (for cost backpropagation).
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
    parent: Optional["_AONode"] = None
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
        Terminal cost value for leaf nodes.
    """
    return cost(state, params) + heuristic(state, lookahead_seconds=5, params=params)


def _recompute_node_cost(node: _AONode) -> float:
    """
    Recompute the cost of a single node based on its children.

    - AND nodes: cost = SUM of costs of ALL children.
    - OR nodes: cost = MIN of child costs.
    - Leaf nodes (no children): use terminal cost.

    Parameters
    ----------
    node : _AONode
        The node to recompute cost for.

    Returns
    -------
    float
        The recomputed cost.
    """
    if not node.children:
        return node.best_cost

    if node.node_type == _NodeType.AND:
        # AND node: sum of all child costs (all must be solved)
        return sum(child.best_cost for child in node.children)
    else:
        # OR node: minimum of child costs (choose best action)
        return min(child.best_cost for child in node.children)


def _propagate_costs_upward(node: _AONode, visited: Optional[Set[int]] = None) -> None:
    """
    Propagate cost changes up the tree via parent pointers.

    Walks back up from the given node to the root, recomputing costs
    at each level until the cost stabilises (no change in one full pass).

    Parameters
    ----------
    node : _AONode
        The starting node for backpropagation.
    visited : set[int] | None
        Set of visited node ids to prevent infinite loops.
    """
    if visited is None:
        visited = set()

    current: Optional[_AONode] = node
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        old_cost = current.best_cost
        new_cost = _recompute_node_cost(current)

        if new_cost != old_cost:
            current.best_cost = new_cost
            current = current.parent
        else:
            # Cost stabilized, stop propagation
            break


def _propagate_costs_full_tree(root: _AONode, params: Dict) -> None:
    """
    Fully propagate costs bottom-up through the entire tree.

    Performs multiple passes until root cost stabilises.

    Parameters
    ----------
    root : _AONode
        Root of the AND-OR tree.
    params : dict
        Parameters for cost computation.
    """
    max_iterations = 100
    for _ in range(max_iterations):
        old_root_cost = root.best_cost

        # Post-order traversal to compute costs bottom-up
        def compute_costs_postorder(node: _AONode) -> float:
            if not node.children:
                # Leaf node
                if not node.solved:
                    node.best_cost = _terminal_cost(node.state, params)
                    node.solved = True
                return node.best_cost

            child_costs = [compute_costs_postorder(child) for child in node.children]

            if node.node_type == _NodeType.AND:
                node.best_cost = sum(child_costs)
            else:
                node.best_cost = min(child_costs)
            node.solved = True
            return node.best_cost

        compute_costs_postorder(root)

        # Check for convergence
        if abs(root.best_cost - old_root_cost) < 0.001:
            break


# ---------------------------------------------------------------------------
# Core AO* logic
# ---------------------------------------------------------------------------

def _build_and_or_tree(
    state: TrafficState,
    depth: int,
    max_depth: int,
    params: Dict,
    parent: Optional[_AONode] = None,
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
        Current intersection state.
    depth : int
        Current depth in the tree.
    max_depth : int
        Maximum depth to expand.
    params : dict
        Algorithm parameters.
    parent : _AONode | None
        Parent node for parent pointer linkage.

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
            parent=parent,
        )
        node.best_cost = _terminal_cost(state, params)
        node.solved = True
        return node

    # If blockage is present, create an AND node representing "solve for all lanes"
    # When a lane is blocked, the AND node represents "solve for N, S, E simultaneously"
    # — all three sub-signals must be scheduled, not just the non-blocked one.
    if state.has_any_blockage:
        and_node = _AONode(
            node_type=_NodeType.AND,
            state=state,
            depth=depth,
            parent=parent,
        )

        # Get blocked and unblocked lanes
        blocked_dirs = state.blocked_lanes()
        unblocked_dirs = [d for d in state.lanes if d not in blocked_dirs]

        # For each lane (blocked or not), create a sub-problem
        # This ensures all lanes are scheduled, not just unblocked ones
        children = []

        # Sub-problem for each unblocked lane - optimize normally
        for direction in unblocked_dirs:
            lane_state = state.clone()
            # Focus on this lane by reducing others' impact
            for d, lane in lane_state.lanes.items():
                if d != direction:
                    lane.vehicle_count = lane.vehicle_count // 2
            child = _build_and_or_tree(lane_state, depth + 1, max_depth, params, and_node)
            child.action_taken = f"FOCUS_{direction}"
            children.append(child)

        # Sub-problem for each blocked lane - plan for when it clears
        for direction in blocked_dirs:
            blocked_lane_state = state.clone()
            # Simulate blocked lane with boosted priority for when it clears
            for d, lane in blocked_lane_state.lanes.items():
                if d == direction:
                    # Keep the lane data but mark as temporarily blocked
                    lane.waiting_time *= 1.5  # Boost priority
                else:
                    lane.vehicle_count = lane.vehicle_count // 3
            child = _build_and_or_tree(blocked_lane_state, depth + 1, max_depth, params, and_node)
            child.action_taken = f"BLOCKED_{direction}"
            children.append(child)

        and_node.children = children
        # AND node cost = sum of ALL child subtree costs
        and_node.best_cost = sum(child.best_cost for child in children)
        and_node.solved = True
        return and_node

    # OR node: expand actions
    or_node = _AONode(
        node_type=_NodeType.OR,
        state=state,
        depth=depth,
        parent=parent,
    )
    min_phase = int(params.get("min_phase_duration", 15))

    for action_name in ("KEEP_PHASE", "SWITCH_PHASE"):
        if (
            action_name == "SWITCH_PHASE"
            and state.phase_duration < min_phase
        ):
            continue

        child_state = _apply_action(state, action_name)
        child = _build_and_or_tree(child_state, depth + 1, max_depth, params, or_node)
        child.action_taken = action_name
        or_node.children.append(child)

    if not or_node.children:
        or_node.best_cost = _terminal_cost(state, params)
        or_node.solved = True
        return or_node

    # OR node cost = min of children's costs (choose best action)
    best_child = min(or_node.children, key=lambda c: c.best_cost)
    or_node.best_cost = best_child.best_cost
    or_node.solved = True
    return or_node


def _extract_best_first_action(root: _AONode) -> str:
    """
    Walk the solved AND-OR tree to find the best first action.

    For OR nodes: follow the child with the lowest cost.
    For AND nodes: all sub-problems must be solved; we use the first
    unblocked sub-problem's first action (unblocked-lane optimisation
    takes priority in real time).

    Parameters
    ----------
    root : _AONode
        Root of the solved AND-OR tree.

    Returns
    -------
    str
        'KEEP_PHASE' or 'SWITCH_PHASE'.
    """
    node = root
    while node.children:
        if node.node_type == _NodeType.OR:
            # OR node: pick the child with minimum cost (best action)
            node = min(node.children, key=lambda c: c.best_cost)
        else:
            # AND node: all children must be solved
            # Find first child with a standard action (FOCUS_* or BLOCKED_*)
            # and extract the best action from that sub-tree
            for child in node.children:
                if child.children:
                    # Navigate into this sub-problem to find the action
                    sub_node = child
                    while sub_node.children and sub_node.node_type == _NodeType.OR:
                        sub_node = min(sub_node.children, key=lambda c: c.best_cost)
                    if sub_node.action_taken in ("KEEP_PHASE", "SWITCH_PHASE"):
                        return sub_node.action_taken
            # Fallback: use first child's path
            node = node.children[0]

    # Return the action, defaulting to KEEP_PHASE
    if node.action_taken in ("KEEP_PHASE", "SWITCH_PHASE"):
        return node.action_taken
    return "KEEP_PHASE"


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

    The AO* algorithm properly handles:
    - AND nodes: cost = SUM of costs of ALL child subtrees
    - OR nodes: cost = MIN of child costs
    - Cost backpropagation via parent pointers after each expansion

    Parameters
    ----------
    initial_state : TrafficState
        Current intersection snapshot (must have at least one blocked lane).
    max_depth : int, optional
        Maximum depth of the AND-OR tree. Default is 6.
    params : dict, optional
        Algorithm parameters (forwarded to cost/heuristic).

    Returns
    -------
    Action
        KEEP_PHASE or SWITCH_PHASE.

    Raises
    ------
    KeyError
        If the extracted action string is not a valid Action enum value.
    """
    params = params or {}
    root = _build_and_or_tree(initial_state, depth=0, max_depth=max_depth, params=params)
    _propagate_costs_full_tree(root, params)
    best_action_str = _extract_best_first_action(root)
    return Action[best_action_str]


def solve(state: TrafficState, max_depth: int = 6, params: Dict | None = None) -> Action:
    """
    Entry point to solve for the best action from the given state.

    This function provides a clean interface to get the best action from
    the root OR node of the AND-OR tree. It handles both blocked and
    unblocked scenarios.

    When a lane is blocked, the AND node represents "solve for N, S, E
    simultaneously" — all three sub-signals must be scheduled.

    Parameters
    ----------
    state : TrafficState
        Current intersection snapshot.
    max_depth : int, optional
        Maximum depth of the AND-OR tree. Default is 6.
    params : dict, optional
        Algorithm parameters (forwarded to cost/heuristic).

    Returns
    -------
    Action
        The best action (KEEP_PHASE, SWITCH_PHASE, or EMERGENCY_OVERRIDE).
        Returns EMERGENCY_OVERRIDE if state.has_any_emergency is True.
        Never returns None.

    Notes
    -----
    - Leaf nodes (terminal states) return final cost from cost function.
    - AND node costs are propagated bottom-up until root stabilises.
    - OR node always returns the action with minimum cost.
    """
    from traffic_controller.algorithms.emergency import check_emergency

    # Check for emergency first (highest priority)
    has_emergency, _ = check_emergency(state)
    if has_emergency:
        return Action.EMERGENCY_OVERRIDE

    # Use AO* for blocked lanes
    if state.has_any_blockage:
        return ao_star(state, max_depth=max_depth, params=params)

    # For unblocked states, build a simple OR-only tree
    params = params or {}
    root = _build_and_or_tree(state, depth=0, max_depth=max_depth, params=params)
    _propagate_costs_full_tree(root, params)
    best_action_str = _extract_best_first_action(root)
    return Action[best_action_str]
