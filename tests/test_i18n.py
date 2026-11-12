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
        # an entry the same in both languages ("Layer") is translated as itself, as for the widgets below
        left = [a.text() for m in w.menuBar().findChildren(QMenu) for a in m.actions()
                if a.text() in cat and cat[a.text()] != a.text()]
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
        d = ParamDialog(i18n.tr("Costruisci DEM"), [Field("quality", "Qualità", "high", choices=QUALITY)], w)
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


# ------------------------------------------------------------------ the messages of the GNSS engine in Italian
GNSS_MODULES = ("ppk", "rinex", "shots")
ENGLISH = re.compile(r"\b(the|of|is|are|not|and|or|by|with|from|to|its|their|them|it|one|this|an|on|as|was|were|has|"
                     r"have|photos?|shots?|times?)\b")
RUN_NOUNS = (("the rover", "the base"), ("your base", "the station"))  # ppk.RUN_ROLES as ppk.ROLES names them
ANTENNA_NOUNS = ("the base", "the station", "your base", "your base")  # the roles of ppk._ANTENNA_EFFECTS
#: the values that are words of the engine, by the expression that writes them in a message (the others are names and
#: numbers); the lists go together, one case after the other
WORDS = {
    "rover_noun": [pair[0] for pair in RUN_NOUNS], "ROLES[roles[0]]": [pair[0] for pair in RUN_NOUNS],
    "base_noun": [pair[1] for pair in RUN_NOUNS], "ROLES[roles[1]]": [pair[1] for pair in RUN_NOUNS],
    "whose": ["of the base or of the drone", "of your base or of the station"],
    "ppk:noun": ["the rover", "the base", "your base", "the station"],
    "shots:noun": ["MRK", "source"],
    "who": [f"{noun} K9.obs" for noun in ("the rover", "the base", "your base", "the station")],
    "what": ["PPK", "PPP"],
    "by": ["number", "order"],
    "more": ["", " (and 2 more)"],
    "where": ["", " of system G"],
    "shots:why": ["the photos have no date"],
    "message or 'RTKLIB wrote no positions'": ["RTKLIB wrote no positions", "K0"],
    "message or 'no message'": ["no message", "K0"],
    "'; '.join(notes)": ["the conversion of the rover K9.ubx is no longer in the cache of conversions; the file of the "
                         "rover K8.obs is no longer where it was"],
    "ppk:where": ["the rover K9.obs", "solution_events.pos"],
}
#: texts of the three modules that are not messages for the user, and why
NOT_SHOWN = {
    "ppk": {
        "# RTKLIB options of a PPK run (pyarchinit-photogrammetry)\n": "the first line of the options file of RTKLIB",
        "Time mark is not valid": "what RTKLIB writes in its files, read back",
        "a {0}, not a dict": "an argument of the Python interface that the windows never give",
        "the roles of the rover and the base of a run are {0}, not {1}": "an argument of the Python interface",
        "the antenna of a receiver in the role {0}: the roles are {1}": "an argument of the Python interface",
    },
    "rinex": {
        "time mark is not valid": "what RTKLIB writes in its files, read back",
        "ecef_to_llh takes the three coordinates x, y, z": "an argument of the Python interface",
        "llh_to_ecef takes the three numbers latitude, longitude, height": "an argument of the Python interface",
    },
    "shots": {
        "kind must be one of {0}, not {1}": "an argument of the Python interface that the windows never give",
        "by must be one of {0}, not {1}": "an argument of the Python interface that the windows never give",
    },
}
#: texts that are a piece of a message: checked inside it, in GNSS_WHOLE or as a value of WORDS
PIECES = {
    "ppk": (" (a backward solution: RTKLIB places the marks from the epoch after them, so with a drone that speeds up "
            "or turns some millimetres are expected)", " (and {0} more)",
            "of the antenna {0} of {1} is not applied",
            "of the antenna of {0} is not applied (its RINEX header names no antenna type)",
            ". The calibrations of the IGS (igs20.atx, 62 MB) are downloaded once by the PPK window, with its option "
            "for the antenna of the base, or by `ppg gnss atx --download`", "approx_time, as given",
            "the middle of the time of {0}", "the rover {0}",
            "{0} (the approximate time of the log, {1} GPS time, was taken from {2}: the log has to be of the same "
            "day)", "the phase-centre offset {0}: {1}, and {2}, more than 10 cm with some choke-ring antennas",
            "the phase-centre offset {0}: no antenna calibrations were given to RTKLIB, so {1}, more than 10 cm with "
            "some choke-ring antennas",
            "the phase-centre offset {0}: its entry {1} in {2} has no offsets of L1 (G01), so RTKLIB has no "
            "calibration for it, and {3}, more than 10 cm with some choke-ring antennas",
            "the antenna {0} of {1} is not in {2} with its radome: RTKLIB used the calibration of {3}, the same "
            "antenna type with another radome, so {4}",
            "the antenna {0} of {1} is not in {2} as its RINEX header writes it: RTKLIB used the calibration of {3}, "
            "the nearest name it found, so {4}, and more if it is another antenna",
            "the antenna calibrations of {0} are left out of the run: for the antenna {1} of {2} RTKLIB's search "
            "finds {3}, the antenna of a satellite, whose offsets are no receiver's"),
    "rinex": ("{{name}}: not a RINEX file", " of system {0}"),
    "shots": ("{0} ({1} photos)", "the times alone cannot pair the photos ({0})"),
}


