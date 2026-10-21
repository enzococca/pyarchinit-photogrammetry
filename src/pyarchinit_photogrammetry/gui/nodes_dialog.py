"""The computers working on this project (:mod:`engine.nodes`): the folder to give them, the
nodes present, the switch that hands them the work."""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..engine import nodes
from .i18n import tr


class NodesDialog(QDialog):
    def __init__(self, files_dir, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Nodi di rete"))
        self.files_dir = files_dir
        self.settings = settings
        self.resize(640, 360)
        lay = QVBoxLayout(self)
        intro = QLabel(tr("Altri computer lavorano a questo progetto dalla cartella condivisa: su ognuno, con la "
                          "cartella del progetto montata, si avvia ppg node con la cartella qui sotto. Punti chiave e "
                          "mappe di profondità si dividono fra tutti, questo computer compreso; l'allineamento resta "
                          "qui. Su una macchina remota via SSH: ppg remote utente@host:/cartella progetto.ppg."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Cartella per i nodi")))
        self.path = QLineEdit(str(nodes.folder(files_dir, create=False)) if files_dir else "")
        self.path.setReadOnly(True)
        row.addWidget(self.path, 1)
        copy = QPushButton(tr("Copia"))
        copy.clicked.connect(self._copy)
        row.addWidget(copy)
        lay.addLayout(row)
        self.use = QCheckBox(tr("Dai lavoro ai nodi presenti"))
        self.use.setChecked(str(settings.value("nodes/enabled", "true")).lower() in ("true", "1", "yes"))
        self.use.toggled.connect(lambda on: settings.setValue("nodes/enabled", "true" if on else "false"))
        lay.addWidget(self.use)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([tr("Nodo"), tr("Core"), tr("GPU"), tr("Visto da (s)")])
        self.table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.table, 1)
        self.state = QLabel("")
        lay.addWidget(self.state)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(3000)
        self.refresh()

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.path.text())

    def refresh(self) -> None:
        present = nodes.present(self.files_dir) if self.files_dir else []
        self.table.setRowCount(len(present))
        for r, info in enumerate(present):
            for c, text in enumerate((str(info.get("machine") or info.get("host", "")), str(info.get("cores", "")),
                                      ", ".join(info.get("gpus") or []) or "-", str(info.get("age", "")))):
                self.table.setItem(r, c, QTableWidgetItem(text))
        self.state.setText(tr("{0} nodi presenti", len(present)) if present else tr("Nessun nodo presente"))
