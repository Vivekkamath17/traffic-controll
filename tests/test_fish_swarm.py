"""
test_fish_swarm.py
------------------
Unit tests for Fish Swarm Optimization.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from traffic_controller.optimization.fish_swarm import (
    FishSwarm,
    Fish,
    PARAM_NAMES,
    DEFAULT_PARAMS,
    _random_position,
)


class TestFishSwarm:
    """Tests for Fish Swarm Optimization."""

    def test_population_initializes_to_exactly_20_fish(self):
        """
        Population initialises to exactly 20 fish.
        """
        swarm = FishSwarm(population_size=20)
        assert len(swarm._swarm) == 20

    def test_population_size_custom(self):
        """
        Custom population size is respected.
        """
        swarm = FishSwarm(population_size=10)
        assert len(swarm._swarm) == 10

    def test_after_10_iterations_best_fitness_improves_or_stays_same(self):
        """
        After 10 iterations, best fitness >= initial best fitness.
        """
        # Simple fitness: maximize negative of first param
        def fitness_fn(params):
            return -params.get("min_phase_duration", 15.0)

        swarm = FishSwarm(population_size=10, fitness_fn=fitness_fn)
        initial_best = swarm._best_fish.fitness

        # Run optimization
        swarm.optimise(iterations=10)

        final_best = swarm._best_fish.fitness
        assert final_best >= initial_best

    def test_output_parameter_dict_contains_all_6_required_keys(self):
        """
        Output parameter dict contains all 6 required keys.
        """
        swarm = FishSwarm(population_size=10)
        result = swarm.optimise(iterations=5)

        required_keys = [
            "min_phase_duration",
            "max_phase_duration",
            "emergency_penalty_weight",
            "starvation_threshold",
            "beam_width",
            "congestion_threshold",
        ]

        for key in required_keys:
            assert key in result, f"Missing required key: {key}"

    def test_no_parameter_goes_outside_bounds(self):
        """
        No parameter goes outside its defined bounds after optimisation.
        """
        from traffic_controller.optimization.fish_swarm import PARAM_BOUNDS

        swarm = FishSwarm(population_size=10)
        result = swarm.optimise(iterations=10)

        for i, (name, (low, high)) in enumerate(zip(PARAM_NAMES, PARAM_BOUNDS)):
            value = result[name]
            assert low <= value <= high, f"Parameter {name}={value} out of bounds [{low}, {high}]"

    def test_fish_to_params_returns_valid_dict(self):
        """
        Fish.to_params() returns valid parameter dictionary.
        """
        pos = _random_position()
        fish = Fish(position=pos)
        params = fish.to_params()

        assert isinstance(params, dict)
        assert len(params) == len(PARAM_NAMES)

        for name in PARAM_NAMES:
            assert name in params
            assert isinstance(params[name], float)