def _antenna_messages() -> list[str]:
    """What ppk.antenna_warning writes, for every role and every case of the calibration."""
    from pyarchinit_photogrammetry.gnss import ppk

    out = []
    for role in ppk._ANTENNA_EFFECTS:
        effect, other = ppk._ANTENNA_EFFECTS[role]
        who = f"{ppk.ROLES[role]} K9"
        for which in (f"of the antenna TRM57971.00 NONE of {who} is not applied",
                      f"of the antenna of {who} is not applied (its RINEX header names no antenna type)"):
            text = (f"the phase-centre offset {which}: no antenna calibrations were given to RTKLIB, so {effect}, more "
                    "than 10 cm with some choke-ring antennas")
            if role == "base":
                text += (". The calibrations of the IGS (igs20.atx, 62 MB) are downloaded once by the PPK window, with "
                         "its option for the antenna of the base, or by `ppg gnss atx --download`")
            out.append(text)
            out.append(f"the phase-centre offset {which}: its entry TRM57971.00 TZGD in igs20.atx has no offsets of L1 "
                       f"(G01), so RTKLIB has no calibration for it, and {effect}, more than 10 cm with some "
                       "choke-ring antennas")
            for why in ("the antenna is not in igs20.atx, so RTKLIB has no calibration for it",
                        "there is no antenna to look for in igs20.atx, so RTKLIB has no calibration for it"):
                out.append(f"the phase-centre offset {which}: {why}, and {effect}, more than 10 cm with some "
                           "choke-ring antennas")
        out.append(f"the antenna TRM57971.00 TZGD of {who} is not in igs20.atx with its radome: RTKLIB used the "
                   f"calibration of TRM57971.00 NONE, the same antenna type with another radome, so {other}")
        out.append(f"the antenna TRM57971 of {who} is not in igs20.atx as its RINEX header writes it: RTKLIB used the "
                   f"calibration of TRM57971.00 NONE, the nearest name it found, so {other}, and more if it is "
                   "another antenna")
        out.append(f"the antenna calibrations of igs20.atx are left out of the run: for the antenna BLOCK IIA of {who} "
                   "RTKLIB's search finds BLOCK IIA G01, the antenna of a satellite, whose offsets are no receiver's")
    return out


