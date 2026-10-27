"""Download and visit statistics of pyarchinit-photogrammetry: the data of the page "Downloads and visits" of
the documentation site (docs/stats.md and docs/stats.it.md, filled by the hook tools/stats_hook.py). The weekly
workflow .github/workflows/stats.yml runs it in three steps:

    python .github/scripts/download_stats.py get stats.json      # the file kept on the orphan branch "stats"
    python .github/scripts/download_stats.py update stats.json   # the new numbers of every configured source
    python .github/scripts/download_stats.py put stats.json      # back on the branch, through the GitHub API

Every source is optional: without its secret it is left out and the page says it is not configured yet.

* releases: downloads of the application, the download_count of the dmg and zip files of the GitHub releases
  (GITHUB_TOKEN). GitHub keeps only the running total of each file, so every run stores a reading; the downloads
  of a month are the gains of the readings made in it, and those counted before the first reading go to the
  month of their release.
* goatcounter: visitors of the site per day, countries, sources, pages, systems and browsers, and the download
  clicks of the installation page (events of docs/js/download.js) (GOATCOUNTER_TOKEN).
* bigquery: pip installs of the two PyPI projects from the public dataset of PyPI (GCP_PROJECT, GCP_SA_KEY), one
  query per day, oldest first: every day since START not kept yet, and the last REQUERY_LAST kept days again.
  The table's metadata (free) says how it is clustered: the queries filter on its first clustering field when
  that is the project, so BigQuery reads only the blocks of these projects, otherwise on file.project; always
  on DATE(timestamp), the partition. A dry run would not help: on a clustered table it gives only an upper
  bound. The bytes each job billed are added up per calendar month instead, and the run stops when the next
  day (1.2 times the last one) would take the month past MONTH_BUDGET_GB, or for the rest of the month when one
  day bills more than DAY_LIMIT_GB (the clustering does not narrow the query to these projects).
* pypistats: raw numbers of pypistats.org, while BigQuery has not read every day since START (no secret needed).
* traffic: views and unique visitors of the repository per day, added up by date (GitHub keeps 14 days), and
  the referrers and paths of the last 14 days (STATS_TOKEN, a token with "Administration: read").

stats.json (schema 1; days are ISO dates in UTC; "rows" of tables are lists):

    schema, updated               1, the time of the last run
    sources.<source>              {"status": "ok" | "not configured" | "error", "checked": day}; BigQuery also
                                   "budget" ("gb" read this month, "budget") or "paused" ("gb" of the day,
                                   "limit"), and "days" read by the run
    releases.assets.<asset id>    {"tag": "v1.6.0", "platform": "macos" | "windows-x64" | "windows-arm64",
                                   "published": day of the release}
    releases.readings[]           {"date": day, "counts": {asset id: download_count}}
    pypi.days.<day>               [[project, country, installer, ci, system, python, version, type, downloads]]
                                  (BigQuery groups; ci true/false/null, python "3.12" or "" when unknown)
    pypi.billed.<month>           bytes billed by the BigQuery jobs of that month; pypi.last_billed: the last job
    pypi.paused                   {"month", "gb"}: a day billed more than DAY_LIMIT_GB in that month
    pypi.clustering, pypi.column  the clustering fields of the table at the last run; the column the queries filter on
    pypistats.days.<day>          downloads without the known mirrors; pypistats.systems.<system>.<day>;
                                  pypistats.recent {"last_day", "last_week", "last_month"}
    goatcounter.days.<day>        visitors of the pages; goatcounter.clicks.<day>: download clicks
    goatcounter.<table>           [[id, name, visitors]] since START: countries, refs, pages, systems, browsers,
                                  clicks_systems, clicks_countries, clicks_refs
    traffic.days.<day>            [views, unique visitors]
    traffic.referrers, .paths     {"until": day, "rows": [[name, views, unique visitors]]} (the last 14 days)
    page                          what the page shows, made again from the above at every run (build_page)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY", "enzococca/pyarchinit-photogrammetry")
GITHUB = "https://api.github.com"
BRANCH, FILE = "stats", "stats.json"
AUTHOR = {"name": "Enzo Cocca", "email": "enzo.ccc@gmail.com"}
START = "2026-10-01"  # the first day of the daily series (BigQuery backfills from here)
PROJECTS = ("pyarchinit-photogrammetry", "pyarchinit-photogrammetry-core")
HUMAN_INSTALLERS = frozenset({"pip", "uv", "poetry", "pdm", "pipenv", "Browser"})
TABLE = "bigquery-public-data.pypi.file_downloads"
REQUERY_LAST = 2  # BigQuery: the last days kept, read again at every run (rows that arrive late)
MONTH_BUDGET_GB = 800  # BigQuery: the bytes billed in a calendar month stop here (the sandbox gives 1 TB)
DAY_LIMIT_GB = 100  # BigQuery: a day billing more means the clustering does not narrow the query
GOATCOUNTER = "https://pyarchinit-photogrammetry.goatcounter.com/api/v0"
EVENTS = ("download-macos", "download-windows-x64", "download-windows-arm64")  # docs/js/download.js
SITE_PATH = "/pyarchinit-photogrammetry/"  # the pages of the site; GoatCounter takes any path anyone sends
PAUSE = 0.3  # GoatCounter answers at most 4 requests a second
TOP = 15  # rows of a table on the page

QUERY = r"""
SELECT
  {column} AS project,
  IFNULL(country_code, '') AS country,
  IFNULL(details.installer.name, '') AS installer,
  details.ci AS ci,
  IFNULL(details.system.name, '') AS system,
  IFNULL(REGEXP_EXTRACT(details.python, r'^[0-9]+\.[0-9]+'), '') AS python,
  IFNULL(file.version, '') AS version,
  IFNULL(file.type, '') AS type,
  COUNT(*) AS downloads
