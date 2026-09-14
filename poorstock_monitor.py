#!/usr/bin/env python3
"""Monitor presentation links published on PoorStock; never fetch MOPS.

Python 3.10+. See README.md for first-run semantics and coverage limitations.
Only PoorStock HTML is downloaded. 'New' means a newly observed URL, not a
verified upload time or a verified change to the underlying file.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import smtplib
import sqlite3
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from filelock import FileLock, Timeout as LockTimeout

BASE = "https://poorstock.com"
CALENDAR = BASE + "/earning_call_home"
BOT = "PoorStockDeckMonitor"
UTC = timezone.utc
LOG = logging.getLogger(BOT)
CODE = r"[0-9A-Za-z]{4,8}"
COMPANY_PATH = re.compile(rf"^/earningcall/({CODE})/?$")
STOCK_PATH = re.compile(rf"^/(?:stock|earningcall)/({CODE})/?$")
DATE_RE = re.compile(r"(?<!\d)(20\d{2}|1\d{2})[/-](\d{1,2})[/-](\d{1,2})(?!\d)")
FILE_RE = re.compile(r"\.(pdf|pptx?|ppsx?)(?:$|[?#&/])", re.I)
PRESENTATION = "\u7c21\u5831"
CALL = "\u6cd5\u8aaa\u6703"
DATE_LABEL = "\u65e5\u671f"
MOPS_SECTION = "\u516c\u958b\u8cc7\u8a0a\u89c0\u6e2c\u7ad9\u8cc7\u8a0a"
HISTORY_SECTION = "\u4e4b\u524d\u6cd5\u8aaa\u6703\u7684\u8cc7\u8a0a"
AI_SECTION = "\u672c\u7ad9AI\u91cd\u9ede\u6574\u7406\u5206\u6790"
CATEGORY_SECTION = "\u6982\u5ff5\u80a1\u5206\u985e"
NO_DATA = ("\u5c1a\u7121", "\u67e5\u7121", "\u5c1a\u672a", "\u6c92\u6709", "\u672a\u63d0\u4f9b")
FIELD_LABELS = {DATE_LABEL, "\u5730\u9ede", "\u76f8\u95dc\u8aaa\u660e",
                "\u516c\u53f8\u63d0\u4f9b\u7684\u9023\u7d50", "PDF" + PRESENTATION,
                "\u76f4\u64ad\u6216\u4e32\u6d41", "\u6587\u4ef6\u5831\u544a"}
DEFAULTS: dict[str, Any] = {
    "mode": "broad", "companies": [], "data_dir": "data",
    "timezone": "UTC-04:00", "daily_time": "11:00",
    "request_delay_seconds": 3.0, "timeout_seconds": 30,
    "discovery_refresh_days": 1, "calendar_followup_days": 45,
    "max_requests_per_run": 6000, "max_category_pages": 100,
    "contact_email": "", "email_enabled": False,
}


class MonitorError(RuntimeError):
    pass


class StopCrawl(MonitorError):
    """Stop the entire scan rather than hammer a blocked/unavailable site."""


class LayoutError(MonitorError):
    pass


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).strip(":\uff1a")


def allowed_url(url: str) -> bool:
    try:
        p = urlsplit(url)
        return (p.scheme == "https" and p.hostname in {"poorstock.com", "www.poorstock.com"}
                and p.port in (None, 443) and not p.username and not p.password)
    except ValueError:
        return False


def canonical_url(href: str, base: str = BASE) -> str | None:
    url = urljoin(base, html.unescape(href).strip())
    try:
        p = urlsplit(url)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            return None
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path,
                           urlencode(sorted(query)), ""))
    except ValueError:
        return None


def extract_date(text: str) -> str | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    if y < 1911:
        y += 1911  # Republic of China year, when present in metadata.
    try:
        return datetime(y, mo, d).date().isoformat()
    except ValueError:
        return None


def file_kind(url: str) -> str:
    p = urlsplit(url)
    m = FILE_RE.search(unquote(p.path))
    if not m:
        for _, value in parse_qsl(p.query):
            m = FILE_RE.search(unquote(value))
            if m:
                break
    return m.group(1).lower() if m else "unverified_resource_link"


def company_name(text: str, ticker: str) -> str:
    m = re.search(rf"(.*?)\s*[\uff08(]\s*{re.escape(ticker)}\s*[\uff09)]", text)
    if m and m.group(1).strip():
        return m.group(1).strip()
    text = re.sub(rf"^\s*{re.escape(ticker)}\s*", "", text).strip()
    return text[:120] if text else ticker


def parse_calendar(body: bytes | str) -> tuple[dict[str, str], list[str]]:
    soup = BeautifulSoup(body, "html.parser")
    companies: dict[str, str] = {}
    categories: set[str] = set()
    for a in soup.find_all("a", href=True):
        url = canonical_url(a["href"], CALENDAR)
        if not url or not allowed_url(url):
            continue
        path = urlsplit(url).path
        m = COMPANY_PATH.fullmatch(path)
        if m:
            code = m.group(1)
            companies[code] = company_name(a.get_text(" ", strip=True), code)
        if path.startswith("/tag/"):
            categories.add(url)
    text = soup.get_text(" ", strip=True)
    if CALL not in text or not categories:
        raise LayoutError("Calendar layout not recognized; refusing to report an empty successful scan.")
    return companies, sorted(categories)


def parse_category(body: bytes | str) -> dict[str, str]:
    soup = BeautifulSoup(body, "html.parser")
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        url = canonical_url(a["href"])
        if not url or not allowed_url(url):
            continue
        m = STOCK_PATH.fullmatch(urlsplit(url).path)
        if m:
            found[m.group(1)] = company_name(a.get_text(" ", strip=True), m.group(1))
    if not found:
        raise LayoutError("Category contains no recognized stock links; discovery may have changed.")
    return found


def parse_company(body: bytes | str, ticker: str) -> tuple[str, list[dict[str, Any]]]:
    """Read labelled metadata in DOM order, excluding PoorStock's AI analysis.

    Date is the *associated meeting date*, never an asserted upload date.
    Multiple meetings reusing the same URL become one record with several dates.
    """
    soup = BeautifulSoup(body, "html.parser")
    title_candidates = [n.get_text(" ", strip=True) for n in soup.find_all(["h1", "title"])]
    title = next((t for t in title_candidates if ticker in t and CALL in t), None)
    if not title:
        raise LayoutError(f"{ticker}: missing expected company/call heading (possibly a challenge page).")
    name = company_name(title, ticker)
    full_text = soup.get_text(" ", strip=True)
    has_sections = MOPS_SECTION in full_text or HISTORY_SECTION in full_text
    if not has_sections and not any(marker in full_text for marker in NO_DATA):
        raise LayoutError(f"{ticker}: presentation metadata sections not found.")
    for node in soup.find_all(["script", "style", "noscript", "nav", "footer", "blockquote"]):
        node.decompose()
    active = not has_sections
    current_date: str | None = None
    field = ""
    awaiting_date = False
    result: dict[str, dict[str, Any]] = {}
    for node in soup.descendants:
        if isinstance(node, Tag):
            if node.name in {"h1", "h2", "h3", "h4"}:
                heading = compact(node.get_text(" ", strip=True))
                if MOPS_SECTION in heading or HISTORY_SECTION in heading:
                    active, current_date, field, awaiting_date = True, None, "", False
                elif AI_SECTION in heading or CATEGORY_SECTION in heading:
                    active, current_date, field, awaiting_date = False, None, "", False
            if not active:
                continue
            if node.name in {"dt", "th", "label"}:
                label = compact(node.get_text(" ", strip=True))
                if label in FIELD_LABELS:
                    field, awaiting_date = label, label == DATE_LABEL
            if node.name != "a" or not node.get("href"):
                continue
            url = canonical_url(node["href"], f"{BASE}/earningcall/{ticker}")
            if not url:
                continue
            label = compact(node.get_text(" ", strip=True))
            kind = file_kind(url)
            # A labelled presentation link may lead to a landing page; do not
            # pretend it is a verified PDF. Unrelated annual reports are ignored.
            is_deck = PRESENTATION in label or PRESENTATION in field
            is_str_file = "/nas/STR/" in urlsplit(url).path and kind != "unverified_resource_link"
            if not (is_deck or is_str_file):
                continue
            if urlsplit(url).path.rstrip("/") in {"", "/earningcall/" + ticker}:
                continue
            item = result.setdefault(url, {"ticker": ticker, "company": name,
                "url": url, "file_type": kind, "meeting_dates": [],
                "poorstock_url": f"{BASE}/earningcall/{ticker}"})
            if current_date and current_date not in item["meeting_dates"]:
                item["meeting_dates"].append(current_date)
        elif isinstance(node, NavigableString) and active:
            value = compact(str(node))
            if value in FIELD_LABELS:
                field, awaiting_date = value, value == DATE_LABEL
                if awaiting_date:
                    current_date = None
            elif awaiting_date and value:
                parsed = extract_date(str(node))
                if parsed:
                    current_date, awaiting_date = parsed, False
    for item in result.values():
        item["meeting_dates"].sort(reverse=True)
    return name, list(result.values())


def open_db(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript("""
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS companies (
        ticker TEXT PRIMARY KEY, name TEXT NOT NULL, initialized INTEGER NOT NULL DEFAULT 0,
        last_calendar_seen TEXT, last_checked TEXT, last_error TEXT);
      CREATE TABLE IF NOT EXISTS http_cache (
        url TEXT PRIMARY KEY, body BLOB NOT NULL, etag TEXT, modified TEXT);
      CREATE TABLE IF NOT EXISTS decks (
        id TEXT PRIMARY KEY, ticker TEXT NOT NULL, company TEXT NOT NULL,
        url TEXT NOT NULL, file_type TEXT NOT NULL, meeting_dates TEXT NOT NULL,
        poorstock_url TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        observation TEXT NOT NULL, reported INTEGER NOT NULL DEFAULT 0,
        email_sent INTEGER NOT NULL DEFAULT 0);
    """)
    return db


def get_meta(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def put_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    with db:
        db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))


def add_companies(db: sqlite3.Connection, companies: dict[str, str], calendar: bool = False) -> None:
    stamp = now_utc() if calendar else None
    with db:
        for code, name in companies.items():
            db.execute("""INSERT INTO companies(ticker,name,last_calendar_seen) VALUES (?,?,?)
              ON CONFLICT(ticker) DO UPDATE SET
              name=CASE WHEN excluded.name=excluded.ticker THEN companies.name ELSE excluded.name END,
              last_calendar_seen=COALESCE(excluded.last_calendar_seen,companies.last_calendar_seen)""",
                       (code, name, stamp))


def observe(db: sqlite3.Connection, ticker: str, name: str, items: list[dict[str, Any]]) -> None:
    row = db.execute("SELECT initialized FROM companies WHERE ticker=?", (ticker,)).fetchone()
    initialized = bool(row and row[0])
    stamp = now_utc()
    with db:
        for item in items:
            identity = hashlib.sha256((ticker + "\n" + item["url"]).encode()).hexdigest()
            previous = db.execute("SELECT meeting_dates FROM decks WHERE id=?", (identity,)).fetchone()
            if previous:
                dates = sorted(set(json.loads(previous[0])) | set(item["meeting_dates"]), reverse=True)
                db.execute("UPDATE decks SET last_seen=?,meeting_dates=?,company=? WHERE id=?",
                           (stamp, json.dumps(dates), name, identity))
            else:
                db.execute("""INSERT INTO decks
                  (id,ticker,company,url,file_type,meeting_dates,poorstock_url,
                   first_seen,last_seen,observation,email_sent) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                  (identity,ticker,name,item["url"],item["file_type"],json.dumps(item["meeting_dates"]),
                   item["poorstock_url"],stamp,stamp,
                   "new_link" if initialized else "initial_inventory", 0 if initialized else 1))
        db.execute("UPDATE companies SET name=?,initialized=1,last_checked=?,last_error=NULL WHERE ticker=?",
                   (name, stamp, ticker))


