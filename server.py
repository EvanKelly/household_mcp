"""Household task tracking MCP server."""

import os
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# FastMCP constructor
# ---------------------------------------------------------------------------
mcp = FastMCP("household")

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("HOUSEHOLD_DB_PATH", "household.db")

VALID_CADENCE_UNITS = ["days", "weeks", "months"]
UNIT_DAYS = {"days": 1, "weeks": 7, "months": 30}

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY_NUMBER = {name: i for i, name in enumerate(DAY_NAMES)}  # Monday=0 … Sunday=6


def _parse_scheduled_days(raw: str | None) -> set[int]:
    """Parse 'Monday,Thursday' → {0, 3}. Returns empty set for None/invalid."""
    if not raw:
        return set()
    return {DAY_NUMBER[d.strip()] for d in raw.split(",") if d.strip() in DAY_NUMBER}


def _cadence_days(row) -> int | None:
    """Return how many days this cadence represents, or None for one-time tasks."""
    value = row["cadence_value"] if hasattr(row, "keys") else row.get("cadence_value")
    unit = row["cadence_unit"] if hasattr(row, "keys") else row.get("cadence_unit")
    if value is None or unit is None:
        return None
    return int(value) * UNIT_DAYS[unit]


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


PACKING_STATUSES = ["Need", "Have", "packed"]
PACKING_NEXT_STATUS = {
    "Need": "Have",
    "Have": "packed",
}
DEFAULT_BAGS = ["Orange Suitcase", "Green Suitcase", "Black Tote"]


def _canonical_status(s):
    """Case-insensitive lookup. Returns the canonical PACKING_STATUSES value or None."""
    if not isinstance(s, str):
        return None
    lower = s.strip().lower()
    # Accept legacy values too so old API clients keep working.
    legacy = {"need to buy": "Need", "need to pack": "Have"}
    if lower in legacy:
        return legacy[lower]
    for canonical in PACKING_STATUSES:
        if canonical.lower() == lower:
            return canonical
    return None


def _init_db() -> None:
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            cadence_value INTEGER,
            cadence_unit TEXT CHECK(cadence_unit IN ('days', 'weeks', 'months') OR cadence_unit IS NULL),
            scheduled_days TEXT,
            notes TEXT,
            last_completed TEXT,
            completed_by TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            next_due TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # Migration: old fixed cadence column → cadence_value + cadence_unit
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "cadence" in cols:
        for col_def in ("cadence_value INTEGER", "cadence_unit TEXT"):
            try:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass
        conn.execute("""
            UPDATE tasks SET
                cadence_value = CASE cadence
                    WHEN 'weekly'    THEN 1
                    WHEN 'monthly'   THEN 1
                    WHEN 'quarterly' THEN 3
                END,
                cadence_unit = CASE cadence
                    WHEN 'weekly'    THEN 'weeks'
                    WHEN 'monthly'   THEN 'months'
                    WHEN 'quarterly' THEN 'months'
                END
            WHERE cadence IS NOT NULL AND cadence_value IS NULL
        """)
    # Migration: add remaining columns if missing (older DBs)
    for col_def in (
        "sort_order INTEGER NOT NULL DEFAULT 0",
        "due_date TEXT",
        "completed_by TEXT",
        "cadence_value INTEGER",
        "cadence_unit TEXT",
        "scheduled_days TEXT",
        "next_due TEXT",
    ):
        try:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass
    # Migration: unify due_date → next_due
    conn.execute("UPDATE tasks SET next_due = due_date WHERE next_due IS NULL AND due_date IS NOT NULL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            task_title TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            completed_by TEXT
        )
    """)

    # Packing list tables
    conn.execute("""
        CREATE TABLE IF NOT EXISTS packing_bags (
            name TEXT PRIMARY KEY,
            sort_order INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS packing_items (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('Need', 'Have', 'packed')),
            bag TEXT,
            priority INTEGER,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    # Migration: rebuild packing_items if it has either the old status CHECK
    # constraint or a NOT NULL bag column. Both renames are applied in a single
    # rebuild so the server converges from any earlier state in one boot.
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='packing_items'"
    ).fetchone()
    schema_sql = schema_row[0] if schema_row else ""
    needs_rebuild = (
        "'need to buy'" in schema_sql
        or "'need to pack'" in schema_sql
        or "bag TEXT NOT NULL" in schema_sql
    )
    if needs_rebuild:
        conn.execute("ALTER TABLE packing_items RENAME TO packing_items_old")
        conn.execute("""
            CREATE TABLE packing_items (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('Need', 'Have', 'packed')),
                bag TEXT,
                priority INTEGER,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO packing_items (id, title, status, bag, priority, sort_order, created_at)
            SELECT id, title,
                CASE status
                    WHEN 'need to buy'  THEN 'Need'
                    WHEN 'need to pack' THEN 'Have'
                    ELSE status
                END,
                bag, priority, sort_order, created_at
            FROM packing_items_old
        """)
        conn.execute("DROP TABLE packing_items_old")

    # Migration: rename old default bags to the new canonical names.
    bag_renames = [
        ("Green", "Green Suitcase"),
        ("Orange", "Orange Suitcase"),
        ("Carry-on Tote", "Black Tote"),
    ]
    for old, new in bag_renames:
        if not conn.execute("SELECT 1 FROM packing_bags WHERE name = ?", (old,)).fetchone():
            continue
        if conn.execute("SELECT 1 FROM packing_bags WHERE name = ?", (new,)).fetchone():
            # Both exist: merge items, drop the old row.
            conn.execute("UPDATE packing_items SET bag = ? WHERE bag = ?", (new, old))
            conn.execute("DELETE FROM packing_bags WHERE name = ?", (old,))
        else:
            # Only old exists: rename in both tables.
            conn.execute("UPDATE packing_items SET bag = ? WHERE bag = ?", (new, old))
            conn.execute("UPDATE packing_bags SET name = ? WHERE name = ?", (new, old))

    # Seed default bags if empty (fresh installs).
    existing = conn.execute("SELECT COUNT(*) FROM packing_bags").fetchone()[0]
    if existing == 0:
        for i, name in enumerate(DEFAULT_BAGS):
            conn.execute(
                "INSERT INTO packing_bags (name, sort_order) VALUES (?, ?)",
                (name, i),
            )
    conn.commit()
    conn.close()


