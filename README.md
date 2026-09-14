# Taiwan Deck Monitor

A Streamlit dashboard with a rolling **30-day list of Taiwanese-company presentation links from PoorStock**. GitHub Actions does the daily collection. Streamlit only displays the saved results.

**You do not need to run a Windows scheduler or keep your computer on.** This package replaces the local setup in the earlier monitor.

**Delivery status:** Code package, not a deployed app. The included data files are empty intentionally. The first successful GitHub collection populates them with real links; no example links are presented as live results.

## Deploy: three steps

### 1. Put the files in GitHub

Create a repository such as `poorstock-dashboard`. Unzip the download and upload the **contents** of its folder to the repository's default branch. Use GitHub Desktop, git, or GitHub's Add file > Upload files.

The root of the repository must contain `app.py` and `requirements.txt`. Do not put everything inside another enclosing folder, and do not upload just the ZIP file.

Include the `.github` and `.streamlit` folders. They may be hidden in your file manager. The critical scheduler file is:

```text
.github/workflows/refresh.yml
```

The first push starts a collection and a separate test workflow automatically. It does not install anything on your own computer.

### 2. Check the first collection

In GitHub, open **Actions > Refresh PoorStock decks**. Inspect the initial run. If no initial run started, select **Run workflow** on your default branch once. Do not queue another collection while one is already running.

A completed run commits real links to `data/decks.json`. Its summary shows how many company pages were checked and any errors. Broad discovery checks the companies found through PoorStock's public category lists; it is deliberately sequential and rate-limited.

If GitHub blocks the data commit, review **Settings > Actions > General > Workflow permissions** and your branch rules. This workflow requests `contents: write`. Organization policy or a protected branch can still prohibit the bot's commit. Use a dedicated dashboard repository/branch that allows these data commits; do not broadly disable protection on an important existing project.

### 3. Deploy on Streamlit

In **Streamlit Community Cloud**, select **Create app**, choose your GitHub repository and its default branch, and set the entrypoint to:

```text
app.py
```

Under Advanced settings, choose **Python 3.12**, then deploy. No app secrets, database account, API key, or personal access token is needed for this default setup.

The dashboard reads the JSON files checked out with the app. Community Cloud reflects updates pushed to the connected GitHub repository. While the app is open, it re-reads its local saved data once per minute. It never scrapes PoorStock when you open, filter, or reload the dashboard.

## What you see

- A sortable, searchable table of stock codes, company names, presentation links, PoorStock source links, file types, and dates.
- A default 30-day view; filters for companies and newly detected links.
- Counts of deck links, companies, and newly detected links in the current window.
- A CSV export of the currently displayed links.
- Collection status, page counts, errors, missing company pages, and the most recent 90 run summaries.

A file extension is used to identify likely decks. A PDF/PPT/PPTX label is **not** confirmation that the remote file is available. Presentation resources without a recognized extension are hidden by default but can be included using the filter.

## Daily schedule

The included workflow starts daily at **15:00 UTC = 11:00 a.m. EDT (UTC-4)**. This is literal EDT, year-round. During New York's standard-time months, that is 10 a.m. New York local time.

GitHub schedules are best effort: the job may start late, or a queued run may be dropped. The dashboard updates when collection finishes and the data commit reaches Streamlit, **not necessarily at exactly 11 a.m.** A stale-data warning appears after 36 hours without a completed collection.

For 11 a.m. New York local time year-round instead, replace the `schedule` section in `.github/workflows/refresh.yml` with the following, and update the display-only schedule labels in `app.py` and `refresh.py`:

```yaml
  schedule:
    - cron: '0 11 * * *'
      timezone: 'America/New_York'
```

Timezone-aware scheduling is documented by GitHub as of the package's creation date, September 14, 2026. On an older self-hosted GitHub Enterprise installation, check whether that syntax is supported before changing it.

You do not need a Streamlit scheduler upgrade for this design: collection happens in GitHub Actions. Broad scans consume Actions runner time; check your GitHub plan and Actions usage/budget, especially for a private repository.

## What "last 30 days" means

**Recent decks + new links (default):** Links associated with a meeting in the last 30 Taiwan calendar days, including today, plus links first observed during those days **after that company's baseline**.

**Recent meeting dates only:** Links associated with meetings in that window. The first successful collection can populate this view from currently visible PoorStock metadata. It cannot reconstruct links that have disappeared from the source.

**Newly detected links only:** Previously unseen URLs on companies already being monitored. This view builds after monitoring starts. A newly discovered company's first collection is an *initial inventory*, not evidence of a new upload.

