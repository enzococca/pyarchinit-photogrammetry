"""Removing a published site from Cloudflare (``site_publish.removal_plan``/``unpublish``, ``ppg web unpublish
cloudflare``) on the fake wrangler: the plan, the full removal, objects the program did not send never deleted, a
partial failure then the rest, cancel, no record, the bucket kept unless asked and allowed. No network, no account."""
from __future__ import annotations

import json
import threading
import time

import pytest

from pyarchinit_photogrammetry.io import site_publish as sp

import wrangler_fake as fake

MiB = 1024 * 1024
DATA = {"ortho.tif": 26 * MiB, "units.geojson": 300, "model/tileset.json": 200}


@pytest.fixture
def wr(tmp_path, monkeypatch):
    home = tmp_path / "fake"
    home.mkdir()
    fake.environment(monkeypatch, home)
    return home


def _no_secret(text: str) -> None:
    for secret in (fake.SECRET, fake.ACCOUNT, fake.EMAIL):
        assert secret not in text


def _published(tmp_path, data_files=None, bucket="scavo-dati", data_url=None, **kw):
    folder = fake.make_site(tmp_path / "sito", data_files=DATA if data_files is None else data_files)
    p = sp.plan(folder, "scavo", bucket=bucket, data_url=data_url, **kw)
    sp.publish(p, public=True)
    return folder


def test_no_record_no_removal(tmp_path, wr):
    folder = fake.make_site(tmp_path / "sito")
    r = sp.removal_plan(folder)
    assert not r.ok and "only what it" in r.problems[0]
    with pytest.raises(sp.NotPublished):
        sp.unpublish(folder)
    assert fake.calls(wr) == []  # nothing run
    assert not sp.publication_recorded(folder)


def test_the_plan_of_a_site_with_r2(tmp_path, wr):
    folder = _published(tmp_path)
    state = sp.read_state(folder)["cloudflare"]
    assert state["bucket_created"] is True and state["dev_url_enabled"] is True and state["records"] == sp.RECORDS
    n = len(fake.calls(wr))
    r = sp.removal_plan(folder)
    assert len(fake.calls(wr)) == n  # the plan runs nothing
    assert r.ok and r.project == "scavo" and r.bucket == "scavo-dati" and r.confirm_name == "scavo"
    assert r.objects == ["data/model/tileset.json", "data/ortho.tif", "data/units.geojson"]
    assert r.dev_url == "disable" and r.bucket_allowed and not r.delete_bucket
    text = "\n".join(r.lines())
    assert "project scavo is deleted" in text and "3 objects" in text and "bucket scavo-dati is kept" in text
    assert "cannot be undone" in text
    assert sp.removal_plan(folder, delete_bucket=True).delete_bucket
    assert sp.publication_recorded(folder)


def test_full_removal(tmp_path, wr):
    folder = _published(tmp_path)
    before = sorted(p.name for p in folder.rglob("*") if p.name != sp.STATE)
    w = sp.Wrangler()
    out = sp.unpublish(folder, delete_bucket=True, wrangler=w)
    assert out.complete and out.project_deleted and out.bucket_deleted and out.dev_url_disabled
    assert out.objects_deleted == 3 and not out.objects_left and not out.failures
    cloud = fake.cloud(wr)
    assert "scavo" not in cloud["projects"] and "scavo-dati" not in cloud["buckets"]
    calls = fake.calls(wr)
    assert ["pages", "project", "delete", "scavo", sp.PAGES_DELETE_YES] in calls
    assert ["r2", "bucket", "dev-url", "disable", "scavo-dati", sp.DEV_URL_DISABLE_YES] in calls
    assert ["r2", "object", "delete", "scavo-dati/data/ortho.tif", "--remote"] in calls
    order = [c[:3] for c in calls if c[:2] in (["r2", "object"], ["r2", "bucket"], ["pages", "project"])]
    assert order.index(["r2", "bucket", "delete"]) > order.index(["r2", "object", "delete"])
    assert order[-1] == ["pages", "project", "delete"]
    assert not (folder / sp.STATE).exists()  # removed at the end
    assert sorted(p.name for p in folder.rglob("*")) == before  # the local folder kept whole
    _no_secret("\n".join(w.transcript + out.lines()))
    with pytest.raises(sp.NotPublished):  # nothing left to remove
        sp.unpublish(folder)


def test_a_pages_only_site(tmp_path, wr):
    folder = fake.make_site(tmp_path / "sito")
    sp.publish(sp.plan(folder, "scavo"), public=True)
    r = sp.removal_plan(folder, delete_bucket=True)
    assert r.ok and r.bucket is None and not r.objects and not r.delete_bucket
    out = sp.unpublish(folder, delete_bucket=True)
    assert out.complete and out.project_deleted
    assert not any(c[:1] == ["r2"] for c in fake.calls(wr))
    assert "scavo" not in fake.cloud(wr)["projects"]


