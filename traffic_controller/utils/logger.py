"""
logger.py
---------
Decision logger for the Adaptive Traffic Controller.

Maintains an in-memory log of every controller decision and provides
helpers to persist or retrieve log entries.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

# Configure stdlib root logger so INFO messages reach stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)

_log = logging.getLogger("traffic_controller")


@dataclass
class DecisionRecord:
    """
    A single logged controller decision.

    Attributes
    ----------
    tick : int
        Simulation tick at the time of the decision.
    algorithm : str
        Name of the algorithm that produced the action.
    action : str
        The action taken ('KEEP_PHASE', 'SWITCH_PHASE', 'EMERGENCY_OVERRIDE').
    reason : str
        Human-readable justification for the decision.
    cost : float
        Evaluated cost of the chosen state.
    phase : str
        Active signal phase after the action.
    real_timestamp : float
        Wall-clock time (seconds since epoch) when the event was logged.
    """

    tick: int
    algorithm: str
    action: str
    reason: str
    cost: float
    phase: str
    real_timestamp: float = 0.0

    def __post_init__(self) -> None:
        if self.real_timestamp == 0.0:
            self.real_timestamp = time.time()


class DecisionLogger:
    """
    Thread-safe in-memory decision logger with optional file export.

    Parameters
    ----------
    max_records : int
        Maximum number of records to keep in memory (oldest are dropped).
    """

    def __init__(self, max_records: int = 10_000) -> None:
        self._records: List[DecisionRecord] = []
        self._max_records = max_records
        self._emergency_count: int = 0

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def log(
        self,
        tick: int,
        algorithm: str,
        action: str,
        reason: str,
        cost: float,
        phase: str,
    ) -> None:
        """
        Record a controller decision.

        Parameters
        ----------
        tick : int
            Current simulation tick.
        algorithm : str
            Algorithm that made the decision.
        action : str
            Action name string.
        reason : str
            Short textual justification.
        cost : float
            Cost associated with this state/action.
        phase : str
            Active signal phase after the action.
        """
        record = DecisionRecord(
            tick=tick,
            algorithm=algorithm,
            action=action,
            reason=reason,
            cost=cost,
            phase=phase,
        )
        self._records.append(record)
        if len(self._records) > self._max_records:
            self._records.pop(0)

        if action == "EMERGENCY_OVERRIDE":
            self._emergency_count += 1
            _log.warning(
                "EMERGENCY OVERRIDE at tick %d — %s", tick, reason
            )
        else:
            _log.debug("tick=%d alg=%s action=%s", tick, algorithm, action)

    def log_emergency(self, tick: int, lane: str, phase: str) -> None:
        """
        Convenience wrapper to log an emergency override event.

        Parameters
        ----------
        tick : int
            Current simulation tick.
        lane : str
            Direction key of the emergency lane ('N', 'S', 'E', 'W').
        phase : str
            Phase assigned as a result of the override.
        """
        self.log(
            tick=tick,
            algorithm="EMERGENCY",
            action="EMERGENCY_OVERRIDE",
            reason=f"Emergency vehicle in lane {lane}",
            cost=0.0,
            phase=phase,
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def emergency_count(self) -> int:
        """Total number of emergency overrides logged so far."""
        return self._emergency_count

    def algorithm_usage(self) -> dict[str, int]:
        """
        Return a count of how many times each algorithm was used.

        Returns
        -------
        dict[str, int]
            Keys are algorithm names; values are call counts.
        """
        usage: dict[str, int] = {}
        for r in self._records:
            usage[r.algorithm] = usage.get(r.algorithm, 0) + 1
        return usage

    def recent_records(self, n: int = 20) -> List[DecisionRecord]:
        """
        Return the n most recent decision records.

        Parameters
        ----------
        n : int
            Number of records to return.

        Returns
        -------
        List[DecisionRecord]
        """
        return self._records[-n:]

    def all_records(self) -> List[DecisionRecord]:
        """Return a copy of all stored decision records."""
        return list(self._records)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def export_json(self, path: Path | str) -> None:
        """
        Serialise all records to a JSON file.

        Parameters
        ----------
        path : Path or str
            Destination file path.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(
                [asdict(r) for r in self._records],
                fh,
                indent=2,
            )
        _log.info("Decision log exported → %s (%d records)", path, len(self._records))
