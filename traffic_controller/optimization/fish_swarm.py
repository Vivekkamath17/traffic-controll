"""
fish_swarm.py
-------------
Fish Swarm Optimization (FSO) for tuning controller parameters.

Inspired by artificial fish-swarm algorithms (AFSA).  Each "fish" is a
vector of controller hyper-parameters.  The swarm evolves by:
  1. Prey   — random local perturbation (exploration).
  2. Swarm  — move toward the group centre if it is fitter.
  3. Follow — move toward the best neighbor fish.

Run after every 100 simulation ticks.  Fitness = negative average
waiting time over the last 100 ticks (higher = better).
"""

from __future__ import annotations

import random
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Parameter bounds
# ---------------------------------------------------------------------------

PARAM_NAMES: List[str] = [
    "min_phase_duration",   # [5, 60]
    "max_phase_duration",   # [20, 120]
    "emergency_penalty",    # [1000, 50000]
    "starvation_threshold", # [30, 300]
    "beam_width",           # [1, 10]
    "congestion_threshold", # [5, 30]
]

PARAM_BOUNDS: List[Tuple[float, float]] = [
    (5.0, 60.0),
    (20.0, 120.0),
    (1000.0, 50_000.0),
    (30.0, 300.0),
    (1.0, 10.0),
    (5.0, 30.0),
]

DEFAULT_PARAMS: Dict[str, float] = {
    name: (low + high) / 2
    for name, (low, high) in zip(PARAM_NAMES, PARAM_BOUNDS)
}


# ---------------------------------------------------------------------------
# Fish
# ---------------------------------------------------------------------------

@dataclass
class Fish:
    """
    A single fish in the swarm.

    Attributes
    ----------
    position : np.ndarray
        Parameter vector (length == len(PARAM_NAMES)).
    fitness : float
        Current fitness score (higher = better).
    """

    position: np.ndarray
    fitness: float = -float("inf")

    def to_params(self) -> Dict[str, float]:
        """
        Convert the position vector to a named parameter dictionary.

        Returns
        -------
        dict[str, float]
            Keys match :data:`PARAM_NAMES`.
        """
        return {
            name: float(np.clip(self.position[i], lo, hi))
            for i, (name, (lo, hi)) in enumerate(
                zip(PARAM_NAMES, PARAM_BOUNDS)
            )
        }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _clip_position(pos: np.ndarray) -> np.ndarray:
    """Clip a position vector to the parameter bounds."""
    return np.array(
        [
            float(np.clip(pos[i], lo, hi))
            for i, (_, (lo, hi)) in enumerate(zip(PARAM_NAMES, PARAM_BOUNDS))
        ],
        dtype=float,
    )


def _random_position() -> np.ndarray:
    """Generate a uniformly random position within parameter bounds."""
    return np.array(
        [random.uniform(lo, hi) for _, (lo, hi) in zip(PARAM_NAMES, PARAM_BOUNDS)],
        dtype=float,
    )


# ---------------------------------------------------------------------------
# Swarm
# ---------------------------------------------------------------------------

