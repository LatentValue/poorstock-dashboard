"""Pure, testable dashboard filtering. No network requests or Streamlit imports."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import parse_qsl, unquote, urlsplit
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
UTC = timezone.utc
WINDOW_DAYS = 30
VIEW_OPTIONS = {
    "Recent decks + new links": "combined",
    "Recent meeting dates only": "meetings",
    "Newly detected links only": "new",
}
COLUMNS = ["Date", "Code", "Company", "File", "Type", "Added as", "First seen (Taipei)",
           "Last seen (Taipei)", "Meeting dates", "Deck", "PoorStock"]


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=UTC) if result.tzinfo is None else result
    except ValueError:
        return None


def parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def local_time(value: Any) -> str:
    stamp = parse_timestamp(value)
    return stamp.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M") if stamp else ""


def safe_link(value: Any) -> str:
    """Only emit web hyperlinks; never javascript/file/data or credential URLs."""
    if not isinstance(value, str):
        return ""
    try:
        p = urlsplit(value)
        return value if p.scheme in {"http", "https"} and p.hostname and not (p.username or p.password) else ""
    except ValueError:
        return ""


def filename(url: str) -> str:
    p = urlsplit(url)
    for key, value in parse_qsl(p.query):
        if key.lower() in {"filename", "file", "file_name", "name"} and value:
            return unquote(value).rsplit("/", 1)[-1]
    return unquote(p.path).rsplit("/", 1)[-1] or "Presentation link"


def select_decks(decks: Iterable[dict[str, Any]], *, today: date | None = None,
                 days: int = WINDOW_DAYS, view: str = "combined", query: str = "",
                 tickers: Iterable[str] = (), include_resources: bool = False) -> list[dict[str, Any]]:
    """Last N Taipei calendar days, inclusive of today; never relabel backfill as new.

    Combined view = any meeting in the window OR a post-baseline link first seen
    in the window. Unknown-date initial inventories are NOT treated as recent.
    A reused URL with both a recent and a future meeting still qualifies by the
    recent date. New links for upcoming meetings qualify by detection date.
    """
    if not 1 <= days <= WINDOW_DAYS:
        raise ValueError("days must be between 1 and 30")
    if view not in {"combined", "meetings", "new"}:
        raise ValueError("Unknown view")
    end = today or datetime.now(TAIPEI).date()
    start = end - timedelta(days=days - 1)
    wanted = set(tickers)
    tokens = query.casefold().split()
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in decks:
        url = safe_link(raw.get("url"))
        if not url or (wanted and str(raw.get("ticker")) not in wanted):
            continue
        kind = raw.get("file_type", "unverified_resource_link")
        if not include_resources and kind == "unverified_resource_link":
            continue
        dates = sorted({d for value in raw.get("meeting_dates", []) if (d := parse_day(value))})
        eligible_meetings = [d for d in dates if start <= d <= end]
        observed_at = parse_timestamp(raw.get("first_seen"))
        observed_day = observed_at.astimezone(TAIPEI).date() if observed_at else None
        is_new = bool(raw.get("observation") == "new_link" and observed_day
                      and start <= observed_day <= end)
        if view == "meetings" and not eligible_meetings:
            continue
        if view == "new" and not is_new:
            continue
        if view == "combined" and not (eligible_meetings or is_new):
            continue
        chosen_dates = ([] if view == "new" else eligible_meetings[:])
        if view != "meetings" and is_new:
            chosen_dates.append(observed_day)
        record = dict(raw)
        record.update(display_date=max(chosen_dates).isoformat(),
                      filename=filename(url), new_in_window=is_new)
        haystack = " ".join(str(record.get(k, "")) for k in
                            ["ticker", "company", "filename", "url", "meeting_dates"]).casefold()
        if tokens and not all(token in haystack for token in tokens):
            continue
        selected[(str(record.get("ticker", "")), url)] = record
    return sorted(selected.values(), key=lambda x: (x["display_date"], x.get("first_seen", ""),
                  x.get("ticker", "")), reverse=True)


def table_rows(decks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for d in decks:
        rows.append({
            "Date": d["display_date"], "Code": d["ticker"], "Company": d["company"],
            "File": d.get("filename", filename(d["url"])),
            "Type": d.get("file_type", "unknown").upper().replace("UNVERIFIED_RESOURCE_LINK", "RESOURCE"),
            "Added as": "New link" if d.get("observation") == "new_link" else "Initial inventory",
            "First seen (Taipei)": local_time(d.get("first_seen")),
            "Last seen (Taipei)": local_time(d.get("last_seen")),
            "Meeting dates": ", ".join(d.get("meeting_dates", [])),
            "Deck": safe_link(d.get("url")), "PoorStock": safe_link(d.get("poorstock_url")),
        })
    return rows


def export_csv(rows: list[dict[str, Any]]) -> bytes:
    """UTF-8 BOM for Chinese names in Excel; neutralize spreadsheet formulas."""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=COLUMNS)
    writer.writeheader()
    for row in rows:
        clean = {}
        for key in COLUMNS:
            value = str(row.get(key, ""))
            if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
                value = "'" + value
            clean[key] = value
        writer.writerow(clean)
    return output.getvalue().encode("utf-8-sig")


def stale_message(status: dict[str, Any], now: datetime | None = None) -> str | None:
    stamp = parse_timestamp(status.get("last_completed_at"))
    if stamp is None:
        return "No completed collection yet. Check the GitHub Actions run before relying on coverage."
    age = (now or datetime.now(UTC)) - stamp
    if age > timedelta(hours=36):
        return "The last completed collection is over 36 hours old. Check GitHub Actions for a missed or failed run."
    return None
