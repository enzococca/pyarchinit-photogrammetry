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
def qapp(tmp_path_factory):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    # the windows of these tests save their geometry and their panes when they close: not in the user's settings
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path_factory.mktemp("settings")))
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


def test_folder_plugins_can_import_a_dependency_that_sorts_after_them(tmp_path):
    """GeoAI depends on ppg-samgeo: plugin activation order must not hide that module."""
    folder = tmp_path / "plugins"
    _install_folder(folder, "samgeo")
    (folder / "samgeo" / "ppg_samgeo").mkdir()
    (folder / "samgeo" / "ppg_samgeo" / "__init__.py").write_text("VALUE = 'shared AI environment'\n")

    geo = folder / "geoai"
    dist = geo / "ppg_geoai_test-0.1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: ppg-geoai-test\nVersion: 0.1.0\n")
    (dist / "entry_points.txt").write_text("[pyarchinit_photogrammetry.plugins]\ngeoai = geoai_plugin:GeoaiPlugin\n")
    (geo / "geoai_plugin.py").write_text('''from pyarchinit_photogrammetry.plugins import Plugin
from ppg_samgeo import VALUE
class GeoaiPlugin(Plugin):
    title = "GeoAI"
    def activate(self, context):
        context.add_command("geoai", lambda: VALUE)
''')
    manager = Manager(folder)
    manager.load()
    assert [i.name for i in manager.plugins] == ["geoai", "samgeo"]
    assert manager.get("geoai").active and manager.get("samgeo").active
    assert manager.commands["geoai"].function() == "shared AI environment"


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
    files = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
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


def test_the_tests_never_write_the_users_preferences(qapp):
    """Every window of the tests keeps its preferences in a temporary INI file (conftest), whatever
    test ran before: a recent project of a test never reaches the user's list."""
    from PySide6.QtCore import QSettings

    from pyarchinit_photogrammetry.gui import app_settings

    s = app_settings()
    assert s.format() == QSettings.Format.IniFormat and not s.fileName().endswith(".plist")
    assert "Library/Preferences" not in s.fileName()


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


@pytest.fixture
def window_settings(tmp_path, monkeypatch):
    """The preferences of the windows of a test in its own file, never the user's."""
    from PySide6.QtCore import QSettings

    import pyarchinit_photogrammetry.gui as gui

    path = tmp_path / "preferenze.ini"
    monkeypatch.setattr(gui, "app_settings", lambda: QSettings(str(path), QSettings.Format.IniFormat))
    return path


