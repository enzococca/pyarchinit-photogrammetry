"""Language of the interface: Italian, as the code is written, or English.

``translations/en.json`` gives the texts of the interface in English:

* ``texts``: fixed texts (menus, buttons, labels, tooltips, messages);
* ``formats``: messages with values as :func:`tr` receives them (``"{0} foto aggiunte"``). At
  display time they also recognise the messages already filled in, the ones that come from the
  engine and the log (:func:`translate`).

:func:`tr` translates where the code builds a text; :func:`install` (English only) adds an event
filter that translates every widget, menu and action when it is shown, so the texts written
directly into widgets, the fields of the dialogs and the menus need nothing else. Combo boxes,
lists and trees are translated only where an item keeps its value apart (``itemData`` /
``UserRole``): elsewhere the text is the value (US types, relationships of PyArchInit) and stays
as it is; a widget with the property ``ppg_no_translate`` is left alone.

The language: ``PPG_LANG`` (``it`` or ``en``), else the choice made in *Vista > Lingua*
(setting ``language``), else the language of the system - Italian on an Italian system, English
elsewhere. A change takes effect at the next start.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

LANGUAGES = {"it": "Italiano", "en": "English"}
CATALOG = Path(__file__).with_name("translations") / "en.json"
NO_TRANSLATE = "ppg_no_translate"  # widget property: leave its texts alone
_FIELD = re.compile(r"\{(\d+)(?:![rsa])?(?::[^{}]*)?\}")
_PERCENT = re.compile(r"%[sd]")

_current: str | None = None


def system_language() -> str:
    """``it`` when the first language of the system is Italian, else ``en``."""
    try:
        from PySide6.QtCore import QLocale

        langs = QLocale.system().uiLanguages()
    except Exception:  # pragma: no cover - Qt missing
        langs = []
    first = (langs[0] if langs else os.environ.get("LANG", "")).lower()
    return "it" if first.startswith("it") else "en"


def saved_language() -> str | None:
    """The language chosen in *Vista > Lingua*, if any."""
    from . import app_settings

    code = str(app_settings().value("language", "") or "")
    return code if code in LANGUAGES else None


def save_language(code: str) -> None:
    """The language of the next starts."""
    from . import app_settings

    settings = app_settings()
    settings.setValue("language", code)
    settings.sync()


def current() -> str:
    """The language of this session (see the module docstring)."""
    global _current
    if _current is None:
        code = os.environ.get("PPG_LANG", "").strip().lower()[:2]
        if code not in LANGUAGES:
            try:
                code = saved_language()
            except Exception:  # no Qt settings here
                code = None
        _current = code if code in LANGUAGES else system_language()
    return _current


def set_current(code: str | None) -> None:
    """The language of this session (``None``: decided again); tests switch it this way."""
    global _current
    _current = code if code in LANGUAGES else None


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def catalog() -> dict[str, str]:
    """Italian text or template -> English."""
    data = _data()
    return {**data.get("texts", {}), **data.get("formats", {})}


def tr(text: str, *args, context: str | None = None) -> str:
    """``text`` in the language of the interface; with ``args`` it is a template filled with
    :meth:`str.format`: ``tr("{0} foto aggiunte", n)``. ``context`` tells apart one Italian word
    with two meanings: ``tr("Annulla", context="undo")`` (catalog key ``"undo|Annulla"``)."""
    if current() != "it":
        cat = catalog()
        text = cat.get(f"{context}|{text}", cat.get(text, text)) if context else cat.get(text, text)
    return text.format(*args) if args else text


def number(value: float, decimals: int = 0) -> str:
    """A number as the interface writes it: 1.234,56 in Italian, 1,234.56 in English."""
    text = f"{value:,.{decimals}f}"
    if current() == "it":
        text = text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return text


def decimal_comma(text: str) -> str:
    """A text with numbers formatted by Python (``0.5``) as the interface writes them: decimal
    comma in Italian, unchanged in English."""
    return text.replace(".", ",") if current() == "it" else text


def thousands_points(text: str) -> str:
    """A text with thousands separated by Python's ``{:,}`` as the interface writes them: points
    in Italian, unchanged in English."""
    return text.replace(",", ".") if current() == "it" else text


def lookup(text: str) -> str:
    """A fixed text in English (the catalog; spaces around it kept), else the text itself."""
    if not text or current() == "it":
        return text
    cat = catalog()
    hit = cat.get(text)
    if hit is not None:
        return hit
    core = text.strip()
    if core != text and core in cat:
        start = text.index(core)
        return text[:start] + cat[core] + text[start + len(core):]
    return text


def _regex(template: str):
    """The regex that recognises a filled template, and the field of each group."""
    parts, fields, pos = [], [], 0
    for m in _FIELD.finditer(template):
        parts.append(re.escape(template[pos:m.start()].replace("{{", "{").replace("}}", "}")))
        parts.append("(.+?)")
        fields.append(int(m.group(1)))
        pos = m.end()
    parts.append(re.escape(template[pos:].replace("{{", "{").replace("}}", "}")))
    return re.compile("^" + "".join(parts) + "$", re.S), fields


@lru_cache(maxsize=1)
def _patterns() -> list:
    """(regex, fields, English template) of the messages with values, the most specific first."""
    data = _data()
    items = list(data.get("formats", {}).items())
    for it, en in data.get("texts", {}).items():  # "%s" texts of the logger
        if _PERCENT.search(it):
            counter = iter(range(100))
            it_t = _PERCENT.sub(lambda _m: "{%d}" % next(counter), it.replace("{", "{{").replace("}", "}}"))
            counter = iter(range(100))
            en_t = _PERCENT.sub(lambda _m: "{%d}" % next(counter), en.replace("{", "{{").replace("}", "}}"))
            items.append((it_t, en_t))
    out = []
    for it, en in items:
        literal = _FIELD.sub("", it)
        if len(re.findall(r"[A-Za-zà-ù]", literal)) < 4:  # "{0}: {1}" would match anything
            continue
        rx, fields = _regex(it)
        out.append((len(literal), rx, fields, en))
    out.sort(key=lambda x: -x[0])
    return [x[1:] for x in out]


def translate(text: str) -> str:
    """A text to show (log, progress, messages) in English: the catalog, else a message with
    values recognised from ``formats``; line by line when it has several lines."""
    if not text or current() == "it":
        return text
    hit = lookup(text)
    if hit != text:
        return hit
    cat = catalog()
    for rx, fields, en in _patterns():
        m = rx.match(text)
        if m:
            values = {}
            for n, v in zip(fields, m.groups()):
                values.setdefault(n, cat.get(v.strip(), v) if v.strip() in cat else v)
            return _FIELD.sub(lambda f: values.get(int(f.group(1)), f.group(0)), en).replace("{{", "{").replace(
                "}}", "}")
    if "\n" in text:
        return "\n".join(translate(line) for line in text.split("\n"))
    return text


# ---------------------------------------------------------------- widgets
def _swap(old: str, setter) -> None:
    if old:
        new = lookup(old)
        if new != old:
            setter(new)


def translate_action(action) -> None:
    _swap(action.text(), action.setText)
    _swap(action.iconText(), action.setIconText)  # the short label of a tool button
    _swap(action.statusTip(), action.setStatusTip)
    _swap(action.whatsThis(), action.setWhatsThis)
    tip = action.toolTip()
    if tip and tip != action.text().replace("&", ""):
        _swap(tip, action.setToolTip)


def translate_menus(widget) -> None:
    """Every menu below ``widget`` (the menu bar of macOS is native: its menus are never shown
    as widgets, so the window translates them here once)."""
    from PySide6.QtWidgets import QMenu

    for menu in widget.findChildren(QMenu):
        _swap(menu.title(), menu.setTitle)
        for a in menu.actions():
            translate_action(a)
    for a in widget.actions():
        translate_action(a)


def _translate_items(item, columns: int) -> None:
    from PySide6.QtCore import Qt

    if item.data(0, Qt.ItemDataRole.UserRole) is not None:
        for c in range(columns):
            _swap(item.text(c), lambda t, c=c: item.setText(c, t))
            _swap(item.toolTip(c), lambda t, c=c: item.setToolTip(c, t))
    for i in range(item.childCount()):
        _translate_items(item.child(i), columns)


def translate_widget(w) -> None:
    """The texts of one widget (not of its children: each gets its own show event)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QComboBox, QDockWidget, QGroupBox, QLabel,
                                   QLineEdit, QListWidget, QMenu, QPlainTextEdit, QTableWidget, QTabWidget, QTextEdit,
                                   QToolButton, QTreeWidget)

    if w.property(NO_TRANSLATE):
        return
    if w.isWindow() or isinstance(w, QDockWidget):
        _swap(w.windowTitle(), w.setWindowTitle)
    _swap(w.toolTip(), w.setToolTip)
    _swap(w.statusTip(), w.setStatusTip)
    if isinstance(w, QLabel):
        _swap(w.text(), w.setText)
    elif isinstance(w, QAbstractButton):
        if not (isinstance(w, QToolButton) and w.defaultAction() is not None):
            _swap(w.text(), w.setText)
    elif isinstance(w, QGroupBox):
        _swap(w.title(), w.setTitle)
    elif isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit)):
        _swap(w.placeholderText(), w.setPlaceholderText)
    elif isinstance(w, QComboBox):
        for i in range(w.count()):
            if w.itemData(i) is not None:
                _swap(w.itemText(i), lambda t, i=i: w.setItemText(i, t))
    elif isinstance(w, QTabWidget):
        for i in range(w.count()):
            _swap(w.tabText(i), lambda t, i=i: w.setTabText(i, t))
            _swap(w.tabToolTip(i), lambda t, i=i: w.setTabToolTip(i, t))
    elif isinstance(w, QTreeWidget):
        head = w.headerItem()
        for c in range(w.columnCount()):
            _swap(head.text(c), lambda t, c=c: head.setText(c, t))
        for i in range(w.topLevelItemCount()):
            _translate_items(w.topLevelItem(i), w.columnCount())
    elif isinstance(w, QTableWidget):
        for c in range(w.columnCount()):
            item = w.horizontalHeaderItem(c)
            if item is not None:
                _swap(item.text(), item.setText)
                _swap(item.toolTip(), item.setToolTip)
    elif isinstance(w, QListWidget):
        for i in range(w.count()):
            item = w.item(i)
            if item.data(Qt.ItemDataRole.UserRole) is not None:
                _swap(item.text(), item.setText)
                _swap(item.toolTip(), item.setToolTip)
    elif isinstance(w, QAbstractSpinBox):
        for get, put in (("prefix", "setPrefix"), ("suffix", "setSuffix"),
                         ("specialValueText", "setSpecialValueText")):
            if hasattr(w, get):
                _swap(getattr(w, get)(), getattr(w, put))
    if isinstance(w, QMenu):
        _swap(w.title(), w.setTitle)
    for a in w.actions():
        translate_action(a)


def install(qapp) -> bool:
    """English: translate every widget as it is shown (see the module docstring) and write the
    numbers of the fields the English way (``2.5``, the system may be Italian). Italian: nothing
    to do (the Qt translator of :func:`..install_translations` gives Qt's own texts)."""
    if current() == "it":
        return False
    from PySide6.QtCore import QEvent, QLocale, QObject

    QLocale.setDefault(QLocale(QLocale.Language.English, QLocale.Country.UnitedKingdom))

    show = QEvent.Type.Show

    class ShowTranslator(QObject):
        def eventFilter(self, obj, event):  # noqa: N802 - Qt name
            if event.type() == show and obj.isWidgetType():
                try:
                    translate_widget(obj)
                except Exception:  # pragma: no cover - a text must never break the interface
                    pass
            return False

    qapp._ppg_translator = ShowTranslator(qapp)
    qapp.installEventFilter(qapp._ppg_translator)
    return True


def uninstall(qapp) -> None:
    """Undo :func:`install` (tests)."""
    from PySide6.QtCore import QLocale

    translator = getattr(qapp, "_ppg_translator", None)
    if translator is not None:
        qapp.removeEventFilter(translator)
        qapp._ppg_translator = None
    QLocale.setDefault(QLocale.system())
