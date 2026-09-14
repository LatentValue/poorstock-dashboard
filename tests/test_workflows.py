from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_daily_workflow_configuration():
    # BaseLoader avoids YAML 1.1 interpreting the key 'on' as boolean True.
    doc = yaml.load((ROOT / ".github/workflows/refresh.yml").read_text(), Loader=yaml.BaseLoader)
    assert doc["on"]["schedule"][0]["cron"] == "0 15 * * *"
    assert "workflow_dispatch" in doc["on"]
    assert doc["permissions"]["contents"] == "write"
    assert doc["concurrency"]["cancel-in-progress"] == "false"
    steps = doc["jobs"]["refresh"]["steps"]
    collect = next(s for s in steps if s.get("id") == "collect")
    assert collect["continue-on-error"] == "true"
    assert any("git add data/state.json data/decks.json data/status.json data/runs.json" in s.get("run", "") for s in steps)
    assert "failure" in steps[-1]["if"]


def test_ci_installs_frontend_and_runs_tests():
    text = (ROOT / ".github/workflows/tests.yml").read_text()
    assert "requirements-dev.txt" in text
    assert "pytest -q" in text
