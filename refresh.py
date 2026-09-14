#!/usr/bin/env python3
"""GitHub Actions collector. Durable JSON history; temporary SQLite/HTML only.

The collector retrieves PoorStock HTML, never the linked presentation files.
A nonzero exit code reports incomplete collection AFTER saving observations and
status, so the workflow can publish the warning before marking the job failed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any

from filelock import FileLock
import poorstock_monitor as monitor
from dashboard_data import select_decks

ROOT = Path(__file__).resolve().parent
SCHEMA = 1
TABLE_COLUMNS = {
    "companies": ["ticker", "name", "initialized", "last_calendar_seen", "last_checked", "last_error"],
    "decks": ["id", "ticker", "company", "url", "file_type", "meeting_dates", "poorstock_url",
              "first_seen", "last_seen", "observation", "reported", "email_sent"],
}


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ValueError(f"Cannot read {path.name}; refusing to reset existing history: {e}") from e


def write_json(path: Path, value: Any) -> None:
    monitor.atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def restore_history(db: sqlite3.Connection, state: dict[str, Any]) -> None:
    if state.get("schema_version") != SCHEMA:
        raise ValueError("Unsupported history schema; refusing to erase or reset history")
    if not isinstance(state.get("companies"), list) or not isinstance(state.get("decks"), list):
        raise ValueError("History is missing its companies or decks array")
    if not isinstance(state.get("meta", {}), dict):
        raise ValueError("History meta must be an object")
    # Table/column names are constants, never supplied by the JSON document.
    with db:
        for key, value in state.get("meta", {}).items():
            db.execute("INSERT INTO meta VALUES (?,?)", (key, value))
        for table, columns in TABLE_COLUMNS.items():
            for row in state[table]:
                values = [json.dumps(row[col], ensure_ascii=False) if col == "meeting_dates"
                          else row[col] for col in columns]
                db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)


def dump_history(db: sqlite3.Connection) -> dict[str, Any]:
    state = {"schema_version": SCHEMA,
             "meta": {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM meta ORDER BY key")}}
    state["companies"] = [dict(r) for r in db.execute("SELECT * FROM companies ORDER BY ticker")]
    state["decks"] = monitor.decode_rows(db.execute("SELECT * FROM decks ORDER BY ticker,id"))
    return state


class BoundedFetcher(monitor.Fetcher):
    """End gracefully before the Actions job timeout; retain partial discoveries."""
    def __init__(self, db: sqlite3.Connection, cfg: dict[str, Any], max_minutes: float):
        super().__init__(db, cfg)
        self.deadline = time.monotonic() + max_minutes * 60

    def _request(self, *args: Any, **kwargs: Any):
        if time.monotonic() + self.delay + 45 >= self.deadline:
            raise monitor.StopCrawl("Collection time budget reached; partial results preserved. Review scan coverage.")
        return super()._request(*args, **kwargs)


def collect(config: Path, data: Path, *, max_minutes: float = 300,
            fetcher_factory: Any = None) -> int:
    data.mkdir(parents=True, exist_ok=True)
    previous = read_json(data / "status.json", {})
    started = monitor.now_utc()
    try:
        cfg = monitor.load_config(config)
        # This wrapper owns scheduling/persistence. No SMTP or local scheduler.
        cfg["email_enabled"] = False
        history_file = data / "state.json"
        state = read_json(history_file)
        if state is None:
            # A missing history file next to populated data is an error, not a
            # reason to silently establish a fresh baseline.
            existing = read_json(data / "decks.json", {"decks": []})
            if previous.get("last_completed_at") or existing.get("decks"):
                raise ValueError("state.json is missing but prior results exist; restore it from Git history")
            state = {"schema_version": SCHEMA, "meta": {}, "companies": [], "decks": []}
        with tempfile.TemporaryDirectory(prefix="poorstock-") as temp:
            db = monitor.open_db(Path(temp) / "state.sqlite3")
            try:
                restore_history(db, state)
                if not monitor.get_meta(db, "tracking_started_at"):
                    monitor.put_meta(db, "tracking_started_at", started)
                client = fetcher_factory(db, cfg) if fetcher_factory else BoundedFetcher(db, cfg, max_minutes)
                report = monitor.scan(db, cfg, Path(temp), fetcher=client)
                all_state = dump_history(db)
            finally:
                db.close()
        status = {k: v for k, v in report.items() if k not in {"new_links", "initial_inventory"}}
        status.update(schema_version=SCHEMA,
                      tracking_started_at=all_state["meta"].get("tracking_started_at"),
                      new_links_this_run=len(report["new_links"]),
                      initial_inventory_this_run=len(report["initial_inventory"]),
                      known_companies=len(all_state["companies"]),
                      total_known_links=len(all_state["decks"]),
                      last_completed_at=previous.get("last_completed_at"),
                      schedule_label="Daily at 11:00 a.m. EDT (15:00 UTC; start time, subject to GitHub delay)")
        if status["status"] == "completed":
            status["last_completed_at"] = status["finished_at"]
            if status.get("missing_pages"):
                status["status"] = "completed_with_missing_pages"
        # Keep the complete deduplication catalog in state.json, but publish only
        # the rolling 30-day view (including separately marked resource links).
        recent = select_decks(all_state["decks"], include_resources=True)
        snapshot = {"schema_version": SCHEMA, "generated_at": status["finished_at"],
                    "window_days": 30, "window_timezone": "Asia/Taipei", "decks": recent}
        write_json(data / "state.json", all_state)
        write_json(data / "decks.json", snapshot)
        write_json(data / "status.json", status)
        append_run(data, status)
        print(f"{status['status']}: {status['checked']}/{status['planned']} company pages checked; "
              f"{len(recent)} recent deck/resource links; {status['new_links_this_run']} newly detected")
        return 0 if status["status"] in {"completed", "completed_with_missing_pages"} else 2
    except Exception as e:
        # Never delete or rewrite the existing catalog on an unexpected error.
        logging.exception("Collection failed; keeping prior history and deck snapshot")
        status = dict(previous, schema_version=SCHEMA, status="failed", started_at=started,
                      finished_at=monitor.now_utc(), checked=0, planned=0,
                      errors=[f"{type(e).__name__}: {e}"], new_links_this_run=0,
                      initial_inventory_this_run=0, previous_snapshot_retained=True)
        write_json(data / "status.json", status)
        append_run(data, status)
        return 2


def append_run(data: Path, status: dict[str, Any]) -> None:
    keys = ["started_at", "finished_at", "status", "mode", "planned", "checked", "requests",
            "new_links_this_run", "initial_inventory_this_run", "errors", "missing_pages"]
    try:
        runs = read_json(data / "runs.json", [])
        if not isinstance(runs, list):
            runs = []
    except ValueError:
        runs = []
    runs.append({key: status.get(key) for key in keys})
    write_json(data / "runs.json", runs[-90:])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--max-minutes", type=float, default=300)
    args = parser.parse_args()
    if args.max_minutes <= 1:
        parser.error("--max-minutes must exceed 1")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.data_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(args.data_dir / ".refresh.lock"), timeout=1):
        code = collect(args.config.resolve(), args.data_dir.resolve(), max_minutes=args.max_minutes)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        status = read_json(args.data_dir / "status.json", {})
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## PoorStock collection\n\n")
            f.write(f"Status: **{status.get('status')}**\n\n")
            f.write(f"Company pages checked: {status.get('checked', 0)} / {status.get('planned', 0)}\n\n")
            f.write(f"New links: {status.get('new_links_this_run', 0)}\n\n")
            if status.get("errors"):
                f.write("```text\n" + "\n".join(status["errors"])[:12000] + "\n```\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
