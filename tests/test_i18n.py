"""English interface: the catalog, tr(), the messages of the engine and the windows."""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from pyarchinit_photogrammetry.gui import i18n  # noqa: E402

FIELD = re.compile(r"\{\d+[^{}]*\}|%[sd]")


@pytest.fixture
def english():
    i18n.set_current("en")
    yield
    i18n.set_current("it")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_every_text_of_the_code_has_its_translation():
    cat = i18n.catalog()
    missing = []
    for path in sorted(Path(i18n.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr" and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                    and node.args[0].value not in cat):
                missing.append(f"{path.name}:{node.lineno} {node.args[0].value!r}")
    assert not missing, "\n".join(missing)
    for it, en in cat.items():  # the values go into the same places
        assert sorted(set(FIELD.findall(it))) == sorted(set(FIELD.findall(en))), it
        assert en.strip(), it


def test_tr_and_numbers(english):
    assert i18n.tr("Salva") == "Save"
    assert i18n.tr("{0} foto aggiunte", 3) == "3 photos added"
    assert i18n.tr("Un testo che non e' nel catalogo") == "Un testo che non e' nel catalogo"
    assert i18n.number(1234.5, 1) == "1,234.5"
    i18n.set_current("it")
    assert i18n.tr("Salva") == "Salva"
    assert i18n.tr("{0} foto aggiunte", 3) == "3 foto aggiunte"
    assert i18n.number(1234.5, 1) == "1.234,5"


def test_messages_already_written(english):
    """The engine and the logger write their messages complete: recognised from the templates."""
    t = i18n.translate
    assert t("SAM: tassello 3 di 10") == "SAM: tile 3 of 10"
    assert t("PyArchInit connesso: Saggio dimostrativo") == "PyArchInit connected: Saggio dimostrativo"
    assert t("Reading photos") == "Reading photos"  # the engine in English stays as it is
    assert t("Pronto\nSalva") == "Ready\nSave"
    assert t("12 maschere dal canale alfa.") == "12 masks from the alpha channel."
    i18n.set_current("it")
    assert t("SAM: tassello 3 di 10") == "SAM: tassello 3 di 10"


def test_language_choice(monkeypatch, tmp_path, qapp):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    monkeypatch.delenv("PPG_LANG", raising=False)
    try:
        i18n.save_language("en")
        i18n.set_current(None)
        assert i18n.current() == "en"
        import ppg_launcher

        assert ppg_launcher.english()
        monkeypatch.setenv("PPG_LANG", "it")  # the variable wins
        i18n.set_current(None)
        assert i18n.current() == "it" and not ppg_launcher.english()
    finally:
        i18n.set_current("it")


def test_window_in_english(qapp, tmp_path, english):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import (QAbstractButton, QComboBox, QLabel, QMenu, QTabWidget, QTreeWidget,
                                   QWidget)

    from pyarchinit_photogrammetry.core.document import Document
    from pyarchinit_photogrammetry.gui.dialogs import QUALITY, Field, ParamDialog

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    assert i18n.install(qapp)
    cat = i18n.catalog()
    try:
        w = MainWindow(Document(), autoconnect=False, check_updates=False)
        w.runner.show_dialog = False
        w.show()
        qapp.processEvents()
        titles = [a.text() for a in w.menuBar().actions()]
        assert i18n.tr("&Modifica") in titles and i18n.tr("&Strumenti") in titles
        left = [a.text() for m in w.menuBar().findChildren(QMenu) for a in m.actions() if a.text() in cat]
        assert not left, left
        for wd in w.findChildren(QWidget):
            if not wd.isVisible():
                continue
            texts = [wd.toolTip()]
            if isinstance(wd, (QLabel, QAbstractButton)):
                texts.append(wd.text())
            if isinstance(wd, QTabWidget):
                texts += [wd.tabText(i) for i in range(wd.count())]
            if isinstance(wd, QTreeWidget):
                texts += [wd.headerItem().text(c) for c in range(wd.columnCount())]
            # the panes behind the tab in front are open too (docks out of sight): their texts are checked;
            # one the same in both languages ("Auto") is not left in Italian
            left += [t for t in texts if t and t in cat and cat[t] != t]
        assert not left, left
        assert w.language_actions["en"].isChecked()
        # status bar and log: also the messages written in Italian by the engine and the logger
        w.statusBar().showMessage("Pronto")
        assert w.statusBar().currentMessage() == "Ready"
        w.log_pane.info("SAM: tassello 1 di 4")
        assert w.log_pane.entries[-1][2] == "SAM: tile 1 of 4"
        # a dialog of fields: labels and choices in English, the values unchanged
        d = ParamDialog(i18n.tr("Costruisci DEM"), [Field("quality", "Qualita'", "high", choices=QUALITY)], w)
        d.show()
        qapp.processEvents()
        combo = d.findChildren(QComboBox)[0]
        assert [combo.itemText(i) for i in range(combo.count())] == [i18n.tr(lab) for _, lab in QUALITY]
        assert d.values()["quality"] == "high"
        from PySide6.QtWidgets import QDoubleSpinBox

        spin = QDoubleSpinBox(w)
        spin.setValue(2.5)
        assert spin.text() == "2.50"  # numbers written the English way, whatever the system
        assert "Quality" in [lab.text() for lab in d.findChildren(QLabel)]
        d.close()
        # a combo box whose text is the value (US types, relationships) is left alone
        data = QComboBox(w)
        data.addItems(["Taglia", "Copre"])
        data.show()
        qapp.processEvents()
        assert data.itemText(0) == "Taglia"
        w.setWindowModified(False)
        w.close()
    finally:
        i18n.uninstall(qapp)