#: the messages made of several pieces, whole, as the engine writes them
#: why the times alone could not pair the photos (shots.pair, what its by-time pairing refused, without the name)
NOT_CONFIRMED = (
    ("number", "the times of the photos fit the times of the shots on no clock of the camera within 120 s of a time "
               "zone: the photos cannot be paired by time"),
    ("order", "only 3 of 40 photos have a shot at their time, on the clock of the camera that pairs the most (+2:00:00 "
              "from UTC): the photos cannot be paired by time"),
    ("number", "the times of the photos fit the shots best on a clock of the camera +2:07:00 from UTC, 0:07:00 from "
               "the nearest time zone (more than 120 s): the photos cannot be paired by time"),
    ("order", "the shots came every 0.7 s (median): photos timed to the whole second cannot be paired with them by "
              "time; pair them by number"),
    ("number", "only 3 of 40 photos were taken at the time of their shot (clock of the camera +2:00:00 from UTC): the "
               "MRK is of another flight, or the folder holds the photos of other flights too"),
    ("order", "the times of the photos fit more than one way of pairing them with the shots, on the clocks of the "
              "camera +2:00:00 (41 photos), +2:00:07 (40 photos) from UTC: the photos cannot be paired by time, and "
              "paired by number or by order they are not checked by the times either"),
    ("number", "the times of the photos fit the shots on more than one clock of the camera, +2:00:00, +3:00:00 from "
               "UTC, as well as each other: the photos cannot be paired by time, and paired by number or by order "
               "they are not checked by the times either"),
)
GNSS_WHOLE = (
    "K9.obs: not a RINEX file",
    "K9.obs has other observation types than K8.obs of system G (C1C L1C and C1C L1C C2W): files with other "
    "observation types are not joined",
    "3 time marks of solution_events.pos are more than 20 mm from where the solution solution.pos puts the antenna at "
    "the same time: up to 31 mm, across or in height (a backward solution: RTKLIB places the marks from the epoch "
    "after them, so with a drone that speeds up or turns some millimetres are expected)",
    "the navigation file K7.nav beside the rover K9.obs or the base K8.obs cannot be read (and 2 more): K7.nav is cut "
    "short or damaged (K5). Download it again, or add another navigation file of the base or of the drone (.nav, "
    ".##n, .##g, .##p or *_MN.rnx)",
    "K7.rtcm3: RTCM 3 carries the time of the week only, an approximate time of the log (its day) is needed (the "
    "approximate time of the log, 2025-06-01 10:00:00 GPS time, was taken from the middle of the time of the base "
    "K8.obs, K6.obs: the log has to be of the same day)",
    "K7.rtcm3: RTCM 3 carries the time of the week only, an approximate time of the log (its day) is needed (the "
    "approximate time of the log, 2025-06-01 10:00:00 GPS time, was taken from the middle of the time of your base "
    "K8.obs: the log has to be of the same day)",
    "K7.rtcm3: RTCM 3 carries the time of the week only, an approximate time of the log (its day) is needed (the "
    "approximate time of the log, 2025-06-01 10:00:00 GPS time, was taken from approx_time, as given: the log has to "
    "be of the same day)",
    "flight.MRK: the times of the photos fit more than one way of pairing them with the shots, on the clocks of the "
    "camera +2:00:00 (41 photos), +2:00:07 (40 photos) from UTC: the photos cannot be paired by time, and paired by "
    "number or by order they are not checked by the times either",
    *(f"pairing by {by} not confirmed by the times: the times alone cannot pair the photos ({why}); a shift by whole "
      "shots cannot be excluded, check the positions on a control point" for by, why in NOT_CONFIRMED),
)