def _rasters(chunk, height: int = 48, width: int = 64, res: float = 0.5):
    """A small DEM and orthophoto made the products of ``chunk``."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    rows, cols = np.mgrid[0:height, 0:width].astype(np.float32)
    transform = from_origin(1000.0, 2000.0 + height * res, res, res)
    dem, ortho = chunk.data_dir / "dem.tif", chunk.data_dir / "ortofoto.tif"
    with rasterio.open(dem, "w", driver="GTiff", height=height, width=width, count=1, dtype="float32",
                       transform=transform) as dst:
        dst.write(100.0 + 0.01 * cols + 0.02 * rows, 1)
    rgba = np.stack([cols % 256, rows % 256, np.full_like(cols, 128), np.full_like(cols, 255)]).astype(np.uint8)
    with rasterio.open(ortho, "w", driver="GTiff", height=height, width=width, count=4, dtype="uint8",
                       transform=transform, photometric="RGB") as dst:
        dst.write(rgba)
    chunk.products["dem"], chunk.products["orthomosaic"] = dem.name, ortho.name
    return dem, ortho


def test_plugins_choose_the_chunk_and_what_the_views_show(tmp_path, qapp, window_settings):
    """Since 1.3.4 a plugin makes a chunk the active one and shows - as the window does - its orthophoto,
    its DEM or another GeoTIFF of theirs in the 2D view, a product or a mesh of its own in the 3D view
    (the list *Mostra* naming it); with nothing of that kind it gets False and the views stay. In the
    command line nothing is shown and the chunk becomes the active one of its project."""
    import shutil

    import trimesh

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.plugins.api import Context

    doc = ppg.Document()
    rilievo, vuoto = doc.add_chunk("rilievo"), doc.add_chunk("vuoto")
    doc.save(tmp_path / "progetto.ppg")
    dem, ortho = _rasters(rilievo)
    kept = Path(shutil.copy2(dem, tmp_path / "dem_conservato.tif"))
    doc.chunk = vuoto
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    v, mv = w.ortho_view, w.model_view
    context = Context("prova", tmp_path / "dati", w)
    shown = []
    context.on_refresh(lambda: shown.append(w.chunk.label))
    try:
        assert w.chunk is vuoto and v.tr is None
        assert context.show_raster() is False and v.tr is None  # nothing to show: the view stays
        assert context.show_raster(chunk=rilievo) is True  # the chunk first becomes the active one
        assert w.chunk is rilievo and shown == ["rilievo"]
        assert v.kind == "ortho" and v.path == ortho and w.tabs.currentWidget() is v
        assert context.show_raster("dem") and v.kind == "dem" and v.path == dem
        assert context.show_raster("dem", path=kept) and v.kind == "dem" and v.path == kept  # a kept version
        assert context.show_raster("ortho", path=tmp_path / "nessuno.tif") is False and v.path == kept
        with pytest.raises(ValueError):
            context.show_raster("modello")
        with pytest.raises(ValueError):
            context.show_raster(path=kept)  # which kind?
        # the 3D view: a product, the best the chunk has, a mesh of the plugin
        assert context.show_model("model") is False and mv.kind == "cameras"  # no model in the chunk
        assert context.show_model() and mv.kind == "cameras" and w.tabs.currentWidget() is mv
        mesh = rilievo.data_dir / "unita.ply"
        trimesh.creation.box(extents=(2.0, 2.0, 1.0)).export(mesh)
        w.show_view(v)
        assert context.show_model("model", path=mesh, title="Unità (1)") and w.tabs.currentWidget() is mv
        assert mv.kind == "model" and mv.combo.currentData() == "model" and len(mv.sd.mesh_faces) == 12
        assert mv.version_label.text() == "Unità (1)" and mv.version_label.isVisibleTo(mv)
        mv.combo.setCurrentIndex(mv.combo.findData("cameras"))  # back to the products of the chunk
        assert mv.kind == "cameras" and not mv.paths and mv.combo.findData("model") < 0
        assert context.show_model("model", path=tmp_path / "nessuno.ply") is False and mv.kind == "cameras"
        with pytest.raises(ValueError):
            context.show_model("tie_points", path=mesh)
        with pytest.raises(ValueError):
            context.show_model("modelli")
        # another chunk active; the active one again changes nothing
        context.activate_chunk(vuoto)
        assert w.chunk is vuoto and shown == ["rilievo", "vuoto"] and v.tr is None
        context.activate_chunk(vuoto)
        assert shown == ["rilievo", "vuoto"]
        with pytest.raises(ValueError):
            context.activate_chunk(ppg.Document().add_chunk("di un altro progetto"))
    finally:
        context.release()
        w.setWindowModified(False)
        w.close()
    line = Context("prova", tmp_path / "dati")
    assert line.show_raster() is False and line.show_model("model") is False
    line.activate_chunk(rilievo)
    assert doc.chunk is rilievo


def test_the_list_of_the_3d_view_names_the_mesh_a_plugin_shows(tmp_path, qapp, window_settings):
    """Reported: after the geoarchaeology plugin showed its units in the 3D view - a mesh of its own
    given to ``model_view.show_version`` on a chunk without a model - the list *Mostra* read "Camere".
    The list names the kind shown, with an entry of its own that goes away with the mesh; a kept
    version of a product the chunk has adds none; when the window shows the project again, or a kind
    the chunk has not is asked for, the list and the view agree."""
    import trimesh

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.pointcloud import PointCloud
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.gui.i18n import tr
    from pyarchinit_photogrammetry.gui.model_view import VIEW_LABELS

    doc = ppg.Document()
    ch = doc.add_chunk("scavo")
    doc.save(tmp_path / "progetto.ppg")
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    mv = w.model_view

    def entries():
        return [mv.combo.itemData(i) for i in range(mv.combo.count())]

    units = ch.data_dir / "geoarcheologia" / "unita_vista.ply"
    units.parent.mkdir(parents=True)
    trimesh.creation.box(extents=(2.0, 2.0, 1.0)).export(units)
    try:
        assert entries() == ["cameras"] and mv.kind == "cameras"
        mv.show_version("model", units, "Unità geoarcheologiche (3 unità)")  # as the plugin 0.1.3 does
        assert mv.kind == "model" and mv.combo.currentData() == "model" and len(mv.sd.mesh_faces) == 12
        assert mv.combo.currentText() == tr(VIEW_LABELS["model"]) and entries() == ["cameras", "model"]
        cloud = PointCloud([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.5]]).save(ch.data_dir / "punti.ply")
        mv.show_version("point_cloud", cloud, "Punti del plugin", version=False)  # one entry at a time
        assert mv.kind == "point_cloud" and entries() == ["cameras", "point_cloud"] and len(mv.sd.points) == 3
        assert mv.version_label.text() == "Punti del plugin"
        w.refresh_all()  # the window shows the project again: its products, the list as the view
        assert mv.kind == "cameras" and mv.combo.currentData() == "cameras" and entries() == ["cameras"]
        assert not mv.paths and not mv.version_label.isVisibleTo(mv)
        # a chunk with a model: a kept version is named beside the entry of the model
        trimesh.creation.box(extents=(3.0, 3.0, 1.0)).export(ch.data_dir / "model.ply")
        ch.products["model"] = "model.ply"
        w._show_model("model")
        assert entries() == ["cameras", "model"] and mv.kind == "model"
        mv.show_version("model", units, "Prima della pulizia")
        assert entries() == ["cameras", "model"] and mv.combo.currentData() == "model"
        assert "Prima della pulizia" in mv.version_label.text() and mv.paths
        mv.combo.setCurrentIndex(mv.combo.findData("cameras"))
        assert entries() == ["cameras", "model"] and not mv.paths
        # a kind the chunk has not: the best it has, named in the list
        del ch.products["model"]
        w._show_model("model")
        assert mv.kind == "cameras" and mv.combo.currentData() == "cameras"
    finally:
        w.setWindowModified(False)
        w.close()


def test_plugins_are_told_the_features_the_user_deletes(tmp_path, qapp, window_settings, monkeypatch):
    """Since 1.3.4 the window tells a plugin which features of its layer the user deleted - the tool
    *Seleziona* and Canc, *Elimina selezionati* - with their attributes, for the plugin to drop its
    records of them; not those of other layers, not after the plugin went away. A plugin that fails
    is reported and the window goes on."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.plugins.api import Context

    doc = ppg.Document()
    doc.add_chunk("griglia")
    doc.save(tmp_path / "progetto.ppg")
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.resize(1200, 800)
    w.show()
    v = w.ortho_view
    layer = "Anomalie di prova"
    context = Context("prova", tmp_path / "dati", w)
    told = []
    context.on_features_deleted(layer, told.append)
    monkeypatch.setattr(mw, "message", lambda *a, **k: QMessageBox.StandardButton.Yes)

    def anomaly(k):
        x = 1010.0 + 20 * k
        v.add_polygon([(x, 2010.0), (x + 10, 2010.0), (x + 10, 2020.0)], f"A{k}", layer=layer, attrs={"id": f"a{k}"})

    def feature(name, ident):
        return next(item for item, _label, attrs in v.layers[name]["features"] if attrs.get("id") == ident)

    try:
        context.show_frame((1000.0, 2000.0, 1100.0, 2050.0), 0.1, "griglia")
        for k in range(3):
            anomaly(k)
        v.add_point(1050.0, 2040.0, layer="Altri punti", attrs={"id": "p0"})
        w.set_draw_mode("select")
        v.select_items([feature(layer, "a0"), feature("Altri punti", "p0")])
        QTest.keyClick(v, Qt.Key.Key_Delete)  # Canc with the tool Seleziona
        assert [[f["id"] for f in features] for features in told] == [["a0"]]
        assert told[0][0]["etichetta"] == "A0" and told[0][0]["_geom"][0] == "Polygon"
        assert [f["id"] for f in v.features(layer)] == ["a1", "a2"] and "Altri punti" not in v.layers
        v.select_items([feature(layer, "a2")])
        w.a_sel_delete.trigger()  # Elimina selezionati of the drawing bar
        assert [f["id"] for f in told[1]] == ["a2"]
        errors = []
        monkeypatch.setattr(w, "_exc", lambda prefix, exc: errors.append((prefix, str(exc))))
        context.on_features_deleted(layer, lambda features: 1 / 0)
        v.select_items([feature(layer, "a1")])
        assert w.delete_selected(confirm=False) == 1
        assert [f["id"] for f in told[2]] == ["a1"]  # the other callbacks are told all the same
        assert errors and "prova" in errors[0][0] and "division" in errors[0][1]
        context.release()  # a plugin that went away is not told
        anomaly(3)
        v.select_items([feature(layer, "a3")])
        assert w.delete_selected(confirm=False) == 1 and len(told) == 3 and len(errors) == 1
    finally:
        context.release()
        w.setWindowModified(False)
        w.close()


