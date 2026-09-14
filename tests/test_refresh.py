from datetime import datetime
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import poorstock_monitor as m
import refresh
from dashboard_data import TAIPEI


def html(code="1234", urls=("https://example.test/one.pdf",), meeting=None):
    day = meeting or datetime.now(TAIPEI).date().strftime("%Y/%m/%d")
    links = "".join(f'<a href="{url}">{m.PRESENTATION}</a>' for url in urls)
    return (f"<h1>Example ({code}) {m.CALL}</h1><h2>{m.MOPS_SECTION}</h2>"
            f"<dl><dt>{m.DATE_LABEL}</dt><dd>{day}</dd><dt>PDF{m.PRESENTATION}</dt><dd>{links}</dd></dl>")


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.requests = 0

    def get(self, url):
        assert m.allowed_url(url), "Collector attempted to request an external deck"
        self.requests += 1
        result = self.pages[url]
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass


def run(tmp_path, pages, codes=("1234",)):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"companies": list(codes)}), encoding="utf-8")
    return refresh.collect(config, tmp_path / "data", fetcher_factory=lambda db, cfg: FakeFetcher(pages))


def read(tmp_path, name):
    return json.loads((tmp_path / "data" / name).read_text(encoding="utf-8"))


def test_cross_run_persistence_and_new_link(tmp_path):
    url = m.BASE + "/earningcall/1234"
    assert run(tmp_path, {url: html()}) == 0
    one = read(tmp_path, "state.json")
    assert len(one["decks"]) == 1
    stamp = one["decks"][0]["first_seen"]
    assert one["decks"][0]["observation"] == "initial_inventory"
    assert read(tmp_path, "status.json")["new_links_this_run"] == 0
    assert run(tmp_path, {url: html(urls=("https://example.test/one.pdf", "https://example.test/two.pdf"))}) == 0
    two = read(tmp_path, "state.json")
    assert len(two["decks"]) == 2
    assert next(d for d in two["decks"] if d["url"].endswith("one.pdf"))["first_seen"] == stamp
    assert read(tmp_path, "status.json")["new_links_this_run"] == 1
    assert run(tmp_path, {url: html(urls=("https://example.test/one.pdf", "https://example.test/two.pdf"))}) == 0
    assert read(tmp_path, "status.json")["new_links_this_run"] == 0
    assert len(read(tmp_path, "runs.json")) == 3


def test_failure_does_not_erase_history(tmp_path):
    url = m.BASE + "/earningcall/1234"
    assert run(tmp_path, {url: html()}) == 0
    initial = read(tmp_path, "state.json")["decks"]
    last_completed = read(tmp_path, "status.json")["last_completed_at"]
    assert run(tmp_path, {url: m.StopCrawl("HTTP 403")}) == 2
    assert read(tmp_path, "state.json")["decks"] == initial
    assert read(tmp_path, "status.json")["last_completed_at"] == last_completed
    assert read(tmp_path, "status.json")["status"] == "failed"
    assert len(read(tmp_path, "decks.json")["decks"]) == 1


def test_historical_link_aged_out_of_view_not_history(tmp_path):
    assert run(tmp_path, {m.BASE + "/earningcall/1234": html(meeting="2024/01/01")}) == 0
    assert len(read(tmp_path, "state.json")["decks"]) == 1
    assert read(tmp_path, "decks.json")["decks"] == []


def test_missing_history_not_silently_recreated(tmp_path):
    url = m.BASE + "/earningcall/1234"
    assert run(tmp_path, {url: html()}) == 0
    (tmp_path / "data/state.json").unlink()
    assert run(tmp_path, {url: html()}) == 2
    assert "missing" in read(tmp_path, "status.json")["errors"][0]
    assert not (tmp_path / "data/state.json").exists()


def test_corrupt_history_retained_for_recovery(tmp_path):
    url = m.BASE + "/earningcall/1234"
    assert run(tmp_path, {url: html()}) == 0
    path = tmp_path / "data/state.json"
    path.write_text("BROKEN", encoding="utf-8")
    assert run(tmp_path, {url: html()}) == 2
    assert path.read_text() == "BROKEN"
    assert len(read(tmp_path, "decks.json")["decks"]) == 1


def test_unexpected_exception_preserves_existing_snapshot(tmp_path):
    url = m.BASE + "/earningcall/1234"
    run(tmp_path, {url: html()})
    before = read(tmp_path, "decks.json")
    assert run(tmp_path, {url: RuntimeError("Unexpected failure")}) == 2
    assert read(tmp_path, "decks.json") == before


def test_partial_status_and_missing_pages_visible(tmp_path):
    pages = {m.BASE + "/earningcall/1234": html(), m.BASE + "/earningcall/5678": None}
    assert run(tmp_path, pages, codes=("1234", "5678")) == 0
    status = read(tmp_path, "status.json")
    assert status["status"] == "completed_with_missing_pages"
    assert status["missing_pages"] == ["5678"]
    assert status["checked"] == 1 and status["planned"] == 2


def test_failed_company_keeps_other_new_results(tmp_path):
    pages = {m.BASE + "/earningcall/1234": html(), m.BASE + "/earningcall/5678": m.LayoutError("Bad layout")}
    assert run(tmp_path, pages, codes=("1234", "5678")) == 2
    assert len(read(tmp_path, "decks.json")["decks"]) == 1
    assert read(tmp_path, "status.json")["status"] == "partial_or_failed"


def test_history_roundtrip_is_lossless(tmp_path):
    run(tmp_path, {m.BASE + "/earningcall/1234": html()})
    state = read(tmp_path, "state.json")
    db = m.open_db(":memory:")
    try:
        refresh.restore_history(db, state)
        assert refresh.dump_history(db) == state
    finally:
        db.close()


def test_wrong_schema_rejected():
    db = m.open_db(":memory:")
    try:
        with pytest.raises(ValueError):
            refresh.restore_history(db, {"schema_version": 88})
    finally:
        db.close()


def test_time_budget_stops_before_request():
    db = m.open_db(":memory:")
    try:
        client = refresh.BoundedFetcher(db, dict(m.DEFAULTS), max_minutes=0.1)
        with pytest.raises(m.StopCrawl):
            client._request(m.CALENDAR)
        assert client.requests == 0
        client.close()
    finally:
        db.close()


def test_rolling_run_log(tmp_path):
    for n in range(100):
        refresh.append_run(tmp_path, {"status": "completed", "finished_at": str(n)})
    runs = json.loads((tmp_path / "runs.json").read_text())
    assert len(runs) == 90 and runs[0]["finished_at"] == "10"
