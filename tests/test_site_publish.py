"""Publishing a site folder on Cloudflare (``io/site_publish``, ``ppg web publish cloudflare``): the plan on synthetic
sites that fit the free plans and that exceed each limit, the privacy gate, the commands run on a fake wrangler
(logged in, not logged in, a failure, a file too large), no token kept. No network, no Cloudflare account."""
from __future__ import annotations

import json

import pytest

from pyarchinit_photogrammetry.io import site_publish as sp

import wrangler_fake as fake

MiB = 1024 * 1024


@pytest.fixture
def wr(tmp_path, monkeypatch):
    """The fake wrangler on the PATH, logged in: its folder."""
    home = tmp_path / "fake"
    home.mkdir()
    fake.environment(monkeypatch, home)
    return home


def _mode(monkeypatch, mode):
    monkeypatch.setenv("FAKE_WRANGLER_MODE", mode)


def _no_secret(text: str) -> None:
    assert fake.SECRET not in text
    assert fake.ACCOUNT not in text
    assert fake.EMAIL not in text


# --------------------------------------------------------------------------------------------------- the plan
def test_a_small_site_goes_to_pages_alone(tmp_path):
    folder = fake.make_site(tmp_path / "sito")
    (folder / ".DS_Store").write_bytes(b"x")
    p = sp.plan(folder, "scavo-prova")
    assert p.ok and p.data == "pages" and not p.r2_files
    names = {e.path for e in p.pages_files}
    assert {"index.html", "site.json", "app/main.js", "data/ortho.tif"} <= names
    assert ".DS_Store" not in names
    assert all(lim.ok for lim in p.limits)
    assert {lim.key for lim in p.limits} == {"pages_files", "pages_file_bytes"}
    text = "\n".join(p.lines())
    assert "scavo-prova" in text and "local frame" in text and "OVER" not in text


def test_a_file_above_25_mib_sends_the_data_to_r2(tmp_path):
    folder = fake.make_site(tmp_path / "sito", data_files={"ortho.tif": 26 * MiB, "dem.tif": 1000})
    p = sp.plan(folder, "scavo")
    assert p.data == "r2" and not p.ok
    assert any("name a bucket" in x for x in p.problems)
    p = sp.plan(folder, "scavo", bucket="scavo-dati")
    assert p.ok, p.problems
    assert {e.path for e in p.r2_files} == {"data/ortho.tif", "data/dem.tif"}
    assert all(not e.path.startswith("data/") for e in p.pages_files)
    assert sorted(p.upload) == ["data/dem.tif", "data/ortho.tif"]
    keys = {lim.key: lim for lim in p.limits}
    assert keys["r2_storage"].ok and keys["r2_class_a"].ok and keys["r2_put_bytes"].ok
    assert any("r2.dev" in w for w in p.warnings) and any("subscription" in w for w in p.warnings)
    assert any("bucket scavo-dati" in x for x in p.public)


def test_forcing_pages_with_a_large_file_is_refused(tmp_path):
    folder = fake.make_site(tmp_path / "sito", data_files={"ortho.tif": 26 * MiB})
    p = sp.plan(folder, "scavo", data="pages")
    assert not p.ok and any("25 MiB" in x and "data/ortho.tif" in x for x in p.problems)
    assert not {lim.key: lim for lim in p.limits}["pages_file_bytes"].ok


def test_too_many_files_for_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "PAGES_FREE_FILES", 6)
    folder = fake.make_site(tmp_path / "sito", data_files={f"tiles/{i}.glb": 10 for i in range(8)})
    p = sp.plan(folder, "scavo", data="pages")
    assert not p.ok and any("Free plan" in x for x in p.problems)
    p = sp.plan(folder, "scavo", bucket="scavo-dati")  # auto: data to R2, the page fits
    assert p.data == "r2" and p.ok and len(p.pages_files) <= 6


def test_a_file_above_what_wrangler_sends_to_r2(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "R2_PUT_BYTES", 5000)
    folder = fake.make_site(tmp_path / "sito", data_files={"cloud.copc.laz": 6000})
    p = sp.plan(folder, "scavo", bucket="scavo-dati", data="r2")
    assert not p.ok and any("rclone" in x for x in p.problems)
    assert not {lim.key: lim for lim in p.limits}["r2_put_bytes"].ok


