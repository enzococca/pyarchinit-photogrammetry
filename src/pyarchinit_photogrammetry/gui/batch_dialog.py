"""Flusso di lavoro > Elaborazione batch: the steps of a batch (operation, parameters, chunks),
edited, saved and loaded as JSON (see :mod:`pyarchinit_photogrammetry.batch`)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMenu, QPushButton, QSpinBox, QVBoxLayout)

from .. import batch
from .dialogs import QUALITY, Field, ParamDialog, message
from .icons import icon

LABELS = {"detect_markers": "Rileva marker codificati", "import_reference": "Importa i riferimenti (CSV)",
          "align_photos": "Allinea le foto", "match_photos": "Cerca i punti (solo match)",
          "align_cameras": "Allinea le camere (dopo il match)", "update_transform": "Aggiorna la georeferenziazione",
          "optimize_cameras": "Ottimizza le camere", "field_check": "Controllo della copertura",
          "build_depth_maps": "Mappe di profondita'", "build_point_cloud": "Nuvola densa",
          "build_model": "Modello 3D", "build_texture": "Texture", "build_dem": "DEM",
          "build_orthomosaic": "Ortofoto", "calibrate_colours": "Calibra i colori",
          "estimate_precision": "Stima la precisione", "export_point_cloud": "Esporta la nuvola densa",
          "export_model": "Esporta il modello", "export_dem": "Esporta il DEM",
          "export_orthomosaic": "Esporta l'ortofoto", "generate_report": "Report"}
GROUPS = [("Allineamento", ["detect_markers", "import_reference", "align_photos", "match_photos", "align_cameras",
                            "update_transform", "optimize_cameras", "field_check", "estimate_precision"]),
          ("Prodotti", ["build_depth_maps", "build_point_cloud", "build_model", "build_texture", "build_dem",
                        "build_orthomosaic", "calibrate_colours"]),
          ("Esportazioni", ["export_point_cloud", "export_model", "export_dem", "export_orthomosaic",
                            "generate_report"])]
TARGETS = [("aruco_4x4_50", "ArUco 4x4 (50 codici)"), ("aruco_4x4_250", "ArUco 4x4 (250 codici)"),
           ("aruco_5x5_100", "ArUco 5x5 (100 codici)"), ("aruco_6x6_250", "ArUco 6x6 (250 codici)"),
           ("apriltag_36h11", "AprilTag 36h11")]
PATH_TIP = "{project} = cartella del progetto, {name} = nome del progetto, {chunk} = nome del chunk"


def fields(op: str, params: dict) -> list[Field]:
    """The form of the parameters of an operation, filled with ``params``."""
    p = {**batch.OPERATIONS[op], **params}
    pairs = [("auto", "Automatica"), ("exhaustive", "Tutte le coppie"), ("sequential", "Sequenziale"),
             ("reference", "Da posizione GNSS")]
    if op == "detect_markers":
        return [Field("target_type", "Tipo di target", p["target_type"], choices=TARGETS)]
    if op == "import_reference":
        return [Field("path", "File CSV (etichetta, X, Y, Z)", p["path"], kind="text", tip=PATH_TIP),
                Field("crs", "Sistema di riferimento (es. EPSG:3004; vuoto = quello del chunk)", p["crs"] or "",
                      kind="text"),
                Field("columns", "Colonne (n etichetta, x y z, a accuratezza, - salta)", p["columns"], kind="text")]
    if op in ("align_photos", "match_photos"):
        return [Field("quality", "Precisione", p["quality"], choices=QUALITY),
                Field("pair_selection", "Selezione coppie", p["pair_selection"], choices=pairs),
                Field("keypoint_limit", "Limite punti chiave", p["keypoint_limit"], minimum=1000, maximum=200000),
                Field("tiepoint_limit", "Limite punti di legame", p["tiepoint_limit"], minimum=500, maximum=100000)]
    if op == "optimize_cameras":
        return [Field("use_markers", "Con i punti di controllo e le barre di scala", p["use_markers"]),
                Field("fit_principal_point", "Stima anche il punto principale", p["fit_principal_point"])]
    if op == "field_check":
        return [Field("min_photos", "Foto che servono per ogni punto", p["min_photos"], minimum=2, maximum=10)]
    if op == "build_depth_maps":
        return [Field("quality", "Qualita'", p["quality"], choices=QUALITY),
                Field("filter_mode", "Filtro", p["filter_mode"],
                      choices=[("mild", "Leggero"), ("moderate", "Moderato"), ("aggressive", "Forte"),
                               ("none", "Nessuno")]),
                Field("skip_unreadable", "Salta le foto non leggibili", p["skip_unreadable"])]
    if op == "build_model":
        return [Field("source", "Dati", p["source"], choices=[("point_cloud", "Nuvola densa"),
                                                              ("depth_maps", "Mappe di profondita'")]),
                Field("surface", "Superficie", p["surface"], choices=[("arbitrary", "Arbitraria (3D)"),
                                                                      ("height_field", "Campo di altezze (2,5D)")]),
                Field("face_count", "Facce", p["face_count"], choices=[("low", "Poche"), ("medium", "Medie"),
                                                                       ("high", "Molte")])]
    if op == "build_texture":
        return [Field("texture_size", "Lato della texture (px)", p["texture_size"], minimum=512, maximum=32768),
                Field("mapping", "Mappatura", p["mapping"], choices=[("auto", "Automatica"),
                                                                     ("orthophoto", "Ortofoto (terreno)"),
                                                                     ("generic", "Generica (oggetti)")]),
                Field("skip_unreadable", "Salta le foto non leggibili", p["skip_unreadable"])]
    if op == "build_dem":
        return [Field("resolution", "Risoluzione (m, vuoto = automatica)", p["resolution"], kind="optional_float"),
                Field("source", "Dati", p["source"], choices=[("point_cloud", "Nuvola densa"),
                                                              ("tie_points", "Punti di legame"),
                                                              ("model", "Modello 3D")])]
    if op == "build_orthomosaic":
        return [Field("resolution", "Risoluzione (m, vuoto = automatica)", p["resolution"], kind="optional_float"),
                Field("blending", "Fusione", p["blending"], choices=[("mosaic", "Mosaico (senza cuciture)"),
                                                                     ("disabled", "Nessuna"), ("average", "Media")]),
                Field("skip_unreadable", "Salta le foto non leggibili", p["skip_unreadable"])]
    if op == "calibrate_colours":
        return [Field("vignetting", "Anche la vignettatura", p["vignetting"])]
    if op.startswith("export_") or op == "generate_report":
        return [Field("path", "File", p["path"], kind="text", tip=PATH_TIP)]
    return []


def describe(s: dict, doc=None) -> str:
    """One line for a step: operation, the parameters that differ from the defaults, chunks."""
    p = s.get("params") or {}
    changed = {k: v for k, v in p.items() if batch.OPERATIONS[s["op"]].get(k) != v or k == "path"}
    form = {f.name: f for f in fields(s["op"], p)}
    parts = []
    for k, v in changed.items():
        f = form.get(k)
        if f is None:
            parts.append(f"{k}: {v}")
            continue
        shown = dict(f.choices or []).get(v, v)
        if isinstance(v, bool):
            shown = "si" if v else "no"
        parts.append(f"{f.label.split(' (')[0]}: {shown}")
    extra = ", ".join(parts)
    chunks = s.get("chunks", "all")
    where = "tutti i chunk" if chunks in ("all", None) else ("chunk attivi" if chunks == "enabled"
                                                            else ", ".join(map(str, chunks)))
    return f"{LABELS.get(s['op'], s['op'])}" + (f" ({extra})" if extra else "") + f"  -  {where}"


class BatchDialog(QDialog):
    """The steps of a batch, the attempts and what to do when a step fails."""

    def __init__(self, doc, steps: list[dict] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Elaborazione batch")
        self.setMinimumSize(620, 420)
        self.doc = doc
        lay = QVBoxLayout(self)
        intro = QLabel("I passi vengono eseguiti in ordine, ognuno su tutti i suoi chunk; il progetto si salva dopo "
                       "ogni passo. Se il batch si interrompe (corrente, crash, Annulla) riparte da dove era arrivato. "
                       "Un passo che fallisce viene ritentato; se fallisce ancora i passi successivi di quel chunk si "
                       "saltano.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        row = QHBoxLayout()
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _it: self.edit_step())
        row.addWidget(self.list, 1)
        buttons = QVBoxLayout()
        self.b_add = QPushButton(icon("add"), "Aggiungi")
        menu = QMenu(self.b_add)
        for title, ops in GROUPS:
            sub = menu.addMenu(title)
            for op in ops:
                sub.addAction(LABELS[op], lambda op=op: self.add_step(op))
        self.b_add.setMenu(menu)
        buttons.addWidget(self.b_add)
        for text, slot in (("Modifica...", self.edit_step), ("Rimuovi", self.remove_step), ("Su", lambda: self.move(-1)),
                           ("Giu'", lambda: self.move(1)), ("Carica...", self.load), ("Salva...", self.save)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch(1)
        row.addLayout(buttons)
        lay.addLayout(row, 1)
        form = QFormLayout()
        self.retries = QSpinBox()
        self.retries.setRange(0, 5)
        self.retries.setValue(1)
        form.addRow("Nuovi tentativi per un passo che fallisce", self.retries)
        self.on_error = QComboBox()
        self.on_error.addItem("Continua con gli altri chunk", "continue")
        self.on_error.addItem("Ferma il batch", "stop")
        form.addRow("Se un passo fallisce", self.on_error)
        lay.addLayout(form)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.button(QDialogButtonBox.StandardButton.Ok).setText("Avvia")
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self._steps: list[dict] = []
        for s in steps or []:
            self._append(s)

    # ------------------------------------------------------------------ steps
    def steps(self) -> list[dict]:
        return [dict(s) for s in self._steps]

    def options(self) -> dict:
        return {"retries": self.retries.value(), "on_error": self.on_error.currentData()}

    def _append(self, s: dict, at: int | None = None) -> None:
        at = len(self._steps) if at is None else at
        self._steps.insert(at, s)
        self.list.insertItem(at, QListWidgetItem(describe(s, self.doc)))
        self.list.setCurrentRow(at)

    def _ask(self, op: str, params: dict, chunks) -> dict | None:
        labels = [c.label for c in self.doc.chunks]
        chosen = labels if chunks in ("all", None) else [c.label for c in batch.targets(self.doc, chunks)]
        extra = [Field("chunks", "Chunk", chosen, choices=[(lb, lb) for lb in labels], kind="checklist")] \
            if len(labels) > 1 else []
        v = ParamDialog.ask(self, LABELS[op], fields(op, params) + extra)
        if v is None:
            return None
        sel = v.pop("chunks", labels)
        if not sel:
            return None
        if op == "import_reference" and not v.get("crs"):
            v["crs"] = None
        return batch.step(op, "all" if set(sel) == set(labels) else list(sel), **v)

    def add_step(self, op: str) -> None:
        s = self._ask(op, {}, "all")
        if s is not None:
            row = self.list.currentRow()
            self._append(s, row + 1 if row >= 0 else None)

    def edit_step(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        old = self._steps[row]
        s = self._ask(old["op"], old["params"], old["chunks"])
        if s is not None:
            self._steps[row] = s
            self.list.item(row).setText(describe(s, self.doc))

    def remove_step(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            del self._steps[row]
            self.list.takeItem(row)

    def move(self, step: int) -> None:
        row = self.list.currentRow()
        new = row + step
        if row < 0 or not 0 <= new < len(self._steps):
            return
        s = self._steps.pop(row)
        self.list.takeItem(row)
        self._append(s, new)

    def load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Carica batch", str(self._folder()), "Batch (*.json)")
        if not path:
            return
        try:
            steps = batch.load_steps(path)
        except Exception as exc:  # noqa: BLE001 - a wrong file is reported, not raised
            message(self, "warning", "Carica batch", f"File non valido: {exc}")
            return
        self._steps.clear()
        self.list.clear()
        for s in steps:
            self._append(s)

    def save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Salva batch", str(self._folder() / "batch.json"),
                                              "Batch (*.json)")
        if path:
            batch.save_steps(self.steps(), path)

    def _folder(self) -> Path:
        return self.doc.path.parent if self.doc.path is not None else Path.home()

    def _accept(self) -> None:
        if not self._steps:
            message(self, "warning", "Elaborazione batch", "Aggiungi almeno un passo.")
            return
        self.accept()
