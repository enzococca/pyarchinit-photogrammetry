"""The page "Downloads and visits" of the documentation site: the weekly script that gathers the numbers
(.github/scripts/download_stats.py) and the MkDocs hook that writes them on the page (tools/stats_hook.py),
both imported by path. Nothing here goes on the network: every source answers with fake responses."""
from __future__ import annotations

import importlib.util
import json
import types
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stats = _module("download_stats", ".github/scripts/download_stats.py")
hook = _module("stats_hook", "tools/stats_hook.py")

NOW = datetime(2026, 10, 27, 5, 30, tzinfo=timezone.utc)
RELEASES = [
    {"tag_name": "v1.6.0", "draft": False, "published_at": "2026-10-26T10:00:00Z", "assets": [
        {"id": 11, "name": "PyArchInit-Photogrammetry-1.6.0-macos-arm64.dmg", "download_count": 5},
        {"id": 12, "name": "PyArchInit-Photogrammetry-macos-arm64.dmg", "download_count": 7},
        {"id": 13, "name": "PyArchInit-Photogrammetry-1.6.0-windows-x64.zip", "download_count": 4},
        {"id": 14, "name": "PyArchInit-Photogrammetry-windows-arm64.zip", "download_count": 1},
        {"id": 15, "name": "pycolmap-4.2.1-cp312-cp312-manylinux_2_28_aarch64.whl", "download_count": 9}]},
    {"tag_name": "v1.7.0", "draft": True, "published_at": None, "assets": [  # a draft of the notarization
        {"id": 31, "name": "PyArchInit-Photogrammetry-1.7.0-macos-arm64.dmg", "download_count": 0}]},
    {"tag_name": "v1.5.5", "draft": False, "published_at": "2026-09-20T08:00:00Z", "assets": [
        {"id": 21, "name": "PyArchInit-Photogrammetry-1.5.5-macos-arm64.dmg", "download_count": 1200}]},
]
PIP = "pyarchinit-photogrammetry"


def _row(day, installer, ci, python, n, country="IT", system="Linux", version="1.6.0", project=PIP):
    return {"day": day, "project": project, "country": country, "installer": installer, "ci": ci, "system": system,
            "python": python, "version": version, "type": "bdist_wheel", "downloads": n}


ROWS = [
    _row(date(2026, 10, 20), "pip", None, "3.12", 40, system="Darwin"),
    _row(date(2026, 10, 20), "pip", True, "3.12", 500, country="US"),  # continuous integration
    _row(date(2026, 10, 21), "bandersnatch", None, "", 7, country="", system="", version="1.5.5"),  # a mirror
    _row(date(2026, 10, 21), "uv", False, "3.11", 12, country="DE", system="Windows", version="1.5.5"),
    _row(date(2026, 10, 22), "pip", None, "3.8", 30, country="CN"),  # a scanner
    _row(date(2026, 10, 22), "pip", None, "3.12", 6, system="Darwin", version="0.5.0", project=f"{PIP}-core"),
]


class FakeBigQuery:
    """The two calls of download_stats.BigQuery: the clustering of the table and the groups of one day, billing
    gb gigabytes (or gb(day))."""

    def __init__(self, gb=0.5, rows=(), clustering=("project", "country_code")):
        self.gb, self.found, self.fields, self.calls = gb, list(rows), list(clustering), []

    def clustering(self):
        return self.fields

    def day(self, day, column):
        self.calls.append(day)
        gb = self.gb(day) if callable(self.gb) else self.gb
        return [r for r in self.found if r["day"] == day], int(gb * 1e9)


