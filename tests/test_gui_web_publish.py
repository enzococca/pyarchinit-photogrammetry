"""Step 4 of the pane *Pubblica sul web*: the publication on Cloudflare from the window, on a fake wrangler (no
network, no account) and a synthetic site folder - its states (wrangler missing, not logged in, ready, publishing
and cancelled, published, failed), the plan shown before the consent, the consent (a second one for true
coordinates), the address kept with the project and shown in the tree."""
from __future__ import annotations

import json
import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

import wrangler_fake as fake  # noqa: E402

MiB = 1024 * 1024


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "fake"
    h.mkdir()
    return h


def _window(qapp, tmp_path, monkeypatch, frame="local", data_files=None):
    """A window on a new project whose last site is a synthetic folder beside it."""
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import web_pane
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    base = tmp_path / "progetto"
    base.mkdir()
    fake.make_site(base / "scavo_sito_web", frame=frame, data_files=data_files)
    doc = ppg.Document()
    doc.meta["web_site"] = {"folder": "scavo_sito_web", "options": {}, "when": "2026-11-14T09:00:00", "size": 1,
                            "frame": frame, "sources": {}}
    doc.save(base / "scavo.ppg")
    opened: list = []
    monkeypatch.setattr(web_pane.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    asked: list = []
    answers: list = []

    def message(parent, kind, title, text, details="", buttons=None, default=None):
        from PySide6.QtWidgets import QMessageBox

        asked.append((kind, text))
        return answers.pop(0) if answers else QMessageBox.StandardButton.No

    monkeypatch.setattr(web_pane, "message", message)
    w = MainWindow(ppg.Document.open(base / "scavo.ppg"), autoconnect=False)
    said: list = []
    monkeypatch.setattr(w, "_warn", lambda text: said.append(("warn", text)))
    monkeypatch.setattr(w, "_error", lambda summary, details="", logged=True: said.append(("error", summary,
                                                                                            details)))
    w.said, w.opened, w.asked, w.answers = said, opened, asked, answers
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    w.show_web_pane()
    for _ in range(3):
        qapp.processEvents()
    return w


@pytest.fixture
def make_window(qapp, tmp_path, monkeypatch):
    made = []

    def make(**kw):
        w = _window(qapp, tmp_path, monkeypatch, **kw)
        made.append(w)
        return w

    yield make
    for w in made:
        w.web.close()
        w.setWindowModified(False)
        w.close()


def _yes():
    from PySide6.QtWidgets import QMessageBox

    return QMessageBox.StandardButton.Yes


def _settle(w, qapp):
    w.runner.wait()
    for _ in range(5):
        qapp.processEvents()


def _check(w, qapp):
    pane = w.web_pane
    assert pane.b_cf_check.isEnabled(), pane.states["publish"].text()
    pane.b_cf_check.click()
    _settle(w, qapp)
    return pane


def _site_item(w):
    items = [it for it in w.workspace._walk() if (it.data(0, 0x0100) or (None,))[0] == "web_site"]
    return items[0]


def test_before_the_check(make_window, home, monkeypatch):
    fake.environment(monkeypatch, home)
    w = make_window()
    pane = w.web_pane
    assert pane.step_states["publish"] == "todo"
    assert "Controlla" in pane.states["publish"].text()
    assert not pane.b_publish.isEnabled() and not pane.b_cf_cancel.isEnabled()
    assert not pane.b_site_open.isEnabled() and not pane.b_site_copy.isEnabled()
    assert pane.cf_project.text() == "scavo"
    assert not pane.cf_warning.isVisible()
    assert fake.calls(home) == []  # opening the pane runs nothing


def test_wrangler_missing(make_window, tmp_path, monkeypatch, qapp):
    fake.no_wrangler(monkeypatch, tmp_path)
    w = make_window()
    pane = _check(w, qapp)
    assert pane.step_states["publish"] == "unavailable"
    assert "npm install -g wrangler" in pane.states["publish"].text()
    assert not pane.b_publish.isEnabled()


def test_not_logged_in(make_window, home, monkeypatch, qapp):
    fake.environment(monkeypatch, home, "logged_out")
    w = make_window()
    pane = _check(w, qapp)
    assert pane.step_states["publish"] == "unavailable"
    assert "wrangler login" in pane.states["publish"].text() and "npx" not in pane.states["publish"].text()
    assert not pane.b_publish.isEnabled()
    assert pane.b_cf_login.isVisible() and pane.b_cf_login.isEnabled()
    assert fake.calls(home) == [["--version"], ["whoami", "--json"]]


def test_plan_consent_publish_and_the_address(make_window, home, monkeypatch, qapp):
    from PySide6.QtGui import QGuiApplication

    fake.environment(monkeypatch, home)
    w = make_window()
    pane = _check(w, qapp)
    assert pane.cf_plan.isVisible()
    plan = pane.cf_plan.text()
    assert "<b>scavo</b>" in plan and "entro" in plan and "sistema locale" in plan
    assert pane.b_publish.isEnabled()
    assert [c for c in fake.calls(home) if c[:1] == ["pages"]] == []  # the check sends nothing

    pane.b_publish.click()  # declined: nothing sent
    assert len(w.asked) == 1 and "PUBBLICO" in w.asked[0][1]
    _settle(w, qapp)
    assert [c for c in fake.calls(home) if c[:1] == ["pages"]] == []
    assert pane.step_states["publish"] == "todo"

    w.answers.append(_yes())
    pane.b_publish.click()
    _settle(w, qapp)
    assert not [s for s in w.said if s[0] == "error"], w.said
    assert pane.step_states["publish"] == "done", pane.states["publish"].text()
    assert "https://scavo.pages.dev" in pane.states["publish"].text()
    assert pane.published_address.text() == "https://scavo.pages.dev"
    pub = w.doc.meta["web_site"]["published"]
    assert pub["address"] == "https://scavo.pages.dev" and pub["project"] == "scavo"
    import pyarchinit_photogrammetry as ppg

    saved = ppg.Document.open(w.doc.path)  # saved with the project, no secret in it
    assert saved.meta["web_site"]["published"]["address"] == "https://scavo.pages.dev"
    text = w.doc.path.read_text(encoding="utf-8")
    for secret in (fake.SECRET, fake.ACCOUNT, fake.EMAIL):
        assert secret not in text
    assert "scavo.pages.dev" in _site_item(w).text(0)
    pane.b_site_copy.click()
    assert QGuiApplication.clipboard().text() == "https://scavo.pages.dev"
    pane.b_site_open.click()
    assert w.opened[-1].rstrip("/") == "https://scavo.pages.dev"
    deploys = fake.cloud(home)["projects"]["scavo"]["deployments"]
    assert len(deploys) == 1 and "data/ortho.tif" in deploys[0]["files"]

    # the site built again after the publication: da rifare
    w.doc.meta["web_site"]["when"] = "2999-01-01T00:00:00"
    w.web_pane.update_states()
    assert pane.step_states["publish"] == "stale"


def test_large_data_need_a_bucket_and_go_to_r2(make_window, home, monkeypatch, qapp):
    fake.environment(monkeypatch, home)
    w = make_window(data_files={"ortho.tif": 26 * MiB, "units.geojson": 300})
    pane = _check(w, qapp)
    assert "Non si può pubblicare" in pane.cf_plan.text() and "bucket" in pane.cf_plan.text()
    assert not pane.b_publish.isEnabled()
    pane.cf_bucket.setText("scavo-dati")
    pane.cf_bucket.textEdited.emit("scavo-dati")
    assert not pane.cf_plan.isVisible() and not pane.b_publish.isEnabled()  # check again after a change
    pane = _check(w, qapp)
    assert "scavo-dati" in pane.cf_plan.text() and "r2.dev" in pane.cf_plan.text()
    w.answers.append(_yes())
    pane.b_publish.click()
    _settle(w, qapp)
    assert pane.step_states["publish"] == "done", pane.states["publish"].text()
    assert fake.R2_DEV in pane.states["publish"].text()
    assert "e nel bucket R2 scavo-dati" in w.asked[-1][1]
    assert w.doc.meta["web_site"]["published"]["bucket"] == "scavo-dati"


def test_true_coordinates_need_a_second_consent(make_window, home, monkeypatch, qapp):
    fake.environment(monkeypatch, home)
    w = make_window(frame="crs")
    pane = w.web_pane
    assert pane.cf_warning.isVisible() and "VERE" in pane.cf_warning.text()
    pane = _check(w, qapp)
    assert "VERE" in pane.cf_plan.text()
    w.answers.append(_yes())  # public yes, coordinates no
    pane.b_publish.click()
    _settle(w, qapp)
    assert len(w.asked) == 2 and w.asked[1][0] == "warning" and "VERE" in w.asked[1][1]
    assert not any(c[:2] == ["pages", "deploy"] for c in fake.calls(home))
    assert "published" not in w.doc.meta["web_site"]
    w.answers.extend([_yes(), _yes()])
    pane.b_publish.click()
    _settle(w, qapp)
    assert pane.step_states["publish"] == "done"
    assert w.doc.meta["web_site"]["published"]["frame"] == "crs"


def test_a_failure_shows_the_message_of_wrangler_without_secrets(make_window, home, monkeypatch, qapp):
    fake.environment(monkeypatch, home)
    w = make_window()
    pane = _check(w, qapp)
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "fail_deploy")
    w.answers.append(_yes())
    pane.b_publish.click()
    _settle(w, qapp)
    assert pane.step_states["publish"] == "failed"
    text = pane.states["publish"].text()
    assert "Authentication error" in text
    errors = [s for s in w.said if s[0] == "error"]
    assert errors
    for secret in (fake.SECRET, fake.ACCOUNT, fake.EMAIL):
        assert secret not in text and secret not in json.dumps(errors)
        assert secret not in json.dumps(w.doc.meta)
    assert "published" not in w.doc.meta["web_site"]