def _templates(module: str) -> list[tuple[int, str, list[str]]]:
    """(line, text with ``{n}`` for its values, the expressions of the values) of every text of a module of the
    engine that is not a docstring, each choice between two words of a value (``'s' if n > 1 else ''``) written out."""
    from pyarchinit_photogrammetry import gnss

    tree = ast.parse((Path(gnss.__file__).parent / f"{module}.py").read_text(encoding="utf-8"))
    skipped = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and ast.get_docstring(node) is not None:
            skipped.add(id(node.body[0].value))
        if isinstance(node, ast.JoinedStr):
            skipped.update(id(child) for child in ast.walk(node) if child is not node)
    out = []
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            variants = [(node.value.replace("{", "{{").replace("}", "}}"), [])]
        elif isinstance(node, ast.JoinedStr):
            choices = sorted({ast.unparse(v.value.test) for v in node.values if _choice(v)})
            variants = []
            for case in range(2 ** len(choices)):
                taken = {test: bool(case >> i & 1) for i, test in enumerate(choices)}
                text, values = "", []
                for v in node.values:
                    if isinstance(v, ast.Constant):
                        text += v.value.replace("{", "{{").replace("}", "}}")
                    elif _choice(v):
                        word = v.value.body if taken[ast.unparse(v.value.test)] else v.value.orelse
                        text += word.value
                    else:
                        source = ast.unparse(v.value)
                        if source not in values:
                            values.append(source)
                        text += "{%d}" % values.index(source)
                variants.append((text, values))
        else:
            continue
        for text, values in variants:
            words = re.sub(r"\{\d+\}", "", text)
            if re.search(r"[a-z]{3}", words) and " " in words.strip() and not words.startswith(("^", "%")):
                out.append((node.lineno, text, values))
    return out


def _choice(value) -> bool:
    return (isinstance(value, ast.FormattedValue) and isinstance(value.value, ast.IfExp)
            and all(isinstance(word, ast.Constant) and isinstance(word.value, str)
                    for word in (value.value.body, value.value.orelse)))


def _cases(module: str, text: str, values: list[str]) -> list[tuple[str, list[str]]]:
    """A template as the engine would write it, in every case of its words: (message, the names and numbers in it)."""
    words = [WORDS.get(f"{module}:{source}", WORDS.get(source)) for source in values]
    out = []
    for case in range(max([len(w) for w in words if w] or [1])):
        filled = [w[case % len(w)] if w else f"K{i}" for i, w in enumerate(words)]
        message = re.sub(r"\{(\d+)\}", lambda m: filled[int(m.group(1))], text).replace("{{", "{").replace("}}", "}")
        out.append((message, [f"K{i}" for i, w in enumerate(words) if not w]))
    return out


def _left_in_english(message: str, kept: list[str] = ()) -> str | None:
    shown = i18n.engine(message)
    plain = re.sub(r"\([^()]*=[^()]*\)|`[^`]*`|\bno memory\b", "", shown)  # options and commands as they are typed
    words = sorted(set(ENGLISH.findall(plain)))
    lost = [value for value in kept if value not in shown]
    if shown == message or words or lost:
        return f"{message!r} -> {shown!r} {words} {lost}"
    return None


def test_every_message_of_the_gnss_engine_has_its_italian():
    """Every text gnss/ppk.py, rinex.py and shots.py write for the user - what they refuse, warn of and note - is in
    the section ``engine`` of the catalog and comes out in Italian whole, with its names and numbers: read from the
    code, so a new message without its Italian fails here. What is not for the user is listed with its reason."""
    i18n.set_current("it")
    left, seen = [], 0
    for module in GNSS_MODULES:
        texts = {text for _line, text, _values in _templates(module)}
        unknown = (set(NOT_SHOWN[module]) | set(PIECES[module])) - texts
        assert not unknown, f"{module}: no longer in the code: {sorted(unknown)}"
        for line, text, values in _templates(module):
            if text in NOT_SHOWN[module] or text in PIECES[module]:
                continue
            seen += 1
            for message, kept in _cases(module, text, values):
                problem = _left_in_english(message, kept)
                if problem:
                    left.append(f"{module}.py:{line} {problem}")
    for message in (*GNSS_WHOLE, *_antenna_messages()):
        problem = _left_in_english(message)
        if problem:
            left.append(problem)
    assert not left, "\n".join(left)
    assert seen >= 130, seen
