"""*Pubblica sul web* as a pane that stays open: the survey as a static web site (:mod:`..io.site`) in four steps,
each with its state - the contents (chunks, products, title, languages, the frame of the coordinates, the folder),
the build in the worker, the preview on a local server of this computer, the publication (not yet: the folder goes as
it is to any static hosting). The site built last is kept with the project (``meta["web_site"]``), shown in the
Area di lavoro and marked *da rifare* when a product it holds is built again."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton, QRadioButton, QSpinBox,
                               QToolButton, QVBoxLayout, QWidget)

from ..io import site
from .flow_layout import FlowLayout
from .i18n import number, tr
from .icons import PALETTE, icon

log = logging.getLogger("pyarchinit_photogrammetry.gui")

STEPS = ("contents", "build", "preview", "publish")
STATE_ICONS = {"todo": "step_todo", "done": "step_done", "stale": "warning", "running": "step_running",
               "failed": "error", "unavailable": "step_todo"}
TEXTURES = (0, 1024, 2048, 4096, 8192)  # the largest side of a texture page of the model (0: vertex colours)


def state_names() -> dict[str, str]:
    """The states of a step as the pane writes them beside its title."""
    return {"todo": tr("da fare"), "done": tr("fatto"), "stale": tr("da rifare"), "running": tr("in corso"),
            "failed": tr("non riuscito"), "unavailable": tr("non ancora disponibile")}


def product_names() -> dict[str, str]:
    """The products of a site (:data:`..io.site.PRODUCTS`) as the pane names them."""
    return {"orthomosaic": tr("Ortofoto"), "dem": tr("DEM e rilievo ombreggiato"), "units": tr("Unità stratigrafiche"),
            "drawings": tr("Disegni"), "model": tr("Modello 3D"), "cloud": tr("Nuvola di punti"),
            "sections": tr("Sezioni e prospetti")}


def size_text(size: int) -> str:
    return f"{number(size / 1e6, 1)} MB"


def when_text(when: str | None) -> str:
    try:
        return datetime.fromisoformat(str(when)).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return str(when or "?")


def default_folder(document) -> Path | None:
    """Where a new site goes: ``<project>_sito_web`` beside the project file; None for a project never saved."""
    if document is None or document.path is None:
        return None
    path = Path(document.path)
    return path.parent / f"{path.stem}_sito_web"


def folder_problem(folder) -> str:
    """Why ``folder`` cannot take a site, in the words of the pane; empty when it can."""
    if not str(folder or "").strip():
        return tr("Scegli la cartella del sito.")
    kind = site.folder_kind(folder)
    if kind == "file":
        return tr("È un file, non una cartella: scegli una cartella.")
    if kind == "other":
        return tr("La cartella contiene altri file e non è un sito costruito dal programma: non viene toccata, "
                  "scegline un'altra (nuova, vuota o un sito costruito prima).")
    return ""


def folder_note(folder) -> str:
    """What a build does to ``folder``."""
    kind = site.folder_kind(folder)
    return {"new": tr("Cartella nuova: sarà creata."), "empty": tr("Cartella vuota."),
            "site": tr("Contiene un sito costruito prima: sarà riscritto (i file aggiunti per l'hosting, come CNAME, "
                       "restano).")}.get(kind, folder_problem(folder))


class WebPane(QWidget):
    """The four steps of a web site of the project, each with its title, its state (*da fare*, *fatto*, *da rifare*,
    *in corso*, *non riuscito*), its commands and a line that says what it has. The work is done by
    :class:`WebPublishing`, which listens to the signals."""

    build_requested = Signal()
    preview_requested = Signal()
    stop_requested = Signal()
    open_requested = Signal()  # the preview in the browser
    folder_requested = Signal()  # the folder of the site in the file manager

    def __init__(self, parent=None):
        super().__init__(parent)
        self.document = None
        self._busy = False
        self.step_states: dict[str, str] = {k: "todo" for k in STEPS}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        intro = QLabel(tr("Il rilievo come sito web statico: ortofoto, DEM, unità, disegni e modello 3D in una cartella "
                          "che qualsiasi hosting pubblica. I passi in ordine; ognuno dice cosa gli manca."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.titles: dict[str, tuple[QLabel, QLabel, str]] = {}  # key: (icon label, title label, title)
        self.states: dict[str, QLabel] = {}

        def step(row: int, key: str, title: str, body: QWidget) -> None:
            head = QHBoxLayout()
            mark = QLabel()
            mark.setFixedWidth(18)
            t = QLabel()
            t.setWordWrap(True)
            head.addWidget(mark)
            head.addWidget(t, 1)
            lay.addLayout(head)
            lay.addWidget(body)
            s = QLabel("")
            s.setWordWrap(True)
            s.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            s.setContentsMargins(0, 0, 0, 8)
            lay.addWidget(s)
            self.titles[key] = (mark, t, f"{row + 1}. {title}")
            self.states[key] = s

        # 1. the contents
        body = QWidget()
        form = QVBoxLayout(body)
        form.setContentsMargins(0, 0, 0, 0)
        form.addWidget(QLabel(tr("Chunk:")))
        self.chunks = QListWidget()
        self.chunks.setMaximumHeight(110)
        self.chunks.itemChanged.connect(lambda _item: self._chunks_changed())
        form.addWidget(self.chunks)
        form.addWidget(QLabel(tr("Prodotti:")))
        products = QWidget()
        bar = FlowLayout(products)
        self.products: dict[str, QCheckBox] = {}
        for key, name in product_names().items():
            c = QCheckBox(name)
            c.setChecked(True)
            c.toggled.connect(lambda _on: self.update_states())
            self.products[key] = c
            bar.addWidget(c)
        form.addWidget(products)
        fields = QFormLayout()
        self.title = QLineEdit()
        self.title.setToolTip(tr("Il titolo della pagina (vuoto: il sito del progetto, o il nome del file)"))
        fields.addRow(tr("Titolo"), self.title)
        self.description = QLineEdit()
        self.description.setToolTip(tr("Una riga sotto il titolo"))
        fields.addRow(tr("Descrizione"), self.description)
        langs = QWidget()
        lbar = QHBoxLayout(langs)
        lbar.setContentsMargins(0, 0, 0, 0)
        self.languages = {"it": QCheckBox(tr("Italiano")), "en": QCheckBox(tr("Inglese"))}
        for c in self.languages.values():
            c.setChecked(True)
            c.toggled.connect(lambda _on: self.update_states())
            lbar.addWidget(c)
        lbar.addStretch(1)
        langs.setToolTip(tr("Le lingue della pagina: l'italiano, quando c'è, si vede per primo"))
        fields.addRow(tr("Lingue"), langs)
        form.addLayout(fields)
        form.addWidget(QLabel(tr("Coordinate:")))
        self.frame_local = QRadioButton(tr("Sistema locale (consigliato)"))
        self.frame_local.setToolTip(tr("Le coordinate spostate al centro del rilievo, senza sistema di riferimento: "
                                       "nulla nella cartella dice dove si trova il sito"))
        self.frame_crs = QRadioButton(tr("Coordinate vere"))
        self.frame_crs.setToolTip(tr("Le coordinate e il sistema di riferimento dei chunk"))
        self.frames = QButtonGroup(self)
        self.frames.addButton(self.frame_local)
        self.frames.addButton(self.frame_crs)
        self.frame_local.setChecked(True)
        self.frame_crs.toggled.connect(lambda _on: self.update_states())
        form.addWidget(self.frame_local)
        form.addWidget(self.frame_crs)
        self.crs_warning = QLabel(tr("Con le coordinate vere le pagine del sito scrivono la posizione reale del "
                                     "sito: chi apre il sito sa dove si trova. Sceglile solo se vuoi pubblicarla."))
        self.crs_warning.setWordWrap(True)
        self.crs_warning.setStyleSheet(f"color: {PALETTE['red']};")
        self.crs_warning.setVisible(False)
        form.addWidget(self.crs_warning)
        self.b_advanced = QToolButton()
        self.b_advanced.setText(tr("Avanzate"))
        self.b_advanced.setCheckable(True)
        self.b_advanced.setArrowType(Qt.ArrowType.RightArrow)
        self.b_advanced.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        form.addWidget(self.b_advanced)
        self.w_advanced = QWidget()
        adv = QFormLayout(self.w_advanced)
        adv.setContentsMargins(0, 0, 0, 0)
        self.quality = QSpinBox()
        self.quality.setRange(1, 100)
        self.quality.setValue(85)
        self.quality.setToolTip(tr("Qualità del WebP dell'ortofoto: più alta, più fedele e più pesante"))
        adv.addRow(tr("Qualità dell'ortofoto"), self.quality)
        self.texture = QComboBox()
        for side in TEXTURES:
            self.texture.addItem(tr("Solo i colori dei vertici") if side == 0 else f"{side} px", side)
        self.texture.setCurrentIndex(TEXTURES.index(site.MODEL_TEXTURE))
        self.texture.setToolTip(tr("Il lato più lungo di una pagina di texture del modello 3D"))
        adv.addRow(tr("Texture del modello"), self.texture)
        self.budget = QSpinBox()
        self.budget.setRange(0, 100_000)
        self.budget.setSingleStep(50)
        self.budget.setSuffix(" MB")
        self.budget.setSpecialValueText(tr("Nessun limite"))
        self.budget.setValue(int(site.BUDGET_MB))
        self.budget.setToolTip(tr("La dimensione del sito intero: il modello e la nuvola vengono sfoltiti per "
                                  "restarci, e il sito dice che cosa ha ridotto"))
        adv.addRow(tr("Dimensione massima"), self.budget)
        self.cloud_points = QSpinBox()
        self.cloud_points.setRange(10_000, 20_000_000)
        self.cloud_points.setSingleStep(500_000)
        self.cloud_points.setValue(site.CLOUD_POINTS)
        self.cloud_points.setToolTip(tr("I punti della nuvola al massimo"))
        adv.addRow(tr("Punti della nuvola"), self.cloud_points)
        self.timeline = QCheckBox(tr("Una data alla volta"))
        self.timeline.setToolTip(tr("I chunk come rilievi di date diverse (la data del chunk o delle sue foto): la "
                                    "pagina ne mostra una alla volta, con un selettore delle date"))
        adv.addRow(tr("Date"), self.timeline)
        self.w_advanced.setVisible(False)
        self.b_advanced.toggled.connect(self._advanced)
        form.addWidget(self.w_advanced)
        form.addWidget(QLabel(tr("Cartella del sito:")))
        frow = QHBoxLayout()
        self.folder = QLineEdit()
        self.folder.setToolTip(tr("Nuova, vuota o un sito costruito prima: una cartella con altri file non viene "
                                  "mai toccata"))
        self.folder.textChanged.connect(lambda _t: self.update_states())
        self.b_folder = QPushButton(icon("folder"), tr("Scegli..."))
        self.b_folder.clicked.connect(self._choose_folder)
        frow.addWidget(self.folder, 1)
        frow.addWidget(self.b_folder)
        form.addLayout(frow)
        step(0, "contents", tr("Scegli i contenuti"), body)

        # 2. the build
        body = QWidget()
        bar = FlowLayout(body)
        self.b_build = QPushButton(icon("run"), tr("Costruisci il sito"))
        self.b_build.setToolTip(tr("Scrive la cartella del sito: la pagina, site.json e i dati (in un'elaborazione "
                                   "che si può annullare)"))
        self.b_build.clicked.connect(self.build_requested)
        bar.addWidget(self.b_build)
        step(1, "build", tr("Costruisci"), body)

        # 3. the preview
        body = QWidget()
        pl = QVBoxLayout(body)
        pl.setContentsMargins(0, 0, 0, 0)
        holder = QWidget()
        bar = FlowLayout(holder)
        self.b_preview = QPushButton(icon("view"), tr("Avvia l'anteprima"))
        self.b_preview.setToolTip(tr("Un server su questo computer (127.0.0.1) che solo tu vedi; la pagina si apre "
                                     "nel browser"))
        self.b_preview.clicked.connect(self.preview_requested)
        self.b_open = QPushButton(icon("browser"), tr("Apri nel browser"))
        self.b_open.clicked.connect(self.open_requested)
        self.b_copy = QPushButton(icon("copy"), tr("Copia indirizzo"))
        self.b_copy.clicked.connect(self._copy_address)
        self.b_stop = QPushButton(icon("stop"), tr("Ferma"))
        self.b_stop.clicked.connect(self.stop_requested)
        for b in (self.b_preview, self.b_open, self.b_copy, self.b_stop):
            bar.addWidget(b)
        pl.addWidget(holder)
        self.address = QLineEdit()
        self.address.setReadOnly(True)
        self.address.setPlaceholderText(tr("L'indirizzo dell'anteprima"))
        pl.addWidget(self.address)
        step(2, "preview", tr("Anteprima"), body)

        # 4. the publication (phase 5: not yet)
        body = QWidget()
        bar = FlowLayout(body)
        self.b_open_folder = QPushButton(icon("folder"), tr("Apri la cartella"))
        self.b_open_folder.clicked.connect(self.folder_requested)
        bar.addWidget(self.b_open_folder)
        step(3, "publish", tr("Pubblica"), body)
        lay.addStretch(1)
        self.preview_url = ""
        self.record: dict | None = None
        self.stale: list[str] = []
        self.failure = ""
        self.set_document(None)

    # --------------------------------------------------------------- choices
    def _advanced(self, on: bool) -> None:
        self.b_advanced.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.w_advanced.setVisible(on)

    def _choose_folder(self) -> None:
        start = self.folder.text().strip() or str(default_folder(self.document) or Path.home())
        folder = QFileDialog.getExistingDirectory(self, tr("Cartella del sito"), str(Path(start).parent))
        if folder:
            self.folder.setText(folder)

    def _copy_address(self) -> None:
        if self.preview_url:
            QGuiApplication.clipboard().setText(self.preview_url)

    def _chunks_changed(self) -> None:
        self.update_states()

    def chosen_chunks(self) -> list:
        """The chunks ticked, in the order of the project."""
        out = []
        for i in range(self.chunks.count()):
            item = self.chunks.item(i)
            if item.checkState() == Qt.CheckState.Checked and item.flags() & Qt.ItemFlag.ItemIsEnabled:
                out.append(item.data(Qt.ItemDataRole.UserRole))
        return out

    def _available(self) -> set[str]:
        """The products some ticked chunk has."""
        have: set[str] = set()
        for ch in self.chosen_chunks():
            have.update(site.chunk_products(ch))
        return have

    def options(self) -> dict:
        """The choices of the first step, as :func:`..io.site.export_site` takes them (``chunks`` as keys)."""
        have = self._available()
        languages = [k for k, c in self.languages.items() if c.isChecked()]
        return {"chunks": [str(ch.key) for ch in self.chosen_chunks()],
                "products": [k for k, c in self.products.items() if k in have and c.isChecked()],
                "frame": "crs" if self.frame_crs.isChecked() else "local",
                "title": self.title.text().strip(), "description": self.description.text().strip(),
                "languages": languages, "quality": int(self.quality.value()),
                "model_texture": int(self.texture.currentData() or 0), "budget_mb": int(self.budget.value()),
                "cloud_points": int(self.cloud_points.value()), "timeline": bool(self.timeline.isChecked())}

    def folder_path(self) -> Path | None:
        text = self.folder.text().strip()
        return Path(text).expanduser() if text else None

    def problems(self) -> list[str]:
        """What keeps the site from being built with these choices."""
        o = self.options()
        out = []
        if not o["chunks"]:
            out.append(tr("Spunta almeno un chunk con qualcosa da pubblicare."))
        elif not o["products"]:
            out.append(tr("Spunta almeno un prodotto."))
        if not o["languages"]:
            out.append(tr("Spunta almeno una lingua."))
        problem = folder_problem(self.folder.text())
        if problem:
            out.append(problem)
        return out

    # --------------------------------------------------------------- the project
    def set_document(self, document) -> None:
        """The chunks, the products and the choices of ``document``: those of its last site when it has one, else
        every chunk with something to publish (the active one alone when they are in different systems)."""
        self.document = document
        record = site.site_record(document) if document is not None else None
        options = (record or {}).get("options") or {}
        self.chunks.blockSignals(True)
        self.chunks.clear()
        chunks = list(document.chunks) if document is not None else []
        publishable = [ch for ch in chunks if site.chunk_products(ch)]
        systems = {str(ch.crs) for ch in publishable}
        wanted = set(options.get("chunks") or [])
        for ch in chunks:
            have = site.chunk_products(ch)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, ch)
            if wanted:
                on = str(ch.key) in wanted
            else:
                on = bool(have) and (len(systems) <= 1 or ch is document.chunk)
            self._chunk_item(item, on)
            self.chunks.addItem(item)
        self.chunks.blockSignals(False)
        for key, c in self.products.items():  # the choices of the last site, else those of ppg web build
            c.setChecked(key in options["products"] if options else key in site.DEFAULT_PRODUCTS)
        (self.frame_crs if options.get("frame") == "crs" else self.frame_local).setChecked(True)
        self.title.setText(str(options.get("title") or ""))
        self.description.setText(str(options.get("description") or ""))
        for key, c in self.languages.items():
            c.setChecked(key in (options.get("languages") or site.LANGUAGES))
        self.quality.setValue(int(options.get("quality") or 85))
        k = self.texture.findData(int(options.get("model_texture", site.MODEL_TEXTURE) or 0))
        self.texture.setCurrentIndex(k if k >= 0 else TEXTURES.index(site.MODEL_TEXTURE))
        self.budget.setValue(int(options.get("budget_mb", site.BUDGET_MB) or 0))
        self.cloud_points.setValue(int(options.get("cloud_points") or site.CLOUD_POINTS))
        self.timeline.setChecked(bool(options.get("timeline")))
        if record is not None:
            self.folder.setText(str(site.site_folder(document, record)))
        else:
            folder = default_folder(document)
            self.folder.setText(str(folder) if folder is not None else "")
        name = (document.meta or {}).get("sito") if document is not None else None
        self.title.setPlaceholderText(str(name or (Path(document.path).stem if document is not None and document.path
                                                   else tr("Rilievo"))))
        self.read_record()

    def _chunk_item(self, item: QListWidgetItem, on: bool) -> None:
        """The line of a chunk: its name and the products it has; ticked when ``on`` and it has some."""
        ch = item.data(Qt.ItemDataRole.UserRole)
        have = site.chunk_products(ch)
        names = product_names()
        item.setText(f"{ch.label}  ({', '.join(names[p] for p in have)})" if have else
                     tr("{0}  (niente da pubblicare)", ch.label))
        item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | (Qt.ItemFlag.ItemIsEnabled if have else
                                                        Qt.ItemFlag.NoItemFlags))
        item.setCheckState(Qt.CheckState.Checked if on and have else Qt.CheckState.Unchecked)

    def refresh_chunks(self) -> None:
        """The products of the chunks listed read again (a product built or removed), the ticks kept."""
        ticked = {str(ch.key) for ch in self.chosen_chunks()}
        self.chunks.blockSignals(True)
        for i in range(self.chunks.count()):
            item = self.chunks.item(i)
            self._chunk_item(item, str(item.data(Qt.ItemDataRole.UserRole).key) in ticked)
        self.chunks.blockSignals(False)

    def read_record(self) -> None:
        """The last site of the project and what changed since, read again (a product built, the project saved)."""
        doc = self.document
        self.record = site.site_record(doc) if doc is not None else None
        try:
            self.stale = site.site_stale(doc, self.record) if self.record is not None else []
        except Exception as exc:  # noqa: BLE001 - a broken record: said as one to build again
            log.warning("Stato del sito web non letto: %s", exc)
            self.stale = ["?"]
        if self.step_states["build"] != "running":
            self.step_states["build"] = self._built_state()
        self.update_states()

    def _built_state(self) -> str:
        if self.failure:
            return "failed"
        if self.record is None:
            return "todo"
        if not (site.site_folder(self.document, self.record) / site.MANIFEST).exists():
            return "todo"
        return "stale" if self.stale else "done"

    # --------------------------------------------------------------- state
    def set_busy(self, busy: bool) -> None:
        """Another step of the program runs: no build meanwhile."""
        self._busy = busy
        self.update_states()

    def set_running(self, running: bool) -> None:
        """A build started, or ended (the record of the site read again)."""
        if running:
            self.step_states["build"] = "running"
            self.update_states()
        else:
            self.step_states["build"] = "todo"
            self.read_record()

    def set_failed(self, text: str) -> None:
        self.failure = text
        self.step_states["build"] = "failed"
        self.update_states()

    def set_preview(self, url: str) -> None:
        self.preview_url = url
        self.address.setText(url)
        self.update_states()

    def _title(self, key: str, state: str) -> None:
        mark, label, title = self.titles[key]
        mark.setPixmap(icon(STATE_ICONS[state]).pixmap(16, 16))
        colour = {"stale": PALETTE["amber"], "failed": PALETTE["red"]}.get(state)
        name = state_names()[state]
        name = f"<span style='color:{colour}'>{name}</span>" if colour else name
        label.setText(f"<b>{title}</b>  -  {name}")

    def update_states(self) -> None:
        """Every step's state, commands and line from the choices, the last site and the preview."""
        have = self._available()
        names = product_names()
        for key, c in self.products.items():
            c.setEnabled(key in have)
            owners = [ch.label for ch in self.chosen_chunks() if key in site.chunk_products(ch)]
            c.setToolTip(tr("Ce l'hanno: {0}", ", ".join(owners)) if owners else
                         tr("Nessun chunk spuntato ha {0}", names[key]))
        self.crs_warning.setVisible(self.frame_crs.isChecked())
        problems = self.problems()
        folder = self.folder.text().strip()
        contents = "failed" if folder and folder_problem(folder) else ("todo" if problems else "done")
        self.step_states["contents"] = contents
        self._title("contents", contents)
        o = self.options()
        if problems:
            text = " ".join(problems)
        else:
            frame = tr("coordinate vere") if o["frame"] == "crs" else tr("sistema locale")
            text = tr("{0} chunk, {1}; {2}.", len(o["chunks"]), ", ".join(names[p] for p in o["products"]), frame)
            text += " " + folder_note(folder)
        self.states["contents"].setText(text)
        self.states["contents"].setStyleSheet(f"color: {PALETTE['red']};" if contents == "failed" else "")

        build = self.step_states["build"]
        self._title("build", build)
        self.b_build.setEnabled(not problems and not self._busy and build != "running")
        record = self.record
        if build == "running":
            text = tr("Costruzione in corso: i passi nel log.")
        elif build == "failed":
            text = tr("Non riuscito: {0}", self.failure)
        elif record is None or build == "todo":
            text = tr("Da fare: scegli i contenuti e costruisci il sito.")
        else:
            frame = tr("coordinate vere") if record.get("frame") == "crs" else tr("sistema locale")
            text = tr("Costruito il {0}: {1}, {2}, in {3}", when_text(record.get("when")),
                      size_text(int(record.get("size") or 0)), frame, site.site_folder(self.document, record))
            if build == "stale":
                text = tr("Da rifare: cambiati dopo il sito {0}.", ", ".join(self.stale)) + " " + text
            built_with = record.get("options") or {}
            if any(built_with.get(k) != v for k, v in o.items()):
                text += " " + tr("Le scelte del passo 1 sono cambiate da allora: costruisci di nuovo per applicarle.")
        self.states["build"].setText(text)
        self.states["build"].setStyleSheet(f"color: {PALETTE['red']};" if build == "failed" else "")

        built = record is not None and build not in ("todo", "running") and \
            (site.site_folder(self.document, record) / site.MANIFEST).exists()
        serving = bool(self.preview_url)
        preview = "running" if serving else "todo"
        self.step_states["preview"] = preview
        self._title("preview", preview)
        self.b_preview.setEnabled(built and not serving and build != "running")
        for b in (self.b_open, self.b_copy, self.b_stop):
            b.setEnabled(serving)
        self.states["preview"].setText(
            tr("Anteprima su {0}: solo questo computer la vede. Ferma il server quando hai finito.", self.preview_url)
            if serving else tr("Costruisci prima il sito.") if not built else
            tr("Un server su questo computer mostra il sito nel browser, come lo vedrà chi lo apre."))

        self.step_states["publish"] = "unavailable"
        self._title("publish", "unavailable")
        self.b_open_folder.setEnabled(built)
        self.states["publish"].setText(tr("La pubblicazione dal programma non è ancora disponibile. La cartella del "
                                          "sito si copia così com'è su qualsiasi hosting statico."))


