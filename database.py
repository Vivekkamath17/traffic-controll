"""
database.py
-----------
SQLite session history for Smart Adaptive Traffic Controller.

Uses Python stdlib sqlite3 only (no SQLAlchemy).
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

# Database path
DB_PATH = Path("traffic_controller.db")


def _get_connection() -> sqlite3.Connection:
    """Get a database connection with row factory."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize the database with required tables."""
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                started_at TEXT,
                duration INTEGER,
                avg_wait_astar REAL,
                avg_wait_beam REAL,
                avg_wait_fixed REAL,
                total_served INTEGER,
                emergency_overrides INTEGER,
                profile TEXT,
                results_json TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def save_session(session_id: str, stats_dict: Dict[str, Any]) -> None:
    """
    Save a session to the database.

    Parameters
    ----------
    session_id : str
        Unique session identifier.
    stats_dict : dict
        Dictionary containing session statistics.
    """
    init_db()  # Ensure table exists

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO sessions (
                id, started_at, duration, avg_wait_astar, avg_wait_beam,
                avg_wait_fixed, total_served, emergency_overrides, profile, results_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id,
            stats_dict.get("started_at", datetime.now().isoformat()),
            stats_dict.get("duration", 0),
            stats_dict.get("avg_wait_astar", 0.0),
            stats_dict.get("avg_wait_beam", 0.0),
            stats_dict.get("avg_wait_fixed", 0.0),
            stats_dict.get("total_served", 0),
            stats_dict.get("emergency_overrides", 0),
            stats_dict.get("profile", "default"),
            json.dumps(stats_dict.get("results_json", {}))
        ))
        conn.commit()
    finally:
        conn.close()


def get_sessions() -> List[Dict[str, Any]]:
    """
    Get all sessions from the database.

    Returns
    -------
    list[dict]
        List of session dictionaries.
    """
    if not DB_PATH.exists():
        return []

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions ORDER BY started_at DESC")
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """
    Get a specific session by ID.

    Parameters
    ----------
    session_id : str
        The session ID to retrieve.

    Returns
    -------
    dict | None
        Session dictionary if found, None otherwise.
    """
    if not DB_PATH.exists():
        return None

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None
    finally:
        conn.close()


def delete_session(session_id: str) -> bool:
    """
    Delete a session by ID.

    Parameters
    ----------
    session_id : str
        The session ID to delete.

    Returns
    -------
    bool
        True if deleted, False if not found.
    """
    if not DB_PATH.exists():
        return False

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# Initialize database on module load
init_db()
