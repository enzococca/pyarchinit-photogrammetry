"""New versions: GitHub releases, downloads, the macOS application swapped in, the interface."""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import urllib.error

import pytest

from pyarchinit_photogrammetry import updates
from pyarchinit_photogrammetry.core.progress import Cancelled, Progress

GITHUB = {
    "tag_name": "v0.7.0", "html_url": "https://github.com/enzococca/pyarchinit-photogrammetry/releases/tag/v0.7.0",
    "body": "## Novita'\n\n* controllo degli aggiornamenti", "published_at": "2026-10-20T10:00:00Z",
    "assets": [
        {"name": "PyArchInit-Photogrammetry-0.7.0-macos-arm64.dmg", "size": 10,
         "browser_download_url": "https://example.org/a.dmg"},
        {"name": "PyArchInit-Photogrammetry-0.7.0-windows-x64.zip", "size": 20,
         "browser_download_url": "https://example.org/a.zip"},
    ],
}


def _json_url(tmp_path, data) -> str:
    f = tmp_path / "latest.json"
    f.write_text(json.dumps(data))
    return f.as_uri()


def test_version_order():
    order = ["0.6.1", "0.7.0.dev1", "0.7.0a1", "v0.7.0b2", "0.7.0rc1", "0.7.0", "0.7.1", "1.0"]
    keys = [updates.version_key(v) for v in order]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    assert updates.version_key("v0.7.0") == updates.version_key("0.7") == updates.version_key("0.7.0+local.1")
    assert updates.is_newer("0.7.0", "0.6.1") and not updates.is_newer("0.6.1", "0.6.1")
    assert not updates.is_newer("nightly", "0.6.1")
    with pytest.raises(ValueError):
        updates.version_key("0.7.0-final")


def test_release_and_installer_of_each_platform():
    r = updates.Release.from_github(GITHUB)
    assert (r.version, r.tag) == ("0.7.0", "v0.7.0") and r.notes.startswith("## Novita'")
    assert updates.asset_for_platform(r, "Darwin", "arm64")["name"].endswith("macos-arm64.dmg")
    assert updates.asset_for_platform(r, "Windows", "AMD64")["name"].endswith("windows-x64.zip")
    assert updates.asset_for_platform(r, "Darwin", "x86_64") is None  # no Intel build published
    assert updates.asset_for_platform(r, "Linux", "x86_64") is None  # pip
    assert updates.platform_tag("Linux") is None


def test_check_reads_the_latest_release(tmp_path):
    url = _json_url(tmp_path, GITHUB)
    assert updates.check("0.6.1", url=url).version == "0.7.0"
    assert updates.check("0.7.0", url=url) is None
    assert updates.check("0.8.0", url=url) is None
    assert updates.check("0.6.1", skip="0.7.0", url=url) is None  # skipped by the user
    assert updates.check("0.6.1", skip="0.6.5", url=url).version == "0.7.0"


def test_check_errors_have_a_kind(tmp_path, monkeypatch):
    with pytest.raises(updates.UpdateError) as e:
        updates.check("0.6.1", url=(tmp_path / "missing.json").as_uri())
    assert e.value.kind == "offline"
    bad = tmp_path / "bad.json"
    bad.write_text("<html>")
    with pytest.raises(updates.UpdateError) as e:
        updates.check("0.6.1", url=bad.as_uri())
    assert e.value.kind == "http"
    with pytest.raises(updates.UpdateError) as e:
        updates.check("0.6.1", url=_json_url(tmp_path, dict(GITHUB, tag_name="latest")))
    assert e.value.kind == "http"

    def refuse(code):
        def urlopen(request, timeout=None, **kwargs):
            raise urllib.error.HTTPError(request.full_url, code, "no", {}, None)
        return urlopen

    for code, kind in ((403, "limit"), (429, "limit"), (404, "missing"), (500, "http")):
        monkeypatch.setattr(updates.urllib.request, "urlopen", refuse(code))
        with pytest.raises(updates.UpdateError) as e:
            updates.check("0.6.1")
        assert e.value.kind == kind