def test_cancel_a_publication(make_window, home, monkeypatch, qapp):
    fake.environment(monkeypatch, home)
    w = make_window()
    pane = _check(w, qapp)
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "slow")
    w.answers.append(_yes())
    pane.b_publish.click()
    assert pane.step_states["publish"] == "running" and pane.b_cf_cancel.isEnabled()
    assert not pane.b_cf_check.isEnabled() and not pane.cf_project.isEnabled()
    limit = time.monotonic() + 30
    while not (home / "deploying").exists() and time.monotonic() < limit:
        qapp.processEvents()
        time.sleep(0.05)
    assert (home / "deploying").exists()
    t0 = time.monotonic()
    pane.b_cf_cancel.click()
    _settle(w, qapp)
    assert time.monotonic() - t0 < 20  # wrangler stopped, not waited for
    assert pane.step_states["publish"] != "running" and not pane.b_cf_cancel.isEnabled()
    assert "published" not in w.doc.meta["web_site"]
    assert not [p for p in (w.doc.path.parent).iterdir() if ".publish-" in p.name]  # no staging left


def test_an_address_not_said_by_wrangler_is_typed_by_the_user(make_window, home, monkeypatch, qapp):
    fake.environment(monkeypatch, home)
    w = make_window()
    pane = _check(w, qapp)
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "taken,garbled,quiet")
    w.answers.append(_yes())
    pane.b_publish.click()
    _settle(w, qapp)
    assert pane.step_states["publish"] == "done", pane.states["publish"].text()
    assert "dashboard di Cloudflare" in pane.states["publish"].text()
    assert w.doc.meta["web_site"]["published"]["address"] is None
    assert not pane.b_site_open.isEnabled() and not pane.published_address.isReadOnly()
    assert "scavo.pages.dev" not in pane.published_address.text()
    pane.published_address.setText("pagina")
    pane.published_address.editingFinished.emit()
    assert w.said and w.said[-1][0] == "warn"
    pane.published_address.setText("https://scavo-7xq.pages.dev")
    pane.published_address.editingFinished.emit()
    assert w.doc.meta["web_site"]["published"]["address"] == "https://scavo-7xq.pages.dev"
    assert pane.published_address.isReadOnly() and pane.b_site_open.isEnabled()
    assert "scavo-7xq.pages.dev" in _site_item(w).text(0)