def fake_web(calls: list):
    """The answers of GitHub, GoatCounter and pypistats.org, by URL."""
    def web(url, token=None, payload=None, method=None, accept=None, raw=False):
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        calls.append((method or ("POST" if payload else "GET"), parts.netloc, parts.path, query))
        path, events = parts.path, "include_paths" in query
        if parts.netloc == "api.github.com":
            if path.endswith("/releases"):
                return RELEASES if query["page"] == "1" else []
            if path.endswith("/traffic/views"):
                return {"count": 81, "uniques": 15, "views": [
                    {"timestamp": "2026-10-20T00:00:00Z", "count": 50, "uniques": 9},
                    {"timestamp": "2026-10-21T00:00:00Z", "count": 31, "uniques": 7}]}
            if path.endswith("/traffic/popular/referrers"):
                return [{"referrer": "github.com", "count": 40, "uniques": 8}, {"referrer": "Google", "count": 5, "uniques": 3}]
            if path.endswith("/traffic/popular/paths"):
                return [{"path": "/enzococca/pyarchinit-photogrammetry", "title": "x", "count": 60, "uniques": 10},
                        {"path": "/enzococca/pyarchinit-photogrammetry/releases", "title": "y", "count": 20, "uniques": 6},
                        {"path": "/enzococca/pyarchinit-photogrammetry/actions", "title": "z", "count": 29, "uniques": 1}]
        if parts.netloc == "pyarchinit-photogrammetry.goatcounter.com":
            assert query["start"] == "2026-10-01T00:00:00Z" and query["end"] == "2026-10-27T05:00:00Z"
            if events:
                assert query["include_paths"] == ",".join(stats.EVENTS) and query["path_by_name"] == "true"
            page = path.removeprefix("/api/v0/stats/")
            answers = {
                ("total", False): {"stats": [{"day": "2026-10-20", "daily": 700}, {"day": "2026-10-21", "daily": 537}]},
                ("total", True): {"stats": [{"day": "2026-10-20", "daily": 0}, {"day": "2026-10-21", "daily": 3}]},
                ("hits", False): {"hits": [
                    {"path": "/pyarchinit-photogrammetry/install/", "title": "Installation", "count": 300},
                    {"path": "download-macos", "count": 2, "event": True},
                    {"path": "/pyarchinit-photogrammetry/<script>alert(1)</script>", "count": 1},
                    {"path": "/casino-bonus/", "title": "spam", "count": 50},  # not a page of the site
                    {"path": "https://spam.example/", "count": 9}]},
                ("hits", True): {"hits": [{"path": "download-macos", "count": 2, "event": True},
                                          {"path": "download-windows-x64", "count": 1, "event": True}]},
                ("locations", False): {"stats": [{"id": "IT", "name": "Italy", "count": 900},
                                                 {"id": "SY", "name": "Syria", "count": 30},
                                                 {"id": "ZW", "name": "Zimbabwe", "count": 1},
                                                 {"id": "", "name": "(unknown)", "count": 4}]},
                ("locations", True): {"stats": [{"id": "IT", "name": "Italy", "count": 2}, {"id": "DE", "name": "Germany", "count": 1}]},
                ("toprefs", False): {"stats": [{"id": "github.com", "name": "github.com", "count": 400},
                                               {"name": "spam | *bold* [x](y)", "count": 1}]},
                ("toprefs", True): {"stats": [{"name": "github.com", "count": 3}]},
                ("systems", False): {"stats": [{"name": "macOS", "count": 600}, {"name": "Windows", "count": 500}]},
                ("browsers", False): {"stats": [{"name": "Firefox", "count": 700}]},
            }
            return answers[page, events]
        if parts.netloc == "pypistats.org":
            if path.endswith("/recent"):
                return {"data": {"last_day": 3, "last_month": 120, "last_week": 30}}
            if path.endswith("/overall"):
                assert query == {"mirrors": "false"}
                return {"data": [{"category": "without_mirrors", "date": "2026-10-20", "downloads": 45},
                                 {"category": "without_mirrors", "date": "2026-10-21", "downloads": 30}]}
            if path.endswith("/system"):
                return {"data": [{"category": "linux", "date": "2026-10-20", "downloads": 40},
                                 {"category": "Darwin", "date": "2026-10-21", "downloads": 35}]}
        raise AssertionError(f"unexpected request {url}")
    return web


ALL = {"GITHUB_TOKEN": "t", "GOATCOUNTER_TOKEN": "g", "GCP_PROJECT": "p", "GCP_SA_KEY": "{}", "STATS_TOKEN": "s"}


@pytest.fixture
def web(monkeypatch):
    calls: list = []
    monkeypatch.setattr(stats, "http", fake_web(calls))
    monkeypatch.setattr(stats, "PAUSE", 0)
    monkeypatch.setattr(stats, "REPO", "enzococca/pyarchinit-photogrammetry")  # the fakes answer for this one
    return calls


def _update(tmp_path, env, bq=None, now=NOW):
    return stats.update(tmp_path / "stats.json", env=env, now=now, bigquery=lambda project, key: bq)


