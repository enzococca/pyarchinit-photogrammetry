"""Plugins: found in the plugin folder or the environment, activated one by one (one that fails is
left out), their actions, commands and export formats; the index of the plugin repository and
installing a wheel from it; the command line and the window."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
import zipfile
from pathlib import Path

import pytest

from pyarchinit_photogrammetry.plugins import API_VERSION, Manager
from pyarchinit_photogrammetry.plugins import index as plugin_index


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])

PLUGIN_CODE = '''
from pyarchinit_photogrammetry.plugins import Plugin


class TestPlugin(Plugin):
    title = "Plugin di prova"
    description = "adds a bit of everything"
    api = {api}

    def activate(self, context):
        {fail}
        context.settings["runs"] = context.settings.get("runs", 0) + 1
        context.save_settings()
        self.calls = []
        context.add_action("Saluta", lambda: self.calls.append("action"), tip="a test action")
        context.add_command("{command}", lambda *args: len(args), help="counts its arguments")
        context.add_exporter("Elenco di prova", "txt",
                             lambda chunk, path, progress: path.write_text(chunk.label))
        context.plugin = self
'''


def _plugin_files(name: str, *, fail: bool = False, api: int = API_VERSION, command: str | None = None,
                  version: str = "0.1.0") -> tuple[str, dict]:
    """The files of a plugin distribution (module + dist-info), unique module name per call."""
    module = f"ppg_test_plugin_{uuid.uuid4().hex[:8]}"
    dist = name.replace("-", "_")
    code = PLUGIN_CODE.format(api=api, fail="raise RuntimeError('broken on purpose')" if fail else "pass",
                              command=command or name)
    files = {
        f"{module}/__init__.py": code,
        f"{dist}-{version}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                                                 f"Summary: plugin {name} for the tests\n",
        f"{dist}-{version}.dist-info/entry_points.txt": f"[pyarchinit_photogrammetry.plugins]\n{name} = {module}:TestPlugin\n",
        f"{dist}-{version}.dist-info/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    return module, files


def _install_folder(folder: Path, name: str, **kw) -> str:
    module, files = _plugin_files(name, **kw)
    for rel, text in files.items():
        path = folder / name / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return module


def _wheel(path: Path, name: str, **kw) -> Path:
    _module, files = _plugin_files(name, **kw)
    with zipfile.ZipFile(path, "w") as z:
        for rel, text in files.items():
            z.writestr(rel, text)
    return path


def test_a_plugin_adds_actions_commands_and_exporters(tmp_path):
    folder = tmp_path / "plugins"
    _install_folder(folder, "prova")
    m = Manager(folder)
    infos = m.load()
    assert [i.name for i in infos] == ["prova"]
    info = infos[0]
    assert info.active and not info.error and info.source == "folder" and info.version == "0.1.0"
    assert info.title == "Plugin di prova" and info.description == "adds a bit of everything"
    assert [a.title for a in m.actions] == ["Saluta"] and m.actions[0].plugin == "prova"
    assert m.commands["prova"].function("a", "b", "c") == 3
    assert [(e.title, e.suffix) for e in m.exporters] == [("Elenco di prova", ".txt")]
    m.actions[0].callback()
    assert info.plugin.calls == ["action"]
    # its own folder and settings, kept between sessions
    assert json.loads((folder / "_data" / "prova" / "settings.json").read_text()) == {"runs": 1}
    m.unload()
    assert not m.actions and not m.commands
    again = Manager(folder)
    again.load()
    assert again.get("prova").context.settings["runs"] == 2


def test_a_failing_plugin_is_left_out_and_the_others_work(tmp_path):
    folder = tmp_path / "plugins"
    _install_folder(folder, "rotto", fail=True)
    _install_folder(folder, "sano")
    _install_folder(folder, "futuro", api=API_VERSION + 1)
    m = Manager(folder)
    m.load()
    assert "broken on purpose" in m.get("rotto").error and not m.get("rotto").active
    assert "plugin API" in m.get("futuro").error and not m.get("futuro").active
    assert m.get("sano").active and list(m.commands) == ["sano"]


def test_disabled_plugins_stay_off_and_safe_mode(tmp_path, monkeypatch):
    folder = tmp_path / "plugins"
    _install_folder(folder, "spento")
    m = Manager(folder)
    m.discover()
    m.set_enabled("spento", False)
    assert json.loads((folder / "state.json").read_text()) == {"disabled": ["spento"]}
    again = Manager(folder)
    again.load()
    assert not again.get("spento").enabled and not again.get("spento").active
    again.set_enabled("spento", True)
    monkeypatch.setenv("PPG_NO_PLUGINS", "1")
    safe = Manager(folder)
    safe.load()
    assert safe.get("spento").enabled and not safe.get("spento").active  # found, not run


def test_two_plugins_cannot_take_the_same_command(tmp_path):
    folder = tmp_path / "plugins"
    _install_folder(folder, "primo", command="misura")
    _install_folder(folder, "secondo", command="misura")
    m = Manager(folder)
    m.load()
    assert m.commands["misura"].plugin == "primo" and m.get("secondo").active


def _index(tmp_path, entries) -> str:
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"api": API_VERSION, "plugins": entries}))
    return path.as_uri()


def _entry(wheel: Path, name: str, **extra) -> dict:
    return {"name": name, "version": "0.1.0", "title": name.title(), "wheel": wheel.as_uri(),
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "api": API_VERSION, "min_app": "1.0.0", **extra}


def test_install_from_the_index(tmp_path, monkeypatch):
    wheel = _wheel(tmp_path / "dal_indice-0.1.0-py3-none-any.whl", "dal-indice")
    good = _entry(wheel, "dal-indice")
    monkeypatch.setenv("PPG_PLUGIN_INDEX", _index(tmp_path, [
        {**good, "title": "From the index", "i18n": {"it": {"title": "Dall'indice"}}},
        {**good, "name": "sbagliato", "sha256": "0" * 64},
        {**good, "name": "nativo", "wheel": good["wheel"].replace("py3-none-any", "cp312-cp312-macosx_14_0_arm64")},
        {**good, "name": "nuovo", "min_app": "99.0"},
        {**good, "name": "bisogni", "requires": ["un_modulo_che_non_esiste"]},
        {"name": "incompleto"},
    ]))
    entries = {e.name: e for e in plugin_index.fetch()}
    assert set(entries) == {"dal-indice", "sbagliato", "nativo", "nuovo", "bisogni"}  # the incomplete one skipped
    assert entries["dal-indice"].problem() == ""
    assert entries["dal-indice"].localized("title", "it") == "Dall'indice"
    assert entries["dal-indice"].localized("title", "en") == "From the index"
    assert entries["sbagliato"].localized("description", "it") == entries["sbagliato"].description
    assert "pure-Python" in entries["nativo"].problem()
    assert "99.0" in entries["nuovo"].problem()
    assert "un_modulo_che_non_esiste" in entries["bisogni"].problem()
    folder = tmp_path / "plugins"
    target = plugin_index.install(entries["dal-indice"], folder=folder)
    assert target == folder / "dal-indice" and (target / "dal_indice-0.1.0.dist-info").is_dir()
    with pytest.raises(plugin_index.PluginError, match="SHA-256"):
        plugin_index.install(entries["sbagliato"], folder=folder)
    assert not (folder / "sbagliato").exists() and not list(folder.glob(".install-*"))
    m = Manager(folder)
    m.load()
    assert m.get("dal-indice").active and m.get("dal-indice").source == "folder"
    m.remove("dal-indice")
    assert not (folder / "dal-indice").exists()


def test_a_wheel_cannot_write_outside_its_folder(tmp_path):
    wheel = tmp_path / "furbo-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("../../fuori.txt", "no")
    entry = plugin_index.Entry.from_dict(_entry(wheel, "furbo"))
    with pytest.raises(plugin_index.PluginError, match="unsafe"):
        plugin_index.install(entry, folder=tmp_path / "plugins")
    assert not (tmp_path / "fuori.txt").exists()


def test_command_line(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    folder = tmp_path / "plugins"
    _install_folder(folder, "riga")
    monkeypatch.setenv("PPG_PLUGINS_DIR", str(folder))
    runner = CliRunner()
    r = runner.invoke(app, ["plugins", "list"])
    assert r.exit_code == 0 and "riga" in r.output and "enabled" in r.output
    r = runner.invoke(app, ["plugin"])
    assert r.exit_code == 0 and "counts its arguments" in r.output
    r = runner.invoke(app, ["plugin", "riga", "uno", "due"])
    assert r.exit_code == 2  # the command returned 2: the exit code
    r = runner.invoke(app, ["plugin", "nessuno"])
    assert r.exit_code == 2 and "no plugin gives the command" in r.output
    r = runner.invoke(app, ["plugins", "disable", "riga"])
    assert r.exit_code == 0 and json.loads((folder / "state.json").read_text()) == {"disabled": ["riga"]}
    wheel = _wheel(tmp_path / "da_riga-0.1.0-py3-none-any.whl", "da-riga")
    monkeypatch.setenv("PPG_PLUGIN_INDEX", _index(tmp_path, [_entry(wheel, "da-riga")]))
    r = runner.invoke(app, ["plugins", "available"])
    assert r.exit_code == 0 and "da-riga" in r.output
    r = runner.invoke(app, ["plugins", "install", "da-riga"])
    assert r.exit_code == 0 and (folder / "da-riga").is_dir()
    r = runner.invoke(app, ["plugins", "remove", "da-riga"])
    assert r.exit_code == 0 and not (folder / "da-riga").exists()


def test_window_menu_export_and_dialog(tmp_path, monkeypatch, qapp):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.gui.plugins_dialog import PluginsDialog

    folder = tmp_path / "plugins"
    _install_folder(folder, "finestra")
    _install_folder(folder, "guasto", fail=True)
    monkeypatch.setenv("PPG_PLUGINS_DIR", str(folder))
    doc = ppg.Document()
    doc.save(tmp_path / "progetto.ppg")
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    try:
        assert w.plugin_menu is not None
        titles = [a.text() for sub in w.plugin_menu.actions() if sub.menu() for a in sub.menu().actions()]
        assert titles == ["Saluta"]
        sub = w.plugin_menu.actions()[0]
        assert sub.text() == "Plugin di prova"
        sub.menu().actions()[0].trigger()
        assert w.plugins.get("finestra").plugin.calls == ["action"]
        exports = [a.text() for a in w.export_menu.actions()]
        assert "Elenco di prova..." in exports
        # an export of a plugin runs in the background and writes the file chosen; what the window
        # shows is in the project before it starts
        out = tmp_path / "elenco.txt"
        w.quote.append({"x": 1.0, "y": 2.0, "quota_q": 3.0})
        monkeypatch.setattr(mw.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
        next(a for a in w.export_menu.actions() if a.text() == "Elenco di prova...").trigger()
        w.runner.wait()
        qapp.processEvents()
        assert out.read_text() == w.chunk.label
        assert w.chunk.meta["drawings"]["quote"][0]["quota_q"] == 3.0
        # ... and a plugin cannot read the window from the background
        import threading

        from pyarchinit_photogrammetry.plugins.api import Context

        context, failures = Context("finestra", tmp_path / "ctx", window=w), []
        worker = threading.Thread(target=lambda: failures.append(pytest.raises(RuntimeError, context.store_drawings)))
        worker.start()
        worker.join()
        assert failures and "interface thread" in str(failures[0].value)
        context.store_drawings()  # in the interface thread it works
        # a failing action is reported, the window stays
        errors = []
        monkeypatch.setattr(w, "_exc", lambda prefix, exc: errors.append((prefix, str(exc))))
        w._run_plugin_action(type(w.plugins.actions[0])("Rompe", lambda: 1 / 0, plugin="finestra"))
        assert errors and "finestra" in errors[0][0] and "division" in errors[0][1]
        dialog = PluginsDialog(w)
        from PySide6.QtCore import Qt

        names = [dialog.installed.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in range(dialog.installed.rowCount())]
        states = [dialog.installed.item(r, 2).text() for r in range(dialog.installed.rowCount())]
        assert names == ["finestra", "guasto"] and states[0] == "attivo" and "broken on purpose" in states[1]
        dialog.installed.selectRow(0)
        dialog.toggle()
        assert json.loads((folder / "state.json").read_text()) == {"disabled": ["finestra"]}
        assert dialog.installed.item(0, 2).text() == "attivo fino al riavvio"
        dialog.close()
    finally:
        w.setWindowModified(False)
        w.close()


def test_new_plugin_project(tmp_path):
    """``ppg plugins new``: a working plugin with the parts chosen, activated with a context of its
    own, its own tests passing (the entry-point test skipped: not installed); the names derived;
    a folder already there is not overwritten; a plugin with fewer parts."""
    import importlib
    import subprocess
    import sys

    from pyarchinit_photogrammetry.plugins import Context, scaffold

    assert scaffold.names("Conta Foto") == {"slug": "conta-foto", "distribution": "ppg-conta-foto",
                                            "package": "ppg_conta_foto", "class_name": "ContaFotoPlugin"}
    assert scaffold.slug_of("ppg-Riepilogo_2") == "riepilogo-2"
    target = scaffold.create(tmp_path, "Conta Foto", title="Conta foto", description="counts the photos",
                             author="Enzo Cocca", email="enzo@example.org")
    assert target == tmp_path / "ppg-conta-foto"
    files = {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()}
    assert files == {"pyproject.toml", "ppg_conta_foto/__init__.py", "tests/conftest.py", "tests/test_ppg_conta_foto.py",
                     "README.md", "README.it.md", "LICENSE", ".gitignore", ".github/workflows/release.yml"}
    pyproject = (target / "pyproject.toml").read_text()
    assert 'conta-foto = "ppg_conta_foto:ContaFotoPlugin"' in pyproject and 'email = "enzo@example.org"' in pyproject
    sys.path.insert(0, str(target))
    try:
        module = importlib.import_module("ppg_conta_foto")
        context = Context("conta-foto", tmp_path / "ctx")
        module.ContaFotoPlugin().activate(context)
        c = context.contributions
        assert [a.title for a in c.actions] == ["Riepilogo del chunk", "Statistiche del DEM"]
        assert list(c.commands) == ["conta-foto"] and [e.suffix for e in c.exporters] == [".md"]
    finally:
        sys.path.remove(str(target))
        sys.modules.pop("ppg_conta_foto", None)
    env = {**os.environ, "PYTHONPATH": str(target), "PPG_PLUGINS_ENVIRONMENT": "0"}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(target),
                        str(target / "tests")], capture_output=True, text=True, env=env, close_fds=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "5 passed, 1 skipped" in r.stdout, r.stdout
    with pytest.raises(FileExistsError):
        scaffold.create(tmp_path, "conta-foto")
    with pytest.raises(ValueError):
        scaffold.create(tmp_path, "1-non-valido")
    with pytest.raises(ValueError):
        scaffold.create(tmp_path, "vuoto", parts=[])
    small = scaffold.create(tmp_path / "s", "solo-comando", parts=["command"])
    code = (small / "ppg_solo_comando" / "__init__.py").read_text()
    assert "add_command" in code and "add_action" not in code and "add_exporter" not in code and "rasterio" not in code
    assert "cd " in scaffold.next_steps(small) and "pull request" in scaffold.next_steps(small)


def test_command_line_new(tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    runner = CliRunner()
    r = runner.invoke(app, ["plugins", "new", "Prova CLI", "--dir", str(tmp_path), "--parts", "command,exporter",
                            "--author", "Enzo"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "ppg-prova-cli" / "ppg_prova_cli" / "__init__.py").exists() and "pip install -e ." in r.output
    r = runner.invoke(app, ["plugins", "new", "prova-cli", "--dir", str(tmp_path)])
    assert r.exit_code == 1 and "exists already" in r.output


def test_new_plugin_dialog(tmp_path, monkeypatch, qapp):
    from pyarchinit_photogrammetry.gui import dialogs, plugins_dialog

    shown = []
    monkeypatch.setattr(dialogs, "message", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(plugins_dialog.QDesktopServices, "openUrl", staticmethod(lambda url: True))
    d = plugins_dialog.NewPluginDialog()
    d.name.setText("Dalla Finestra")
    d.folder.setText(str(tmp_path))
    d.parts["background"].setChecked(False)
    d.create()
    assert d.created == tmp_path / "ppg-dalla-finestra" and (d.created / "pyproject.toml").exists()
    assert "rasterio" not in (d.created / "ppg_dalla_finestra" / "__init__.py").read_text()
    assert shown and "Prossimi passi" in shown[-1][3]
    d.name.setText("")  # a bad name is reported, nothing written
    d.create()
    assert "not a plugin name" in shown[-1][3]


def test_help_of_a_plugin_command_goes_to_the_plugin():
    """``ppg plugin NAME --help`` is the plugin's own help, not the program's (Typer took --help:
    a plugin could not show its usage, and an unknown NAME looked fine)."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry import cli

    runner = CliRunner()
    assert runner.invoke(cli.app, ["plugin", "--help"]).exit_code == 0  # the commands of the plugins
    r = runner.invoke(cli.app, ["plugin", "nothing", "--help"])
    assert r.exit_code == 2 and "no plugin gives the command" in r.output


