"""Plugins in the tool rails (since 1.7.4): ``context.add_rail_group`` puts a group of the plugin's actions at the end
of the 2D or 3D rail, gone with ``release()``; without it, every plugin with actions is reachable from the **Plugin**
group at the end of the 2D rail (a sub-menu per plugin, the same actions as the menu Plugin). An action added after
the window was built goes in the menu Plugin and the search of the commands finds it."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

PLUGIN_GROUP = "plugin:"  # the key of the Plugin group of the 2D rail


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _settle(qapp) -> None:
    from PySide6.QtCore import QCoreApplication, QEvent

    for _ in range(3):
        qapp.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _write_plugin(folder, name: str, module: str, body: str) -> None:
    dist = folder / name / f"{name.replace('-', '_')}-0.1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1.0\nSummary: rail test\n")
    (dist / "entry_points.txt").write_text(f"[pyarchinit_photogrammetry.plugins]\n{name} = {module}:RailPlugin\n")
    (dist / "WHEEL").write_text("Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
    (folder / name / module).mkdir()
    (folder / name / module / "__init__.py").write_text(body)


GROUPED = '''
from PySide6.QtGui import QAction

from pyarchinit_photogrammetry.plugins import Plugin


class RailPlugin(Plugin):
    title = "Rilievo di prova"

    def activate(self, context):
        self.context, self.calls = context, []
        one = context.add_action("Calcola", lambda: self.calls.append("calcola"))
        two = context.add_action("Esporta tutto", lambda: self.calls.append("esporta"))
        self.extra = QAction("Solo nel 3D", context.window)
        self.extra.triggered.connect(lambda: self.calls.append("3d"))
        context.add_rail_group("tools", "Rilievo", "relief", [one, two])
        context.add_rail_group("tools3d", "Rilievo 3D", "plugin", [one, self.extra], view="3d")
'''

PLAIN = '''
from pyarchinit_photogrammetry.plugins import Plugin


class RailPlugin(Plugin):
    title = "Semplice"

    def activate(self, context):
        self.context, self.calls = context, []
        context.add_action("Saluta", lambda: self.calls.append("saluta"))
        context.add_action("Conta", lambda: self.calls.append("conta"))
'''


@pytest.fixture
def make_window(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    made = []

    def make(plugins: dict[str, str]):
        folder = tmp_path / f"plugins{len(made)}"
        folder.mkdir()
        for i, (name, body) in enumerate(plugins.items()):
            _write_plugin(folder, name, f"ppg_rail_test_{name.replace('-', '_')}_{len(made)}_{i}", body)
        monkeypatch.setenv("PPG_PLUGINS_DIR", str(folder))
        w = MainWindow(ppg.Document(), autoconnect=False, check_updates=False)
        w.runner.show_dialog = False
        w.resize(1400, 900)
        w.show()
        _settle(qapp)
        made.append(w)
        return w

    yield make
    for w in made:
        w.setWindowModified(False)
        w.close()


def _texts(group) -> list[str]:
    return [w.text() for w in group.widgets]


def test_a_plugin_adds_its_groups_to_the_2d_and_3d_rails(make_window, qapp):
    w = make_window({"rilievo": GROUPED})
    plugin = w.plugins.get("rilievo").plugin
    rail2, rail3 = w.raster_rail, w.model_view.rail
    key2, key3 = "plugin:rilievo:tools", "plugin:rilievo:tools3d"
    assert list(rail2.groups)[-1] == key2 and key2 not in rail3.groups  # after the program's own groups
    assert list(rail3.groups)[-1] == key3 and key3 not in rail2.groups
    assert PLUGIN_GROUP not in rail2.groups  # its actions are on the rail already: no Plugin group for it
    group = rail2.groups[key2]
    assert group.title == "Rilievo" and not group.button.icon().isNull()
    w.show_view(w.ortho_view)
    rail2.open(key2)
    _settle(qapp)
    assert rail2.flyout.isVisible() and rail2.flyout.title.text() == "Rilievo"
    assert _texts(group) == ["Calcola", "Esporta tutto"]
    group.widgets[1].click()  # the same action as the menu Plugin
    assert plugin.calls == ["esporta"]
    menu_titles = [a.text() for sub in w.plugin_menu.actions() if sub.menu() for a in sub.menu().actions()]
    assert menu_titles == ["Calcola", "Esporta tutto"]
    assert rail2.groups[key2].actions[0] in w.plugin_menu.actions()[0].menu().actions()
    assert _texts(rail3.groups[key3]) == ["Calcola", "Solo nel 3D"]
    rail3.groups[key3].widgets[1].click()
    assert plugin.calls == ["esporta", "3d"]
    w.plugins.unload()  # deactivated: release() takes its groups away
    _settle(qapp)
    assert key2 not in rail2.groups and key3 not in rail3.groups and not rail2.is_open()
    assert not [k for k in rail2.groups if k.startswith("plugin")]
    assert w.plugin_menu is None or not [a for a in w.plugin_menu.actions() if a.menu() and a.menu().actions()]


def test_plugins_without_a_group_are_in_the_plugin_group(make_window, qapp):
    w = make_window({"rilievo": GROUPED, "semplice": PLAIN})
    rail = w.raster_rail
    assert list(rail.groups)[-1] == PLUGIN_GROUP
    group = rail.groups[PLUGIN_GROUP]
    assert group.title == "Plugin" and _texts(group) == ["Semplice"]  # one entry per plugin, not the grouped one
    menu = group.widgets[0].menu()
    assert [a.text() for a in menu.actions()] == ["Saluta", "Conta"]
    plain = w.plugins.get("semplice")
    menu_actions = next(a.menu() for a in w.plugin_menu.actions() if a.text() == "Semplice").actions()
    assert menu.actions() == menu_actions  # the same QActions as the menu Plugin
    menu.actions()[1].trigger()
    assert plain.plugin.calls == ["conta"]
    plain.context.release()  # the plugin goes: so does its entry, and the group with it
    _settle(qapp)
    assert PLUGIN_GROUP not in rail.groups


def test_no_plugins_no_plugin_group(make_window, qapp):
    w = make_window({})
    assert not [k for k in w.raster_rail.groups if k.startswith("plugin")]
    assert not [k for k in w.model_view.rail.groups if k.startswith("plugin")]


def test_an_action_added_later_is_in_the_menu_the_rail_and_the_search(make_window, qapp):
    w = make_window({"semplice": PLAIN})
    plain = w.plugins.get("semplice")
    search = w.command_search
    search.setFocus()
    _settle(qapp)
    assert not search.matches("Misura il fosso")
    plain.context.add_action("Misura il fosso", lambda: plain.plugin.calls.append("fosso"))
    _settle(qapp)
    found = search.matches("misura il fosso")
    assert found and found[0].startswith("Misura il fosso")  # without leaving and entering the field again
    assert search.run(found[0])
    _settle(qapp)
    assert plain.plugin.calls == ["fosso"]
    entries = [w_.text() for w_ in w.raster_rail.groups[PLUGIN_GROUP].widgets]
    assert entries == ["Semplice"]
    assert [a.text() for a in w.raster_rail.groups[PLUGIN_GROUP].widgets[0].menu().actions()] == \
        ["Saluta", "Conta", "Misura il fosso"]
    group = plain.context.add_rail_group("later", "Dopo", "plugin", plain.context.contributions.actions)
    _settle(qapp)
    assert group.key == "later" and "plugin:semplice:later" in w.raster_rail.groups
    assert PLUGIN_GROUP not in w.raster_rail.groups  # now it has a group of its own


def test_add_rail_group_in_the_command_line_and_bad_view(tmp_path):
    from pyarchinit_photogrammetry.plugins.api import Context

    ctx = Context("riga", tmp_path / "riga")
    action = ctx.add_action("Uno", lambda: None)
    group = ctx.add_rail_group("g", "Gruppo", "plugin", [action])
    assert group.actions == [action] and group.view == "2d"  # kept, nothing to show
    with pytest.raises(ValueError):
        ctx.add_rail_group("h", "Altro", "plugin", [action], view="4d")
    with pytest.raises(TypeError):
        ctx.add_rail_group("h", "Altro", "plugin", [action, 3])
    assert [g.key for g in ctx.contributions.rail_groups] == ["g"]
    assert ctx.add_rail_group("s", "Con linea", "plugin", [action, None, action]).actions[1] is None
    ctx.release()
    assert not ctx.contributions.rail_groups


SEPARATED = '''
from pyarchinit_photogrammetry.plugins import Plugin


class RailPlugin(Plugin):
    title = "Con separatore"

    def activate(self, context):
        self.context = context
        one = context.add_action("Uno", lambda: None)
        two = context.add_action("Due", lambda: None)
        context.add_rail_group("tools", "Separati", "plugin", [one, None, two])
'''

BROKEN = '''
from pyarchinit_photogrammetry.plugins import Plugin


class RailPlugin(Plugin):
    title = "Gruppo rotto"

    def activate(self, context):
        self.context, self.refused = context, None
        one = context.add_action("Uno", lambda: None)
        try:
            context.add_rail_group("bad", "Rotto", "plugin", [one, "non un'azione"])
        except TypeError as exc:
            self.refused = str(exc)
'''

MENUS = '''
from pyarchinit_photogrammetry.plugins import Plugin


class RailPlugin(Plugin):
    title = "Con menu"

    def activate(self, context):
        self.context = context
        context.add_action("Libera", lambda: None)
        context.add_action("Primo", lambda: None, menu="Analisi")
        context.add_action("Secondo", lambda: None, menu="Analisi")
        context.add_action("Esporta", lambda: None, menu="Uscite")
'''


def test_a_separator_and_a_bad_item_in_a_rail_group(make_window, qapp, caplog):
    from pyarchinit_photogrammetry.plugins.api import RailGroup

    w = make_window({"a-sep": SEPARATED, "b-rotto": BROKEN, "c-semplice": PLAIN})
    group = w.raster_rail.groups["plugin:a-sep:tools"]
    from PySide6.QtWidgets import QFrame

    assert _texts(group) == ["Uno", "Due"] and group.page.findChildren(QFrame, "railSeparator")  # None: a line
    broken = w.plugins.get("b-rotto")
    assert broken.active and broken.plugin.refused and "non un'azione" in broken.plugin.refused  # told at once
    assert not broken.context.contributions.rail_groups  # nothing kept of the refused group
    # a group that went wrong all the same (put in by hand, as an older program would have kept it): the others stay
    forced = RailGroup("forced", "Forzato", "plugin", [object()], "2d", "b-rotto")
    broken.context.contributions.rail_groups.append(forced)
    plain = w.plugins.get("c-semplice")
    with caplog.at_level("WARNING"):
        plain.context.add_action("Ancora", lambda: None)  # another plugin's call: not broken by the bad group
    _settle(qapp)
    assert "plugin:a-sep:tools" in w.raster_rail.groups and "plugin:b-rotto:forced" not in w.raster_rail.groups
    assert any("b-rotto" in m for m in caplog.messages)
    entries = {b.text(): [a.text() for a in b.menu().actions()] for b in w.raster_rail.groups[PLUGIN_GROUP].widgets}
    assert "Ancora" in entries["Semplice"] and "Gruppo rotto" in entries  # its group failed: reachable here
    w.plugins.unload()  # releasing does not raise either
    _settle(qapp)


def test_a_bad_group_at_start_never_stops_the_window(make_window, qapp, monkeypatch, caplog):
    """A group that cannot be shown when the window starts: written in the log, the window opens with the others."""
    from pyarchinit_photogrammetry.gui import main_window as mw

    real = mw.MainWindow._plugin_qaction

    def flaky(self, action):
        if getattr(action, "title", "") == "Due":
            raise RuntimeError("rotto apposta")
        return real(self, action)

    monkeypatch.setattr(mw.MainWindow, "_plugin_qaction", flaky)
    with caplog.at_level("WARNING"):
        w = make_window({"a-sep": SEPARATED, "c-semplice": PLAIN})
    assert "plugin:a-sep:tools" not in w.raster_rail.groups
    assert PLUGIN_GROUP in w.raster_rail.groups and any("rotto apposta" in m for m in caplog.messages)


def test_the_plugin_group_splits_the_actions_by_their_menu(make_window, qapp):
    w = make_window({"menu": MENUS})
    menu = w.raster_rail.groups[PLUGIN_GROUP].widgets[0].menu()
    flat = [a.text() for a in menu.actions() if a.menu() is None and not a.isSeparator()]
    subs = {a.text(): [x.text() for x in a.menu().actions()] for a in menu.actions() if a.menu() is not None}
    assert flat == ["Libera"] and subs == {"Analisi": ["Primo", "Secondo"], "Uscite": ["Esporta"]}
    top = {a.text(): [x.text() for x in a.menu().actions()] for a in w.plugin_menu.actions() if a.menu()}
    assert top["Analisi"] == ["Primo", "Secondo"] and top["Uscite"] == ["Esporta"]  # as the menu Plugin does


def test_the_empty_plugin_menu_goes_and_rail_only_actions_go_with_the_plugin(make_window, qapp):
    from PySide6.QtGui import QAction

    from pyarchinit_photogrammetry.plugins.api import Action

    w = make_window({"semplice": PLAIN})
    plain = w.plugins.get("semplice")
    only = Action("Solo nella barra", lambda: None, plugin="semplice")  # never given to add_action
    plain.context.add_rail_group("g", "G", "plugin", [only])
    _settle(qapp)
    qa = only.qaction
    assert isinstance(qa, QAction) and qa not in [x for a in w.plugin_menu.actions() if a.menu()
                                                  for x in a.menu().actions()]
    assert w.plugin_menu.menuAction().isVisible()
    w.plugins.unload()
    _settle(qapp)
    assert not w.plugin_menu.menuAction().isVisible()  # no plugin action left: no empty menu Plugin
    assert only.qaction is None