# ------------------------------------------------------------------------------------------- pip installs
@pytest.mark.parametrize("installer, ci, python, real", [
    ("pip", None, "3.12", True), ("pip", False, "3.10", True), ("uv", None, "", True), ("Browser", None, "", True),
    ("poetry", None, "3.11", True), ("pdm", None, "3.13", True), ("pipenv", None, "3.12", True),
    ("pip", True, "3.12", False),  # continuous integration
    ("pip", None, "3.9", False), ("uv", None, "2.7", False),  # scanners
    ("bandersnatch", None, "", False), ("requests", None, "3.12", False), ("Nexus", None, "", False), ("", None, "", False),
])
def test_real_installs(installer, ci, python, real):
    assert stats.is_real(installer, ci, python) is real


def test_day_rows():
    assert stats.day_rows(ROWS[:2]) == [[PIP, "IT", "pip", None, "Darwin", "3.12", "1.6.0", "bdist_wheel", 40],
                                        [PIP, "US", "pip", True, "Linux", "3.12", "1.6.0", "bdist_wheel", 500]]


def _days(first: date, n: int) -> list:
    return [first + timedelta(days=i) for i in range(n)]


def test_bigquery_reads_the_missing_days_one_at_a_time():
    data = stats.empty()
    first = FakeBigQuery(0.5, ROWS)
    assert stats.update_bigquery(data, first, date(2026, 10, 27)) == {"status": "ok", "days": 26}
    assert first.calls == _days(date(2026, 10, 1), 26)  # from START, oldest first, complete days only
    store = data["pypi"]
    assert store["billed"] == {"2026-10": 13 * 10**9} and store["last_billed"] == 5 * 10**8
    assert store["days"]["2026-10-05"] == [] and len(store["days"]["2026-10-20"]) == 2  # a day without downloads is kept too
    later = FakeBigQuery(0.5, ROWS)
    assert stats.update_bigquery(data, later, date(2026, 11, 2)) == {"status": "ok", "days": 8}
    assert later.calls == _days(date(2026, 10, 25), 8)  # the last 2 kept again, then the days not kept yet
    assert store["billed"] == {"2026-10": 13 * 10**9, "2026-11": 4 * 10**9}  # by calendar month
    assert stats.bigquery_complete(store["days"], date(2026, 11, 2))
    assert not stats.bigquery_complete(store["days"], date(2026, 11, 4))


def test_days_read_again_replace_the_kept_ones():
    data = stats.empty()
    stats.update_bigquery(data, FakeBigQuery(0.1, ROWS), date(2026, 10, 23))  # kept up to the 22nd
    kept = dict(data["pypi"]["days"])
    late = [r for r in ROWS if r["day"] != date(2026, 10, 21)] + [_row(date(2026, 10, 22), "pip", None, "3.12", 9, version="1.6.1")]
    again = FakeBigQuery(0.1, late)
    stats.update_bigquery(data, again, date(2026, 10, 24))
    days = data["pypi"]["days"]
    assert again.calls == [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)]
    assert days["2026-10-22"] == stats.day_rows([r for r in late if r["day"] == date(2026, 10, 22)])  # rows arrived late
    assert days["2026-10-21"] == []  # read again, nothing found: replaced all the same
    assert days["2026-10-20"] == kept["2026-10-20"]  # not read again


def test_bigquery_month_budget():
    data = stats.empty()
    status = stats.update_bigquery(data, FakeBigQuery(90.0), date(2026, 10, 27))
    assert status == {"status": "budget", "gb": 720.0, "budget": 800, "days": 8}  # a ninth day (1.2 x 90 GB) would pass 800
    assert sorted(data["pypi"]["days"]) == [d.isoformat() for d in _days(date(2026, 10, 1), 8)]
    same_month = FakeBigQuery(90.0)
    assert stats.update_bigquery(data, same_month, date(2026, 10, 30))["status"] == "budget" and same_month.calls == []
    november = FakeBigQuery(1.0)
    assert stats.update_bigquery(data, november, date(2026, 11, 2)) == {"status": "ok", "days": 26}
    assert november.calls[:3] == [date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)]
    assert data["pypi"]["billed"] == {"2026-10": 720 * 10**9, "2026-11": 26 * 10**9}


def test_bigquery_pauses_when_one_day_bills_too_much():
    data = stats.empty()
    costly = FakeBigQuery(lambda day: 150.0 if day == date(2026, 10, 3) else 0.2, ROWS)
    assert stats.update_bigquery(data, costly, date(2026, 10, 27)) == {"status": "paused", "gb": 150.0, "limit": 100, "days": 3}
    assert sorted(data["pypi"]["days"]) == ["2026-10-01", "2026-10-02", "2026-10-03"]  # what was read is kept
    assert data["pypi"]["paused"] == {"month": "2026-10", "gb": 150.0}
    same_month = FakeBigQuery(0.2, ROWS)
    assert stats.update_bigquery(data, same_month, date(2026, 10, 30)) == {"status": "paused", "gb": 150.0, "limit": 100, "days": 0}
    assert same_month.calls == []  # nothing for the rest of the month
    next_month = FakeBigQuery(0.2, ROWS)
    assert stats.update_bigquery(data, next_month, date(2026, 11, 2)) == {"status": "ok", "days": 31}
    assert next_month.calls[0] == date(2026, 10, 2)