def test_download_is_complete_or_absent(tmp_path):
    payload = os.urandom(3 * (1 << 20) + 123)
    src = tmp_path / "server" / "PyArchInit-Photogrammetry-0.7.0-macos-arm64.dmg"
    src.parent.mkdir()
    src.write_bytes(payload)
    asset = {"name": src.name, "url": src.as_uri(), "size": len(payload)}
    seen = []
    out = updates.download(asset, tmp_path / "dl", progress=lambda f, m: seen.append(f))
    assert out.read_bytes() == payload and out.parent == tmp_path / "dl"
    assert seen and seen[-1] == 1.0 and sorted(seen) == seen
    assert not list((tmp_path / "dl").glob("*.part"))

    # cancelled after the first block: no file left that looks finished
    prog = Progress(lambda f, m: prog.cancel() if 0 < f < 1 else None)
    with pytest.raises(Cancelled):
        updates.download(asset, tmp_path / "cancelled", progress=prog)
    assert not list((tmp_path / "cancelled").iterdir())

    # a short download (the size announced by GitHub differs)
    with pytest.raises(updates.UpdateError) as e:
        updates.download(dict(asset, size=len(payload) + 1), tmp_path / "short")
    assert e.value.kind == "incomplete" and not list((tmp_path / "short").iterdir())

    # the name from the server never chooses the folder
    out = updates.download(dict(asset, name="../../escape.dmg"), tmp_path / "safe")
    assert out == tmp_path / "safe" / "escape.dmg"


def _fake_app(folder, version: str):
    app = folder / "PyArchInit Photogrammetry.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleShortVersionString": version, "CFBundleName": "PyArchInit Photogrammetry"}, f)
    (app / "Contents" / "MacOS" / "version.txt").write_text(version)
    return app


