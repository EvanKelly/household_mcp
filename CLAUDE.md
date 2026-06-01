# household_mcp — Codebase Guide

## Architecture

- **Backend MCP server**: `server.py` — FastMCP 2.0 tools for LLM agents (list/add/edit/complete/delete tasks and packing items)
- **HTTP REST API + SPA server**: `deploy/server.py` — Starlette app, wraps the same SQLite DB with JSON endpoints and serves the frontend
- **Frontend**: `static/index.html` — single-file vanilla JS SPA (no framework, no build step)
- **Database**: SQLite with WAL mode; path controlled by `HOUSEHOLD_DB_PATH` env var (loaded from `.env` in deploy server)

## Database Schema

### `tasks`
| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | UUID[:8] |
| `title` | TEXT | |
| `cadence_value` | INTEGER | NULL = one-time task |
| `cadence_unit` | TEXT | `'days'`, `'weeks'`, `'months'` |
| `scheduled_days` | TEXT | Comma-separated day names e.g. `"Monday,Thursday"` |
| `notes` | TEXT | |
| `last_completed` | TEXT | ISO datetime |
| `completed_by` | TEXT | Person name |
| `sort_order` | INTEGER | Manual order for one-time tasks |
| `next_due` | TEXT | YYYY-MM-DD |
| `category` | TEXT | Free-form label e.g. `"Cleaning"` |
| `created_at` | TEXT | ISO datetime |

**Recurring** = `cadence_value IS NOT NULL`. **One-time** = both NULL.

### `task_completions`
History log of completions: `task_id`, `task_title`, `completed_at`, `completed_by`.

### `packing_items`
`id`, `title`, `status` (`Need`/`Have`/`packed`), `bag`, `priority` (1–3), `sort_order`, `created_at`.

### `packing_bags`
`id`, `name`, `sort_order`.

## Task Status Logic

Computed dynamically (not stored) by `_task_status()` in `server.py`:

- **"To Do"** — needs completion
- **"Complete"** — completed within cadence window
- **"Upcoming"** — due > 14 days out

Rules (simplified):
- `next_due`-based: compare `last_completed` against `next_due - cadence_days`
- `scheduled_days`-based: find most recent scheduled weekday within cadence window
- Plain cadence: daily = completed today; otherwise completed within 80% of cadence window
- One-time + `next_due` > today+14 → Upcoming; else To Do if never completed

## HTTP API Endpoints (`deploy/server.py`)

| Method | Path | Action |
|---|---|---|
| GET | `/api/categories` | List distinct category values (sorted) |
| GET | `/api/tasks` | List all tasks (with computed status) |
| POST | `/api/tasks` | Add task |
| POST | `/api/tasks/reorder` | Reorder one-time tasks (`{ task_ids: [...] }`) |
| PUT | `/api/tasks/{id}` | Edit task |
| POST | `/api/tasks/{id}/complete` | Complete (`{ completed_by: "Name" }`) |
| DELETE | `/api/tasks/{id}` | Delete |
| GET | `/api/packing/items` | List packing items |
| POST | `/api/packing/items` | Add item |
| POST | `/api/packing/items/bulk` | Bulk add |
| PUT | `/api/packing/items/{id}` | Edit item |
| DELETE | `/api/packing/items/{id}` | Delete |
| POST | `/api/packing/items/{id}/advance` | Cycle status Need→Have→packed |
| GET | `/api/packing/bags` | List bags |
| POST | `/api/packing/bags` | Add bag |

## Frontend (`static/index.html`)

Single file — all HTML, CSS, and JS inline. Key JS globals:

- `activeTab` — persisted in `localStorage`; values: `'household'`, `'calendar'`, `'recurring'`, `'packing'`
- `packingItems`, `packingBags` — cached packing state

### Tabs / Views

| Tab | View div | Load function |
|---|---|---|
| Household Tasks | `#view-household` / `#app` | `loadTasks()` → `render(tasks)` |
| Calendar | `#view-calendar` | `loadCalendar()` → `renderCal()` / `renderCalSidebar()` / `renderCalGrid()` |
| Recurring | `#view-recurring` / `#app-recurring` | `loadRecurring()` → `renderRecurring(tasks)` |
| Bahamas Packing | `#view-packing` | `loadPacking()` → `renderPacking()` |

The Packing tab button is hidden by default (`style="display:none"`).

### Key JS Functions

- `taskCard(t, complete)` — renders a task card HTML string; used by both household and recurring views
- `taskColor(t)` → `'tc-green'|'tc-blue'|'tc-orange'|'tc-red'|null` — cadence progress color
- `daysOverdue(t)` → number or null
- `render(tasks)` — groups tasks into To Do / Complete / Upcoming sections for the household view
- `renderRecurring(tasks)` — filters to recurring only (`cadence_value != null`), groups same way
- `switchTab(tab)` — shows/hides views, persists active tab, calls appropriate load function
- `completeTask(id)` → opens "Who did it?" modal → `submitComplete(person)`
- `submitForm()` — handles add and edit; reloads current tab after save
- `deleteTask(id, title)` — confirms then deletes; reloads current tab

### Task Card Behavior

- Recurring tasks: sorted alphabetically within each status group
- One-time tasks: sorted by `sort_order`; draggable in the "To Do" section only
- Drag handle (`⠿`) only appears on one-time To Do tasks

## Deployment

- Docker via `Dockerfile` + `entrypoint.sh`
- Railway config in `railway.toml`
- `.env` file read by `deploy/server.py` for `HOUSEHOLD_DB_PATH`
- Cross-app nav: kitchen app at `http://<hostname>:8001`
