"""Start page shown instead of the views when no project is open."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from .. import __version__
from .flow_layout import FlowLayout
from .i18n import tr
from .icons import app_icon, icon


def _card(name: str, title: str, text: str) -> QPushButton:
    b = QPushButton(icon(name), f"  {title}")
    b.setObjectName("card")
    b.setIconSize(QSize(28, 28))
    b.setToolTip(text)
    b.setMinimumHeight(56)
    b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return b


class StartPage(QWidget):
    new_requested = Signal()
    open_requested = Signal()
    demo_requested = Signal()
    recent_requested = Signal(str)
    doctor_requested = Signal()
    log_requested = Signal()
    help_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("startPage")
        # the page scrolls on a small screen instead of widening the main window
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        page.setObjectName("startPage")
        scroll.setWidget(page)
        top = QVBoxLayout(self)
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(scroll)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(40, 30, 40, 30)
        outer.addStretch(1)
        column = QFrame()
        column.setObjectName("card")
        column.setMaximumWidth(760)
        col = QVBoxLayout(column)
        col.setContentsMargins(36, 32, 36, 28)
        col.setSpacing(14)
        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(84, 84))
        head.addWidget(logo)
        titles = QVBoxLayout()
        t = QLabel("PyArchInit Photogrammetry")
        t.setObjectName("pageTitle")
        s = QLabel(tr("Fotogrammetria open source per l'archeologia e il BIM  -  versione {0}", __version__))
        s.setObjectName("pageSubtitle")
        s.setWordWrap(True)
        titles.addWidget(t)
        titles.addWidget(s)
        titles.addStretch(1)
        head.addSpacing(16)
        head.addLayout(titles, 1)
        col.addLayout(head)
        cards = FlowLayout(spacing=12)  # side by side, one under the other on a narrow window
        self.b_new = _card("new", "Nuovo progetto", "Crea un progetto vuoto e aggiungi le foto")
        self.b_open = _card("open", "Apri progetto...", "Apri un file .ppg")
        self.b_demo = _card("demo", "Progetto demo", "Crea e apri il saggio di scavo dimostrativo")
        for b in (self.b_new, self.b_open, self.b_demo):
            cards.addWidget(b)
        col.addLayout(cards)
        rec = QLabel(tr("PROGETTI RECENTI"))
        rec.setObjectName("sectionTitle")
        col.addWidget(rec)
        self.recent = QListWidget()
        self.recent.setIconSize(QSize(18, 18))
        self.recent.setMaximumHeight(170)
        self.recent.itemActivated.connect(self._pick_recent)
        self.recent.itemClicked.connect(self._pick_recent)
        col.addWidget(self.recent)
        self.empty = QLabel(tr("Nessun progetto recente. Inizia con il progetto demo per vedere tutto il flusso di "
                               "lavoro."))
        self.empty.setObjectName("pageSubtitle")
        self.empty.setWordWrap(True)
        col.addWidget(self.empty)
        foot = FlowLayout(spacing=12)
        self.b_help = QPushButton(icon("help"), tr("Guida"))
        self.b_doctor = QPushButton(icon("doctor"), tr("Diagnostica ambiente"))
        self.b_log = QPushButton(icon("log"), tr("Cartella dei log"))
        for b in (self.b_help, self.b_doctor, self.b_log):
            b.setFlat(True)
            foot.addWidget(b)
        col.addLayout(foot)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(column, 8)  # as wide as the page allows, up to its maximum width
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(2)
        self.b_new.clicked.connect(self.new_requested)
        self.b_open.clicked.connect(self.open_requested)
        self.b_demo.clicked.connect(self.demo_requested)
        self.b_help.clicked.connect(self.help_requested)
        self.b_doctor.clicked.connect(self.doctor_requested)
        self.b_log.clicked.connect(self.log_requested)
        self.set_recent([])

    def set_recent(self, paths: list[str]) -> None:
        self.recent.clear()
        paths = [p for p in paths if Path(p).exists()]
        for p in paths:
            it = QListWidgetItem(icon("project"), Path(p).name)
            it.setToolTip(p)
            it.setData(Qt.ItemDataRole.UserRole, p)
            self.recent.addItem(it)
        self.recent.setVisible(bool(paths))
        self.empty.setVisible(not paths)

    def _pick_recent(self, item: QListWidgetItem) -> None:
        self.recent_requested.emit(item.data(Qt.ItemDataRole.UserRole))


__all__ = ["StartPage"]