# ------------------------------------------------------------------------------------- the login from the pane
def _no_secret_logged(caplog):
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (fake.SECRET, fake.ACCOUNT, fake.EMAIL, "oauth2"):
        assert secret not in text, secret


def test_the_hint_names_npx_when_wrangler_runs_only_through_it(make_window, home, monkeypatch, qapp):
    fake.npx_environment(monkeypatch, home, "logged_out")
    w = make_window()
    pane = _check(w, qapp)
    assert pane.step_states["publish"] == "unavailable"
    assert "npx wrangler login" in pane.states["publish"].text()
    assert pane.b_cf_login.isVisible() and pane.b_cf_login.isEnabled()


def test_login_from_the_pane_then_checked_again(make_window, home, monkeypatch, qapp, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    fake.npx_environment(monkeypatch, home, "logged_out")
    w = make_window()
    pane = _check(w, qapp)
    assert pane.b_cf_login.isVisible() and not pane.b_publish.isEnabled()
    pane.b_cf_login.click()
    assert pane.step_states["publish"] == "running" and pane.b_cf_cancel.isEnabled()
    assert not pane.b_cf_check.isEnabled() and not pane.b_cf_login.isEnabled()
    _settle(w, qapp)
    assert ["login"] in fake.calls(home)
    assert fake.calls(home)[-2:] == [["--version"], ["whoami", "--json"]]  # checked again by itself
    assert pane.cf_status.state == "ready" and pane.b_publish.isEnabled()  # the plan made again too
    assert not pane.b_cf_login.isVisible() and not pane.b_cf_cancel.isEnabled()
    _no_secret_logged(caplog)
    assert fake.SECRET not in pane.states["publish"].text() and fake.EMAIL not in pane.states["publish"].text()


def test_a_failed_login_in_the_pane(make_window, home, monkeypatch, qapp, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    fake.environment(monkeypatch, home, "logged_out,login_fail")
    w = make_window()
    pane = _check(w, qapp)
    pane.b_cf_login.click()
    _settle(w, qapp)
    assert pane.step_states["publish"] == "failed"
    assert "non completato" in pane.states["publish"].text()
    assert pane.b_cf_login.isVisible() and pane.b_cf_login.isEnabled()  # to try again
    _no_secret_logged(caplog)
    assert w.said == []  # no error window: the pane says it


def test_a_login_never_finished_stops_by_itself(make_window, home, monkeypatch, qapp):
    from pyarchinit_photogrammetry.io import site_publish

    monkeypatch.setattr(site_publish, "LOGIN_TIMEOUT", 2.0)
    fake.environment(monkeypatch, home, "logged_out,login_hang")
    w = make_window()
    pane = _check(w, qapp)
    t0 = time.monotonic()
    pane.b_cf_login.click()
    _settle(w, qapp)
    assert time.monotonic() - t0 < 20
    assert pane.step_states["publish"] == "failed" and "2 s" in pane.states["publish"].text()


def test_cancel_a_login(make_window, home, monkeypatch, qapp):
    fake.environment(monkeypatch, home, "logged_out,login_hang")
    w = make_window()
    pane = _check(w, qapp)
    pane.b_cf_login.click()
    limit = time.monotonic() + 30
    while not (home / "login_started").exists() and time.monotonic() < limit:
        qapp.processEvents()
        time.sleep(0.05)
    assert (home / "login_started").exists()
    t0 = time.monotonic()
    pane.b_cf_cancel.click()
    _settle(w, qapp)
    assert time.monotonic() - t0 < 20  # wrangler stopped, not waited for
    assert pane.step_states["publish"] == "unavailable" and not pane.cf_login_running
    assert pane.b_cf_login.isEnabled() and not pane.b_cf_cancel.isEnabled()


def test_a_wrangler_folder_in_the_home_is_named_by_the_pane(make_window, home, monkeypatch, qapp):
    fake.environment(monkeypatch, home, "logged_out")
    legacy = home / "home" / ".wrangler"
    (legacy / "logs").mkdir(parents=True)
    w = make_window()
    pane = _check(w, qapp)
    assert pane.step_states["publish"] == "unavailable"
    assert str(legacy) in pane.states["publish"].text() and "cancellala" in pane.states["publish"].text()


# ------------------------------------------------------------------------------------- removing the site (1.10.2)
def _publish_r2(w, qapp):
    pane = w.web_pane
    pane.cf_bucket.setText("scavo-dati")
    pane.cf_bucket.textEdited.emit("scavo-dati")
    pane = _check(w, qapp)
    w.answers.append(_yes())
    pane.b_publish.click()
    _settle(w, qapp)
    assert pane.step_states["publish"] == "done", pane.states["publish"].text()
    return pane


def test_the_removal_dialog_needs_the_name(qapp, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.gui.web_pane import UnpublishDialog
    from pyarchinit_photogrammetry.io import site_publish as sp

    home = tmp_path / "fake"
    home.mkdir()
    fake.environment(monkeypatch, home)
    folder = fake.make_site(tmp_path / "sito", data_files={"ortho.tif": 26 * MiB, "units.geojson": 300})
    sp.publish(sp.plan(folder, "scavo", bucket="scavo-dati"), public=True)
    d = UnpublishDialog(sp.removal_plan(folder))
    text = d.text.text()
    assert "<b>scavo</b>" in text and "<b>scavo-dati</b>" in text and "2 file" in text and "Non si può" in text
    assert "spento" in text
    assert not d.b_remove.isEnabled() and d.c_bucket.isEnabled() and not d.delete_bucket()
    d.name.setText("scav")
    assert not d.b_remove.isEnabled()
    d.accept()
    assert d.result() != d.DialogCode.Accepted  # Enter with the name not typed does nothing
    d.name.setText("scavo")
    assert d.b_remove.isEnabled()
    d.c_bucket.setChecked(True)
    assert d.delete_bucket()
    d.deleteLater()
    # a bucket the program did not create: the tick is off
    state = sp.read_state(folder)
    state["cloudflare"].pop("bucket_created")
    (folder / sp.STATE).write_text(json.dumps(state))
    d = UnpublishDialog(sp.removal_plan(folder))
    assert not d.c_bucket.isEnabled() and not d.delete_bucket()
    d.deleteLater()
    assert [c for c in fake.calls(home) if c[:3] == ["pages", "project", "delete"]] == []


def test_remove_the_site_from_the_pane(make_window, home, monkeypatch, qapp):
    from pyarchinit_photogrammetry.gui import web_pane

    fake.environment(monkeypatch, home)
    w = make_window(data_files={"ortho.tif": 26 * MiB, "units.geojson": 300})
    pane = w.web_pane
    assert not pane.b_unpublish.isVisible()  # nothing published yet
    pane = _publish_r2(w, qapp)
    assert pane.b_unpublish.isVisible() and pane.b_unpublish.isEnabled()
    asked: list = []
    answers = [None, True]
    monkeypatch.setattr(web_pane, "ask_removal", lambda parent, removal: asked.append(removal) or answers.pop(0))
    pane.b_unpublish.click()  # declined: nothing removed
    _settle(w, qapp)
    assert asked[0].project == "scavo" and len(asked[0].objects) == 2 and asked[0].bucket_allowed
    assert not any(c[:3] == ["pages", "project", "delete"] for c in fake.calls(home))
    assert "published" in w.doc.meta["web_site"]

    pane.b_unpublish.click()
    _settle(w, qapp)
    assert not [s for s in w.said if s[0] == "error"], w.said
    cloud = fake.cloud(home)
    assert "scavo" not in cloud["projects"] and "scavo-dati" not in cloud["buckets"]
    assert "published" not in w.doc.meta["web_site"]
    import pyarchinit_photogrammetry as ppg

    assert "published" not in ppg.Document.open(w.doc.path).meta["web_site"]  # saved
    assert not pane.b_unpublish.isVisible() and pane.step_states["publish"] != "failed"
    folder = w.doc.path.parent / "scavo_sito_web"
    assert (folder / "site.json").exists() and not (folder / ".ppg-publish.json").exists()


def test_a_removal_that_fails_halfway_goes_on_later(make_window, home, monkeypatch, qapp):
    from pyarchinit_photogrammetry.gui import web_pane

    fake.environment(monkeypatch, home)
    w = make_window()
    pane = _check(w, qapp)
    w.answers.append(_yes())
    pane.b_publish.click()
    _settle(w, qapp)
    monkeypatch.setattr(web_pane, "ask_removal", lambda parent, removal: False)
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "fail_project_delete")
    pane.b_unpublish.click()
    _settle(w, qapp)
    assert pane.step_states["publish"] == "failed"
    text = pane.states["publish"].text()
    assert "Rimuovi dal web" in text and "Authentication error" in text
    for secret in (fake.SECRET, fake.ACCOUNT, fake.EMAIL):
        assert secret not in text
    assert pane.b_unpublish.isVisible() and "published" in w.doc.meta["web_site"]
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "")
    pane.b_unpublish.click()
    _settle(w, qapp)
    assert "scavo" not in fake.cloud(home)["projects"] and "published" not in w.doc.meta["web_site"]
    assert not pane.b_unpublish.isVisible()


def test_cancel_a_removal(make_window, home, monkeypatch, qapp):
    from pyarchinit_photogrammetry.gui import web_pane

    fake.environment(monkeypatch, home)
    w = make_window(data_files={"ortho.tif": 26 * MiB, "units.geojson": 300})
    pane = _publish_r2(w, qapp)
    monkeypatch.setattr(web_pane, "ask_removal", lambda parent, removal: False)
    monkeypatch.setenv("FAKE_WRANGLER_MODE", "slow_delete")
    pane.b_unpublish.click()
    assert pane.step_states["publish"] == "running" and pane.b_cf_cancel.isEnabled()
    assert not pane.b_unpublish.isEnabled()
    limit = time.monotonic() + 30
    while not (home / "deleting").exists() and time.monotonic() < limit:
        qapp.processEvents()
        time.sleep(0.05)
    t0 = time.monotonic()
    pane.b_cf_cancel.click()
    _settle(w, qapp)
    assert time.monotonic() - t0 < 20
    assert pane.step_states["publish"] != "running" and not pane.cf_removing
    assert pane.b_unpublish.isVisible() and pane.b_unpublish.isEnabled()  # what is left, removed next time
    assert "scavo" in fake.cloud(home)["projects"]
