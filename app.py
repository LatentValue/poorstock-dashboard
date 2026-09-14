"""Streamlit front end. Reads saved JSON only: opening this app never scrapes."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from dashboard_data import (COLUMNS, TAIPEI, VIEW_OPTIONS, export_csv, local_time,
                            select_decks, stale_message, table_rows)

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("POORSTOCK_DATA_DIR", str(ROOT / "data")))

st.set_page_config(page_title="Taiwan Deck Monitor", layout="wide")
st.markdown("""
<style>
.block-container {padding-top:2rem; padding-bottom:2rem; max-width:1600px;}
[data-testid="stMetric"] {border:1px solid rgba(145,160,180,.25); border-radius:12px; padding:16px;}
[data-testid="stMetricLabel"] {font-size:.88rem;}
h1 {letter-spacing:-.035em;}
</style>
""", unsafe_allow_html=True)


def read_saved(name: str, fallback):
    path = DATA / name
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        st.error(f"Cannot read {name}. Check the latest GitHub Actions run. Details: {exc}")
        st.stop()


with st.sidebar:
    st.header("Taiwan Deck Monitor")
    st.caption("PoorStock presentation links")
    st.divider()
    st.markdown("**Collection schedule**")
    st.write("Daily at 11:00 a.m. EDT")
    st.caption("15:00 UTC year-round. GitHub may start a run late. The table changes when collection finishes.")
    st.markdown("**No scraping in this app**")
    st.caption("GitHub Actions gathers links. Streamlit displays the saved results, even when the collector is not running.")
    st.link_button("Open PoorStock calendar", "https://poorstock.com/earning_call_home")
    st.divider()
    st.caption("Deck buttons may open MOPS or another original host. Files are not downloaded or validated by this monitor.")


@st.fragment(run_every=60)
def dashboard() -> None:
    # Re-read local files every minute during an active session, not the website.
    # Community Cloud supplies GitHub commits to this checkout automatically.
    status = read_saved("status.json", {"status": "not_initialized"})
    snapshot = read_saved("decks.json", {"decks": []})
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("decks"), list):
        st.error("Invalid deck snapshot. Check data/decks.json and the collector run.")
        return
    if not isinstance(status, dict):
        st.error("Invalid status.json. Restore the file from Git history.")
        return
    decks = snapshot["decks"]
    today = datetime.now(TAIPEI).date()
    start = today - timedelta(days=29)

    st.caption("TAIWAN / INVESTOR PRESENTATIONS")
    st.title("Company decks. One daily screen.")
    st.write("A rolling 30-day collection of presentation links found on PoorStock.")
    st.caption(f"Window: {start:%b %d, %Y} to {today:%b %d, %Y} | Calendar dates use Taiwan time")

    state = status.get("status", "not_initialized")
    if state == "not_initialized":
        st.info("Ready for its first collection. In your GitHub repository, open Actions, choose "
                "Refresh PoorStock decks, and click Run workflow. No live links have been collected yet.")
    else:
        detail = (f"Last attempt: {local_time(status.get('finished_at')) or 'unknown'} Taipei | "
                  f"Checked {status.get('checked', 0):,} / {status.get('planned', 0):,} company pages")
        if state == "completed":
            st.success(detail)
        elif state == "completed_with_missing_pages":
            st.warning(detail + f" | {len(status.get('missing_pages') or [])} company pages unavailable")
        else:
            st.error(detail + " | Collection incomplete; saved links remain available")
        warning = stale_message(status)
        if warning:
            st.warning(warning)
    if status.get("finished_at"):
        st.caption("Snapshot published: " + (local_time(snapshot.get("generated_at")) or "not yet") + " Taipei")

    current = select_decks(decks, today=today)
    a, b, c = st.columns(3)
    a.metric("Deck links in 30-day window", len(current))
    b.metric("Companies in this window", len({d["ticker"] for d in current}))
    c.metric("Newly detected links in window", sum(d["new_in_window"] for d in current))

    links_tab, health_tab, about_tab = st.tabs(["Deck links", "Collection health", "How dates work"])
    with links_tab:
        f1, f2 = st.columns([2, 1])
        query = f1.text_input("Search", placeholder="Company name, stock code, or filename", key="search")
        view_label = f2.selectbox("Show", list(VIEW_OPTIONS), key="view")
        with st.expander("Company and file filters"):
            names = {d["ticker"]: d["company"] for d in decks}
            codes = st.multiselect("Companies (empty = all)", sorted(names),
                                   format_func=lambda code: f"{code} - {names[code]}", key="companies")
            include_resources = st.checkbox("Include presentation resource links without a recognized file extension",
                                            value=False, key="resources")
        filtered = select_decks(decks, today=today, view=VIEW_OPTIONS[view_label], query=query,
                                tickers=codes, include_resources=include_resources)
        rows = table_rows(filtered)
        st.caption(f"{len(rows):,} matching links. Click a column heading to sort. "
                   "Search covers link metadata, not text inside the decks.")
        if rows:
            frame = pd.DataFrame(rows, columns=COLUMNS)
            st.dataframe(
                frame, hide_index=True, use_container_width=True,
                height=min(740, max(240, len(rows) * 36 + 44)),
                column_order=["Date", "Code", "Company", "Deck", "PoorStock", "Added as", "Type", "File",
                              "First seen (Taipei)", "Last seen (Taipei)", "Meeting dates"],
                column_config={
                    "Date": st.column_config.TextColumn("Date", help="Recent meeting date or new-link detection date, depending on selected view."),
                    "Code": st.column_config.TextColumn("Code", width="small"),
                    "Company": st.column_config.TextColumn("Company", width="medium"),
                    "Deck": st.column_config.LinkColumn("Presentation", display_text="Open deck", width="small"),
                    "PoorStock": st.column_config.LinkColumn("Source page", display_text="PoorStock", width="small"),
                    "Added as": st.column_config.TextColumn("Added as", width="medium"),
                    "File": st.column_config.TextColumn("Filename", width="large"),
                },
            )
        elif state != "not_initialized":
            st.info("No saved links match this view. Broaden the filters or inspect Collection health; "
                    "an empty result is not proof that no company published a deck.")
        st.download_button("Export displayed links as CSV", export_csv(rows),
                           file_name=f"taiwan-decks-{today}.csv", mime="text/csv", key="csv")
        st.caption("New link = first observed after that company's baseline. Initial inventory = backfill, not a confirmed new upload.")

    with health_tab:
        st.subheader("Coverage and freshness")
        st.write(f"Tracking started: {local_time(status.get('tracking_started_at')) or 'not started'} Taipei")
        st.write(f"Last completed collection: {local_time(status.get('last_completed_at')) or 'none'} Taipei")
        st.write(f"Known companies: {status.get('known_companies', 0):,} | "
                 f"Links retained for duplicate tracking: {status.get('total_known_links', 0):,}")
        st.write(f"Latest collection mode: {status.get('mode', 'not run')}")
        st.caption("Scope: companies discoverable through PoorStock's calendar and category pages by default. "
                   "This is not a complete MOPS filings feed. Missing pages and collection errors are reported below.")
        errors = status.get("errors") or []
        if errors:
            st.code("\n".join(str(e) for e in errors), language=None)
        if status.get("missing_pages"):
            st.write("Unavailable company pages: " + ", ".join(status["missing_pages"]))
        runs = read_saved("runs.json", [])
        if runs:
            compact = [{"Finished (Taipei)": local_time(r.get("finished_at")),
                        "Status": r.get("status"), "Checked": r.get("checked"), "Planned": r.get("planned"),
                        "New links": r.get("new_links_this_run"), "Initial inventory": r.get("initial_inventory_this_run")}
                       for r in reversed(runs)]
            st.dataframe(pd.DataFrame(compact), hide_index=True, use_container_width=True)
        st.caption("To force another collection, use Actions > Refresh PoorStock decks > Run workflow in GitHub. "
                   "Reloading this page only reloads saved data.")

    with about_tab:
        st.markdown("""
**Recent decks + new links** is the default. A link appears when it is associated
with a meeting in the last 30 Taiwan calendar days, or was first detected during
that window after its company's first successful check.

**Recent meeting dates only** shows decks associated with meetings in that window.
It can show useful backfill immediately after the first successful collection.
It cannot reconstruct links that PoorStock no longer exposes.

**Newly detected links only** excludes each company's initial inventory. This
view starts building after tracking begins. Detection dates are not upload dates.

Older records leave the visible window automatically, but remain in the history
so the same URL is not repeatedly flagged as new. Replacing a file at exactly the
same URL is not detected. A reused URL is one row, even if linked at several meetings.

The collector requests PoorStock pages only. It does not request MOPS, open the
presentation files, analyze their contents, or bypass access controls. Clicking
**Open deck** may contact the original host. A recognized file extension is not
verification of a working file.
""")


dashboard()