def test_beyond_the_free_storage_of_r2_is_said(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "R2_FREE_STORAGE", 1000)
    folder = fake.make_site(tmp_path / "sito", data_files={"ortho.tif": 3000})
    p = sp.plan(folder, "scavo", bucket="scavo-dati", data="r2")
    assert p.ok  # billed, not refused: said
    assert not {lim.key: lim for lim in p.limits}["r2_storage"].ok
    assert any("billed" in w for w in p.warnings)


@pytest.mark.parametrize("project,bucket,url,word", [
    ("Scavo Prova", "ok-bucket", None, "Pages project"),
    ("-scavo", "ok-bucket", None, "Pages project"),
    ("scavo", "B", None, "bucket"),
    ("scavo", "ok-bucket", "http://dati.example.org", "https://"),
])
def test_names_and_addresses_are_checked(tmp_path, project, bucket, url, word):
    folder = fake.make_site(tmp_path / "sito")
    p = sp.plan(folder, project, bucket=bucket, data="r2", data_url=url)
    assert not p.ok and any(word in x for x in p.problems)


def test_a_folder_that_is_not_a_site(tmp_path):
    (tmp_path / "x").mkdir()
    p = sp.plan(tmp_path / "x", "scavo")
    assert not p.ok and "not a site" in p.problems[0]


def test_true_coordinates_are_said(tmp_path):
    p = sp.plan(fake.make_site(tmp_path / "sito", frame="crs"), "scavo")
    assert p.frame == "crs"
    assert any("TRUE coordinates" in w for w in p.warnings)
    assert any("true coordinates" in x for x in p.public)


def test_a_frame_not_written_counts_as_true_coordinates(tmp_path):
    folder = fake.make_site(tmp_path / "sito")
    data = json.loads((folder / "site.json").read_text())
    data.pop("frame")
    (folder / "site.json").write_text(json.dumps(data))
    assert sp.site_frame(folder) == "crs"


def test_default_project_name():
    assert sp.default_project("Scavo è Tell 2024_sito_web") == "scavo-e-tell-2024-sito-web"
    assert sp.default_project("___") == "rilievo"


# --------------------------------------------------------------------------------------------------- the gate
def test_no_publication_without_consent(tmp_path, wr):
    p = sp.plan(fake.make_site(tmp_path / "sito"), "scavo")
    with pytest.raises(sp.ConsentError):
        sp.publish(p)
    assert fake.calls(wr) == []  # wrangler never run
    sp.check_consent("local", True, False)


def test_true_coordinates_need_their_own_consent(tmp_path, wr):
    p = sp.plan(fake.make_site(tmp_path / "sito", frame="crs"), "scavo")
    with pytest.raises(sp.ConsentError, match="TRUE coordinates"):
        sp.publish(p, public=True)
    assert fake.calls(wr) == []
    sp.publish(p, public=True, coordinates=True)
    assert fake.cloud(wr)["projects"]["scavo"]["deployments"]


def test_a_plan_with_problems_is_not_carried_out(tmp_path, wr):
    p = sp.plan(fake.make_site(tmp_path / "sito", data_files={"ortho.tif": 26 * MiB}), "scavo", data="pages")
    with pytest.raises(ValueError):
        sp.publish(p, public=True)
    assert fake.calls(wr) == []


# --------------------------------------------------------------------------------------------------- wrangler
def test_wrangler_missing(tmp_path, monkeypatch):
    fake.no_wrangler(monkeypatch, tmp_path)
    assert sp.find_wrangler() is None
    w = sp.Wrangler()
    assert w.status().state == "missing"
    p = sp.plan(fake.make_site(tmp_path / "sito"), "scavo")
    with pytest.raises(sp.WranglerMissing, match="npm install -g wrangler"):
        sp.publish(p, public=True, wrangler=w)