def test_objects_not_sent_by_the_program_are_never_deleted(tmp_path, wr):
    folder = _published(tmp_path)
    cloud = fake.cloud(wr)
    cloud["buckets"]["scavo-dati"]["objects"]["altro/foto.jpg"] = {"sha": "x"}
    (wr / "cloud.json").write_text(json.dumps(cloud))
    out = sp.unpublish(folder, delete_bucket=True)
    deleted = [c[3] for c in fake.calls(wr) if c[:3] == ["r2", "object", "delete"]]
    assert "scavo-dati/altro/foto.jpg" not in deleted and len(deleted) == 3
    assert fake.cloud(wr)["buckets"]["scavo-dati"]["objects"] == {"altro/foto.jpg": {"sha": "x"}}
    # the bucket holds an object of somebody else: not deleted, said; the rest went
    assert not out.complete and not out.bucket_deleted and out.project_deleted
    assert any("not empty" in f for f in out.failures)
    # again without deleting the bucket: done, the bucket and its object kept
    out = sp.unpublish(folder)
    assert out.complete and not (folder / sp.STATE).exists()
    assert "scavo-dati" in fake.cloud(wr)["buckets"]


def test_partial_failure_then_the_rest(tmp_path, wr, monkeypatch):
    folder = _published(tmp_path)
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "fail_object_delete,fail_project_delete")
    monkeypatch.setenv("FAKE_WRANGLER_FAIL_KEY", "units")
    w = sp.Wrangler()
    out = sp.unpublish(folder, delete_bucket=True, wrangler=w)
    assert not out.complete and out.objects_deleted == 2 and out.objects_left == ["data/units.geojson"]
    assert not out.project_deleted and not out.bucket_deleted  # the bucket waits for its objects
    assert out.dev_url_disabled
    assert any("units.geojson" in f and "Internal error" in f for f in out.failures)
    _no_secret("\n".join(out.failures + w.transcript))
    r = sp.removal_plan(folder, delete_bucket=True)  # what is left
    assert r.objects == ["data/units.geojson"] and r.dev_url == "done" and not r.project_done
    assert "scavo" in fake.cloud(wr)["projects"]

    monkeypatch.setenv("FAKE_WRANGLER_MODE", "")
    n = len(fake.calls(wr))
    out = sp.unpublish(folder, delete_bucket=True)
    new = fake.calls(wr)[n:]
    assert out.complete and out.project_deleted and out.bucket_deleted
    assert [c[3] for c in new if c[:3] == ["r2", "object", "delete"]] == ["scavo-dati/data/units.geojson"]
    assert not any(c[:4] == ["r2", "bucket", "dev-url", "disable"] for c in new)  # done before
    cloud = fake.cloud(wr)
    assert "scavo" not in cloud["projects"] and "scavo-dati" not in cloud["buckets"]


def test_a_project_gone_already_counts_as_removed(tmp_path, wr):
    folder = fake.make_site(tmp_path / "sito")
    sp.publish(sp.plan(folder, "scavo"), public=True)
    cloud = fake.cloud(wr)
    del cloud["projects"]["scavo"]  # deleted in the dashboard meanwhile
    (wr / "cloud.json").write_text(json.dumps(cloud))
    out = sp.unpublish(folder)
    assert out.complete and out.project_deleted and not out.failures


def test_cancel_then_go_on(tmp_path, wr, monkeypatch):
    from pyarchinit_photogrammetry.core.progress import Cancelled, Progress

    folder = _published(tmp_path)
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "slow_delete")
    prog = Progress()

    def cancel():
        limit = time.monotonic() + 30
        while not (wr / "deleting").exists() and time.monotonic() < limit:
            time.sleep(0.05)
        prog.cancel()

    t = threading.Thread(target=cancel)
    t.start()
    t0 = time.monotonic()
    with pytest.raises(Cancelled):
        sp.unpublish(folder, progress=prog)
    t.join()
    assert time.monotonic() - t0 < 30
    assert sp.publication_recorded(folder) and len(sp.removal_plan(folder).objects) == 3
    assert "scavo" in fake.cloud(wr)["projects"]
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "")
    out = sp.unpublish(folder)
    assert out.complete


def test_the_bucket_is_kept_unless_created_by_the_program(tmp_path, wr):
    folder = fake.make_site(tmp_path / "sito", data_files=DATA)
    sp.Wrangler().run(["r2", "bucket", "create", "scavo-dati"], check=True)  # the user's own bucket
    sp.publish(sp.plan(folder, "scavo", bucket="scavo-dati"), public=True)
    state = sp.read_state(folder)["cloudflare"]
    assert "bucket_created" not in state and state["dev_url_enabled"] is True  # off before: turned on by it
    r = sp.removal_plan(folder, delete_bucket=True)
    assert r.bucket_rule == "not_created" and not r.delete_bucket and r.dev_url == "disable"
    out = sp.unpublish(folder, delete_bucket=True)
    assert out.complete and not out.bucket_deleted
    assert any("did not create" in n for n in out.notes)
    assert not any(c[:3] == ["r2", "bucket", "delete"] for c in fake.calls(wr))
    assert "scavo-dati" in fake.cloud(wr)["buckets"]


