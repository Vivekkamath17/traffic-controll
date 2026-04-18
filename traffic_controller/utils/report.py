"""
report.py
---------
Statistics aggregation and comparison report generation.

Produces per-algorithm averages and a comparison table versus the
fixed-timer BFS baseline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List


class ReportGenerator:
    """
    Collect simulation statistics and generate a final comparison report.

    Parameters
    ----------
    tick_count : int
        Total number of simulation ticks expected (for percentage calc).
    """

    def __init__(self, tick_count: int = 500) -> None:
        self._tick_count = tick_count

        # Per-algorithm accumulator: algorithm -> list of avg waiting times
        self._algo_wait_samples: Dict[str, List[float]] = {}

        # Lane-level statistics
        self._lane_total_wait: Dict[str, float] = {
            "N": 0.0, "S": 0.0, "E": 0.0, "W": 0.0
        }
        self._lane_ticks: Dict[str, int] = {
            "N": 0, "S": 0, "E": 0, "W": 0
        }

        # Overall
        self._total_vehicles_served: int = 0
        self._emergency_overrides: int = 0

        # Fixed-timer baseline accumulator
        self._fixed_timer_wait_samples: List[float] = []

    # ------------------------------------------------------------------
    # Recording helpers
    # ------------------------------------------------------------------

    def record_tick(
        self,
        algorithm: str,
        lane_waits: Dict[str, float],
        vehicles_served: int,
    ) -> None:
        """
        Record statistics for a single simulation tick.

        Parameters
        ----------
        algorithm : str
            Algorithm that controlled this tick.
        lane_waits : dict
            Mapping from direction ('N','S','E','W') to waiting_time.
        vehicles_served : int
            Number of vehicles that cleared the intersection this tick.
        """
        avg_wait = (
            sum(lane_waits.values()) / len(lane_waits)
            if lane_waits
            else 0.0
        )

        if algorithm not in self._algo_wait_samples:
            self._algo_wait_samples[algorithm] = []
        self._algo_wait_samples[algorithm].append(avg_wait)

        for lane, wait in lane_waits.items():
            if lane in self._lane_total_wait:
                self._lane_total_wait[lane] += wait
                self._lane_ticks[lane] += 1

        self._total_vehicles_served += vehicles_served

    def record_emergency(self) -> None:
        """Increment the emergency override counter."""
        self._emergency_overrides += 1

    def record_fixed_timer(self, avg_wait: float) -> None:
        """
        Record a fixed-timer baseline sample.

        Parameters
        ----------
        avg_wait : float
            Average waiting time for this tick under a naive fixed timer.
        """
        self._fixed_timer_wait_samples.append(avg_wait)

    # ------------------------------------------------------------------
    # Computation
    # ------------------------------------------------------------------

    def _algo_avg(self, algorithm: str) -> float:
        """Return average waiting time for a specific algorithm."""
        samples = self._algo_wait_samples.get(algorithm, [])
        return sum(samples) / len(samples) if samples else 0.0

    def _overall_avg(self) -> float:
        """Return grand average waiting time across all algorithms."""
        all_samples = [
            s for samples in self._algo_wait_samples.values() for s in samples
        ]
        return sum(all_samples) / len(all_samples) if all_samples else 0.0

    def _fixed_timer_avg(self) -> float:
        """Return average waiting time under fixed-timer baseline."""
        if not self._fixed_timer_wait_samples:
            return 0.0
        return sum(self._fixed_timer_wait_samples) / len(
            self._fixed_timer_wait_samples
        )

    # ------------------------------------------------------------------
    # Report generation
    # ------------------------------------------------------------------

    def generate(self, algorithm_usage: Dict[str, int]) -> Dict:
        """
        Build the full statistics dictionary.

        Parameters
        ----------
        algorithm_usage : dict
            Counts from the decision logger (algorithm -> calls).

        Returns
        -------
        dict
            Structured stats ready for JSON export or console display.
        """
        algo_avgs = {
            alg: round(self._algo_avg(alg), 2)
            for alg in self._algo_wait_samples
        }
        fixed_avg = round(self._fixed_timer_avg(), 2)
        overall_avg = round(self._overall_avg(), 2)

        # Improvement is measured against the normal-traffic algorithms
        # (ASTAR + BEAM) which are directly comparable to a fixed-timer.
        # EMERGENCY and AO_STAR handle special conditions that fixed-timer
        # simply ignores, so including them skews the comparison.
        normal_algos = ["ASTAR", "BEAM"]
        normal_samples = [
            s
            for alg in normal_algos
            for s in self._algo_wait_samples.get(alg, [])
        ]
        normal_avg = (
            round(sum(normal_samples) / len(normal_samples), 2)
            if normal_samples
            else overall_avg
        )

        improvement = 0.0
        if fixed_avg > 0:
            improvement = round(
                (fixed_avg - normal_avg) / fixed_avg * 100, 1
            )

        lane_avgs = {
            lane: round(
                self._lane_total_wait[lane] / max(1, self._lane_ticks[lane]),
                2,
            )
            for lane in ("N", "S", "E", "W")
        }

        return {
            "simulation_ticks": self._tick_count,
            "total_vehicles_served": self._total_vehicles_served,
            "emergency_overrides": self._emergency_overrides,
            "algorithm_usage": algorithm_usage,
            "per_algorithm_avg_wait_sec": algo_avgs,
            "per_lane_avg_wait_sec": lane_avgs,
            "overall_avg_wait_sec": overall_avg,
            "normal_traffic_avg_wait_sec": normal_avg,
            "fixed_timer_avg_wait_sec": fixed_avg,
            "improvement_over_fixed_pct": improvement,
        }

    def print_comparison_table(self, stats: Dict) -> None:
        """
        Print a formatted comparison table to stdout.

        Parameters
        ----------
        stats : dict
            Output of :meth:`generate`.
        """
        W = 52
        sep = "-" * W
        print(f"\n{'=' * W}")
        print("  SIMULATION RESULTS -- ALGORITHM COMPARISON")
        print(f"{'=' * W}")
        print(f"  Total ticks        : {stats['simulation_ticks']}")
        print(f"  Vehicles served    : {stats['total_vehicles_served']}")
        print(f"  Emergency overrides: {stats['emergency_overrides']}")

        print(f"\n{sep}")
        print(f"  {'Algorithm':<22} {'Avg Wait (s)':>10}")
        print(sep)

        algo_labels = {
            "ASTAR":     "A* Search",
            "BEAM":      "Beam Search",
            "AO_STAR":   "AO* (blocked lane)",
            "BFS":       "BFS (baseline)",
            "EMERGENCY": "Emergency Override",
        }
        for alg, avg in stats["per_algorithm_avg_wait_sec"].items():
            label = algo_labels.get(alg, alg)
            print(f"  {label:<22} {avg:>10.2f}")

        fixed  = stats["fixed_timer_avg_wait_sec"]
        normal = stats["normal_traffic_avg_wait_sec"]
        overall = stats["overall_avg_wait_sec"]
        imp    = stats["improvement_over_fixed_pct"]

        print(sep)
        print(f"  {'Fixed-Timer (30s)':<22} {fixed:>10.2f}")
        print(f"  {'Adaptive (overall)':<22} {overall:>10.2f}")
        print(f"  {'Adaptive (A*+Beam)':<22} {normal:>10.2f}  <- vs fixed")
        print(sep)
        sign = "+" if imp >= 0 else ""
        print(f"  A*/Beam vs Fixed-Timer: {sign}{imp:.1f}%")
        print(f"{'=' * W}\n")

        print("  Per-lane average incremental wait (vehicles x 1s / tick):")
        for lane, avg in stats["per_lane_avg_wait_sec"].items():
            print(f"    Lane {lane}: {avg:.2f}")
        print()

    def export_json(self, path: Path | str, stats: Dict) -> None:
        """
        Write the statistics dictionary to a JSON file.

        Parameters
        ----------
        path : Path or str
            Destination file path.
        stats : dict
            Output of :meth:`generate`.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(stats, fh, indent=2)
        print(f"  Results exported → {path}")
