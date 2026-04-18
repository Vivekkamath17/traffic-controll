"""
controller.py
-------------
Adaptive Traffic Signal Controller.

Routes each tick to the appropriate algorithm based on observed
traffic conditions, logs the decision, and periodically triggers
Fish Swarm Optimization to tune its own parameters.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from traffic_controller.algorithms.ao_star import ao_star
from traffic_controller.algorithms.astar import astar_search
from traffic_controller.algorithms.beam_search import beam_search
from traffic_controller.algorithms.bfs import bfs_baseline, fixed_timer_avg_wait
from traffic_controller.algorithms.emergency import handle_emergency, check_emergency
from traffic_controller.models import Action, TrafficState
from traffic_controller.optimization.fish_swarm import DEFAULT_PARAMS, FishSwarm
from traffic_controller.utils.cost import cost
from traffic_controller.utils.logger import DecisionLogger


class AdaptiveController:
    """
    Hybrid AI controller for a 4-way traffic intersection.

    Algorithm routing logic
    -----------------------
    Priority 1 — EMERGENCY  : any lane has  has_emergency == True
    Priority 2 — AO*        : any lane has  is_blocked    == True
    Priority 3 — BEAM SEARCH: vehicle_count >= congestion_threshold
                               OR total vehicles > 60
    Priority 4 — A*         : default (light/moderate traffic)

    Parameters
    ----------
    params : dict, optional
        Initial algorithm parameters.  Defaults to
        :data:`~traffic_controller.optimization.fish_swarm.DEFAULT_PARAMS`.
    fso_interval : int
        Number of ticks between Fish Swarm optimisation runs.
    simulator_ref : object, optional
        Reference to the running simulator (needed for FSO fitness).
    """

    def __init__(
        self,
        params: Dict | None = None,
        fso_interval: int = 100,
        simulator_ref=None,
    ) -> None:
        self.params: Dict = dict(DEFAULT_PARAMS) if params is None else dict(params)
        self._fso_interval = fso_interval
        self._simulator = simulator_ref
        self._logger = DecisionLogger()
        self._fso: FishSwarm | None = None

        # Initialise FSO if simulator is wired up
        if self._simulator is not None:
            self._init_fso()

        # Track emergency override hold time
        self._emergency_hold_ticks: int = 0
        self._emergency_target_phase: str | None = None

    # ------------------------------------------------------------------
    # Algorithm selection
    # ------------------------------------------------------------------

    def select_algorithm(self, state: TrafficState) -> str:
        """
        Determine which algorithm should handle the current state.

        Parameters
        ----------
        state : TrafficState

        Returns
        -------
        str
            One of: 'EMERGENCY', 'AO_STAR', 'BEAM', 'ASTAR'.
        """
        if state.has_any_emergency:
            return "EMERGENCY"

        if state.has_any_blockage:
            return "AO_STAR"

        congestion_thresh = self.params.get("congestion_threshold", 20)
        if (
            any(
                lane.vehicle_count >= congestion_thresh
                for lane in state.lanes.values()
            )
            or state.total_vehicles > 60
        ):
            return "BEAM"

        return "ASTAR"

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------

    def decide(self, state: TrafficState) -> tuple[Action, str | None, str]:
        """
        Choose and return the best action for the current state.

        Parameters
        ----------
        state : TrafficState
            Current intersection snapshot.

        Returns
        -------
        tuple[Action, str | None, str]
            (action, target_phase, algorithm_used)
            target_phase is only set for EMERGENCY_OVERRIDE; None otherwise.
            algorithm_used is the name of the algorithm that decided.
        """
        # Respect emergency hold
        if self._emergency_hold_ticks > 0:
            self._emergency_hold_ticks -= 1
            self._logger.log(
                tick=state.timestamp,
                algorithm="EMERGENCY",
                action="EMERGENCY_OVERRIDE",
                reason=f"Holding emergency phase {self._emergency_target_phase}",
                cost=cost(state, self.params),
                phase=self._emergency_target_phase or state.current_phase,
            )
            return Action.EMERGENCY_OVERRIDE, self._emergency_target_phase, "EMERGENCY"

        algorithm = self.select_algorithm(state)
        action, target_phase, reason = self._route(algorithm, state)

        # _route may force a SWITCH_PHASE (max duration override) — keep label accurate
        effective_algo = algorithm if action != Action.SWITCH_PHASE or algorithm == "EMERGENCY" else algorithm

        self._logger.log(
            tick=state.timestamp,
            algorithm=effective_algo,
            action=action.name,
            reason=reason,
            cost=cost(state, self.params),
            phase=self._resolved_phase(state, action, target_phase),
        )

        return action, target_phase, effective_algo

    # ------------------------------------------------------------------
    # FSO parameter optimisation
    # ------------------------------------------------------------------

    def optimize_params(self) -> Dict:
        """
        Run Fish Swarm Optimisation and update controller parameters.

        Should be called every *fso_interval* ticks.

        Returns
        -------
        dict
            Updated parameter dictionary.
        """
        if self._fso is None:
            self._init_fso()

        new_params = self._fso.optimise(iterations=10)  # type: ignore[union-attr]
        self.params.update(new_params)
        return self.params

    def maybe_optimize(self, tick: int) -> bool:
        """
        Trigger FSO if *tick* is a multiple of *fso_interval*.

        Parameters
        ----------
        tick : int
            Current simulation tick.

        Returns
        -------
        bool
            True if optimisation was run this call.
        """
        if tick > 0 and tick % self._fso_interval == 0:
            self.optimize_params()
            return True
        return False

    # ------------------------------------------------------------------
    # Property helpers
    # ------------------------------------------------------------------

    @property
    def logger(self) -> DecisionLogger:
        """Expose the internal decision logger."""
        return self._logger

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _init_fso(self) -> None:
        """Create the Fish Swarm instance wired to simulator fitness."""
        self._fso = FishSwarm(
            population_size=20,
            fitness_fn=self._simulator.fso_fitness
            if self._simulator is not None
            else None,
        )

    def _route(
        self, algorithm: str, state: TrafficState
    ) -> Tuple[Action, str | None, str]:
        """
        Dispatch to the chosen algorithm and return (action, phase, reason).

        Parameters
        ----------
        algorithm : str
        state : TrafficState

        Returns
        -------
        tuple[Action, str | None, str]
        """
        # --- Max phase duration enforcement (anti-starvation safety valve) ---
        max_phase = int(self.params.get("max_phase_duration", 60))
        if state.phase_duration >= max_phase and algorithm != "EMERGENCY":
            return (
                Action.SWITCH_PHASE,
                None,
                f"Max phase duration ({max_phase}s) reached → forced switch",
            )
        if algorithm == "EMERGENCY":
            action, target_phase, reason = handle_emergency(state)
            # Lock emergency phase for 15 ticks (realistic urban ambulance clearance)
            self._emergency_hold_ticks = 14  # current tick accounts for 1
            self._emergency_target_phase = target_phase
            return action, target_phase, reason

        elif algorithm == "AO_STAR":
            action = ao_star(state, params=self.params)
            return action, None, "Blocked lane detected → AO* replanning"

        elif algorithm == "BEAM":
            bw = int(self.params.get("beam_width", 5))
            action = beam_search(state, beam_width=bw, params=self.params)
            return action, None, f"High congestion → Beam Search (k={bw})"

        else:  # ASTAR
            action = astar_search(state, params=self.params)
            return action, None, "Normal traffic → A* search"

    @staticmethod
    def _resolved_phase(
        state: TrafficState, action: Action, target_phase: str | None
    ) -> str:
        """
        Compute the phase that will be active after *action*.

        Parameters
        ----------
        state : TrafficState
        action : Action
        target_phase : str | None

        Returns
        -------
        str
        """
        if action == Action.SWITCH_PHASE:
            return state.opposite_phase()
        if action == Action.EMERGENCY_OVERRIDE and target_phase:
            return target_phase
        return state.current_phase