def test_plugins_put_their_products_in_the_workspace(tmp_path, qapp, window_settings, monkeypatch, caplog):
    """Since 1.3.4 a plugin lists its products in the workspace under each chunk, after those of the
    program: a node with two children; a double click or Enter calls ``activate`` with the chunk of the
    item the active one, the right click offers its ``actions``; ``refresh_tree`` asks the provider
    again and a node keeps the expansion the user gave it; a provider that fails leaves the tree whole
    without its items; the items go away with the plugin."""
    import logging

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.plugins.api import Context

    doc = ppg.Document()
    primo, secondo = doc.add_chunk("primo"), doc.add_chunk("secondo")
    doc.save(tmp_path / "progetto.ppg")
    doc.chunk = secondo
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.resize(1200, 800)
    w.show()
    qapp.processEvents()
    tree = w.workspace
    role = Qt.ItemDataRole.UserRole
    calls, products = [], {"primo": ["NDVI", "NDRE"], "secondo": []}
    menus = []

    class Menu(mw.QMenu):  # the right click: the entries recorded, the first one chosen
        def exec(self, *args):
            menus.append([a.text() for a in self.actions()])
            self.actions()[0].trigger()

    monkeypatch.setattr(mw, "QMenu", Menu)

    def provider(chunk):
        calls.append(chunk.label)
        names = products[chunk.label]
        children = [{"label": f"Indice {n}", "icon": "index", "tooltip": f"l'indice {n}",
                     "activate": lambda n=n: calls.append(("mostra", n, w.chunk.label)),
                     "actions": [("Esporta...", lambda n=n: calls.append(("esporta", n, w.chunk.label)))]}
                    for n in names]
        return [{"label": f"Sentinel-2 ({len(names)} indici)", "icon": "multispectral", "children": children,
                 "expanded": True}] if names else []

    def chunk_node(label):
        root = tree.topLevelItem(0)
        return next(root.child(i) for i in range(root.childCount()) if root.child(i).data(0, role)[1].label == label)

    def of_plugins(item):
        return [item.child(i) for i in range(item.childCount()) if item.child(i).data(0, role)[0] == "plugin"]

    context = Context("prova", tmp_path / "dati", w)
    try:
        before = chunk_node("primo").childCount()
        context.add_tree_items(provider)  # the tree filled again at once
        assert calls == ["primo", "secondo"]
        node = chunk_node("primo")
        (s2,) = of_plugins(node)
        assert node.childCount() == before + 1 and node.child(node.childCount() - 1) is s2  # after the products
        assert s2.text(0) == "Sentinel-2 (2 indici)" and s2.isExpanded()
        assert [s2.child(i).text(0) for i in range(s2.childCount())] == ["Indice NDVI", "Indice NDRE"]
        assert s2.child(0).toolTip(0) == "l'indice NDVI" and of_plugins(chunk_node("secondo")) == []
        # a double click: the chunk of the item becomes the active one, then activate
        tree.itemDoubleClicked.emit(s2.child(1), 0)
        assert calls[-1] == ("mostra", "NDRE", "primo") and w.chunk is primo
        ndvi = of_plugins(chunk_node("primo"))[0].child(0)  # the tree was filled again for the chunk
        tree.setCurrentItem(ndvi)
        QTest.keyClick(tree, Qt.Key.Key_Return)
        assert calls[-1] == ("mostra", "NDVI", "primo")
        tree.scrollToItem(ndvi)
        tree.customContextMenuRequested.emit(tree.visualItemRect(ndvi).center())
        assert menus == [["Esporta..."]] and calls[-1] == ("esporta", "NDVI", "primo")
        # filled again: the provider asked again, the expansion the user gave kept
        of_plugins(chunk_node("primo"))[0].setExpanded(False)
        n = len(calls)
        context.refresh_tree()
        assert calls[n:] == ["primo", "secondo"] and not of_plugins(chunk_node("primo"))[0].isExpanded()
        # a provider that fails: written in the log, the tree whole without its items
        other = Context("rotto", tmp_path / "rotto", w)
        with caplog.at_level(logging.ERROR, logger="pyarchinit_photogrammetry"):
            other.add_tree_items(lambda chunk: 1 / 0)
        assert "rotto" in caplog.text and len(of_plugins(chunk_node("primo"))) == 1
        other.release()
        context.release()  # the plugin goes away, its items with it
        assert of_plugins(chunk_node("primo")) == [] and tree.providers == []
        n = len(calls)
        w.refresh_all()
        assert len(calls) == n
    finally:
        context.release()
        w.setWindowModified(False)
        w.close()