class WebPublishing(QObject):
    """What the pane *Pubblica sul web* does in the window: the build in the worker, the record of the site with the
    project, the preview server (one at a time, stopped when the project or the window closes)."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.pane = WebPane()
        self.preview: site.Preview | None = None
        self._building = False
        self.pane.build_requested.connect(self.build)
        self.pane.preview_requested.connect(self.start_preview)
        self.pane.stop_requested.connect(self.stop_preview)
        self.pane.open_requested.connect(self.open_browser)
        self.pane.folder_requested.connect(self.open_folder)

    # --------------------------------------------------------------- build
    def build(self) -> bool:
        w = self.window
        pane = self.pane
        problems = pane.problems()
        if problems:
            w._warn(" ".join(problems))
            return False
        folder = pane.folder_path()
        problem = folder_problem(folder)
        if problem:  # the folder changed on the disk since it was chosen
            w._warn(problem)
            pane.update_states()
            return False
        try:
            w._store_drawings()  # the units and the heights drawn on the active chunk: in its meta, as on saving
        except Exception as exc:  # noqa: BLE001 - the site is built from what is stored
            log.warning("Disegni non salvati nel chunk: %s", exc)
        if self.preview is not None and self.preview.folder.resolve() == folder.resolve():
            self.stop_preview()  # the folder is about to be rewritten
        o = pane.options()
        doc = w.doc
        try:
            sources = site.site_sources(doc, o["chunks"], o["products"])
        except ValueError as exc:
            w._warn(str(exc))
            return False

        def job(progress):
            path = site.export_site(doc, folder, chunks=o["chunks"], products=o["products"], frame=o["frame"],
                                    title=o["title"] or None, description=o["description"] or None,
                                    language=o["languages"], quality=o["quality"],
                                    model_texture=o["model_texture"] or None, progress=progress,
                                    budget_mb=o.get("budget_mb") or None,
                                    cloud_points=o.get("cloud_points") or site.CLOUD_POINTS,
                                    timeline=bool(o.get("timeline")))
            return path, site.site_size(path.parent)

        def done(result):
            self._building = False
            path, size = result
            site.record_site(doc, path, o, sources)
            pane.failure = ""
            log.info(tr("Sito web: {0}, {1}", path.parent, size_text(size)))
            for note in site.build_notes(path):  # what the build reduced or left out
                log.warning(note)
            if o["frame"] == "crs":
                log.warning(tr("Il sito contiene le coordinate vere del rilievo"))
            if doc.path:
                try:
                    doc.save()
                except Exception as exc:  # noqa: BLE001 - said in the log, the site is there anyway
                    log.error("Salvataggio non riuscito: %s", exc)
            else:
                w._dirty(True)
            pane.set_running(False)
            w.workspace.populate(doc)

        def failed(summary, details=""):
            self._building = False
            pane.set_failed(summary.split(": ", 1)[-1] if summary else tr("errore"))
            w._error(summary, details)

        self._building = True
        pane.failure = ""
        pane.set_running(True)
        if not w.runner.run(tr("Sito web"), job, done, failed):
            self._building = False
            pane.set_running(False)
            return False
        return True

    def runner_idle(self) -> None:
        """The worker stopped: a build cancelled ends here (neither done nor failed)."""
        if self._building:
            self._building = False
            self.pane.set_running(False)

    # --------------------------------------------------------------- preview
    def start_preview(self) -> str | None:
        record = self.pane.record
        if record is None:
            return None
        folder = site.site_folder(self.window.doc, record)
        if not (folder / site.MANIFEST).exists():
            self.window._warn(tr("La cartella {0} non contiene più il sito: costruiscilo di nuovo.", folder))
            return None
        self.stop_preview()
        try:
            self.preview = site.Preview(folder).start()  # 127.0.0.1, a free port
        except OSError as exc:
            self.window._warn(tr("Il server dell'anteprima non è partito: {0}", exc))
            return None
        url = self.preview.url
        log.info(tr("Anteprima del sito web su {0}", url))
        self.pane.set_preview(url)
        self.open_browser()
        return url

    def open_browser(self) -> None:
        if self.preview is not None:
            QDesktopServices.openUrl(QUrl(self.preview.url))

    def stop_preview(self) -> None:
        """The server stopped, its port and its thread freed (closing the project or the window calls it too)."""
        preview, self.preview = self.preview, None
        if preview is not None:
            preview.stop()
            log.info(tr("Anteprima del sito web fermata"))
        self.pane.set_preview("")

    def open_folder(self) -> None:
        record = self.pane.record
        if record is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(site.site_folder(self.window.doc, record))))

    # --------------------------------------------------------------- the project
    def set_document(self, doc) -> None:
        if self.pane.document is not doc:
            self.stop_preview()
            self.pane.failure = ""
        self.pane.set_document(doc)

    def refresh(self) -> None:
        """Products built or removed: the chunks and the state of the site read again, the ticks kept."""
        pane, doc = self.pane, self.window.doc
        listed = [pane.chunks.item(i).data(Qt.ItemDataRole.UserRole) for i in range(pane.chunks.count())]
        if pane.document is not doc or len(listed) != len(doc.chunks) or \
                any(a is not b for a, b in zip(listed, doc.chunks)):
            self.set_document(doc)  # another project, a chunk added or removed: the list made again
            return
        pane.refresh_chunks()
        pane.read_record()

    def close(self) -> None:
        self.stop_preview()


__all__ = ["WebPane", "WebPublishing", "state_names", "product_names", "default_folder", "folder_problem"]