def test_requests_in_projected_coordinates(tmp_path, qapp):
    """In UTM coordinates (hundreds of km) vertices a few metres apart stay apart: Enter ends a short
    line and a small polygon; the same pixel clicked twice counts once."""
    import numpy as np
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.plugins.api import Context

    doc = ppg.Document()
    doc.add_chunk("utm")
    doc.save(tmp_path / "progetto.ppg")
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.resize(1200, 800)
    w.show()
    v = w.ortho_view
    context = Context("prova", tmp_path / "dati", w)
    try:
        context.show_frame((500000.0, 4500000.0, 500020.0, 4500010.0), 0.01, "griglia")
        qapp.processEvents()

        def click(x, y):
            QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             v.mapFromScene(v.scene_pt(x, y)))

        got = []
        assert context.request_line(got.append)
        for x, y in ((500002.0, 4500002.0), (500002.0, 4500005.0), (500002.0, 4500005.0)):
            click(x, y)
        QTest.keyClick(v, Qt.Key.Key_Return)
        assert len(got) == 1 and np.allclose(got[0], [[500002.0, 4500002.0], [500002.0, 4500005.0]], atol=0.05)
        assert context.request_polygon(got.append)
        for x, y in ((500003.0, 4500003.0), (500006.0, 4500003.0), (500005.0, 4500006.0)):
            click(x, y)
        QTest.keyClick(v, Qt.Key.Key_Return)
        assert len(got) == 2 and len(got[1]) == 3
    finally:
        context.release()
        w.setWindowModified(False)
        w.close()