def test_a_costly_day_does_not_stop_bigquery_for_good():
    data = stats.empty()
    stats.update_bigquery(data, FakeBigQuery(700.0), date(2026, 10, 27))  # 1.2 x 700 GB would pass 800 GB
    assert data["pypi"]["paused"] == {"month": "2026-10", "gb": 700.0}
    november = FakeBigQuery(0.2)
    assert stats.update_bigquery(data, november, date(2026, 11, 2)) == {"status": "ok", "days": 32}  # tried again


class FakeClient:
    """The two calls of google.cloud.bigquery.Client the script makes: the table's metadata and a query."""

    def __init__(self, clustering):
        self.clustering, self.queries = clustering, []

    def get_table(self, name):
        assert name == "bigquery-public-data.pypi.file_downloads"
        return types.SimpleNamespace(clustering_fields=self.clustering)

    def query(self, sql, job_config):
        self.queries.append((sql, job_config))
        row = {"project": PIP, "country": "IT", "installer": "pip", "ci": None, "system": "Linux", "python": "3.12",
               "version": "1.6.0", "type": "bdist_wheel", "downloads": 3}
        return types.SimpleNamespace(result=lambda: [row], total_bytes_billed=10485760)  # the 10 MB a query bills at least


class FakeLibrary:  # the classes of google.cloud.bigquery the script uses, as plain records
    QueryJobConfig = staticmethod(lambda **kwargs: kwargs)
    ScalarQueryParameter = staticmethod(lambda *args: args)
    ArrayQueryParameter = staticmethod(lambda *args: args)


@pytest.mark.parametrize("clustering, column", [
    (["project", "country_code"], "project"),  # clustered on the top-level project: BigQuery reads only its blocks
    (None, "file.project"),  # not clustered
    (["country_code"], "file.project"),
])
def test_clustering_of_the_table_decides_the_column(clustering, column):
    bq = object.__new__(stats.BigQuery)
    bq.bq, bq.client = FakeLibrary, FakeClient(clustering)
    data = stats.empty()
    assert stats.update_bigquery(data, bq, date(2026, 10, 3)) == {"status": "ok", "days": 2}
    assert data["pypi"]["clustering"] == list(clustering or []) and data["pypi"]["column"] == column
    sql, config = bq.client.queries[0]
    assert f"SELECT\n  {column} AS project," in sql
    assert f"WHERE DATE(timestamp) = @day AND {column} IN UNNEST(@projects)" in sql  # always on the partition
    assert config["query_parameters"] == [("day", "DATE", date(2026, 10, 1)), ("projects", "STRING", list(stats.PROJECTS))]
    assert data["pypi"]["days"]["2026-10-02"] == [[PIP, "IT", "pip", None, "Linux", "3.12", "1.6.0", "bdist_wheel", 3]]
    assert data["pypi"]["billed"] == {"2026-10": 2 * 10485760}
    with pytest.raises(ValueError):
        stats.query("project; DROP TABLE x")


# --------------------------------------------------------------------------------- downloads of the application
@pytest.mark.parametrize("name, platform", [
    ("PyArchInit-Photogrammetry-1.6.0-macos-arm64.dmg", "macos"), ("PyArchInit-Photogrammetry-macos-arm64.dmg", "macos"),
    ("PyArchInit-Photogrammetry-1.6.0-windows-x64.zip", "windows-x64"),
    ("PyArchInit-Photogrammetry-windows-x64.zip", "windows-x64"),
    ("PyArchInit-Photogrammetry-1.6.0-windows-arm64.zip", "windows-arm64"),
    ("PyArchInit-Photogrammetry-windows-arm64.zip", "windows-arm64"),
    ("PyArchInit-Photogrammetry-1.6.0-linux-x64.tar.gz", None),
    ("pycolmap-4.2.1-cp312-cp312-manylinux_2_28_aarch64.whl", None),
    ("open3d-0.19.0-cp312-cp312-win_arm64.zip", None),
])
def test_platform_of_release_assets(name, platform):
    assert stats.asset_platform(name) == platform


