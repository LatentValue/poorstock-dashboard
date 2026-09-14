"""Streamlit smoke tests run in GitHub Actions; skipped without Streamlit locally."""
from datetime import datetime
import json
from pathlib import Path
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest
from dashboard_data import TAIPEI

ROOT = Path(__file__).resolve().parents[1]


def test_app_empty_state(tmp_path, monkeypatch):
    monkeypatch.setenv("POORSTOCK_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=30)
    assert not app.exception
    assert "Company decks" in app.title[0].value
    assert any("first collection" in item.value for item in app.info)


def test_app_renders_and_filters_synthetic_data(tmp_path, monkeypatch):
    monkeypatch.setenv("POORSTOCK_DATA_DIR", str(tmp_path))
    day = datetime.now(TAIPEI).date().isoformat()
    decks = [{"ticker": "1234", "company": "Synthetic Company", "url": "https://example.test/file.pdf",
              "file_type": "pdf", "poorstock_url": "https://poorstock.com/earningcall/1234",
              "meeting_dates": [day], "first_seen": day + "T00:00:00+08:00",
              "last_seen": day + "T00:00:00+08:00", "observation": "initial_inventory"}]
    (tmp_path / "decks.json").write_text(json.dumps({"decks": decks}), encoding="utf-8")
    app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=30)
    assert not app.exception
    assert len(app.dataframe[0].value) == 1
    app.text_input(key="search").set_value("does-not-exist").run(timeout=30)
    assert not app.exception
    assert not app.dataframe