def test_plugins_borrow_the_drawing_and_follow_the_view(tmp_path, qapp):
    """The window lends its drawing to a plugin (a polygon, a line, a point), tells it when the 2D
    view shows another raster, gives it an empty frame to draw on, marks the project modified and
    runs its functions in the interface thread."""
    import threading

    import numpy as np
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.plugins.api import Context

    doc = ppg.Document()
    doc.add_chunk("vuoto")
    doc.save(tmp_path / "progetto.ppg")
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.resize(1200, 800)
    w.show()
    v = w.ortho_view
    context = Context("prova", tmp_path / "dati", w)
    loaded = []
    context.on_raster_loaded(lambda: loaded.append(v.kind))
    try:
        context.show_frame((1000.0, 2000.0, 1100.0, 2050.0), 0.1, "griglia")  # no orthophoto or DEM: a frame
        qapp.processEvents()
        assert loaded == ["frame"] and v.tr is not None

        def at(x, y):
            return v.mapFromScene(v.scene_pt(x, y))

        def click(x, y):
            QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(x, y))

        got, cancelled = [], []
        assert context.request_polygon(got.append, lambda: cancelled.append("poligono"))
        assert v.mode == "req_polygon" and not any(a.isChecked() for a in w.draw_group.actions())
        for x, y in ((1010, 2010), (1060, 2010), (1040, 2040)):
            click(x, y)
        QTest.mouseDClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(1040, 2040))
        assert len(got) == 1 and got[0].shape[1] == 2 and len(got[0]) >= 3
        assert np.allclose(got[0][:3], [[1010, 2010], [1060, 2010], [1040, 2040]], atol=0.5)
        assert v.mode == "pan" and w.draw_actions["pan"].isChecked()
        assert context.request_line(got.append)  # two vertices, Enter ends; Backspace drops the last
        for x, y in ((1010, 2005), (1090, 2045), (1095, 2045)):
            click(x, y)
        QTest.keyClick(v, Qt.Key.Key_Backspace)
        QTest.keyClick(v, Qt.Key.Key_Return)
        assert np.allclose(got[1], [[1010, 2005], [1090, 2045]], atol=0.5)
        assert context.request_point(got.append)
        click(1050, 2025)
        assert got[2].shape == (1, 2) and np.allclose(got[2], [[1050, 2025]], atol=0.5)
        # another tool of the drawing bar, Esc and another request end a request
        context.request_polygon(got.append, lambda: cancelled.append("strumento"))
        w.draw_actions["measure"].trigger()
        assert cancelled == ["strumento"] and v.mode == "measure"
        context.request_line(got.append, lambda: cancelled.append("esc"))
        click(1010, 2010)
        QTest.keyClick(v, Qt.Key.Key_Escape)
        assert cancelled[-1] == "esc" and v.mode == "pan"
        context.request_point(got.append, lambda: cancelled.append("primo"))
        context.request_point(got.append, lambda: cancelled.append("secondo"))
        assert cancelled[-1] == "primo" and len(got) == 3
        # the project modified; a function from a background thread run in the interface thread
        w.setWindowModified(False)
        context.mark_modified()
        assert w.isWindowModified()
        ran = []
        t = threading.Thread(target=lambda: context.call_in_gui(
            lambda value: ran.append((value, threading.current_thread() is threading.main_thread())), 7))
        t.start()
        t.join()
        qapp.processEvents()
        assert ran == [(7, True)]
        # the window shows the project again: another chunk, a processing step, an undo
        refreshed = []
        context.on_refresh(lambda: refreshed.append(w.chunk.label))
        w.refresh_all()
        doc.add_chunk("altro")
        w._activate_chunk(doc.chunks[-1])
        assert refreshed == ["vuoto", "altro"]
        # a plugin that goes away is no longer told
        told = len(loaded)
        context.release()
        context.show_frame((0.0, 0.0, 10.0, 10.0), 0.05)
        w.refresh_all()
        assert len(loaded) == told and refreshed == ["vuoto", "altro"]
    finally:
        w.setWindowModified(False)
        w.close()