def _store(readings):
    assets = {"11": {"tag": "v1.6.0", "platform": "macos", "published": "2026-10-26"},
              "13": {"tag": "v1.6.0", "platform": "windows-x64", "published": "2026-10-26"},
              "21": {"tag": "v1.5.5", "platform": "macos", "published": "2026-09-20"},
              "31": {"tag": "v1.6.0", "platform": "macos", "published": "2026-10-26"}}  # the dmg 11 uploaded again
    return {"assets": assets, "readings": [{"date": d, "counts": c} for d, c in readings]}


def test_snapshots_to_months():
    store = _store([("2026-10-26", {"11": 5, "13": 4, "21": 30}),
                    ("2026-11-02", {"11": 9, "13": 4, "21": 31}),
                    ("2026-11-09", {"13": 6, "31": 3}),  # 11 replaced by 31; 21 missing from this reading
                    ("2026-11-16", {"13": 6, "21": 33, "31": 5})])
    gains, before = stats.release_gains(store)
    assert before == [("2026-10", "11", 5), ("2026-10", "13", 4), ("2026-09", "21", 30)]  # in the month of the release
    assert gains == [("2026-11-02", "11", 4), ("2026-11-02", "21", 1), ("2026-11-09", "13", 2), ("2026-11-09", "31", 3),
                     ("2026-11-16", "21", 2), ("2026-11-16", "31", 2)]  # 21 back: counted from 31, not from 0
    page = stats.releases_page(store, date(2026, 11, 16))
    assert page["months"] == {"2026-09": [0, 30], "2026-10": [0, 9], "2026-11": [14, 0]}
    assert page["summary"] == {"last30": None, "total": 53, "since": "2026-09-20"}  # readings span 3 weeks only
    assert page["platforms"] == [["macos", None, 47], ["windows-x64", None, 6]]
    assert page["versions"] == [["1.6.0", None, 20], ["1.5.5", None, 33]]
    weeks = [("2026-10-26", 0), ("2026-11-02", 2), ("2026-11-09", 5), ("2026-11-16", 9), ("2026-11-23", 10),
             ("2026-11-30", 16), ("2026-12-07", 20)]
    page = stats.releases_page(_store([(d, {"13": n}) for d, n in weeks]), date(2026, 12, 7))
    assert page["summary"]["last30"] == 20 - 5  # from the first reading inside the 30 days (9 November)
    assert page["months"] == {"2026-11": [16, 0], "2026-12": [4, 0]}


def test_update_releases_one_reading_a_day(web):
    data = stats.empty()
    stats.update_releases(data, "t", date(2026, 10, 27))
    stats.update_releases(data, "t", date(2026, 10, 27))
    store = data["releases"]
    assert [r["date"] for r in store["readings"]] == ["2026-10-27"]
    assert store["readings"][0]["counts"] == {"11": 5, "12": 7, "13": 4, "14": 1, "21": 1200}  # no wheel, no draft
    assert store["assets"]["14"] == {"tag": "v1.6.0", "platform": "windows-arm64", "published": "2026-10-26"}


# ------------------------------------------------------------------------------------ visits of the repository
def test_traffic_accumulated_by_date():
    days: dict = {}
    stats.merge_traffic(days, [{"timestamp": f"2026-10-{d:02d}T00:00:00Z", "count": d, "uniques": 1} for d in range(13, 27)])
    stats.merge_traffic(days, [{"timestamp": "2026-10-26T00:00:00Z", "count": 30, "uniques": 4},
                               {"timestamp": "2026-10-27T00:00:00Z", "count": 2, "uniques": 1}])
    assert days["2026-10-13"] == [13, 1]  # GitHub no longer gives it: kept
    assert days["2026-10-26"] == [30, 4]  # seen again, grown
    assert len(days) == 15


def test_update_traffic(web):
    data = stats.empty()
    stats.update_traffic(data, "s", date(2026, 10, 27))
    store = data["traffic"]
    assert store["days"] == {"2026-10-20": [50, 9], "2026-10-21": [31, 7]}
    assert store["paths"] == {"until": "2026-10-27", "rows": [["/", 60, 10], ["/releases", 20, 6], ["/actions", 29, 1]]}
    assert {c[3].get("per") for c in web if c[2].endswith("/views")} == {"day"}


