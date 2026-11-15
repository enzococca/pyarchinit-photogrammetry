"""Removing a published site from Cloudflare (``site_publish.removal_plan``/``unpublish``, ``ppg web unpublish
cloudflare``) on the fake wrangler: the plan, the full removal, objects the program did not send never deleted, a
partial failure then the rest, cancel, no record, the bucket kept unless asked and allowed, what wrangler does not
find. No network, no account."""
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
    n = len(fake.calls(wr))
    out = sp.unpublish(folder, delete_bucket=True, wrangler=w)
    assert out.complete and out.project_deleted and out.bucket_deleted and out.dev_url_disabled
    assert out.objects_deleted == 3 and not out.objects_left and not out.failures
    assert not out.not_found and not out.notes
    cloud = fake.cloud(wr)
    assert "scavo" not in cloud["projects"] and "scavo-dati" not in cloud["buckets"]
    new = [c for c in fake.calls(wr)[n:] if c[:1] in (["pages"], ["r2"])]
    # the page first (its address stops answering), then the public address of the data, the objects, the bucket
    assert new[0] == ["pages", "project", "delete", "scavo", sp.PAGES_DELETE_YES]
    assert new[1] == ["r2", "bucket", "dev-url", "disable", "scavo-dati", sp.DEV_URL_DISABLE_YES]
    assert sorted(new[2:5]) == [["r2", "object", "delete", f"scavo-dati/data/{name}", "--remote"]
                                for name in ("model/tileset.json", "ortho.tif", "units.geojson")]
    assert new[5:] == [["r2", "bucket", "delete", "scavo-dati"]]
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
    # wrangler looks only in the account it is logged in to: said, with the address to look at
    assert out.not_found == ["project"] and [c for c, _v in out.codes] == ["project_not_found"]
    assert "https://scavo.pages.dev" in out.notes[0] and "another account" in out.notes[0]
    assert any("not found in this Cloudflare account" in line for line in out.lines())
    assert not (folder / sp.STATE).exists()


def test_a_bucket_not_found_counts_as_removed_and_is_said(tmp_path, wr):
    folder = _published(tmp_path)
    (wr / "cloud.json").write_text(json.dumps({"projects": {}, "buckets": {}}))  # as in another account
    n = len(fake.calls(wr))
    out = sp.unpublish(folder, delete_bucket=True)
    new = [c[:4] for c in fake.calls(wr)[n:] if c[:1] in (["pages"], ["r2"])]
    # nothing is asked of a bucket that is not there: no object, no deletion of the bucket
    assert new == [["pages", "project", "delete", "scavo"], ["r2", "bucket", "dev-url", "disable"]]
    assert out.complete and not out.failures and out.not_found == ["project", "bucket"]
    assert out.objects_deleted == 0 and not out.objects_left and not out.bucket_deleted and not out.dev_url_disabled
    assert [c for c, _v in out.codes] == ["project_not_found", "bucket_not_found"]
    assert fake.R2_DEV in out.notes[1] and "another account" in out.notes[1]
    text = "\n".join(out.lines())
    assert text.count("not found in this Cloudflare account") == 2 and "bucket deleted" not in text
    assert not (folder / sp.STATE).exists()


def test_the_objects_of_a_bucket_not_found_are_not_asked_one_by_one(tmp_path, wr):
    folder = _published(tmp_path, data_url="https://dati.example.org")  # no r2.dev address to turn off first
    cloud = fake.cloud(wr)
    del cloud["buckets"]["scavo-dati"]  # deleted in the dashboard meanwhile
    (wr / "cloud.json").write_text(json.dumps(cloud))
    n = len(fake.calls(wr))
    out = sp.unpublish(folder)
    assert len([c for c in fake.calls(wr)[n:] if c[:3] == ["r2", "object", "delete"]]) == 1
    assert out.complete and out.project_deleted and out.not_found == ["bucket"] and not out.objects_left
    assert "scavo" not in fake.cloud(wr)["projects"]


def test_a_bucket_not_found_by_an_earlier_run_is_not_asked_again(tmp_path, wr, monkeypatch):
    folder = _published(tmp_path)
    cloud = fake.cloud(wr)
    del cloud["buckets"]["scavo-dati"]
    (wr / "cloud.json").write_text(json.dumps(cloud))
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "fail_project_delete")
    out = sp.unpublish(folder, delete_bucket=True)
    assert not out.complete and not out.project_deleted and out.not_found == ["bucket"] and not out.objects_left
    r = sp.removal_plan(folder, delete_bucket=True)
    assert r.ok and r.bucket_missing and not r.objects and not r.delete_bucket and not r.project_done
    assert r.as_dict()["bucket_missing"] is True
    assert any("not found in the Cloudflare account by an earlier run" in line for line in r.lines())
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "")
    n = len(fake.calls(wr))
    out = sp.unpublish(folder, delete_bucket=True)
    assert [c[:3] for c in fake.calls(wr)[n:] if c[:1] in (["pages"], ["r2"])] == [["pages", "project", "delete"]]
    assert out.complete and out.project_deleted and out.not_found == ["bucket"]
    assert not (folder / sp.STATE).exists()