Meeting dates and detection dates are **not publication timestamps**. Unknown-date initial inventories are not silently treated as recent. The original site is not a verified upload-time feed.

Older links disappear from the displayed window automatically. Their identifiers and observations remain in `data/state.json` to prevent duplicate alerts if they reappear. **Do not delete this file.** A deck linked from multiple meetings has one row per stock code and URL, with its associated meeting dates retained.

## Collection and coverage

The default is broad PoorStock discovery: read its earnings-call calendar, collect company codes from the public category links, and check those companies' presentation pages daily. Retain discovered companies across runs. Previously unchecked/oldest-checked companies receive priority if an earlier broad scan stopped partway through.

To monitor a limited list instead, edit `config.json` and populate `companies`, for example:

```json
"companies": ["2330", "4906"]
```

Those are example stock codes, not recommendations. Leave the array empty for broad discovery. The dashboard's company filter only changes what you see; it does not change the crawler's universe.

Only PoorStock pages are fetched. Requests are sequential, at least three seconds apart by default, respect robots.txt rules, and stop on access restrictions or repeated failures. No CAPTCHA workaround, proxy rotation, MOPS scraping, or deck downloads are included. Clicking a presentation link can still open MOPS or another original file host.

Coverage is **PoorStock-discoverable pages**, not every Taiwanese company or every MOPS filing. The source may omit or delay links. A file replaced at the same URL is not detected. Links added and removed between scans can be missed. Search covers company names, codes and filenames, not PDF contents or PoorStock's AI summaries.

## Failures do not erase the catalog

Expected source/network failures retain prior observations and save an incomplete status. The workflow commits that status before marking collection failed. Unexpected errors preserve the previous deck snapshot. Missing pages are explicitly reported rather than counted as checked.

If all previous deck links on a company page disappear, the parser flags that page for review instead of assuming it successfully became empty. A layout change may therefore require a parser update.

A long scan has a 300-minute collection budget and a 330-minute GitHub job timeout. Those are safety ceilings, not expected runtimes. A canceled job or runner termination before publication cannot commit its in-progress observations; the previous published catalog remains available.

Scheduled workflows must be on the default branch. GitHub may disable public-repository schedules after 60 days without repository activity. A normally running collector commits its timestamps daily; after a long stoppage, check whether the workflow needs re-enabling.

## Files

```text
app.py                          Streamlit dashboard
refresh.py                      Daily collector and JSON persistence
poorstock_monitor.py            Parsing and restricted HTTP client from the earlier monitor
dashboard_data.py              Date filtering, table formatting, CSV export
config.json                     Broad collection or optional company list
requirements.txt                Dashboard dependencies
requirements-collector.txt      Collector dependencies
requirements-dev.txt            Dependencies for all tests
.github/workflows/refresh.yml    Scheduled collection and data commit
.github/workflows/tests.yml      Automated tests, including Streamlit smoke tests
.streamlit/config.toml           Dashboard appearance
data/decks.json                 Rolling 30-day public snapshot
data/state.json                 Persistent full link history / deduplication
data/status.json                Latest collection health
data/runs.json                  Recent run summaries
```

The full SQLite database and fetched HTML are temporary inside each collector run, not committed. Persistent history is JSON. The frontend therefore remains lightweight and the data can be reused by another dashboard framework later. A different hosting platform will need to sync/read the published JSON; it should not rely on Streamlit's local filesystem for scheduled collection.

## Development and validation

Optional local development:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python refresh.py
python -m streamlit run app.py
```

`python refresh.py` performs a real collection immediately. Do not run it just to preview the interface; `streamlit run app.py` alone displays the saved data or its empty setup state.

Validation in the authoring runtime: **82 offline tests passed; the Streamlit test module was skipped because Streamlit was unavailable and package installation was blocked by this runtime's outbound DNS restrictions.** That module contains two UI smoke tests which the GitHub test workflow runs after installing dependencies. Parsing fixtures are synthetic HTML, not captured raw PoorStock responses.

Public PoorStock pages and official deployment/scheduling documentation were inspected using web retrieval. **A live collector crawl, actual Streamlit rendering, GitHub commit, and hosted deployment were not exercised here.** Treat the first GitHub collection and test workflows as deployment validation.

## References checked

- PoorStock earnings-call calendar: https://poorstock.com/earning_call_home
- Representative company page: https://poorstock.com/earningcall/4906
- Streamlit deployment: https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy
- Streamlit GitHub updates: https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app
- GitHub scheduled workflow behavior and timezones: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule

This is an independent personal-use link monitor, not an official PoorStock product.