_init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_status(row: sqlite3.Row) -> str:
    days = _cadence_days(row)
    next_due = row["next_due"]

    if days is None:
        # One-time task
        if row["last_completed"]:
            return "Complete"
        if next_due:
            today = datetime.now(timezone.utc).date()
            due = date.fromisoformat(next_due)
            return "Upcoming" if due > today + timedelta(days=14) else "To Do"
        return "To Do"

    if next_due:
        today = datetime.now(timezone.utc).date()
        due = date.fromisoformat(next_due)
        return "Upcoming" if due > today + timedelta(days=14) else "To Do"

    scheduled = _parse_scheduled_days(row["scheduled_days"])
    now = datetime.now(timezone.utc)

    if scheduled:
        # Find the most recent scheduled weekday that falls within the cadence window.
        most_recent_due = None
        for delta in range(days + 1):
            candidate = now - timedelta(days=delta)
            if candidate.weekday() in scheduled:
                most_recent_due = candidate
                break
        if most_recent_due is None:
            return "To Do"
        if not row["last_completed"]:
            return "To Do"
        last = datetime.fromisoformat(row["last_completed"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return "Complete" if last.date() >= most_recent_due.date() else "To Do"

    # No scheduled days — plain time-based check.
    if row["last_completed"] is None:
        return "To Do"
    last = datetime.fromisoformat(row["last_completed"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    threshold = now - timedelta(days=days * 0.8)
    return "Complete" if last >= threshold else "To Do"


def _cadence_label(cadence_value, cadence_unit, scheduled_days=None) -> str:
    """Human-readable cadence string, e.g. 'every 3 days' or 'every Monday'."""
    if cadence_value is None or cadence_unit is None:
        return "once"
    if scheduled_days:
        short = " & ".join(d[:3] for d in scheduled_days.split(",") if d.strip())
        if cadence_value == 1 and cadence_unit == "weeks":
            return f"every {short}"
        unit = cadence_unit.rstrip("s") if cadence_value == 1 else cadence_unit
        return f"every {cadence_value} {unit} on {short}"
    unit = cadence_unit.rstrip("s") if cadence_value == 1 else cadence_unit
    return f"every {cadence_value} {unit}"


def _format_task(row: sqlite3.Row) -> dict:
    status = _task_status(row)
    cadence_value = int(row["cadence_value"]) if row["cadence_value"] is not None else None
    cadence_unit = row["cadence_unit"]
    scheduled_days = row["scheduled_days"]
    return {
        "id": row["id"],
        "title": row["title"],
        "cadence": _cadence_label(cadence_value, cadence_unit, scheduled_days),
        "cadence_value": cadence_value,
        "cadence_unit": cadence_unit,
        "scheduled_days": scheduled_days,
        "notes": row["notes"] or "",
        "status": status,
        "last_completed": row["last_completed"],
        "completed_by": row["completed_by"],
        "sort_order": row["sort_order"],
        "next_due": row["next_due"],
        "created_at": row["created_at"],
    }


def _sort_tasks(tasks: list[dict]) -> list[dict]:
    """Sort: To Do, Complete, Upcoming. Recurring alphabetical; one-time by sort_order."""
    status_ord = {"To Do": 0, "Complete": 1, "Upcoming": 2}
    def sort_key(t):
        s = status_ord.get(t["status"], 1)
        recurring_ord = 0 if t["cadence_value"] is not None else 1
        order = t["title"].lower() if t["cadence_value"] is not None else str(t["sort_order"]).zfill(10)
        return (s, recurring_ord, order)
    return sorted(tasks, key=sort_key)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def list_tasks() -> str:
    """List all household tasks with their current status.

    Returns tasks sorted: To Do first (recurring above one-time), then Complete.
    Status is computed from last_completed date and cadence.
    """
    conn = _get_db()
    rows = conn.execute("SELECT * FROM tasks ORDER BY title").fetchall()
    conn.close()

    if not rows:
        return "No tasks yet. Use add_task to create one."

    tasks = _sort_tasks([_format_task(r) for r in rows])

    lines = []
    current_status = None
    for t in tasks:
        if t["status"] != current_status:
            current_status = t["status"]
            lines.append(f"\n## {current_status}")
        notes_bit = f" — {t['notes']}" if t["notes"] else ""
        by_bit = f" by {t['completed_by']}" if t.get("completed_by") else ""
        completed_bit = f" (last: {t['last_completed'][:10]}{by_bit})" if t["last_completed"] else ""
        due_bit = f" (next due: {t['next_due']})" if t.get("next_due") else ""
        cadence_display = t["cadence"]
        lines.append(f"- **{t['title']}** [{cadence_display}]{completed_bit}{due_bit}{notes_bit}")
        lines.append(f"  id: `{t['id']}`")

    return "\n".join(lines)


@mcp.tool()
def add_task(
    title: str,
    cadence_value: Optional[int] = None,
    cadence_unit: Optional[str] = None,
    scheduled_days: Optional[str] = None,
    notes: Optional[str] = None,
    next_due: Optional[str] = None,
) -> str:
    """Add a new household task.

    Args:
        title: Name of the task (e.g. "Clean gutters")
        cadence_value: How many units between repetitions (e.g. 2 for "every 2 weeks"). Omit for one-time tasks.
        cadence_unit: Unit of repetition — one of: days, weeks, months. Required if cadence_value is set.
        scheduled_days: Optional comma-separated days of week (e.g. "Monday" or "Monday,Thursday"). Only for repeating tasks.
        notes: Optional free text notes about the task
        next_due: Optional date (YYYY-MM-DD) when this task is next due. Works for both one-time and recurring tasks.
                  Tasks due more than 14 days away show as Upcoming instead of To Do.
                  For recurring tasks, this advances automatically by the cadence on each completion.
    """
    is_recurring = cadence_value is not None
    if is_recurring:
        if cadence_unit not in VALID_CADENCE_UNITS:
            return f"Invalid cadence_unit '{cadence_unit}'. Must be one of: {', '.join(VALID_CADENCE_UNITS)}."
        if cadence_value < 1:
            return "cadence_value must be 1 or greater."
    db_scheduled = scheduled_days if is_recurring else None
    if db_scheduled and not _parse_scheduled_days(db_scheduled):
        return f"Invalid scheduled_days '{db_scheduled}'. Use full day names, e.g. 'Monday,Thursday'."
    task_id = str(uuid.uuid4())[:8]
    conn = _get_db()
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) FROM tasks WHERE cadence_value IS NULL").fetchone()[0]
    sort_order = max_order + 1 if not is_recurring else 0
    conn.execute(
        "INSERT INTO tasks (id, title, cadence_value, cadence_unit, scheduled_days, notes, sort_order, next_due, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, title.strip(), cadence_value, cadence_unit, db_scheduled, notes, sort_order, next_due, _now_iso()),
    )
    conn.commit()
    conn.close()
    label = _cadence_label(cadence_value, cadence_unit, db_scheduled)
    next_bit = f", next due: {next_due}" if next_due else ""
    return f"Added task '{title}' ({label}{next_bit}). ID: {task_id}"


@mcp.tool()
def edit_task(
    task_id: str,
    title: Optional[str] = None,
    cadence_value: Optional[int] = None,
    cadence_unit: Optional[str] = None,
    scheduled_days: Optional[str] = None,
    notes: Optional[str] = None,
    next_due: Optional[str] = None,
) -> str:
    """Edit an existing task. Only provided fields are updated.

    To make a task one-time (remove recurrence), pass cadence_value=0.
    To clear scheduled days, pass scheduled_days=''.
    To clear next_due, pass next_due=''.

    Args:
        task_id: The task ID (use list_tasks to find it)
        title: New title
        cadence_value: New repetition interval (e.g. 2). Pass 0 to make it one-time.
        cadence_unit: New unit — one of: days, weeks, months
        scheduled_days: Comma-separated days of week (e.g. 'Monday') or '' to clear
        notes: New notes (free text)
        next_due: Next due date (YYYY-MM-DD format, or empty string to clear)
    """
    conn = _get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return f"No task found with ID '{task_id}'."

    updates = []
    values = []
    if title is not None:
        updates.append("title = ?")
        values.append(title.strip())
    if cadence_value is not None:
        if cadence_value == 0:
            updates += ["cadence_value = ?", "cadence_unit = ?"]
            values += [None, None]
        else:
            if cadence_unit not in VALID_CADENCE_UNITS:
                conn.close()
                return f"Invalid cadence_unit '{cadence_unit}'. Must be one of: {', '.join(VALID_CADENCE_UNITS)}."
            updates += ["cadence_value = ?", "cadence_unit = ?"]
            values += [cadence_value, cadence_unit]
    if scheduled_days is not None:
        if scheduled_days == "":
            updates.append("scheduled_days = ?")
            values.append(None)
        else:
            parsed = _parse_scheduled_days(scheduled_days)
            if not parsed:
                conn.close()
                return f"Invalid scheduled_days '{scheduled_days}'. Use full day names, e.g. 'Monday,Thursday'."
            updates.append("scheduled_days = ?")
            values.append(scheduled_days)
    if notes is not None:
        updates.append("notes = ?")
        values.append(notes)
    if next_due is not None:
        updates.append("next_due = ?")
        values.append(next_due if next_due else None)

    if not updates:
        conn.close()
        return "Nothing to update — provide at least one of: title, cadence_value/cadence_unit, scheduled_days, notes, next_due."

    values.append(task_id)
    conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", values)
    conn.commit()

    updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    t = _format_task(updated)
    due_bit = f", next due: {t['next_due']}" if t['next_due'] else ""
    return f"Updated '{t['title']}' — cadence: {t['cadence']}, status: {t['status']}{due_bit}"


@mcp.tool()
def complete_task(task_id: str, completed_by: Optional[str] = None) -> str:
    """Mark a task as complete. Sets last_completed to now.

    For recurring tasks, it will move back to To Do after the cadence period.
    For one-time tasks, it stays complete permanently.

    Args:
        task_id: The task ID (use list_tasks to find it)
        completed_by: Who completed the task (e.g. "Evan" or "Emily")
    """
    conn = _get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return f"No task found with ID '{task_id}'."

    now = _now_iso()
    new_next_due = None
    cadence_days = _cadence_days(row)
    if row["next_due"] and cadence_days:
        old_due = date.fromisoformat(row["next_due"])
        new_next_due = (old_due + timedelta(days=cadence_days)).isoformat()

    conn.execute(
        "UPDATE tasks SET last_completed = ?, completed_by = ?, next_due = COALESCE(?, next_due) WHERE id = ?",
        (now, completed_by, new_next_due, task_id),
    )
    conn.execute(
        "INSERT INTO task_completions (task_id, task_title, completed_at, completed_by) VALUES (?, ?, ?, ?)",
        (task_id, row["title"], now, completed_by),
    )
    conn.commit()
    conn.close()

    by_bit = f" (by {completed_by})" if completed_by else ""
    if cadence_days is None:
        return f"Completed '{row['title']}'{by_bit} (one-time task — done!)."
    next_bit = f" Next due: {new_next_due}." if new_next_due else ""
    return f"Completed '{row['title']}'{by_bit}.{next_bit}"


@mcp.tool()
def delete_task(task_id: str, confirm: bool = False) -> str:
    """Delete a task permanently.

    Args:
        task_id: The task ID (use list_tasks to find it)
        confirm: Must be True to proceed
    """
    if not confirm:
        return "Set confirm=True to delete this task."

    conn = _get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return f"No task found with ID '{task_id}'."

    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return f"Deleted '{row['title']}'."


@mcp.tool()
def get_summary() -> str:
    """Quick dashboard: how many tasks to do vs. complete, and what's most overdue."""
    conn = _get_db()
    rows = conn.execute("SELECT * FROM tasks ORDER BY title").fetchall()
    conn.close()

    if not rows:
        return "No tasks tracked yet."

    tasks = [_format_task(r) for r in rows]
    todo = [t for t in tasks if t["status"] == "To Do"]
    done = [t for t in tasks if t["status"] == "Complete"]

    lines = [f"**{len(todo)}** to do, **{len(done)}** complete ({len(tasks)} total)"]

    if todo:
        def overdue_sort(t):
            if t["last_completed"] is None:
                return datetime.min
            return datetime.fromisoformat(t["last_completed"])

        most_overdue = sorted(todo, key=overdue_sort)[0]
        if most_overdue["last_completed"]:
            lines.append(f"Most overdue: **{most_overdue['title']}** (last done {most_overdue['last_completed'][:10]})")
        else:
            lines.append(f"Most overdue: **{most_overdue['title']}** (never completed)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Packing helpers
# ---------------------------------------------------------------------------
def _format_packing_item(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "bag": row["bag"],
        "priority": row["priority"],
        "sort_order": row["sort_order"],
        "created_at": row["created_at"],
    }


def _list_bags(conn) -> list[str]:
    rows = conn.execute("SELECT name FROM packing_bags ORDER BY sort_order, name").fetchall()
    return [r["name"] for r in rows]


def _ensure_bag(conn, bag: str) -> None:
    """Insert bag if it doesn't exist."""
    existing = conn.execute("SELECT 1 FROM packing_bags WHERE name = ?", (bag,)).fetchone()
    if not existing:
        max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM packing_bags").fetchone()[0]
        conn.execute(
            "INSERT INTO packing_bags (name, sort_order) VALUES (?, ?)",
            (bag, max_order + 1),
        )


# ---------------------------------------------------------------------------
# Packing MCP Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def list_packing_items() -> str:
    """List all Bahamas packing items grouped by status."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM packing_items ORDER BY status, COALESCE(priority, 99), title"
    ).fetchall()
    conn.close()
    if not rows:
        return "No packing items yet."
    lines = []
    current = None
    for r in rows:
        item = _format_packing_item(r)
        if item["status"] != current:
            current = item["status"]
            lines.append(f"\n## {current}")
        prio = f" P{item['priority']}" if item["priority"] else ""
        lines.append(f"- **{item['title']}** [{item['bag']}]{prio}  id: `{item['id']}`")
    return "\n".join(lines)


@mcp.tool()
def add_packing_item(
    title: str,
    bag: Optional[str] = None,
    status: str = "Have",
    priority: Optional[int] = None,
) -> str:
    """Add a new packing item. Only title is required.

    Args:
        title: Item name (required)
        bag: Optional. Which bag (e.g. "Orange Suitcase", "Green Suitcase", "Black Tote", or a custom one).
        status: Optional. One of: "Need", "Have", "packed". Defaults to "Have".
        priority: Optional priority — 1, 2, or 3.
    """
    title = (title or "").strip()
    if not title:
        return "title is required."
    canonical = _canonical_status(status)
    if canonical is None:
        return f"Invalid status. Must be one of: {', '.join(PACKING_STATUSES)}."
    status = canonical
    if priority is not None and priority not in (1, 2, 3):
        return "priority must be 1, 2, 3, or omitted."
    bag = bag.strip() if isinstance(bag, str) and bag.strip() else None

    item_id = str(uuid.uuid4())[:8]
    conn = _get_db()
    if bag:
        _ensure_bag(conn, bag)
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) FROM packing_items").fetchone()[0]
    conn.execute(
        "INSERT INTO packing_items (id, title, status, bag, priority, sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (item_id, title, status, bag, priority, max_order + 1, _now_iso()),
    )
    conn.commit()
    conn.close()
    where = f" to {bag}" if bag else ""
    return f"Added '{title}'{where} ({status}). ID: {item_id}"


@mcp.tool()
def edit_packing_item(
    item_id: str,
    title: Optional[str] = None,
    status: Optional[str] = None,
    bag: Optional[str] = None,
    priority: Optional[int] = None,
) -> str:
    """Edit a packing item. Provide only fields you want to change.

    To clear priority, pass priority=0.
    """
    conn = _get_db()
    row = conn.execute("SELECT * FROM packing_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return f"No packing item with ID '{item_id}'."

    updates, values = [], []
    if title is not None:
        updates.append("title = ?")
        values.append(title.strip())
    if status is not None:
        canonical = _canonical_status(status)
        if canonical is None:
            conn.close()
            return f"Invalid status. Must be one of: {', '.join(PACKING_STATUSES)}."
        updates.append("status = ?")
        values.append(canonical)
    if bag is not None:
        bag_clean = bag.strip() if isinstance(bag, str) else None
        if bag_clean:
            _ensure_bag(conn, bag_clean)
            updates.append("bag = ?")
            values.append(bag_clean)
        else:
            # Empty string clears the bag.
            updates.append("bag = ?")
            values.append(None)
    if priority is not None:
        if priority == 0:
            updates.append("priority = ?")
            values.append(None)
        elif priority in (1, 2, 3):
            updates.append("priority = ?")
            values.append(priority)
        else:
            conn.close()
            return "priority must be 1, 2, 3, or 0 to clear."

    if not updates:
        conn.close()
        return "Nothing to update."

    values.append(item_id)
    conn.execute(f"UPDATE packing_items SET {', '.join(updates)} WHERE id = ?", values)
    conn.commit()
    conn.close()
    return f"Updated packing item '{item_id}'."


@mcp.tool()
def advance_packing_status(item_id: str) -> str:
    """Move a packing item to the next status.

    need to buy -> need to pack -> packed.
    """
    conn = _get_db()
    row = conn.execute("SELECT * FROM packing_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return f"No packing item with ID '{item_id}'."
    current = row["status"]
    nxt = PACKING_NEXT_STATUS.get(current)
    if nxt is None:
        conn.close()
        return f"'{row['title']}' is already packed."
    conn.execute("UPDATE packing_items SET status = ? WHERE id = ?", (nxt, item_id))
    conn.commit()
    conn.close()
    return f"'{row['title']}': {current} -> {nxt}"


@mcp.tool()
def delete_packing_item(item_id: str, confirm: bool = False) -> str:
    """Delete a packing item permanently."""
    if not confirm:
        return "Set confirm=True to delete this item."
    conn = _get_db()
    row = conn.execute("SELECT * FROM packing_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return f"No packing item with ID '{item_id}'."
    conn.execute("DELETE FROM packing_items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return f"Deleted '{row['title']}'."


@mcp.tool()
def list_packing_bags() -> str:
    """List the available packing bag categories."""
    conn = _get_db()
    bags = _list_bags(conn)
    conn.close()
    return ", ".join(bags) if bags else "No bags yet."


@mcp.tool()
def add_packing_bag(name: str) -> str:
    """Add a new packing bag category."""
    name = name.strip()
    if not name:
        return "Bag name is required."
    conn = _get_db()
    _ensure_bag(conn, name)
    conn.commit()
    conn.close()
    return f"Bag '{name}' available."