def test_plugins_make_units_as_if_drawn(trench, tmp_path):
    """Since 1.5.0 a plugin makes stratigraphic units: on the active chunk they are drawn on the view, with
    the site of the project, all of them one entry of Modifica > Annulla and one line of the log; an
    outline crossing itself is made valid, one that encloses no area is left out. On another chunk, and in
    the command line, they go into the drawings of the chunk, where the window finds them. Never from
    another thread."""
    import threading

    from shapely.geometry import box

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.plugins.api import Context

    from conftest import TRENCH_ORIGIN

    w, app, _mw = trench
    x0, y0 = TRENCH_ORIGIN
    context = Context("prova", tmp_path / "dati", w)
    log = w.log_pane.table
    drawn, entries = len(w.units), w.history.index
    changes = []
    w.ortho_view.layers_changed.connect(lambda: changes.append(1))
    bowtie = [(x0 + 4, y0 + 4), (x0 + 5, y0 + 5), (x0 + 5, y0 + 4), (x0 + 4, y0 + 5)]  # crossing itself
    added = context.add_units([box(x0 + 1, y0 + 1, x0 + 2, y0 + 2),
                               [(x0 + 3, y0 + 3), (x0 + 3.5, y0 + 3), (x0 + 3.5, y0 + 3.6)],
                               bowtie, [(x0, y0), (x0 + 1, y0)]],
                              attributes=[{"samgeo_score": 0.91}, {"us": 12, "tipo": "USM", "disegnatore": "E. C."},
                                          {}, {}])
    assert added == 3 and len(w.units) == drawn + 3  # the line encloses no area
    assert changes == [1]  # the layer pane and the QGIS link updated once for all of them
    first, second, third = w.units[-3:]
    assert first.us is None and first.tipo == "US" and first.sito == "Saggio"
    assert first.attributes["samgeo_score"] == 0.91 and first.geometry.area == pytest.approx(1.0)
    assert second.us == "12" and second.tipo == "USM" and second.attributes["disegnatore"] == "E. C."
    assert third.geometry.is_valid and third.geometry.area == pytest.approx(0.5)  # the two triangles
    assert len(w.ortho_view.features("Unita' stratigrafiche")) == drawn + 4  # one feature per part
    assert w.history.index == entries + 1 and w.isWindowModified()  # one entry for all of them
    assert log.item(log.rowCount() - 1, 2).text() == "3 unità aggiunte da prova: area 1.650 m2"
    w.undo()
    assert len(w.units) == drawn and len(w.ortho_view.features("Unita' stratigrafiche")) == drawn
    w.redo()
    assert len(w.units) == drawn + 3
    assert context.add_units([]) == 0 and w.history.index == entries + 1

    # no orthophoto or DEM shown yet: the units kept, and drawn with the raster
    w.ortho_view.clear_raster()
    assert context.add_units([box(x0 + 6, y0 + 6, x0 + 7, y0 + 7), box(x0 + 8, y0 + 6, x0 + 9, y0 + 7)]) == 2
    assert len(w.units) == drawn + 5 and w.history.index == entries + 2
    w._load_raster()
    assert len(w.ortho_view.features("Unita' stratigrafiche")) == drawn + 6

    # another chunk: into its drawings, back when it becomes the active one
    other = w.doc.add_chunk("Saggio 2")
    assert context.add_units([box(x0, y0, x0 + 2, y0 + 1)], tipo="USM", chunk=other) == 1
    assert len(w.units) == drawn + 5
    stored = other.meta["drawings"]["units"]
    assert len(stored) == 1 and stored[0]["tipo"] == "USM" and stored[0]["sito"] == "Saggio"
    context.activate_chunk(other)
    assert [u.tipo for u in w.units] == ["USM"] and w.units[0].geometry.area == pytest.approx(2.0)

    failed = []
    worker = threading.Thread(target=lambda: failed.append(_raises(context.add_units, [box(0, 0, 1, 1)])))
    worker.start()
    worker.join()
    assert failed == [RuntimeError]
    with pytest.raises(ValueError):
        context.add_units([box(0, 0, 1, 1)], chunk=ppg.Document().add_chunk("di un altro progetto"))
    with pytest.raises(ValueError):
        context.add_units([box(0, 0, 1, 1), box(1, 1, 2, 2)], attributes=[{}])  # one dict per polygon
    context.release()

    # the command line: into the drawings of the chunk, saved by the plugin, read by the window
    doc = ppg.Document()
    ch = doc.add_chunk("rilievo")
    doc.meta["sito"] = "Altro scavo"
    doc.save(tmp_path / "riga.ppg")
    line = Context("prova", tmp_path / "dati")
    assert line.add_units([[(0, 0), (4, 0), (4, 1), (0, 1)]], attributes={"us": "3"}, chunk=ch) == 1
    doc.save()
    again = ppg.Document.open(tmp_path / "riga.ppg")
    units = [UnitPolygon.from_dict(d) for d in again.chunk.meta["drawings"]["units"]]
    assert [(u.us, u.sito, round(u.geometry.area, 3)) for u in units] == [("3", "Altro scavo", 4.0)]


def _raises(function, *args):
    try:
        function(*args)
    except Exception as exc:  # noqa: BLE001 - its kind is the result
        return type(exc)
    return None