FROM `{table}`
WHERE DATE(timestamp) = @day AND {column} IN UNNEST(@projects)
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
"""


# ------------------------------------------------------------------------------------------- the network
def http(url: str, token: str | None = None, payload: dict | None = None, method: str | None = None,
         accept: str = "application/vnd.github+json", raw: bool = False):
    """One request; the answer as JSON (or as text with raw). An error of the server raises HTTPError."""
    headers = {"Accept": accept, "User-Agent": "pyarchinit-photogrammetry-stats", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if payload is not None:
        body, headers["Content-Type"] = json.dumps(payload).encode(), "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=120) as response:
        text = response.read().decode()
    return text if raw else json.loads(text)


def get_file(token: str | None) -> str | None:
    """stats.json of the branch "stats", or None before the first run, when the branch does not exist. A branch
    without the file stops the run: starting again from nothing would lose the readings and the traffic that
    GitHub no longer gives."""
    try:
        return http(f"{GITHUB}/repos/{REPO}/contents/{FILE}?ref={BRANCH}", token,
                    accept="application/vnd.github.raw+json", raw=True)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    try:
        http(f"{GITHUB}/repos/{REPO}/git/ref/heads/{BRANCH}", token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    raise SystemExit(f"the branch {BRANCH} exists but GitHub gives no {FILE}: stopped, so that a new file does not "
                     "take the place of its history")


def check(text: str) -> dict:
    """The statistics about to be written: a JSON object of schema 1 with every top-level key."""
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("schema") != 1 or not data.get("updated") or set(empty()) - set(data):
        raise SystemExit(f"not a statistics file of schema 1: {sorted(data) if isinstance(data, dict) else type(data).__name__}")
    return data


def put_file(token: str, text: str, message: str) -> str:
    """A commit with stats.json alone on the branch "stats", which the first call creates without a parent
    (an orphan branch): the Git data API, so nothing else of the repository is touched. The sha of the commit."""
    api = f"{GITHUB}/repos/{REPO}/git"
    try:
        head = http(f"{api}/ref/heads/{BRANCH}", token)["object"]["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        head = None
    tree = http(f"{api}/trees", token, {"tree": [{"path": FILE, "mode": "100644", "type": "blob", "content": text}]})
    commit = http(f"{api}/commits", token, {"message": message, "tree": tree["sha"], "parents": [head] if head else [],
                                            "author": AUTHOR, "committer": AUTHOR})
    if head:  # never forced: the history of the branch keeps every earlier file (one run at a time: see stats.yml)
        http(f"{api}/refs/heads/{BRANCH}", token, {"sha": commit["sha"], "force": False}, method="PATCH")
    else:
        http(f"{api}/refs", token, {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
    return commit["sha"]


# ------------------------------------------------------------------------------- downloads of the application
def asset_platform(name: str) -> str | None:
    """macos, windows-x64 or windows-arm64 for a build of the application (the dmg and the zips, with or
    without the version in the name); None for every other file (wheels, checksums)."""
    n = name.lower()
    if not n.startswith("pyarchinit-photogrammetry"):
        return None
    if n.endswith(".dmg") or (n.endswith(".zip") and "macos" in n):
        return "macos"
    if n.endswith(".zip") and "windows" in n:
        return "windows-arm64" if "arm64" in n else "windows-x64"
    return None


def update_releases(data: dict, token: str | None, today: date) -> None:
    releases, page = [], 1
    while True:
        batch = http(f"{GITHUB}/repos/{REPO}/releases?per_page=100&page={page}", token)
        releases += batch
        if len(batch) < 100:
            break
        page += 1
    store, counts = data["releases"], {}
    for release in releases:
        if release.get("draft"):  # the drafts of the notarization, seen by the token of the workflow
            continue
        published = (release.get("published_at") or release.get("created_at") or today.isoformat())[:10]
        for asset in release.get("assets") or []:
            platform = asset_platform(asset.get("name", ""))
            if platform:
                key = str(asset["id"])
                store["assets"][key] = {"tag": release.get("tag_name", ""), "platform": platform, "published": published}
                counts[key] = int(asset.get("download_count") or 0)
    readings = [r for r in store["readings"] if r["date"] != today.isoformat()]  # one reading a day
    store["readings"] = readings + [{"date": today.isoformat(), "counts": counts}]


def release_gains(store: dict) -> tuple[list, list]:
    """The readings as downloads: [(day of the reading, asset id, downloads since the reading before)] and
    those counted before the first reading, [(month of the release, asset id, downloads)]. A file replaced in a
    release is a new asset: all its downloads are new. A count never goes back (a file missing from a reading
    and back in the next one is not counted twice)."""
    readings = sorted(store.get("readings", []), key=lambda r: r["date"])
    if not readings:
        return [], []
    assets = store.get("assets", {})
    first = readings[0]
    before = [(assets[a]["published"][:7], a, n) for a, n in sorted(first["counts"].items()) if n and a in assets]
    last, gains = dict(first["counts"]), []
    for reading in readings[1:]:
        for a, n in sorted(reading["counts"].items()):
            gain = n - last.get(a, 0)
            if gain > 0:
                gains.append((reading["date"], a, gain))
            last[a] = max(n, last.get(a, 0))
    return gains, before


# ---------------------------------------------------------------------------------------------- pip installs
def is_real(installer: str, ci, python: str) -> bool:
    """A pip install by a person: an installer people use, not a CI run, and a Python the program runs on
    (3.10 or later) when the user agent says which (older ones are scanners)."""
    if installer not in HUMAN_INSTALLERS or ci is True:
        return False
    m = re.match(r"(\d+)\.(\d+)", python or "")
    return m is None or (int(m[1]), int(m[2])) >= (3, 10)


class BigQuery:
    """The table's metadata and the query of one day, with google-cloud-bigquery (the workflow installs it only
    when the secrets GCP_PROJECT and GCP_SA_KEY exist). The key of the service account stays in memory: no file
    is written."""

    def __init__(self, project: str, key: str):
        from google.cloud import bigquery
        from google.oauth2 import service_account

        self.bq = bigquery
        credentials = service_account.Credentials.from_service_account_info(json.loads(key))
        self.client = bigquery.Client(project=project, credentials=credentials)

    def clustering(self) -> list[str]:
        """The clustering fields of the table: metadata, which costs nothing."""
        return list(self.client.get_table(TABLE).clustering_fields or [])

    def day(self, day: date, column: str) -> tuple[list[dict], int]:
        """The groups of one day and the bytes the job billed."""
        bq = self.bq
        config = bq.QueryJobConfig(query_parameters=[
            bq.ScalarQueryParameter("day", "DATE", day), bq.ArrayQueryParameter("projects", "STRING", list(PROJECTS))])
        job = self.client.query(query(column), job_config=config)
        rows = [dict(row.items()) for row in job.result()]
        return rows, int(job.total_bytes_billed or 0)


def project_column(clustering: list[str] | None) -> str:
    """The column the queries filter on: the first clustering field of the table when it is the project, so that
    BigQuery reads only the blocks of these projects; otherwise file.project (the whole day is read)."""
    first = (clustering or [""])[0]
    return first if first in ("project", "file.project") else "file.project"


def query(column: str) -> str:
    if column not in ("project", "file.project"):  # never a name from elsewhere in the text of the query
        raise ValueError(column)
    return QUERY.format(column=column, table=TABLE)


def day_rows(rows: list[dict]) -> list:
    """The groups of a day as [[project, country, installer, ci, system, python, version, type, downloads]]."""
    return sorted(([r["project"], r["country"] or "", r["installer"] or "", r["ci"], r["system"] or "", r["python"] or "",
                    r["version"] or "", r["type"] or "", int(r["downloads"])] for r in rows), key=lambda x: [str(v) for v in x])


def bigquery_todo(days: dict, today: date) -> list[date]:
    """The days to read, oldest first: every complete day since START not kept yet, and the last REQUERY_LAST
    kept days again (rows arrive late)."""
    again, todo, day = set(sorted(days)[-REQUERY_LAST:]), [], date.fromisoformat(START)
    while day < today:
        if day.isoformat() not in days or day.isoformat() in again:
            todo.append(day)
        day += timedelta(days=1)
    return todo


def bigquery_complete(days: dict, today: date) -> bool:
    """BigQuery has read every day since START: only then the page shows its numbers."""
    return not any(d.isoformat() not in days for d in bigquery_todo({}, today))


def update_bigquery(data: dict, bq, today: date) -> dict:
    """The days of bigquery_todo, one job each, within the budget of the month: before each day but the first of
    a month, the bytes billed this month plus 1.2 times those of the last job must stay within MONTH_BUDGET_GB; a
    day billing more than DAY_LIMIT_GB stops BigQuery until the next month. What was read is kept also when the run
    stops or fails."""
    store, month = data["pypi"], today.strftime("%Y-%m")
    store["clustering"] = bq.clustering()
    column = store["column"] = project_column(store["clustering"])
    paused = store.get("paused") or {}
    if paused.get("month") == month:
        return {"status": "paused", "gb": paused["gb"], "limit": DAY_LIMIT_GB, "days": 0}
    billed, done = store.setdefault("billed", {}), 0
    for day in bigquery_todo(store["days"], today):
        used = billed.get(month, 0)  # the first day of a month always runs: one costly day must not stop BigQuery for good
        if used and used + 1.2 * store.get("last_billed", 0) > MONTH_BUDGET_GB * 1e9:
            print(f"::warning::BigQuery stopped for {month}: {used / 1e9:.1f} GB billed, the next day would pass {MONTH_BUDGET_GB} GB")
            return {"status": "budget", "gb": round(used / 1e9, 1), "budget": MONTH_BUDGET_GB, "days": done}
        rows, cost = bq.day(day, column)
        store["days"][day.isoformat()] = day_rows(rows)
        billed[month] = used + cost
        store["last_billed"], done = cost, done + 1
        if cost > DAY_LIMIT_GB * 1e9:
            store["paused"] = {"month": month, "gb": round(cost / 1e9, 1)}
            print(f"::warning::BigQuery stopped for {month}: {day} billed {cost / 1e9:.1f} GB (clustering {store['clustering']})")
            return {"status": "paused", "gb": store["paused"]["gb"], "limit": DAY_LIMIT_GB, "days": done}
    print(f"BigQuery: {done} days on {column} (clustering {store['clustering']}), {billed.get(month, 0) / 1e9:.2f} GB billed in {month}")
    return {"status": "ok", "days": done}


def update_pypistats(data: dict) -> None:
    base = f"https://pypistats.org/api/packages/{PROJECTS[0]}"
    store = data["pypistats"]
    store["recent"] = http(f"{base}/recent", accept="application/json")["data"]
    for row in http(f"{base}/overall?mirrors=false", accept="application/json")["data"]:
        if row.get("category") == "without_mirrors":
            store["days"][row["date"]] = int(row["downloads"])
    for row in http(f"{base}/system", accept="application/json")["data"]:
        store["systems"].setdefault(str(row.get("category")).lower(), {})[row["date"]] = int(row["downloads"])


# ------------------------------------------------------------------------------------------ visits of the site
def goatcounter(token: str, path: str, **params) -> dict:
    time.sleep(PAUSE)
    return http(f"{GOATCOUNTER}/{path}?{urllib.parse.urlencode(params)}", token, accept="application/json")


def update_goatcounter(data: dict, token: str, now: datetime) -> None:
    """Everything since START at every run: GoatCounter keeps it all. Visitors of the pages = all the visitors
    of a day less the download clicks (events) of that day. All or nothing: a failed request keeps the old numbers."""
    store: dict = {}
    span = {"start": f"{START}T00:00:00Z", "end": now.strftime("%Y-%m-%dT%H:00:00Z")}
    events = {"include_paths": ",".join(EVENTS), "path_by_name": "true"}

    def per_day(answer):
        return {s["day"]: int(s.get("daily") or 0) for s in answer.get("stats") or []}

    def rows(answer):
        return [[s.get("id") or "", s.get("name") or "", int(s.get("count") or 0)] for s in answer.get("stats") or []]

    every, clicks = per_day(goatcounter(token, "stats/total", **span)), per_day(goatcounter(token, "stats/total", **span, **events))
    store["days"] = {d: max(0, n - clicks.get(d, 0)) for d, n in every.items()}
    store["clicks"] = clicks
    hits = goatcounter(token, "stats/hits", **span, limit=40).get("hits") or []
    store["pages"] = [[h["path"], h.get("title") or "", int(h.get("count") or 0)] for h in hits
                      if not h.get("event") and str(h.get("path")).startswith(SITE_PATH)]  # anyone can send a path
    hits = goatcounter(token, "stats/hits", **span, **events, limit=len(EVENTS)).get("hits") or []
    store["clicks_systems"] = [[h["path"].removeprefix("download-"), "", int(h.get("count") or 0)] for h in hits if h.get("path") in EVENTS]
    for table, page in (("countries", "locations"), ("refs", "toprefs"), ("systems", "systems"), ("browsers", "browsers")):
        store[table] = rows(goatcounter(token, f"stats/{page}", **span, limit=50))
    store["clicks_countries"] = rows(goatcounter(token, "stats/locations", **span, **events, limit=50))
    store["clicks_refs"] = rows(goatcounter(token, "stats/toprefs", **span, **events, limit=50))
    data["goatcounter"] = store


# ------------------------------------------------------------------------------------ visits of the repository
def merge_traffic(days: dict, views: list) -> None:
    """The views of the last 14 days added to those kept, by date: a day seen again (today's, still growing)
    takes the newer numbers, the older days stay."""
    for v in views:
        days[v["timestamp"][:10]] = [int(v["count"]), int(v["uniques"])]


def update_traffic(data: dict, token: str, today: date) -> None:
    api, store = f"{GITHUB}/repos/{REPO}/traffic", data["traffic"]
    merge_traffic(store["days"], http(f"{api}/views?per=day", token).get("views") or [])
    store["referrers"] = {"until": today.isoformat(),
                          "rows": [[r["referrer"], r["count"], r["uniques"]] for r in http(f"{api}/popular/referrers", token)]}
    prefix = f"/{REPO}"
    store["paths"] = {"until": today.isoformat(),
                      "rows": [[p["path"].removeprefix(prefix) or "/", p["count"], p["uniques"]]
                               for p in http(f"{api}/popular/paths", token)]}


# --------------------------------------------------------------------------------------------------- the page
def _last30(days: dict, today: date) -> int:
    """The 30 complete days before today (today's numbers are still growing)."""
    first = (today - timedelta(days=30)).isoformat()
    return sum(v for d, v in days.items() if first <= d < today.isoformat())


def _months(days: dict, columns: int = 1) -> dict:
    out: dict = {}
    for d, v in days.items():
        values = v if isinstance(v, list) else [v]
        month = out.setdefault(d[:7], [0] * columns)
        for i in range(columns):
            month[i] += values[i]
    return out


def _since(days: dict) -> str | None:
    used = [d for d, v in days.items() if (v[-1] if isinstance(v, list) else v)]
    return min(used) if used else None


def _ranked(counts: dict, limit: int = TOP) -> list:
    return [[k, n] for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def _version_key(text: str):
    return [int(x) if x.isdigit() else -1 for x in re.split(r"[.\-+]", text.lstrip("v"))]


def releases_page(store: dict, today: date) -> dict | None:
    """Downloads of the application: the summary, the months ([at the readings, before the first reading]),
    platforms and versions ([name, last 30 days or None, total]). The last 30 days count from the first reading
    inside them, and only once the readings span 30 days."""
    gains, before = release_gains(store)
    readings = sorted(r["date"] for r in store.get("readings", []))
    if not readings:
        return None
    assets, latest = store["assets"], date.fromisoformat(readings[-1])
    first_in = min(d for d in readings if d >= (latest - timedelta(days=30)).isoformat())
    complete = date.fromisoformat(readings[0]) <= latest - timedelta(days=30)
    months: dict = {}
    platforms: dict = {}
    versions: dict = {}
    for day, a, n in gains:
        months.setdefault(day[:7], [0, 0])[0] += n
        recent = n if day > first_in else 0
        for table, key in ((platforms, assets[a]["platform"]), (versions, assets[a]["tag"])):
            table.setdefault(key, [0, 0])
            table[key][0] += recent
            table[key][1] += n
    for month, a, n in before:
        months.setdefault(month, [0, 0])[1] += n
        for table, key in ((platforms, assets[a]["platform"]), (versions, assets[a]["tag"])):
            table.setdefault(key, [0, 0])[1] += n
    published = {a["tag"]: a["published"] for a in assets.values()}
    order = ("macos", "windows-x64", "windows-arm64")
    return {
        "summary": {"last30": sum(v[0] for v in platforms.values()) if complete else None,
                    "total": sum(v[1] for v in platforms.values()),
                    "since": min((a["published"] for a in assets.values()), default=None)},
        "months": months,
        "platforms": [[p, v[0] if complete else None, v[1]] for p, v in sorted(platforms.items(), key=lambda kv: order.index(kv[0]))],
        "versions": [[t.lstrip("v"), v[0] if complete else None, v[1]]
                     for t, v in sorted(versions.items(), key=lambda kv: published.get(kv[0], ""), reverse=True)][:TOP],
    }


def pip_page(data: dict, today: date) -> dict | None:
    """pip installs: from BigQuery (raw and real) when it has read every day since START, otherwise the raw
    numbers of pypistats.org. Months are [raw, real] or [raw]."""
    days = data["pypi"]["days"]
    if days and bigquery_complete(days, today):
        raw, real, projects = {}, {}, {p: [0, 0] for p in PROJECTS}
        tables: dict = {k: {} for k in ("versions", "systems", "python", "countries")}
        for day, rows in days.items():
            raw.setdefault(day, 0)
            real.setdefault(day, 0)
            for project, country, installer, ci, system, python, version, _type, n in rows:
                human = is_real(installer, ci, python)
                counts = projects.setdefault(project, [0, 0])
                counts[0] += n if human else 0
                counts[1] += n
                if project != PROJECTS[0]:
                    continue
                raw[day] += n
                if human:
                    real[day] += n
                    for table, key in ((tables["versions"], version), (tables["systems"], system),
                                       (tables["python"], python), (tables["countries"], country)):
                        table[key] = table.get(key, 0) + n
        raw_months, real_months = _months(raw), _months(real)
        return {
            "source": "bigquery",
            "real": {"last30": _last30(real, today), "total": sum(real.values()), "since": min(days)},
            "raw": {"last30": _last30(raw, today), "total": sum(raw.values()), "since": min(days)},
            "months": {m: [n, real_months[m][0]] for m, (n,) in raw_months.items()},
            "projects": [[p, real_n, raw_n] for p, (real_n, raw_n) in projects.items()],
            "versions": [[v, n] for v, n in sorted(tables["versions"].items(), key=lambda kv: _version_key(kv[0]), reverse=True)][:TOP],
            "systems": _ranked(tables["systems"]),
            "python": [[v, n] for v, n in sorted(tables["python"].items(), key=lambda kv: _version_key(kv[0]), reverse=True)][:TOP],
            "countries": _ranked(tables["countries"], 20),
        }
    store = data["pypistats"]
    if not store["days"]:
        return None
    systems = {name: sum(series.values()) for name, series in store["systems"].items()}
    return {
        "source": "pypistats",
        "raw": {"last30": int(store.get("recent", {}).get("last_month") or _last30(store["days"], today)),
                "total": sum(store["days"].values()), "since": _since(store["days"]) or min(store["days"])},
        "months": _months(store["days"]),
        "systems": _ranked({k: v for k, v in systems.items() if v}),
    }


def goatcounter_page(store: dict, today: date) -> dict | None:
    if "days" not in store:
        return None
    countries: dict = {}
    for i, table in enumerate(("countries", "clicks_countries")):
        for code, name, n in store.get(table, []):
            row = countries.setdefault(code, [code, name, 0, 0])
            row[2 + i] += n
    return {
        "site": {"last30": _last30(store["days"], today), "total": sum(store["days"].values()), "since": _since(store["days"])},
        "clicks": {"last30": _last30(store["clicks"], today), "total": sum(store["clicks"].values()), "since": _since(store["clicks"])},
        "months": _months(store["days"]),
        "countries": sorted(countries.values(), key=lambda r: (-r[2] - r[3], r[0]))[:20],
        "refs": [[name, n] for _id, name, n in store.get("refs", [])][:TOP],
        "pages": [["/" + path.removeprefix(SITE_PATH), n] for path, _title, n in store.get("pages", []) if path.startswith(SITE_PATH)][:TOP],
        "systems": [[name, n] for _id, name, n in store.get("systems", [])][:10],
        "browsers": [[name, n] for _id, name, n in store.get("browsers", [])][:10],
        "clicks_systems": [[name, n] for name, _x, n in store.get("clicks_systems", [])],
        "clicks_refs": [[name, n] for _id, name, n in store.get("clicks_refs", [])][:10],
    }


def traffic_page(store: dict, today: date) -> dict | None:
    days = store.get("days") or {}
    if not days and not store.get("referrers"):
        return None
    uniques = {d: v[1] for d, v in days.items()}
    return {
        "summary": {"last30": _last30(uniques, today), "total": sum(uniques.values()), "since": min(days) if days else None},
        "months": _months(days, 2),
        "referrers": store.get("referrers") or {"until": None, "rows": []},
        "paths": store.get("paths") or {"until": None, "rows": []},
    }


def build_page(data: dict, today: date) -> dict:
    return {
        "updated": data["updated"],
        "sources": data["sources"],
        "releases": releases_page(data["releases"], today),
        "pip": pip_page(data, today),
        "site": goatcounter_page(data["goatcounter"], today),
        "repository": traffic_page(data["traffic"], today),
    }


# ------------------------------------------------------------------------------------------------- the run
def empty() -> dict:
    return {"schema": 1, "updated": None, "sources": {}, "releases": {"assets": {}, "readings": []},
            "pypi": {"days": {}}, "pypistats": {"days": {}, "systems": {}}, "goatcounter": {},
            "traffic": {"days": {}}, "page": None}


def load(path: Path) -> dict:
    data = empty()
    if path.exists():
        stored = json.loads(path.read_text())
        if stored.get("schema") != 1:
            raise SystemExit(f"{path}: unknown schema {stored.get('schema')!r}")
        data.update(stored)
    return data


def update(path: Path, env=os.environ, now: datetime | None = None, bigquery=BigQuery) -> dict:
    """The new numbers of every configured source into the file at path (missing: a new one). A source that
    fails keeps what it had and is marked "error"; the others go on."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    data = load(path)

    def run(source, needed, work):
        if needed is not None and not all(env.get(name) for name in needed):
            data["sources"][source] = {"status": "not configured"}
            return
        try:
            result = work() or {"status": "ok"}
        except Exception as e:  # the log says why; the page only that it was not updated
            print(f"::warning::{source}: {type(e).__name__}: {e}")
            result = {"status": "error"}
        data["sources"][source] = {**result, "checked": today.isoformat()}

    run("releases", None, lambda: update_releases(data, env.get("GITHUB_TOKEN"), today))
    run("goatcounter", ["GOATCOUNTER_TOKEN"], lambda: update_goatcounter(data, env["GOATCOUNTER_TOKEN"], now))
    run("bigquery", ["GCP_PROJECT", "GCP_SA_KEY"],
        lambda: update_bigquery(data, bigquery(env["GCP_PROJECT"], env["GCP_SA_KEY"]), today))
    if bigquery_complete(data["pypi"]["days"], today):
        data["sources"].pop("pypistats", None)
    else:  # until BigQuery has read every day, the raw numbers of pypistats.org
        run("pypistats", None, lambda: update_pypistats(data))
    run("traffic", ["STATS_TOKEN"], lambda: update_traffic(data, env["STATS_TOKEN"], today))
    data["updated"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    data["page"] = build_page(data, today)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n")  # compact: it grows every day
    tmp.replace(path)
    for source, status in sorted(data["sources"].items()):
        print(f"{source}: {status['status']}")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Statistics of the page Downloads and visits: get, update or put "
                                             "stats.json (put checks it first).")
    ap.add_argument("command", choices=("get", "update", "put"))
    ap.add_argument("file", nargs="?", type=Path, default=Path(FILE))
    args = ap.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    if args.command == "get":
        text = get_file(token)
        if text is None:
            print(f"no {FILE} yet on the branch {BRANCH}: the first run starts a new one")
            args.file.unlink(missing_ok=True)
        else:
            json.loads(text)  # a broken file stops the run before anything is written over it
            args.file.write_text(text)
    elif args.command == "update":
        update(args.file)
    else:
        if not token:
            raise SystemExit("GITHUB_TOKEN is needed to write the branch")
        text = args.file.read_text()
        data = check(text)
        print("commit", put_file(token, text, f"chore(stats): the numbers of {data['updated']}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