def test_another_failure_is_not_taken_for_a_thing_not_there(tmp_path, wr, monkeypatch):
    for text in ("✘ [ERROR] A request to the Cloudflare API failed. Project not found. [code: 8000007]",
                 "Project not found. The specified project name does not match any of your existing projects.",
                 "The specified bucket does not exist. [code: 10006]", "The specified key does not exist.",
                 "NoSuchBucket [code: 10006]", "no such bucket: scavo-dati"):
        assert sp._GONE.search(text), text
    for text in ("✘ [ERROR] A file or directory could not be found.\n  Missing file or directory: /.wrangler/cache\n"
                 "  The error was: ENOENT: no such file or directory, mkdir '/.wrangler/cache'",
                 "npm error 404 Not Found - GET https://registry.npmjs.org/wrangler - Not found",
                 "zsh: command not found: wrangler", "Could not find wrangler.toml", "Account not found",
                 "Authentication error [code: 10000]", "fetch failed"):
        assert not sp._GONE.search(text), text
    assert sp._NO_BUCKET.search("The specified bucket does not exist. [code: 10006]")
    assert not sp._NO_BUCKET.search("The specified key does not exist. [code: 10007]")
    folder = fake.make_site(tmp_path / "sito")
    sp.publish(sp.plan(folder, "scavo"), public=True)
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "cache_error")  # "A file or directory could not be found"
    out = sp.unpublish(folder)
    assert not out.complete and not out.project_deleted and not out.not_found
    assert any("could not be found" in f for f in out.failures)
    assert "scavo" in fake.cloud(wr)["projects"] and sp.publication_recorded(folder)


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
    left = sp.removal_plan(folder)
    assert sp.publication_recorded(folder) and len(left.objects) == 3
    # the page went first: cancelled while the data are removed, the site answers no more
    assert "scavo" not in fake.cloud(wr)["projects"] and left.project_done and left.dev_url == "done"
    assert fake.cloud(wr)["buckets"]["scavo-dati"]["dev_url"] is False
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "")
    n = len(fake.calls(wr))
    out = sp.unpublish(folder)
    assert out.complete and out.project_deleted and out.objects_deleted == 3 and not out.not_found
    assert not any(c[:3] == ["pages", "project", "delete"] for c in fake.calls(wr)[n:])  # done before


def test_a_new_publication_after_a_removal_left_halfway(tmp_path, wr, monkeypatch):
    folder = _published(tmp_path)
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "fail_object_delete")
    monkeypatch.setenv("FAKE_WRANGLER_FAIL_KEY", "units")
    out = sp.unpublish(folder)
    assert not out.complete and out.project_deleted and out.dev_url_disabled
    assert out.objects_left == ["data/units.geojson"]
    assert sp.read_state(folder)["removing"]["project_deleted"] is True
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "")
    result = sp.publish(sp.plan(folder, "scavo", bucket="scavo-dati"), public=True)  # published again
    assert result.sent == 2 and result.unchanged == 1
    assert "removing" not in sp.read_state(folder)  # what the removal did is to do again
    cloud = fake.cloud(wr)
    assert "scavo" in cloud["projects"] and cloud["buckets"]["scavo-dati"]["dev_url"] is True
    r = sp.removal_plan(folder)
    assert not r.project_done and r.dev_url == "disable" and len(r.objects) == 3
    out = sp.unpublish(folder)
    assert out.complete and out.project_deleted and out.objects_deleted == 3 and not out.not_found
    cloud = fake.cloud(wr)
    assert "scavo" not in cloud["projects"]
    assert cloud["buckets"]["scavo-dati"] == {"objects": {}, "cors": cloud["buckets"]["scavo-dati"]["cors"],
                                              "dev_url": False}


def test_a_publication_that_cannot_start_keeps_the_removal_left_halfway(tmp_path, wr, monkeypatch):
    folder = fake.make_site(tmp_path / "sito")
    sp.publish(sp.plan(folder, "scavo"), public=True)
    state = sp.read_state(folder)
    state["removing"] = {"started": "2026-11-15T10:00:00", "project_deleted": True}
    (folder / sp.STATE).write_text(json.dumps(state))
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "cache_error")  # the list of the projects fails: nothing was changed
    with pytest.raises(sp.WranglerFailed):
        sp.publish(sp.plan(folder, "scavo"), public=True)
    assert sp.read_state(folder)["removing"]["project_deleted"] is True


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


def test_a_record_before_1_10_2_stays_unknown_when_published_again(tmp_path, wr):
    folder = _published(tmp_path)
    state = sp.read_state(folder)
    for key in ("records", "bucket_created", "dev_url_enabled"):  # as 1.10.0-1.10.1 wrote it
        state["cloudflare"].pop(key)
    (folder / sp.STATE).write_text(json.dumps(state))
    (folder / "data" / "units.geojson").write_bytes(b"{}")  # something changed
    sp.publish(sp.plan(folder, "scavo", bucket="scavo-dati"), public=True)  # with 1.10.2, the same bucket
    cf = sp.read_state(folder)["cloudflare"]
    # who created the bucket and turned its address on is not known, and publishing again does not make it known
    assert "records" not in cf and "bucket_created" not in cf and "dev_url_enabled" not in cf
    r = sp.removal_plan(folder, delete_bucket=True)
    assert r.dev_url == "unknown" and r.bucket_rule == "unknown" and not r.delete_bucket


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
