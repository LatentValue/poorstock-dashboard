from datetime import date, datetime, timezone
import pytest
from dashboard_data import (export_csv, filename, local_time, parse_day, parse_timestamp,
                            safe_link, select_decks, stale_message, table_rows)

TODAY = date(2026, 9, 14)


def deck(**changes):
    value = {"id": "one", "ticker": "1234", "company": "Example",
             "url": "https://example.test/slides.pdf", "file_type": "pdf",
             "meeting_dates": ["2026-09-01"], "poorstock_url": "https://poorstock.com/earningcall/1234",
             "first_seen": "2026-09-14T01:00:00+00:00", "last_seen": "2026-09-14T02:00:00+00:00",
             "observation": "initial_inventory"}
    return dict(value, **changes)


@pytest.mark.parametrize("day,expected", [("2026-08-15", 0), ("2026-08-16", 1),
                                        ("2026-09-14", 1), ("2026-09-15", 0)])
def test_thirty_calendar_day_boundary(day, expected):
    assert len(select_decks([deck(meeting_dates=[day])], today=TODAY)) == expected


def test_unknown_date_backfill_not_new():
    assert select_decks([deck(meeting_dates=[])], today=TODAY) == []


def test_old_baseline_does_not_appear_because_seen_today():
    assert select_decks([deck(meeting_dates=["2024-01-01"])], today=TODAY) == []


def test_late_added_old_meeting_link_appears_in_combined():
    d = deck(meeting_dates=["2024-01-01"], observation="new_link")
    assert select_decks([d], today=TODAY)[0]["new_in_window"]
    assert select_decks([d], today=TODAY, view="meetings") == []


def test_future_meeting_link_detected_after_baseline_is_visible():
    d = deck(meeting_dates=["2026-10-01"], observation="new_link")
    assert select_decks([d], today=TODAY)[0]["display_date"] == "2026-09-14"


def test_future_first_seen_does_not_qualify_by_detection():
    d = deck(meeting_dates=[], observation="new_link", first_seen="2026-09-15T00:00:00Z")
    assert select_decks([d], today=TODAY) == []


def test_reused_file_recent_and_future_dates():
    d = deck(meeting_dates=["2026-10-01", "2026-09-10"])
    assert select_decks([d], today=TODAY)[0]["display_date"] == "2026-09-10"


def test_new_view_never_includes_initial_inventory():
    assert select_decks([deck()], today=TODAY, view="new") == []


def test_expiration_uses_first_seen_not_last_seen():
    d = deck(meeting_dates=[], observation="new_link", first_seen="2026-08-14T00:00:00Z")
    assert select_decks([d], today=TODAY) == []


def test_taiwan_midnight_boundary():
    d = deck(meeting_dates=[], observation="new_link", first_seen="2026-08-15T16:01:00Z")
    assert len(select_decks([d], today=TODAY)) == 1
    d["first_seen"] = "2026-08-15T15:59:00Z"
    assert select_decks([d], today=TODAY) == []


def test_untouched_input_and_deduplication():
    d = deck()
    assert len(select_decks([d, dict(d)], today=TODAY)) == 1
    assert "display_date" not in d


def test_same_file_different_companies_retained():
    assert len(select_decks([deck(), deck(ticker="5678")], today=TODAY)) == 2


def test_resources_opt_in():
    d = deck(file_type="unverified_resource_link")
    assert select_decks([d], today=TODAY) == []
    assert len(select_decks([d], today=TODAY, include_resources=True)) == 1


def test_company_and_query_filters():
    d = deck(company="Mixed Case Name")
    assert len(select_decks([d], today=TODAY, query="mixed slides", tickers=["1234"])) == 1
    assert select_decks([d], today=TODAY, tickers=["5678"]) == []
    assert select_decks([d], today=TODAY, query="[literal regex]") == []


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///tmp/x", "data:text/html,test", "https://user:pass@host.test", "//example.test/file.pdf"])
def test_unsafe_links_not_rendered(url):
    assert safe_link(url) == ""
    assert select_decks([deck(url=url)], today=TODAY) == []


def test_exports_and_filenames():
    assert filename("https://example.test/download?file_name=report%20one.pdf") == "report one.pdf"
    rows = table_rows(select_decks([deck(company="=FORMULA()")], today=TODAY))
    content = export_csv(rows).decode("utf-8-sig")
    assert "'=FORMULA()" in content
    assert "https://example.test/slides.pdf" in content
    assert "First seen (Taipei)" in content


def test_csv_empty_has_headers():
    assert "Company" in export_csv([]).decode("utf-8-sig")


@pytest.mark.parametrize("n", [0, -1, 31])
def test_invalid_window(n):
    with pytest.raises(ValueError):
        select_decks([], today=TODAY, days=n)


def test_invalid_view():
    with pytest.raises(ValueError):
        select_decks([], today=TODAY, view="invented")


def test_invalid_dates_do_not_crash():
    d = deck(meeting_dates=[None, "not-a-date", "2026-02-30"], first_seen="invalid")
    assert select_decks([d], today=TODAY) == []
    assert parse_timestamp(None) is None
    assert parse_day(7) is None
    assert local_time(None) == ""


def test_stale_state():
    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    assert stale_message({}, now)
    assert stale_message({"last_completed_at": "2026-09-12T00:00:00Z"}, now)
    assert stale_message({"last_completed_at": "2026-09-14T00:00:00Z"}, now) is None