@pytest.mark.skipif(sys.platform != "darwin", reason="disk images and application bundles are macOS")
def test_macos_application_is_swapped_after_quitting(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "staging_folder", lambda: tmp_path / "staging")
    source = tmp_path / "dmg-content"
    source.mkdir()
    _fake_app(source, "9.9.9")
    os.symlink("/Applications", source / "Applications")
    dmg = tmp_path / "PyArchInit-Photogrammetry-9.9.9-macos-arm64.dmg"
    r = subprocess.run(["/usr/bin/hdiutil", "create", "-volname", "PyArchInit Photogrammetry", "-srcfolder", str(source),
                        "-ov", "-format", "UDZO", str(dmg)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"hdiutil create: {r.stderr.strip()}")
    target = _fake_app(tmp_path / "Applications", "0.6.1")

    with pytest.raises(updates.UpdateError) as e:
        updates.prepare_macos_update(dmg, target, expected_version="9.9.8")
    assert e.value.kind == "install"
    new = updates.prepare_macos_update(dmg, target, expected_version="9.9.9")
    assert updates.bundle_version(new) == "9.9.9" and updates.bundle_version(target) == "0.6.1"
    assert not any(p.name.startswith("ppg-update-") for p in tmp_path.iterdir())  # mount point removed

    running = subprocess.Popen(["/bin/sleep", "1"], close_fds=False)  # stands for the running application
    helper = updates.finish_macos_update(new, target, pid=running.pid, relaunch=False)
    assert updates.bundle_version(target) == "0.6.1"  # not before the application has quit
    running.wait()
    helper.wait(timeout=30)
    assert updates.bundle_version(target) == "9.9.9"
    assert (target / "Contents" / "MacOS" / "version.txt").read_text() == "9.9.9"
    assert not new.exists() and not (target.parent / ".PyArchInit Photogrammetry.old.app").exists()


@pytest.mark.skipif(sys.platform != "darwin" or os.geteuid() == 0, reason="permissions of a macOS folder")
def test_macos_update_refused_where_the_user_cannot_write(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "staging_folder", lambda: tmp_path / "staging")
    target = _fake_app(tmp_path / "Locked", "0.6.1")
    os.chmod(target.parent, 0o555)
    try:
        with pytest.raises(updates.UpdateError) as e:
            updates.prepare_macos_update(tmp_path / "any.dmg", target)
        assert e.value.kind == "install"
    finally:
        os.chmod(target.parent, 0o755)
        shutil.rmtree(target.parent)


def test_install_kind_of_this_checkout():
    assert updates.install_kind() in ("source", "pip")
    assert updates.app_bundle("/Applications/X.app/Contents/MacOS/X") == updates.Path("/Applications/X.app")
    assert updates.app_bundle("/usr/bin/python3") is None


# ---------------------------------------------------------------------------------- interface
pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")


@pytest.fixture
def window(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    from pyarchinit_photogrammetry.core.document import Document
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    w = MainWindow(Document(), autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    yield w, app
    w.setWindowModified(False)
    w.close()


def test_update_dialog_offers_what_fits_the_installation(window):
    from pyarchinit_photogrammetry.gui.update_dialog import UpdateDialog

    _, app = window
    release = updates.Release.from_github(GITHUB)
    pip = UpdateDialog(release, kind="pip")
    assert set(pip.buttons) == {"copy", "page", "skip", "later"}
    pip.buttons["copy"].click()
    assert pip.action == "copy" and app.clipboard().text() == updates.PIP_COMMAND
    src = UpdateDialog(release, kind="source")
    assert src.command == "git pull"
    mac = UpdateDialog(release, kind="macos-app")
    assert ("install" in mac.buttons) == (updates.asset_for_platform(release) is not None)
    win = UpdateDialog(updates.Release.from_github(dict(GITHUB, assets=[])), kind="windows-app")
    assert "download" not in win.buttons  # nothing to download for this system
    mac.buttons["skip"].click()
    assert mac.action == "skip"


def test_automatic_check_once_a_day_and_skip(window, monkeypatch):
    from pyarchinit_photogrammetry.gui import main_window as mw

    w, app = window
    assert w.a_update_auto.isChecked()
    started = []
    monkeypatch.setattr(w.update_checker, "start", lambda manual=False, skip=None: started.append((manual, skip)) or True)
    w._auto_check_updates()
    w._auto_check_updates()  # the same day: no second request
    assert started == [(False, None)]
    w.settings.setValue("updates_last_check", "2000-01-01")
    w.a_update_auto.setChecked(False)
    w._auto_check_updates()
    assert len(started) == 1 and w.settings.value("updates_auto") == "false"

    class Skip:
        def __init__(self, release, parent=None):
            self.action, self.command = "skip", None

        def exec(self):
            return 0

    monkeypatch.setattr(mw, "UpdateDialog", Skip)
    w._updates_checked(updates.Release.from_github(GITHUB), None, False)
    assert w.settings.value("updates_skip") == "0.7.0"

    shown = []
    monkeypatch.setattr(mw, "message", lambda parent, kind, title, text, *a, **k: shown.append((kind, text)))
    w._updates_checked(None, None, True)
    w._updates_checked(None, updates.UpdateError("x", "offline"), True)
    w._updates_checked(None, updates.UpdateError("x", "offline"), False)  # automatic: only logged
    assert [k for k, _ in shown] == ["info", "warning"] and "connessione" in shown[1][1]


def test_checker_reports_in_the_interface_thread(window):
    from pyarchinit_photogrammetry.gui.update_dialog import UpdateChecker

    _, app = window
    got = []
    release = updates.Release.from_github(GITHUB)
    ok = UpdateChecker(check=lambda skip=None: release)
    ok.finished.connect(lambda r, e, manual: got.append((r, e, manual)))
    assert ok.start(manual=True) and not ok.start(manual=True)  # one check at a time

    def fail(skip=None):
        raise updates.UpdateError("down", "offline")

    bad = UpdateChecker(check=fail)
    bad.finished.connect(lambda r, e, manual: got.append((r, e, manual)))
    bad.start()
    import time

    t0 = time.time()
    while len(got) < 2 and time.time() - t0 < 10:
        app.processEvents()
        time.sleep(0.01)
    assert (release, None, True) in got
    err = [e for _, e, _ in got if e is not None]
    assert err and err[0].kind == "offline" and not ok.running and not bad.running