# ------------------------------------------------------------------------------------------ the whole run
def test_update_with_every_source(tmp_path, web):
    bq = FakeBigQuery(3.0, ROWS)
    data = _update(tmp_path, ALL, bq)
    assert {k: v["status"] for k, v in data["sources"].items()} == {
        "releases": "ok", "goatcounter": "ok", "bigquery": "ok", "traffic": "ok"}  # no pypistats: BigQuery has data
    assert json.loads((tmp_path / "stats.json").read_text()) == data
    pip = data["page"]["pip"]
    assert pip["source"] == "bigquery"
    assert pip["real"]["total"] == 52 and pip["raw"]["total"] == 589  # CI, mirror and scanner out
    assert pip["projects"] == [[PIP, 52, 589], [f"{PIP}-core", 6, 6]]
    assert pip["countries"] == [["IT", 40], ["DE", 12]] and pip["python"] == [["3.12", 40], ["3.11", 12]]
    site = data["page"]["site"]
    assert site["site"] == {"last30": 1234, "total": 1234, "since": "2026-10-20"}  # the clicks are not visits
    assert site["clicks"] == {"last30": 3, "total": 3, "since": "2026-10-21"}
    assert site["clicks_systems"] == [["macos", 2], ["windows-x64", 1]]
    assert site["pages"] == [["/install/", 300], ["/<script>alert(1)</script>", 1]]  # no events, no paths of others
    assert [p for p, _t, _n in data["goatcounter"]["pages"]] == ["/pyarchinit-photogrammetry/install/",
                                                                 "/pyarchinit-photogrammetry/<script>alert(1)</script>"]
    assert site["countries"][:2] == [["IT", "Italy", 900, 2], ["SY", "Syria", 30, 0]]
    repo = data["page"]["repository"]
    assert repo["summary"] == {"last30": 16, "total": 16, "since": "2026-10-20"}
    assert data["page"]["releases"]["summary"] == {"last30": None, "total": 1217, "since": "2026-09-20"}
    again = _update(tmp_path, ALL, FakeBigQuery(1.0, ROWS), now=datetime(2026, 11, 3, 5, 30, tzinfo=timezone.utc))
    assert len(again["releases"]["readings"]) == 2 and again["page"]["pip"]["real"]["total"] == 52


def test_update_without_secrets(tmp_path, web):
    data = _update(tmp_path, {})
    assert {k: v["status"] for k, v in data["sources"].items()} == {
        "releases": "ok", "goatcounter": "not configured", "bigquery": "not configured", "pypistats": "ok",
        "traffic": "not configured"}
    assert data["page"]["pip"] == {"source": "pypistats", "raw": {"last30": 120, "total": 75, "since": "2026-10-20"},
                                   "months": {"2026-10": [75]}, "systems": [["linux", 40], ["darwin", 35]]}
    assert data["page"]["site"] is None and data["page"]["repository"] is None
    assert not any(c[1] in ("pyarchinit-photogrammetry.goatcounter.com",) or "traffic" in c[2] for c in web)


def test_a_failing_source_keeps_its_numbers(tmp_path, web, monkeypatch):
    _update(tmp_path, ALL, FakeBigQuery(3.0, ROWS))
    real = stats.http

    def broken(url, *args, **kwargs):  # new totals, then an error halfway through: none of it is kept
        if "/stats/total" in url and "include_paths" not in url:
            return {"stats": [{"day": "2026-10-20", "daily": 1}]}
        if "/stats/toprefs" in url:
            raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)
        return real(url, *args, **kwargs)

    monkeypatch.setattr(stats, "http", broken)
    data = _update(tmp_path, ALL, FakeBigQuery(3.0, ROWS), now=datetime(2026, 11, 3, 5, 30, tzinfo=timezone.utc))
    assert data["sources"]["goatcounter"] == {"status": "error", "checked": "2026-11-03"}
    assert data["page"]["site"]["site"]["total"] == 1234


# --------------------------------------------------------------------------------- the branch "stats"
class FakeGitHub:
    def __init__(self, head=None, file=True):
        self.head, self.file, self.calls = head, file, []

    def __call__(self, url, token=None, payload=None, method=None, accept=None, raw=False):
        self.calls.append((method or ("POST" if payload is not None else "GET"), url.split("/git/")[-1], payload))
        if url.endswith("/git/ref/heads/stats") or "/contents/" in url:
            if self.head is None or ("/contents/" in url and not self.file):
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return '{"schema": 1}' if raw else {"object": {"sha": self.head}}
        if url.endswith("/git/trees"):
            return {"sha": "tree1"}
        if url.endswith("/git/commits"):
            return {"sha": "commit1"}
        return {}