def test_npx_is_used_without_installing(tmp_path, monkeypatch):
    fake.no_wrangler(monkeypatch, tmp_path)
    npx = tmp_path / "empty_bin" / "npx"
    npx.write_text("#!/bin/sh\nexit 1\n")
    npx.chmod(0o755)
    assert sp.find_wrangler() == [str(npx), "--no", "wrangler"]


def test_not_logged_in(tmp_path, wr, monkeypatch):
    _mode(monkeypatch, "logged_out")
    st = sp.Wrangler().status()
    assert st.state == "logged_out" and "wrangler login" in st.text
    p = sp.plan(fake.make_site(tmp_path / "sito"), "scavo")
    with pytest.raises(sp.NotLoggedIn):
        sp.publish(p, public=True)
    assert [c for c in fake.calls(wr) if c[:1] == ["pages"]] == []  # nothing sent


def test_an_old_wrangler_without_json(tmp_path, wr, monkeypatch):
    _mode(monkeypatch, "old")
    assert sp.Wrangler().status().state == "ready"
    _mode(monkeypatch, "old,logged_out")
    assert sp.Wrangler().status().state == "logged_out"


def test_publish_on_pages_alone(tmp_path, wr):
    folder = fake.make_site(tmp_path / "sito")
    before = {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    w = sp.Wrangler()
    result = sp.publish(sp.plan(folder, "scavo"), public=True, wrangler=w)
    assert result.address == "https://scavo.pages.dev" and result.data == "pages" and result.data_address is None
    calls = fake.calls(wr)
    assert calls[0] == ["--version"] and calls[1] == ["whoami", "--json"]
    assert ["pages", "project", "list", "--json"] in calls
    assert ["pages", "project", "create", "scavo", "--production-branch", "main"] in calls
    deploy = calls[-1]
    assert deploy[:2] == ["pages", "deploy"] and deploy[3:] == ["--project-name", "scavo", "--branch", "main",
                                                                  "--commit-dirty=true"]
    assert not any(c[0] == "r2" for c in calls)
    sent = fake.cloud(wr)["projects"]["scavo"]["deployments"][-1]
    assert "data/ortho.tif" in sent["files"] and "_headers" in sent["files"]
    assert not any(f.startswith(".") for f in sent["files"])
    assert sent["manifest"]["chunks"][0]["layers"][0]["url"] == "data/ortho.tif"  # Pages alone: as built
    # the folder as it was built (one state file added) and no staging left beside it
    after = {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    assert {k: v for k, v in after.items() if k != sp.STATE} == before
    assert not [p for p in folder.parent.iterdir() if p.name.startswith(".sito.publish")]
    _no_secret("\n".join(w.transcript))
    _no_secret(json.dumps(result.record()))


def test_a_name_taken_gives_another_address(tmp_path, wr, monkeypatch):
    _mode(monkeypatch, "taken")
    result = sp.publish(sp.plan(fake.make_site(tmp_path / "sito"), "scavo"), public=True)
    assert result.address == "https://scavo-7xq.pages.dev"


def test_publish_with_the_data_in_r2_and_again(tmp_path, wr):
    folder = fake.make_site(tmp_path / "sito", data_files={"ortho.tif": 26 * MiB, "units.geojson": 300,
                                                           "model/tileset.json": 200})
    manifest_before = (folder / "site.json").read_text()
    w = sp.Wrangler()
    p = sp.plan(folder, "scavo", bucket="scavo-dati")
    result = sp.publish(p, public=True, wrangler=w)
    assert result.data == "r2" and result.data_address == fake.R2_DEV and result.sent == 3
    cloud = fake.cloud(wr)
    bucket = cloud["buckets"]["scavo-dati"]
    assert bucket["dev_url"] is True
    assert bucket["objects"]["data/ortho.tif"]["type"] == "image/tiff"
    assert bucket["objects"]["data/units.geojson"]["type"].startswith("application/geo+json")
    assert bucket["objects"]["data/ortho.tif"]["cache"] == sp.CACHE_DATA
    rule = bucket["cors"]["rules"][0]
    assert rule["allowed"]["origins"] == ["https://scavo.pages.dev"]
    assert "Range" in rule["allowed"]["headers"] and set(rule["allowed"]["methods"]) == {"GET", "HEAD"}
    assert "Content-Range" in rule["exposeHeaders"]
    deployed = cloud["projects"]["scavo"]["deployments"][-1]
    assert not any(f.startswith("data/") for f in deployed["files"])
    layers = deployed["manifest"]["chunks"][0]["layers"]
    assert layers[0]["url"] == f"{fake.R2_DEV}/data/ortho.tif"
    assert deployed["manifest"]["chunks"][0]["report"] == f"{fake.R2_DEV}/data/report.html"
    assert deployed["manifest"]["data_base"] == fake.R2_DEV
    assert f"default-src 'self' {fake.R2_DEV}" in deployed["page"]
    assert f"img-src 'self' {fake.R2_DEV}" in deployed["page"] and "script-src 'self';" in deployed["page"]
    assert (folder / "site.json").read_text() == manifest_before  # the local folder as built
    calls = fake.calls(wr)
    assert ["r2", "bucket", "info", "scavo-dati"] in calls and ["r2", "bucket", "create", "scavo-dati"] in calls
    put = next(c for c in calls if c[:3] == ["r2", "object", "put"])
    assert put[3].startswith("scavo-dati/data/") and "--remote" in put and "--content-type" in put
    assert ["r2", "bucket", "dev-url", "enable", "scavo-dati", "-y"] in calls
    cors = next(c for c in calls if c[:4] == ["r2", "bucket", "cors", "set"])
    assert cors[5] == "--file" and cors[-1] == "-y"
    text = "\n".join(w.transcript)
    _no_secret(text)
    assert "[hidden]" in text and fake.R2_DEV in text

    # again: only what changed is sent, what is gone is deleted
    (folder / "data" / "units.geojson").write_bytes(b'{"type": "FeatureCollection", "features": []}')
    (folder / "data" / "model" / "tileset.json").unlink()
    p2 = sp.plan(folder, "scavo", bucket="scavo-dati")
    assert p2.upload == ["data/units.geojson"] and p2.remove == ["data/model/tileset.json"]
    assert "data/ortho.tif" in p2.unchanged
    n = len(fake.calls(wr))
    again = sp.publish(p2, public=True)
    new_calls = fake.calls(wr)[n:]
    assert again.sent == 1 and again.removed == 1
    assert [c[3] for c in new_calls if c[:3] == ["r2", "object", "put"]] == ["scavo-dati/data/units.geojson"]
    assert ["r2", "object", "delete", "scavo-dati/data/model/tileset.json", "--remote"] in new_calls
    assert ["r2", "bucket", "create", "scavo-dati"] not in new_calls
    assert ["pages", "project", "create", "scavo", "--production-branch", "main"] not in new_calls
    assert "data/model/tileset.json" not in fake.cloud(wr)["buckets"]["scavo-dati"]["objects"]


def test_a_custom_domain_for_the_data(tmp_path, wr):
    folder = fake.make_site(tmp_path / "sito")
    p = sp.plan(folder, "scavo", bucket="scavo-dati", data="r2", data_url="https://dati.example.org/")
    result = sp.publish(p, public=True, origins=["https://scavo.example.org/x"])
    assert result.data_address == "https://dati.example.org"
    calls = fake.calls(wr)
    assert not any(c[:3] == ["r2", "bucket", "dev-url"] for c in calls)  # r2.dev left as it is
    rule = fake.cloud(wr)["buckets"]["scavo-dati"]["cors"]["rules"][0]
    assert rule["allowed"]["origins"] == ["https://scavo.example.org", "https://scavo.pages.dev"]


def test_a_file_too_large_for_wrangler(tmp_path, wr, monkeypatch):
    _mode(monkeypatch, "too_large")
    folder = fake.make_site(tmp_path / "sito", data_files={"a.tif": 100, "b.tif": 5000})
    w = sp.Wrangler()
    with pytest.raises(sp.WranglerFailed, match="300 MiB"):
        sp.publish(sp.plan(folder, "scavo", bucket="scavo-dati", data="r2"), public=True, wrangler=w)
    assert not any(c[:2] == ["pages", "deploy"] for c in fake.calls(wr))  # the page waits for its data
    state = sp.read_state(folder)
    assert list(state["cloudflare"]["objects"]) == ["data/a.tif"]  # what went is remembered
    assert "published" not in state
    _no_secret("\n".join(w.transcript))


def test_a_failed_deploy_says_the_message_without_secrets(tmp_path, wr, monkeypatch):
    _mode(monkeypatch, "fail_deploy")
    w = sp.Wrangler()
    with pytest.raises(sp.WranglerFailed) as err:
        sp.publish(sp.plan(fake.make_site(tmp_path / "sito"), "scavo"), public=True, wrangler=w)
    assert "Authentication error" in str(err.value)
    _no_secret(str(err.value))
    _no_secret(err.value.output)
    _no_secret("\n".join(w.transcript))
    assert not [p for p in (tmp_path).iterdir() if p.name.startswith(".sito.publish")]


def test_cancel_stops_wrangler(tmp_path, wr):
    from pyarchinit_photogrammetry.core.progress import Cancelled, Progress

    prog = Progress()
    prog.cancel()
    with pytest.raises(Cancelled):
        sp.publish(sp.plan(fake.make_site(tmp_path / "sito"), "scavo"), public=True, progress=prog)


def test_scrub():
    text = (f"token={fake.SECRET} Authorization: Bearer abc.def account {fake.ACCOUNT} {fake.EMAIL} "
            f"api_key: 1234 {fake.R2_DEV}/data/x.tif https://a1b2.scavo.pages.dev")
    out = sp.scrub(text)
    _no_secret(out)
    assert "abc.def" not in out and "1234" not in out
    assert f"{fake.R2_DEV}/data/x.tif" in out and "https://a1b2.scavo.pages.dev" in out


# --------------------------------------------------------------------------------------------------- the project
def test_the_address_is_kept_with_the_project_and_survives_a_build(tmp_path, wr):
    from pyarchinit_photogrammetry.core.document import Document
    from pyarchinit_photogrammetry.io import site

    doc = Document()
    doc.meta[site.META_KEY] = {"folder": str(tmp_path / "sito"), "when": "2026-11-14T10:00:00"}
    result = sp.publish(sp.plan(fake.make_site(tmp_path / "sito"), "scavo"), public=True)
    rec = sp.record_published(doc, result)
    assert rec["address"] == "https://scavo.pages.dev" and rec["project"] == "scavo"
    assert set(rec) == {"host", "address", "project", "data", "data_address", "bucket", "when", "frame"}
    _no_secret(json.dumps(doc.meta))
    assert sp.published_record(doc)["address"] == rec["address"] and not sp.stale_publication(doc)
    site.record_site(doc, tmp_path / "sito" / "site.json", {"frame": "local"}, {})  # built again later
    assert doc.meta[site.META_KEY]["published"] == rec
    doc.meta[site.META_KEY]["when"] = "2999-01-01T00:00:00"
    assert sp.stale_publication(doc)


# --------------------------------------------------------------------------------------------------- the command
def test_cli_dry_run_and_gate(tmp_path, wr, monkeypatch):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    folder = fake.make_site(tmp_path / "sito", frame="crs")
    runner = CliRunner()
    r = runner.invoke(app, ["web", "publish", "cloudflare", str(folder), "--project", "scavo", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "dry run" in r.output and "Cloudflare Pages, project scavo" in r.output
    assert fake.calls(wr) == []  # nothing run
    r = runner.invoke(app, ["web", "publish", "cloudflare", str(folder), "--project", "scavo"])
    assert r.exit_code == 2 and "--yes-public" in r.output
    r = runner.invoke(app, ["web", "publish", "cloudflare", str(folder), "--project", "scavo", "--yes-public"])
    assert r.exit_code == 2 and "--yes-true-coordinates" in r.output
    assert fake.calls(wr) == []
    r = runner.invoke(app, ["web", "publish", "cloudflare", str(folder), "--project", "scavo", "--yes-public",
                            "--yes-true-coordinates"])
    assert r.exit_code == 0, r.output
    assert "published: https://scavo.pages.dev" in r.output
    _no_secret(r.output)


def test_cli_records_in_the_project(tmp_path, wr):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app
    from pyarchinit_photogrammetry.core.document import Document

    project = tmp_path / "p.ppg"
    Document().save(project)
    folder = fake.make_site(tmp_path / "sito")
    r = CliRunner().invoke(app, ["web", "publish", "cloudflare", str(folder), "--yes-public", "--record",
                                 str(project)])
    assert r.exit_code == 0, r.output
    assert Document.open(project).meta["web_site"]["published"]["address"] == "https://sito.pages.dev"


def test_cli_without_wrangler(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    fake.no_wrangler(monkeypatch, tmp_path)
    folder = fake.make_site(tmp_path / "sito")
    r = CliRunner().invoke(app, ["web", "publish", "cloudflare", str(folder), "--yes-public"])
    assert r.exit_code == 1 and "npm install -g wrangler" in r.output


# --------------------------------------------------------------------------------------------------- addresses
def test_a_suffixed_subdomain_read_from_the_list_when_the_answers_say_none(tmp_path, wr, monkeypatch):
    _mode(monkeypatch, "taken,quiet")
    w = sp.Wrangler()
    result = sp.publish(sp.plan(fake.make_site(tmp_path / "sito"), "scavo"), public=True, wrangler=w)
    assert result.address == "https://scavo-7xq.pages.dev"  # read, not the plain name
    lists = [c for c in fake.calls(wr) if c[:3] == ["pages", "project", "list"]]
    assert len(lists) == 2  # before creating, and after it to read the address
    assert result.unread == [] and result.unread_output == []


def test_a_suffixed_subdomain_with_r2_corrects_nothing_silently(tmp_path, wr, monkeypatch):
    _mode(monkeypatch, "taken,quiet")
    folder = fake.make_site(tmp_path / "sito")
    result = sp.publish(sp.plan(folder, "scavo", bucket="scavo-dati", data="r2"), public=True)
    rule = fake.cloud(wr)["buckets"]["scavo-dati"]["cors"]["rules"][0]
    assert rule["allowed"]["origins"] == ["https://scavo-7xq.pages.dev"]  # the list read before the bucket
    assert result.address == "https://scavo-7xq.pages.dev"


def test_unparseable_answers_record_no_address_and_keep_the_output(tmp_path, wr, monkeypatch):
    from pyarchinit_photogrammetry.core.document import Document

    _mode(monkeypatch, "taken,garbled,quiet")
    w = sp.Wrangler()
    folder = fake.make_site(tmp_path / "sito")
    result = sp.publish(sp.plan(folder, "scavo"), public=True, wrangler=w)
    assert result.address is None  # never the guessed scavo.pages.dev
    assert fake.cloud(wr)["projects"]["scavo"]["deployments"]  # published all the same
    assert any("pages project list" in u for u in result.unread)
    assert any("address of the site" in u for u in result.unread)
    whole = "\n".join(result.unread_output)
    assert "Projects:" in whole and "Deployment complete" in whole  # the answers whole
    _no_secret(whole)
    doc = Document()
    doc.meta["web_site"] = {"folder": str(folder), "when": "2026-11-14T10:00:00"}
    rec = sp.record_published(doc, result)
    assert rec["address"] is None and sp.published_record(doc) is rec
    with pytest.raises(ValueError):
        sp.set_address(doc, "scavo-7xq.pages.dev")
    sp.set_address(doc, "https://scavo-7xq.pages.dev/")
    assert sp.published_record(doc)["address"] == "https://scavo-7xq.pages.dev"


def test_cli_says_when_the_address_is_unknown(tmp_path, wr, monkeypatch):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    _mode(monkeypatch, "garbled,quiet")
    folder = fake.make_site(tmp_path / "sito")
    r = CliRunner().invoke(app, ["web", "publish", "cloudflare", str(folder), "--project", "scavo", "--yes-public"])
    assert r.exit_code == 0, r.output
    assert "published; the address is shown in the Cloudflare dashboard" in r.output
    assert "https://scavo.pages.dev" not in r.output and "Projects:" in r.output
    _no_secret(r.output)