class FishSwarm:
    """
    Fish Swarm Optimiser for adaptive traffic controller parameters.

    Parameters
    ----------
    population_size : int
        Number of fish in the swarm.
    step_size : float
        Maximum perturbation per dimension during prey behaviour.
    visual_range : float
        Fraction of parameter range within which neighbours are visible.
    crowding_factor : float
        Prevent overcrowding (move only if centre/best is less crowded).
    fitness_fn : callable
        Function that maps a params dict → float fitness score.
        Fitness = negative of average waiting time (higher = better).
    """

    def __init__(
        self,
        population_size: int = 20,
        step_size: float = 0.1,
        visual_range: float = 0.3,
        crowding_factor: float = 0.618,
        fitness_fn: Callable[[Dict[str, float]], float] | None = None,
    ) -> None:
        self._n = population_size
        self._step = step_size
        self._visual = visual_range
        self._delta = crowding_factor
        self._fitness_fn: Callable[[Dict[str, float]], float] = (
            fitness_fn or self._default_fitness
        )

        # Threading lock to protect parameter writes
        self._lock = threading.Lock()

        # Initialise swarm
        self._swarm: List[Fish] = [
            Fish(position=_random_position()) for _ in range(self._n)
        ]
        self._best_fish: Fish = self._swarm[0]

    # ------------------------------------------------------------------
    # Default / override fitness
    # ------------------------------------------------------------------

    @staticmethod
    def _default_fitness(params: Dict[str, float]) -> float:
        """
        Placeholder fitness: reward shorter phase durations (quick test).
        Real use must supply a fitness_fn from the simulator.
        """
        return -params.get("min_phase_duration", 15.0)

    def set_fitness_fn(self, fn: Callable[[Dict[str, float]], float]) -> None:
        """
        Override the fitness function after construction.

        Parameters
        ----------
        fn : callable
            params dict → float.
        """
        self._fitness_fn = fn

    # ------------------------------------------------------------------
    # Behaviour implementations
    # ------------------------------------------------------------------

    def _evaluate_all(self) -> None:
        """Evaluate fitness for every fish and track the global best."""
        for fish in self._swarm:
            fish.fitness = self._fitness_fn(fish.to_params())

        self._best_fish = max(self._swarm, key=lambda f: f.fitness)

    def _prey(self, fish: Fish) -> np.ndarray:
        """
        Prey behaviour: random local search around current position.

        Try *try_count* random neighbours; move toward the best if it is
        fitter than the current position.

        Parameters
        ----------
        fish : Fish

        Returns
        -------
        np.ndarray
            Candidate next position.
        """
        try_count = 5
        best_neighbour = fish.position.copy()
        best_fit = fish.fitness

        for _ in range(try_count):
            delta = np.array(
                [
                    random.uniform(-self._step * (hi - lo), self._step * (hi - lo))
                    for _, (lo, hi) in zip(PARAM_NAMES, PARAM_BOUNDS)
                ],
                dtype=float,
            )
            candidate = _clip_position(fish.position + delta)
            fit = self._fitness_fn({
                name: float(candidate[i])
                for i, name in enumerate(PARAM_NAMES)
            })
            if fit > best_fit:
                best_fit = fit
                best_neighbour = candidate

        return best_neighbour

    def _swarm_behaviour(self, fish: Fish) -> np.ndarray:
        """
        Swarm behaviour: move toward the centre of visible neighbours.

        Parameters
        ----------
        fish : Fish

        Returns
        -------
        np.ndarray
            Candidate next position (may equal current position).
        """
        # Compute pairwise distances (simplified: always use full swarm)
        centre = np.mean([f.position for f in self._swarm], axis=0)
        centre_params = {name: float(centre[i]) for i, name in enumerate(PARAM_NAMES)}
        centre_fit = self._fitness_fn(centre_params)

        if centre_fit > fish.fitness:
            # Move toward centre
            step_vec = centre - fish.position
            norm = np.linalg.norm(step_vec)
            if norm > 1e-9:
                step_vec = step_vec / norm
            candidate = _clip_position(
                fish.position
                + step_vec * self._step * random.random()
            )
            return candidate

        return fish.position.copy()

    def _follow(self, fish: Fish) -> np.ndarray:
        """
        Follow behaviour: move toward the global best fish.

        Parameters
        ----------
        fish : Fish

        Returns
        -------
        np.ndarray
            Candidate next position.
        """
        best_pos = self._best_fish.position
        step_vec = best_pos - fish.position
        norm = np.linalg.norm(step_vec)
        if norm < 1e-9:
            return _clip_position(self._prey(fish))

        step_vec = step_vec / norm
        candidate = _clip_position(
            fish.position + step_vec * self._step * random.random()
        )
        return candidate

    # ------------------------------------------------------------------
    # Main optimisation step
    # ------------------------------------------------------------------

    def _update_fish(self, fish: Fish) -> None:
        """
        Apply one iteration of FSO behaviours to a single fish.

        Priority: Prey → Swarm → Follow.

        Parameters
        ----------
        fish : Fish
            Fish to update (mutated in-place).
        """
        # Try prey
        prey_pos = self._prey(fish)
        prey_fit = self._fitness_fn({
            name: float(prey_pos[i]) for i, name in enumerate(PARAM_NAMES)
        })

        if prey_fit > fish.fitness:
            fish.position = prey_pos
            fish.fitness = prey_fit
            return

        # Try swarm
        swarm_pos = self._swarm_behaviour(fish)
        swarm_fit = self._fitness_fn({
            name: float(swarm_pos[i]) for i, name in enumerate(PARAM_NAMES)
        })

        if swarm_fit > fish.fitness:
            fish.position = swarm_pos
            fish.fitness = swarm_fit
            return

        # Fall back to follow
        follow_pos = self._follow(fish)
        follow_fit = self._fitness_fn({
            name: float(follow_pos[i]) for i, name in enumerate(PARAM_NAMES)
        })
        if follow_fit > fish.fitness:
            fish.position = follow_pos
            fish.fitness = follow_fit

    def optimise(self, iterations: int = 10) -> Dict[str, float]:
        """
        Run the fish swarm optimisation for a number of iterations.

        Thread-safe: uses internal lock to protect parameter writes.

        Parameters
        ----------
        iterations : int
            Number of swarm update rounds.

        Returns
        -------
        dict[str, float]
            Best parameter set found during optimisation.
        """
        with self._lock:
            self._evaluate_all()

            for _ in range(iterations):
                for fish in self._swarm:
                    self._update_fish(fish)

                # Refresh global best
                current_best = max(self._swarm, key=lambda f: f.fitness)
                if current_best.fitness > self._best_fish.fitness:
                    self._best_fish = deepcopy(current_best)

            best_params = self._best_fish.to_params()
            # Ensure integer params are rounded
            best_params["min_phase_duration"] = round(best_params["min_phase_duration"])
            best_params["max_phase_duration"] = round(best_params["max_phase_duration"])
            best_params["beam_width"] = round(best_params["beam_width"])
            best_params["congestion_threshold"] = round(best_params["congestion_threshold"])
            return best_params

    def run(self, iterations: int = 10) -> Dict[str, float]:
        """
        Compatibility wrapper used by server/controller orchestration.
        Returns complete, typed optimized parameter dictionary.
        """
        best_params = self.optimise(iterations=iterations)
        return {
            "min_phase_duration": int(best_params.get("min_phase_duration", 15)),
            "max_phase_duration": int(best_params.get("max_phase_duration", 60)),
            "emergency_penalty": float(best_params.get("emergency_penalty", 10000)),
            "starvation_threshold": int(best_params.get("starvation_threshold", 120)),
            "beam_width": int(best_params.get("beam_width", 5)),
            "congestion_threshold": int(best_params.get("congestion_threshold", 20)),
        }

    @property
    def best_params(self) -> Dict[str, float]:
        """Return the current best parameter set without re-optimising."""
        with self._lock:
            return self._best_fish.to_params()

    def get_best_params_threadsafe(self) -> Dict[str, float]:
        """Thread-safe access to best parameters during optimization."""
        with self._lock:
            if self._best_fish:
                return self._best_fish.to_params()
            return DEFAULT_PARAMS.copy()