def test_branch_stats_created_without_a_parent_then_fast_forwarded(monkeypatch):
    github = FakeGitHub()
    monkeypatch.setattr(stats, "http", github)
    assert stats.get_file("t") is None  # no file, no branch: the first run
    github.calls.clear()
    assert stats.put_file("t", '{"schema": 1}', "chore(stats): x") == "commit1"
    (_, _, tree), (_, _, commit), (method, ref, create) = github.calls[1:]
    assert tree == {"tree": [{"path": "stats.json", "mode": "100644", "type": "blob", "content": '{"schema": 1}'}]}
    assert commit["parents"] == [] and commit["author"] == commit["committer"] == stats.AUTHOR
    assert (method, ref, create) == ("POST", "refs", {"ref": "refs/heads/stats", "sha": "commit1"})
    github = FakeGitHub(head="old")
    monkeypatch.setattr(stats, "http", github)
    assert stats.get_file("t") == '{"schema": 1}'
    stats.put_file("t", "{}", "chore(stats): y")
    assert github.calls[-2][2]["parents"] == ["old"]
    assert github.calls[-1] == ("PATCH", "refs/heads/stats", {"sha": "commit1", "force": False})


def test_a_branch_without_its_file_stops_the_run(monkeypatch):
    github = FakeGitHub(head="old", file=False)
    monkeypatch.setattr(stats, "http", github)
    with pytest.raises(SystemExit, match="exists"):
        stats.get_file("t")
    assert [c[0] for c in github.calls] == ["GET", "GET"]  # the file, then the branch; nothing written


def test_the_file_is_checked_before_it_is_written(tmp_path, web, monkeypatch):
    data = _update(tmp_path, {})
    assert stats.check(json.dumps(data)) == data
    for broken in ("[]", '{"schema": 1}', json.dumps({**data, "schema": 2}), json.dumps({**data, "updated": None})):
        with pytest.raises(SystemExit):
            stats.check(broken)
    (tmp_path / "broken.json").write_text('{"schema": 1}')
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    with pytest.raises(SystemExit, match="not a statistics file"):  # before any request
        stats.main(["put", str(tmp_path / "broken.json")])


def test_reading_the_branch_fails_loudly(monkeypatch):
    def forbidden(url, *args, **kwargs):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(stats, "http", forbidden)
    with pytest.raises(urllib.error.HTTPError):
        stats.get_file("t")


# ------------------------------------------------------------------------------------------------ the page
@pytest.fixture
def sample(tmp_path, web):
    return _update(tmp_path, ALL, FakeBigQuery(3.0, ROWS))


def test_page_in_english(sample):
    text = hook.render(sample, "en")
    assert text.count("<svg") == 4 and "Numbers of 27 October 2026." in text
    for heading in ("## Summary", "## Application", "## pip installs", "## This site", "## Repository"):
        assert f"\n{heading}\n" in text
    assert "\U0001F1EE\U0001F1F9 IT Italy" in text and "\U0001F1F8\U0001F1FE SY Syria" in text
    assert "\U0001F1FF\U0001F1FC ZW<" in text  # not among the common ones: flag and code
    assert "1,234" in text and "1,217" in text and "20 September 2026" in text
    assert "The last 30 days appear once the weekly readings span a month." in text
    assert "<script>" not in text and "&lt;script&gt;" in text  # anyone can send a visit
    assert "<td>spam | *bold* [x](y)</td>" in text  # inside raw HTML, which Markdown leaves alone
    assert "macOS" in text and "Windows ARM64" in text and "not configured yet" not in text


def test_pages_of_the_repository_seen_by_one_visitor_are_left_out(sample):
    for lang, note in (("en", "Pages seen by only one visitor are left out."),
                       ("it", "Le pagine viste da un solo visitatore sono escluse.")):
        repository = hook.render(sample, lang).split("\n## ")[-1]
        assert "<td>/releases</td>" in repository and "/actions" not in repository  # 29 views, 1 visitor: the author
        assert f'<p class="ppg-stats-note">{note}</p>' in repository


def test_wide_tables_stack_and_narrow_ones_sit_side_by_side(sample):
    sections = {s.split("\n", 1)[0]: s for s in hook.render(sample, "en").split("\n## ")}
    assert "ppg-stats-grid" not in sections["Repository"]  # sources and pages: three columns each, one under the other
    grid = [line for line in sections["pip installs"].splitlines() if "ppg-stats-grid" in line]
    assert len(grid) == 1 and grid[0].count("<table>") == 4  # versions, systems, Python, countries
    assert "<strong>Projects</strong>" not in grid[0]  # three columns: the whole width
    assert ".ppg-stats-grid>*{min-width:0}" in hook.STYLE  # a cell never grows past its column