def test_an_address_on_before_the_program_is_left_on(tmp_path, wr):
    folder = fake.make_site(tmp_path / "sito", data_files=DATA)
    w = sp.Wrangler()
    w.run(["r2", "bucket", "create", "scavo-dati"], check=True)
    w.run(["r2", "bucket", "dev-url", "enable", "scavo-dati", "-y"], check=True)
    sp.publish(sp.plan(folder, "scavo", bucket="scavo-dati"), public=True)
    assert "dev_url_enabled" not in sp.read_state(folder)["cloudflare"]
    assert sp.removal_plan(folder).dev_url == "not_ours"
    sp.unpublish(folder)
    assert not any(c[:4] == ["r2", "bucket", "dev-url", "disable"] for c in fake.calls(wr))
    assert fake.cloud(wr)["buckets"]["scavo-dati"]["dev_url"] is True


def test_a_custom_data_domain_is_never_touched(tmp_path, wr):
    folder = _published(tmp_path, data_url="https://dati.example.org")
    r = sp.removal_plan(folder, delete_bucket=True)
    assert r.dev_url == "custom" and "dati.example.org" in "\n".join(r.lines())
    out = sp.unpublish(folder, delete_bucket=True)
    assert out.complete and out.bucket_deleted
    assert not any(c[:3] == ["r2", "bucket", "dev-url"] for c in fake.calls(wr))
    assert any("dati.example.org" in n for n in out.notes)


def test_a_record_before_1_10_2_leaves_the_address_and_the_bucket(tmp_path, wr):
    folder = _published(tmp_path)
    state = sp.read_state(folder)
    for key in ("records", "bucket_created", "dev_url_enabled"):  # as 1.10.0-1.10.1 wrote it
        state["cloudflare"].pop(key)
    (folder / sp.STATE).write_text(json.dumps(state))
    r = sp.removal_plan(folder, delete_bucket=True)
    assert r.dev_url == "unknown" and r.bucket_rule == "unknown" and not r.delete_bucket
    out = sp.unpublish(folder, delete_bucket=True)
    assert out.complete and not out.dev_url_disabled and not out.bucket_deleted
    assert [c for c, _v in out.codes] == ["dev_url_unknown", "bucket_unknown"]
    calls = fake.calls(wr)
    assert not any(c[:4] == ["r2", "bucket", "dev-url", "disable"] for c in calls)
    assert not any(c[:3] == ["r2", "bucket", "delete"] for c in calls)
    assert fake.cloud(wr)["buckets"]["scavo-dati"]["objects"] == {}


def test_not_logged_in(tmp_path, wr, monkeypatch):
    folder = fake.make_site(tmp_path / "sito")
    sp.publish(sp.plan(folder, "scavo"), public=True)
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "logged_out")
    with pytest.raises(sp.NotLoggedIn):
        sp.unpublish(folder)
    assert "scavo" in fake.cloud(wr)["projects"] and sp.publication_recorded(folder)


# --------------------------------------------------------------------------------------------------- the command
def test_cli_plan_confirmation_and_removal(tmp_path, wr):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    folder = _published(tmp_path)
    runner = CliRunner()
    n = len(fake.calls(wr))
    r = runner.invoke(app, ["web", "unpublish", "cloudflare", str(folder)], input="sbagliato\n")
    assert r.exit_code == 2, r.output
    assert "project scavo is deleted" in r.output and "nothing was removed" in r.output
    assert len(fake.calls(wr)) == n
    r = runner.invoke(app, ["web", "unpublish", "cloudflare", str(folder), "--delete-bucket"], input="scavo\n")
    assert r.exit_code == 0, r.output
    assert "removed from Cloudflare" in r.output
    _no_secret(r.output)
    cloud = fake.cloud(wr)
    assert "scavo" not in cloud["projects"] and "scavo-dati" not in cloud["buckets"]
    r = runner.invoke(app, ["web", "unpublish", "cloudflare", str(folder), "--yes"])
    assert r.exit_code == 1 and "only what it" in r.output


def test_cli_yes_and_a_partial_failure(tmp_path, wr, monkeypatch):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    folder = fake.make_site(tmp_path / "sito")
    sp.publish(sp.plan(folder, "scavo"), public=True)
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "fail_project_delete")
    r = CliRunner().invoke(app, ["web", "unpublish", "cloudflare", str(folder), "--yes"])
    assert r.exit_code == 1 and "run it again" in r.output
    _no_secret(r.output)
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "")
    r = CliRunner().invoke(app, ["web", "unpublish", "cloudflare", str(folder), "--yes"])
    assert r.exit_code == 0, r.output
