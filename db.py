"""
Simple SQLite persistence layer for the points bot.

Kept intentionally synchronous (sqlite3) since a group bot's call volume is
low -- there's no need for async DB drivers here.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# DB_PATH env var lets a persistent volume (e.g. on Railway) be pointed at
# instead of the file living next to this script, which is wiped on redeploy.
DB_PATH = Path(os.getenv("DB_PATH", str(Path(__file__).parent / "points_bot.db")))

TEAM_NAMES = {
    "team1": "Team 1",
    "team2": "Team 2",
}


def normalize_team(raw: str) -> str | None:
    """Accepts '1', 'team1', 'Team1', etc. Returns 'team1'/'team2' or None."""
    if not raw:
        return None
    raw = raw.strip().lower().replace(" ", "")
    if raw in ("1", "team1"):
        return "team1"
    if raw in ("2", "team2"):
        return "team2"
    return None


def team_display_name(team: str) -> str:
    return TEAM_NAMES.get(team, team)


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS members (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                team TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_members (
                username TEXT PRIMARY KEY,  -- lowercase, no '@'
                team TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS points_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                team TEXT,
                points INTEGER,
                reason TEXT,
                outing_id INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS pending_shares (
                user_id INTEGER PRIMARY KEY,
                outing_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

def add_or_update_member(user_id: int, username: str | None, display_name: str, team: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO members (user_id, username, display_name, team)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                display_name=excluded.display_name,
                team=excluded.team
            """,
            (user_id, (username or "").lower(), display_name, team),
        )


def find_member_by_username(username: str) -> sqlite3.Row | None:
    username = username.lstrip("@").lower()
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM members WHERE username = ?", (username,))
        return cur.fetchone()


def get_member(user_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM members WHERE user_id = ?", (user_id,))
        return cur.fetchone()


def remove_member(username: str) -> bool:
    username = username.lstrip("@").lower()
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM members WHERE username = ?", (username,))
        conn.execute("DELETE FROM pending_members WHERE username = ?", (username,))
        return cur.rowcount > 0


def get_all_members() -> list[sqlite3.Row]:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM members ORDER BY team, display_name")
        return cur.fetchall()


def add_pending_member(username: str, team: str):
    username = username.lstrip("@").lower()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO pending_members (username, team) VALUES (?, ?)
            ON CONFLICT(username) DO UPDATE SET team=excluded.team
            """,
            (username, team),
        )


def resolve_pending_member_if_any(user_id: int, username: str | None, display_name: str) -> bool:
    """Called on every group message. If this username was pre-registered
    via /addmember before they ever posted, promote them to a full member.
    Returns True if a pending record was resolved."""
    if not username:
        return False
    username = username.lower()
    with get_conn() as conn:
        cur = conn.execute("SELECT team FROM pending_members WHERE username = ?", (username,))
        row = cur.fetchone()
        if not row:
            return False
        team = row["team"]
        conn.execute(
            """
            INSERT INTO members (user_id, username, display_name, team)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username, display_name=excluded.display_name, team=excluded.team
            """,
            (user_id, username, display_name, team),
        )
        conn.execute("DELETE FROM pending_members WHERE username = ?", (username,))
        return True


def sync_member_identity(user_id: int, username: str | None, display_name: str):
    """Keep username/display_name fresh for already-registered members."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE members SET username = ?, display_name = ?
            WHERE user_id = ?
            """,
            ((username or "").lower(), display_name, user_id),
        )


# ---------------------------------------------------------------------------
# Outings & points
# ---------------------------------------------------------------------------

def create_outing(description: str) -> int:
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO outings (description) VALUES (?)", (description,))
        return cur.lastrowid


def list_outings(limit: int = 10) -> list[sqlite3.Row]:
    """Most recent outings first, with total points logged and distinct
    participant names aggregated from points_log."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT o.id, o.description, o.created_at,
                   COALESCE(SUM(pl.points), 0) AS total_points,
                   GROUP_CONCAT(DISTINCT m.display_name) AS participants
            FROM outings o
            LEFT JOIN points_log pl ON pl.outing_id = o.id
            LEFT JOIN members m ON m.user_id = pl.user_id
            GROUP BY o.id
            ORDER BY o.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return cur.fetchall()


def log_points(user_id: int, team: str, points: int, reason: str, outing_id: int | None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO points_log (user_id, team, points, reason, outing_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, team, points, reason, outing_id),
        )


def team_totals() -> dict:
    totals = {"team1": 0, "team2": 0}
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT team, SUM(points) as total FROM points_log GROUP BY team"
        )
        for row in cur.fetchall():
            if row["team"] in totals:
                totals[row["team"]] = row["total"] or 0
    return totals


def member_total_points(user_id: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT SUM(points) as total FROM points_log WHERE user_id = ?", (user_id,)
        )
        row = cur.fetchone()
        return (row["total"] or 0) if row else 0


# ---------------------------------------------------------------------------
# Pending shares (DM reflection flow)
# ---------------------------------------------------------------------------

def set_pending_share(user_id: int, outing_id: int):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO pending_shares (user_id, outing_id) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET outing_id=excluded.outing_id
            """,
            (user_id, outing_id),
        )


def get_pending_share(user_id: int) -> int | None:
    with get_conn() as conn:
        cur = conn.execute("SELECT outing_id FROM pending_shares WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return row["outing_id"] if row else None


def pop_pending_share(user_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM pending_shares WHERE user_id = ?", (user_id,))


# ---------------------------------------------------------------------------
# Settings (e.g. which group chat to post the weekly summary to)
# ---------------------------------------------------------------------------

def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )


def get_setting(key: str) -> str | None:
    with get_conn() as conn:
        cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None