def test_page_in_italian(sample):
    text = hook.render(sample, "it")
    assert "## Riepilogo" in text and "Numeri del 27 ottobre 2026." in text
    assert "\U0001F1EE\U0001F1F9 IT Italia" in text and "\U0001F1E9\U0001F1EA DE Germania" in text
    assert "1.234" in text and "1.217" in text and "20 settembre 2026" in text and "ott" in text
    assert "Installazioni con pip (PyPI), reali" in text


def test_page_without_data_and_without_sources(tmp_path, web):
    for lang, words in (("en", "every week"), ("it", "ogni settimana")):
        assert words in hook.render(None, lang) and "<svg" not in hook.render(None, lang)
        assert words in hook.render({"schema": 1, "page": None}, lang)
    data = _update(tmp_path, {})
    text = hook.render(data, "en")
    assert text.count("not configured yet") >= 4  # clicks, real pip installs, visitors of the site and of the repository
    assert "pip installs (PyPI), raw: robots included, known mirrors left out" in text and text.count("<svg") == 2
    assert "non ancora configurato" in hook.render(data, "it")


def test_stopped_bigquery_on_the_page(tmp_path, web):
    (tmp_path / "budget").mkdir()
    data = _update(tmp_path / "budget", ALL, FakeBigQuery(90.0, ROWS))
    assert data["sources"]["bigquery"] == {"status": "budget", "gb": 720.0, "budget": 800, "days": 8, "checked": "2026-10-27"}
    assert data["page"]["pip"]["source"] == "pypistats"  # BigQuery has not read every day: the raw numbers meanwhile
    en, it = hook.render(data, "en"), hook.render(data, "it")
    assert "this month's queries billed 720 GB, and one more day would pass the 800 GB" in en
    assert "pip installs (PyPI), raw: robots included, known mirrors left out" in en
    assert "le query di questo mese hanno fatturato 720 GB, e un altro giorno supererebbe il limite di 800 GB" in it
    assert "grezze: robot compresi, mirror noti esclusi" in it
    data = _update(tmp_path, ALL, FakeBigQuery(1234.5, ROWS))
    assert data["sources"]["bigquery"] == {"status": "paused", "gb": 1234.5, "limit": 100, "days": 1, "checked": "2026-10-27"}
    assert "one day of downloads billed 1,234.5 GB, more than 100 GB" in hook.render(data, "en")
    assert "un giorno di download ha fatturato 1.234,5 GB, oltre 100 GB" in hook.render(data, "it")


def test_hook_fills_the_placeholder(tmp_path, sample, monkeypatch):
    path = tmp_path / "copy.json"
    path.write_text(json.dumps(sample))
    monkeypatch.setenv("PPG_STATS_JSON", str(path))
    config = {"config_file_path": str(tmp_path / "mkdocs.yml")}
    page = hook.on_page_markdown("# Download e visite\n\n<!-- ppg-stats it -->\n\n## Note\n", page=None, config=config, files=None)
    assert page.startswith("# Download e visite\n\n<style>") and "## Riepilogo" in page and page.endswith("## Note\n")
    assert hook.on_page_markdown("# Other\n", page=None, config=config, files=None) == "# Other\n"
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    monkeypatch.setenv("PPG_STATS_JSON", str(broken))
    with pytest.raises((ValueError, hook.PluginError)):  # MkDocs' PluginError when MkDocs is installed
        hook.on_page_markdown("<!-- ppg-stats en -->", page=None, config=config, files=None)
    monkeypatch.setenv("PPG_STATS_JSON", str(tmp_path / "missing.json"))
    assert "every week" in hook.on_page_markdown("<!-- ppg-stats en -->", page=None, config=config, files=None)


def test_chart_months_and_scale():
    svg = hook.chart({"2026-11": [3, 1], "2027-02": [9, 4]}, [("b0", "raw"), ("b1", "real")], "Installs", "en")
    assert svg.count("<rect") == 8  # four months from November to February, gaps at zero
    assert ">2026<" in svg and ">2027<" in svg and ">Feb<" in svg
    assert ">0<" in svg and ">5<" in svg and ">10<" in svg and ">15<" not in svg  # steps of 5, the top just above 9
    assert "February 2027, raw: 9" in svg
    assert hook.chart({}, [("b1", "x")], "Empty", "en") == ""