class Fetcher:
    """Sequential HTTPS client, restricted to PoorStock before every redirect."""
    def __init__(self, db: sqlite3.Connection, cfg: dict[str, Any]):
        self.db, self.cfg = db, cfg
        self.session = requests.Session()
        contact = f"; contact: {cfg['contact_email']}" if cfg["contact_email"] else ""
        self.session.headers.update({"User-Agent": f"{BOT}/1.0 (personal presentation-link monitor{contact})",
                                     "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"})
        self.robots: dict[str, Any] = {}
        self.delay = float(cfg["request_delay_seconds"])
        self.last_request = 0.0
        self.requests = 0

    def close(self) -> None:
        self.session.close()

    def _request(self, url: str, headers: dict[str, str] | None = None,
                 robots_request: bool = False) -> tuple[int, bytes, dict[str, str]]:
        for redirect in range(6):
            if not allowed_url(url):
                raise StopCrawl(f"Refusing a non-PoorStock URL or redirect: {url}")
            if not robots_request:
                self.check_robots(url)
            if self.requests >= self.cfg["max_requests_per_run"]:
                raise StopCrawl("Request safety limit reached; raise max_requests_per_run only after reviewing coverage.")
            time.sleep(max(0.0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            self.requests += 1
            try:
                with self.session.get(url, headers=headers, timeout=(10, self.cfg["timeout_seconds"]),
                                      allow_redirects=False, stream=True) as r:
                    if r.status_code in {401, 403, 429}:
                        retry_after = r.headers.get("Retry-After", "not supplied")
                        raise StopCrawl(f"HTTP {r.status_code}; stopping, not bypassing. Retry-After: {retry_after}")
                    if r.status_code in {301, 302, 303, 307, 308}:
                        target = urljoin(url, r.headers.get("Location", ""))
                        if not allowed_url(target) or target == url:
                            raise StopCrawl(f"Unsafe or invalid redirect from {url}")
                        url, headers = target, None
                        continue
                    body = bytearray()
                    for chunk in r.iter_content(65536):
                        body.extend(chunk)
                        if len(body) > 8 * 1024 * 1024:
                            raise StopCrawl("Page exceeded the 8 MiB safety limit.")
                    return r.status_code, bytes(body), dict(r.headers)
            except requests.RequestException as e:
                raise MonitorError(f"Network error fetching {url}: {e}") from e
        raise StopCrawl("Too many redirects.")

    def check_robots(self, url: str) -> None:
        if not allowed_url(url):
            raise StopCrawl("Only PoorStock may be fetched.")
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.robots:
            # Imported here so offline parsing/state tests do not require network
            # setup. Protego >=0.6.2 includes the wildcard-matching security fix.
            try:
                from protego import Protego
            except ImportError as e:
                raise StopCrawl("Missing Protego: install requirements.txt before a live scan.") from e
            status, body, _ = self._request(origin + "/robots.txt", robots_request=True)
            if status in {404, 410}:
                rules = Protego.parse("")
            elif status == 200:
                text = body.decode("utf-8-sig", errors="replace")
                if re.search(r"<\s*(?:!doctype|html|body)\b", text, re.I):
                    raise StopCrawl("robots.txt returned HTML, possibly a challenge. Stopping.")
                rules = Protego.parse(text)
            else:
                raise StopCrawl(f"robots.txt returned HTTP {status}; failing closed.")
            self.robots[origin] = rules
            self.delay = max(self.delay, rules.crawl_delay(BOT) or 0)
            rate = rules.request_rate(BOT)
            if rate and rate.requests:
                self.delay = max(self.delay, rate.seconds / rate.requests)
            if rules.visit_time(BOT) or (rate and (rate.start_time or rate.end_time)):
                raise StopCrawl("robots.txt specifies visit hours; review those hours before using this monitor.")
        if not self.robots[origin].can_fetch(url, BOT):
            raise StopCrawl(f"robots.txt disallows {url}; no bypass attempted.")

    def get(self, url: str) -> bytes | None:
        if not allowed_url(url):
            raise StopCrawl(f"Refusing non-PoorStock request: {url}")
        row = self.db.execute("SELECT * FROM http_cache WHERE url=?", (url,)).fetchone()
        headers = {}
        if row:
            if row["etag"]:
                headers["If-None-Match"] = row["etag"]
            if row["modified"]:
                headers["If-Modified-Since"] = row["modified"]
        status, body, response_headers = self._request(url, headers)
        if status == 304 and row:
            return bytes(row["body"])
        if status in {404, 410}:
            return None
        if status != 200:
            raise MonitorError(f"HTTP {status} fetching {url}")
        if b"<" not in body:
            raise LayoutError(f"Expected HTML at {url}")
        h = {k.lower(): v for k, v in response_headers.items()}
        with self.db:
            self.db.execute("""INSERT INTO http_cache VALUES (?,?,?,?) ON CONFLICT(url)
              DO UPDATE SET body=excluded.body,etag=excluded.etag,modified=excluded.modified""",
              (url, body, h.get("etag"), h.get("last-modified")))
        return body


def decode_rows(rows: Any) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        d = dict(row)
        d["meeting_dates"] = json.loads(d["meeting_dates"])
        result.append(d)
    return result


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)


def render_html(report: dict[str, Any], catalog: bool = False) -> str:
    esc = lambda s: html.escape(str(s), quote=True)
    sections = []
    for title, items in [("New links on previously monitored company pages", report["new_links"]),
                         ("Initial inventory (not claimed to be newly uploaded)", report["initial_inventory"])]:
        rows = []
        for d in items:
            rows.append("<tr>" + "".join([
                f"<td>{esc(d['ticker'])}</td>", f"<td>{esc(d['company'])}</td>",
                f"<td>{esc(', '.join(d['meeting_dates']) or 'Not parsed')}</td>",
                f"<td>{esc(d['file_type'])}</td>", f"<td>{esc(d['first_seen'])}</td>",
                f"<td><a target='_blank' rel='noopener noreferrer' href='{esc(d['url'])}'>Deck / resource</a>"
                f"<br><small>{esc(d['url'])}</small></td>",
                f"<td><a target='_blank' rel='noopener noreferrer' href='{esc(d['poorstock_url'])}'>PoorStock</a></td>"
            ]) + "</tr>")
        sections.append(f"<section><h2>{esc(title)} ({len(items)})</h2><table><thead><tr>"
                        "<th>Code</th><th>Company</th><th>Associated meeting dates</th><th>Type</th>"
                        "<th>First seen (UTC)</th><th>Link</th><th>Source</th></tr></thead><tbody>"
                        + "".join(rows) + "</tbody></table></section>")
    title = "PoorStock link catalog" if catalog else "PoorStock daily deck-link monitor"
    stats = {k: v for k, v in report.items() if k not in {"new_links", "initial_inventory"}}
    return """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>""" + esc(title) + """</title><style>
body{font:16px system-ui,sans-serif;margin:2rem;line-height:1.45}table{border-collapse:collapse;width:100%}
th,td{border:1px solid #bbb;padding:.55rem;text-align:left;vertical-align:top}th{background:#eee}
small{overflow-wrap:anywhere}input{font:inherit;padding:.65rem;width:min(40rem,90%)}
pre{white-space:pre-wrap}section{margin-top:2rem}td:nth-child(6){max-width:28rem}
</style><h1>""" + esc(title) + """</h1>
<p>Links observed on PoorStock, not verified uploads. Meeting dates are not publication dates.
No presentation files were downloaded or validated. Clicking a deck may open MOPS.</p>
<label>Filter visible rows <input id="q" placeholder="Company, stock code, date, or filename"></label>
<details><summary>Run status and coverage</summary><pre>""" + esc(json.dumps(stats, ensure_ascii=False, indent=2)) + "</pre></details>" + "".join(sections) + """
<script>document.getElementById('q').addEventListener('input',function(){
const q=this.value.toLocaleLowerCase();document.querySelectorAll('tbody tr').forEach(r=>{
r.hidden=!r.textContent.toLocaleLowerCase().includes(q);});});</script></html>"""


def export_reports(db: sqlite3.Connection, data: Path, stats: dict[str, Any]) -> dict[str, Any]:
    pending = decode_rows(db.execute("SELECT * FROM decks WHERE reported=0 ORDER BY ticker,first_seen DESC"))
    report = dict(stats, new_links=[d for d in pending if d["observation"] == "new_link"],
                  initial_inventory=[d for d in pending if d["observation"] == "initial_inventory"])
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    rendered = render_html(report)
    for path, content in [(data / "reports" / f"{stamp}.json", encoded),
                          (data / "reports" / f"{stamp}.html", rendered),
                          (data / "latest.json", encoded), (data / "latest.html", rendered)]:
        atomic_write(path, content)
    catalog_rows = decode_rows(db.execute("SELECT * FROM decks ORDER BY first_seen DESC,ticker"))
    catalog = dict(stats, new_links=[d for d in catalog_rows if d["observation"] == "new_link"],
                   initial_inventory=[d for d in catalog_rows if d["observation"] == "initial_inventory"])
    atomic_write(data / "catalog.json", json.dumps(catalog, ensure_ascii=False, indent=2))
    atomic_write(data / "catalog.html", render_html(catalog, catalog=True))
    with db:
        db.executemany("UPDATE decks SET reported=1 WHERE id=?", [(d["id"],) for d in pending])
    return report


def send_email(db: sqlite3.Connection, cfg: dict[str, Any], report: dict[str, Any]) -> None:
    if not cfg["email_enabled"]:
        return
    pending = decode_rows(db.execute("SELECT * FROM decks WHERE email_sent=0 AND observation='new_link'"))
    host, sender, recipient = (os.getenv(k, "") for k in ["SMTP_HOST", "SMTP_FROM", "SMTP_TO"])
    if not all([host, sender, recipient]):
        raise MonitorError("Email enabled but SMTP_HOST, SMTP_FROM, or SMTP_TO is missing.")
    msg = EmailMessage()
    msg["From"], msg["To"] = sender, recipient
    msg["Subject"] = f"PoorStock: {len(pending)} new links | scan {report['status']}"
    lines = [msg["Subject"], f"Finished: {report['finished_at']}",
             f"Pages checked: {report['checked']} / {report['planned']}",
             f"New-company initial inventory links: {len(report['initial_inventory'])}",
             "Initial inventories are in the local HTML report; they are not new-upload alerts.",
             "New means first-observed URL on an already monitored page. No MOPS files were fetched.", ""]
    for d in pending:
        lines.extend([f"{d['ticker']} {d['company']} | {d['file_type']}",
                      f"Meeting dates: {', '.join(d['meeting_dates']) or 'not parsed'}",
                      d["url"], d["poorstock_url"], ""])
    if report["errors"]:
        lines.extend(["Errors / incomplete coverage:", *report["errors"]])
    msg.set_content("\n".join(lines))
    port = int(os.getenv("SMTP_PORT", "587"))
    context = ssl.create_default_context()
    client = smtplib.SMTP_SSL(host, port, timeout=30, context=context) if port == 465 else smtplib.SMTP(host, port, timeout=30)
    with client as smtp:
        if port != 465:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        user = os.getenv("SMTP_USER", "")
        if user:
            password = os.getenv("SMTP_PASSWORD", "")
            if not password:
                raise MonitorError("SMTP_USER is set but SMTP_PASSWORD is missing.")
            smtp.login(user, password)
        smtp.send_message(msg)
    with db:
        db.executemany("UPDATE decks SET email_sent=1 WHERE id=?", [(d["id"],) for d in pending])
    # At-least-once delivery: a crash after SMTP acceptance but before this commit
    # may duplicate an email, but failed sends do not silently lose observations.


def scan(db: sqlite3.Connection, cfg: dict[str, Any], data: Path,
         fetcher: Fetcher | None = None) -> dict[str, Any]:
    client = fetcher or Fetcher(db, cfg)
    stats: dict[str, Any] = {"started_at": now_utc(), "mode": cfg["mode"], "planned": 0,
        "checked": 0, "missing_pages": [], "errors": [], "requests": 0,
        "coverage_note": "PoorStock-discoverable companies only; not a completeness guarantee for MOPS."}
    try:
        if cfg["companies"]:
            add_companies(db, {code: code for code in cfg["companies"]})
            codes = sorted(set(cfg["companies"]))
            stats["mode"] = "watchlist"
        else:
            body = client.get(CALENDAR)
            if body is None:
                raise LayoutError("PoorStock calendar is missing.")
            recent, categories = parse_calendar(body)
            add_companies(db, recent, calendar=True)
            stats["calendar_companies"] = len(recent)
            if cfg["mode"] == "broad":
                previous = get_meta(db, "last_discovery")
                due = not previous or datetime.now(UTC).date() - datetime.fromisoformat(previous).date() >= timedelta(days=cfg["discovery_refresh_days"])
                if due:
                    if len(categories) > cfg["max_category_pages"]:
                        raise StopCrawl("Category discovery exceeded safety limit; review the page layout.")
                    discovery_ok = True
                    for url in categories:
                        try:
                            category_body = client.get(url)
                            if category_body is None:
                                raise LayoutError(f"Category missing: {url}")
                            add_companies(db, parse_category(category_body))
                        except StopCrawl:
                            raise
                        except MonitorError as e:
                            discovery_ok = False
                            stats["errors"].append(str(e))
                    if discovery_ok:
                        put_meta(db, "last_discovery", now_utc())
                codes = [r[0] for r in db.execute("SELECT ticker FROM companies ORDER BY COALESCE(last_checked,''),ticker")]
            else:
                cutoff = (datetime.now(UTC) - timedelta(days=cfg["calendar_followup_days"])).isoformat(timespec="seconds")
                codes = [r[0] for r in db.execute("SELECT ticker FROM companies WHERE last_calendar_seen>=? ORDER BY ticker", (cutoff,))]
        if not codes:
            raise LayoutError("No company pages selected. Scan is not an all-clear.")
        stats["planned"] = len(codes)
        consecutive_failures = 0
        LOG.info("Checking %d company pages (%s)", len(codes), stats["mode"])
        for number, code in enumerate(codes, 1):
            try:
                page = client.get(f"{BASE}/earningcall/{code}")
                if page is None:
                    stats["missing_pages"].append(code)
                    continue
                name, items = parse_company(page, code)
                previous_count = db.execute("SELECT COUNT(*) FROM decks WHERE ticker=?", (code,)).fetchone()[0]
                if previous_count and not items:
                    raise LayoutError(f"{code}: all previously observed links disappeared; review layout or page removal.")
                observe(db, code, name, items)
                stats["checked"] += 1
                consecutive_failures = 0
                if number % 25 == 0 or number == len(codes):
                    LOG.info("Progress %d/%d; checked=%d, missing=%d, errors=%d", number,
                             len(codes),stats["checked"],len(stats["missing_pages"]),len(stats["errors"]))
            except StopCrawl:
                raise
            except MonitorError as e:
                stats["errors"].append(str(e))
                with db:
                    db.execute("UPDATE companies SET last_error=? WHERE ticker=?", (str(e), code))
                LOG.warning("%s", e)
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    raise StopCrawl("Five consecutive company errors; stopping this scan.")
    except MonitorError as e:
        stats["errors"].append(str(e))
        LOG.error("%s", e)
    finally:
        stats["finished_at"] = now_utc()
        stats["requests"] = client.requests
        client.close()
    stats["status"] = "partial_or_failed" if stats["errors"] else "completed"
    if stats["checked"] == 0:
        stats["status"] = "failed"
    report = export_reports(db, data, stats)
    try:
        send_email(db, cfg, report)
        report["email_status"] = "sent" if cfg["email_enabled"] else "disabled"
        error_path = data / "email_error.txt"
        if error_path.exists():
            error_path.unlink()
    except (MonitorError, OSError, smtplib.SMTPException, ValueError) as e:
        LOG.error("Email failed; observations retained for a later retry: %s", e)
        report["email_error"] = str(e)
        report["email_status"] = "failed"
        atomic_write(data / "email_error.txt", now_utc() + " " + str(e))
    atomic_write(data / "latest.json", json.dumps(report, ensure_ascii=False, indent=2))
    atomic_write(data / "latest.html", render_html(report))
    LOG.info("Scan %s: %d new links, %d initial-inventory links. Report: %s",
             report["status"],len(report["new_links"]),len(report["initial_inventory"]),data / "latest.html")
    return report


def schedule_zone(name: str):
    if name in {"EDT", "UTC-04:00"}:
        return timezone(timedelta(hours=-4), "EDT")
    if name == "UTC":
        return UTC
    return ZoneInfo(name)


def is_due(db: sqlite3.Connection, cfg: dict[str, Any], now: datetime | None = None) -> bool:
    local = (now or datetime.now(UTC)).astimezone(schedule_zone(cfg["timezone"]))
    hour, minute = map(int, cfg["daily_time"].split(":"))
    return ((local.hour, local.minute) >= (hour, minute)
            and get_meta(db, "last_scheduled_attempt") != local.date().isoformat())


def load_config(path: Path) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if path.exists():
        supplied = json.loads(path.read_text(encoding="utf-8-sig"))
        unknown = set(supplied) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
        cfg.update(supplied)
    else:
        raise ValueError(f"Config not found: {path}; use the supplied config.json.")
    if cfg["mode"] not in {"broad", "calendar"}:
        raise ValueError("mode must be broad or calendar")
    if not isinstance(cfg["companies"], list) or not all(isinstance(c, str) and re.fullmatch(CODE, c) for c in cfg["companies"]):
        raise ValueError("companies must be an array of stock-code strings")
    if not isinstance(cfg["email_enabled"], bool):
        raise ValueError("email_enabled must be true or false")
    if float(cfg["request_delay_seconds"]) < 1:
        raise ValueError("request_delay_seconds must be at least 1; default is 3")
    for key in ["timeout_seconds", "discovery_refresh_days", "calendar_followup_days", "max_requests_per_run", "max_category_pages"]:
        if not isinstance(cfg[key], (int, float)) or cfg[key] <= 0:
            raise ValueError(f"{key} must be positive")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", cfg["daily_time"]):
        raise ValueError("daily_time must be HH:MM, for example 11:00")
    schedule_zone(cfg["timezone"])
    cfg["data_dir"] = str((path.parent / cfg["data_dir"]).resolve())
    return cfg


def live_check(cfg: dict[str, Any], data: Path) -> int:
    db = open_db(":memory:")
    client = Fetcher(db, cfg)
    diagnostic = data / "diagnostics"
    diagnostic.mkdir(parents=True, exist_ok=True)
    try:
        body = client.get(CALENDAR)
        if body is None:
            raise LayoutError("Calendar not found.")
        (diagnostic / "calendar.html").write_bytes(body)
        companies, categories = parse_calendar(body)
        print(f"Calendar: {len(companies)} companies; {len(categories)} category links")
        category = client.get(categories[0])
        if category is None:
            raise LayoutError("First category not found.")
        print(f"Category smoke test: {len(parse_category(category))} company links")
        candidates = cfg["companies"] or sorted(companies)
        if not candidates:
            raise LayoutError("No company available for the smoke test.")
        for code in candidates[:2]:
            page = client.get(f"{BASE}/earningcall/{code}")
            if page is None:
                raise LayoutError(f"Company page not found: {code}")
            (diagnostic / f"{code}.html").write_bytes(page)
            name, items = parse_company(page, code)
            print(json.dumps({"ticker":code,"company":name,"presentation_links":items}, ensure_ascii=False, indent=2))
        print("Live smoke test passed. This does not establish full-market coverage.")
        return 0
    except MonitorError as e:
        print(f"Live check failed: {e}", file=sys.stderr)
        return 2
    finally:
        client.close()
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="Run a scan immediately (default)")
    group.add_argument("--scheduled", action="store_true", help="Scan only when today's configured time is due")
    group.add_argument("--daemon", action="store_true", help="Stay open and check the daily schedule every 30 seconds")
    group.add_argument("--check", action="store_true", help="Small live smoke test; does not create a baseline")
    args = parser.parse_args()
    try:
        cfg = load_config(args.config.resolve())
    except (OSError, ValueError, KeyError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    data = Path(cfg["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [RotatingFileHandler(
        data / "monitor.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr is not None:  # pythonw.exe under Windows Task Scheduler has no console.
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    try:
        if args.check:
            return live_check(cfg, data)
        while True:
            try:
                with FileLock(str(data / "monitor.lock"), timeout=0):
                    db = open_db(data / "state.sqlite3")
                    try:
                        due = not (args.scheduled or args.daemon) or is_due(db, cfg)
                        if due:
                            if args.scheduled or args.daemon:
                                day = datetime.now(schedule_zone(cfg["timezone"])).date().isoformat()
                                # One automatic attempt/day, including failed attempts. This
                                # prevents a broken site from being hammered every minute.
                                put_meta(db, "last_scheduled_attempt", day)
                            result = scan(db, cfg, data)
                            if not args.daemon:
                                return 0 if result["status"] == "completed" and "email_error" not in result else 2
                    finally:
                        db.close()
            except LockTimeout:
                LOG.info("Another monitor process is active; skipping this invocation.")
            if not args.daemon:
                return 0
            time.sleep(30)
    except KeyboardInterrupt:
        LOG.info("Stopped. Saved observations remain in SQLite.")
        return 130
    except (OSError, sqlite3.Error) as e:
        LOG.exception("Monitor stopped: %s", e)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
