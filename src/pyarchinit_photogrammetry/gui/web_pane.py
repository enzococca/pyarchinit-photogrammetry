"""*Pubblica sul web* as a pane that stays open: the survey as a static web site (:mod:`..io.site`) in four steps,
each with its state - the contents (chunks, products, title, languages, the frame of the coordinates, the folder),
the build in the worker, the preview on a local server of this computer, the publication on Cloudflare Pages (and R2
for large data) through the user's own wrangler (:mod:`..io.site_publish`: the plan shown first, an explicit consent,
a second one for true coordinates). The site built last is kept with the project (``meta["web_site"]``, its address
once published in ``["published"]``), shown in the Area di lavoro and marked *da rifare* when a product it holds is
built again."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton, QRadioButton, QSpinBox,
                               QToolButton, QVBoxLayout, QWidget)

from ..io import site
from ..io import site_publish
from .dialogs import message
from .flow_layout import FlowLayout
from .i18n import number, tr
from .icons import PALETTE, icon

log = logging.getLogger("pyarchinit_photogrammetry.gui")

STEPS = ("contents", "build", "preview", "publish")
STATE_ICONS = {"todo": "step_todo", "done": "step_done", "stale": "warning", "running": "step_running",
               "failed": "error", "unavailable": "step_todo"}
TEXTURES = (0, 1024, 2048, 4096, 8192)  # the largest side of a texture page of the model (0: vertex colours)


def data_modes() -> dict[str, str]:
    """Where the data of a published site go (:data:`..io.site_publish.DATA_MODES`)."""
    return {"auto": tr("Automatico: Pages, R2 se serve"), "pages": tr("Tutto su Pages"),
            "r2": tr("Dati su R2 (bucket)")}


def issue_texts() -> dict[str, str]:
    """The problems and warnings of a publication plan (:data:`..io.site_publish.ISSUES`) in the words of the pane."""
    return {
        "data_mode": tr("Dove vanno i dati: una tra {0}"),
        "not_site": tr("{0} non è un sito costruito dal programma: costruiscilo prima"),
        "project_name": tr("Il nome del progetto Pages: lettere minuscole, cifre e trattini, non agli estremi "
                           "(diventa <nome>.pages.dev)"),
        "pages_file_bytes": tr("{0} file superano i {1} MiB per file di Pages ({2}): manda i dati su R2 (un bucket) "
                               "o costruisci un sito più leggero"),
        "page_file_bytes": tr("{0} file fuori da data/ superano i {1} MiB per file di Pages ({2})"),
        "pages_files": tr("{0} file su Pages, più dei {1} del piano gratuito: manda i dati su R2 (un bucket)"),
        "page_files": tr("{0} file su Pages, più dei {1} del piano gratuito"),
        "no_bucket": tr("I dati devono andare su R2 (file oltre i limiti di Pages): scrivi il nome di un bucket"),
        "bucket_name": tr("Il nome del bucket: 3-63 lettere minuscole, cifre e trattini, non agli estremi"),
        "data_url": tr("L'indirizzo dei dati deve iniziare con https:// (il dominio collegato al bucket)"),
        "r2_put_bytes": tr("File più grandi dei {0} MB che wrangler manda su R2 in una volta ({1}): costruisci un "
                           "sito più leggero (budget) o mandali con rclone"),
        "r2_billed": tr("I dati superano i 10 GB-mese gratuiti di R2: il resto si paga"),
        "r2_dev": tr("I dati sono serviti dall'indirizzo r2.dev del bucket: limitato e pensato per le prove; per un "
                     "sito che dura collega un dominio al bucket e scrivi qui il suo indirizzo"),
        "r2_subscription": tr("R2 va attivato una volta sull'account (abbonamento R2 nella dashboard di Cloudflare)"),
        "cors_replaced": tr("La regola CORS del bucket {0} viene sostituita con quella di questo sito: usa un bucket "
                            "solo per questo sito"),
        "true_coordinates": tr("Il sito contiene le coordinate VERE del rilievo: pubblicandolo chiunque saprà dove "
                               "si trova"),
    }


def limit_texts(lim) -> str:
    """A limit of the free plans met or exceeded (:class:`..io.site_publish.Limit`) in the words of the pane."""
    v, n = lim.value, lim.limit
    text = {"pages_files": lambda: tr("file su Pages {0} di {1} (piano gratuito)", number(v), number(n)),
            "pages_file_bytes": lambda: tr("file più grande su Pages {0} MiB di {1}", number(v / 2**20, 1),
                                           number(n / 2**20)),
            "r2_put_bytes": lambda: tr("file più grande verso R2 {0} MB di {1}", number(v / 1e6, 1), number(n / 1e6)),
            "r2_storage": lambda: tr("spazio su R2 {0} GB di {1} GB-mese gratuiti", number(v / 1e9, 2),
                                     number(n / 1e9)),
            "r2_class_a": lambda: tr("scritture R2 di questa pubblicazione {0} di {1} gratuite al mese", number(v),
                                     number(n)),
            "r2_class_b": lambda: tr("letture R2 di una visita che apre tutto circa {0} di {1} gratuite al mese",
                                     number(v), number(n))}.get(lim.key)
    return text() if text else f"{lim.key} {v} / {n}"


def plan_text(plan) -> str:
    """The plan of a publication (:func:`..io.site_publish.plan`) as the pane shows it before the consent (rich
    text): where the files go, the limits, what becomes public, the warnings and the problems."""
    import html

    esc = html.escape
    rows = [tr("Pagina su Cloudflare Pages, progetto <b>{0}</b>: {1} file, {2}.", esc(plan.project),
               number(len(plan.pages_files)), size_text(plan.pages_bytes))]
    if plan.data == "r2":
        rows.append(tr("Dati nel bucket R2 <b>{0}</b>: {1} file, {2}; da inviare {3} ({4}), invariati {5}, da "
                       "cancellare {6}.", esc(plan.bucket or "?"), number(len(plan.r2_files)),
                       size_text(plan.r2_bytes), number(len(plan.upload)), size_text(plan.upload_bytes),
                       number(len(plan.unchanged)), number(len(plan.remove))))
        rows.append(tr("Indirizzo dei dati: {0}", esc(plan.data_url) if plan.data_url else
                       tr("l'indirizzo r2.dev del bucket, attivato alla pubblicazione")))
    limits = []
    for lim in plan.limits:
        word = tr("entro") if lim.ok else f"<span style='color:{PALETTE['red']}'>{tr('oltre')}</span>"
        limits.append(f"{word}: {esc(limit_texts(lim))}")
    rows.append(tr("Limiti gratuiti (letti il {0}):", site_publish.CHECKED) + "<br>&nbsp;&nbsp;" +
                "<br>&nbsp;&nbsp;".join(limits))
    public = tr("Diventa pubblico: tutti i file del sito, per chiunque abbia l'indirizzo.")
    if plan.frame == "crs":
        public += f" <span style='color:{PALETTE['red']}'><b>{tr('Con le coordinate VERE del rilievo.')}</b></span>"
    else:
        public += " " + tr("Coordinate nel sistema locale: il luogo del rilievo non è scritto.")
    rows.append(public)
    texts = issue_texts()
    for kind, code, values in plan.codes:
        line = esc(texts[code].format(*values)) if code in texts else esc(code)
        if kind == "problem":
            rows.append(f"<span style='color:{PALETTE['red']}'>{tr('Non si può pubblicare:')} {line}</span>")
        elif code != "true_coordinates":
            rows.append(f"<span style='color:{PALETTE['amber']}'>{line}</span>")
    return "<br>".join(rows)


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
    check_requested = Signal()  # wrangler, the login and the plan of the publication
    publish_requested = Signal()
    cancel_requested = Signal()  # the publication running
    address_typed = Signal(str)  # the address of the site typed by the user (wrangler did not say it)

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

        # 4. the publication on Cloudflare (the user's wrangler)
        body = QWidget()
        cl = QVBoxLayout(body)
        cl.setContentsMargins(0, 0, 0, 0)
        cf = QFormLayout()
        self.cf_project = QLineEdit()
        self.cf_project.setToolTip(tr("Il nome del progetto Cloudflare Pages: il sito sarà su <nome>.pages.dev"))
        cf.addRow(tr("Progetto Pages"), self.cf_project)
        self.cf_data = QComboBox()
        for key, name in data_modes().items():
            self.cf_data.addItem(name, key)
        self.cf_data.setToolTip(tr("Pages tiene file fino a 25 MiB e 20.000 file; oltre, i dati vanno in un bucket "
                                   "R2"))
        cf.addRow(tr("Dati"), self.cf_data)
        self.cf_bucket = QLineEdit()
        self.cf_bucket.setPlaceholderText(tr("solo se i dati vanno su R2"))
        cf.addRow(tr("Bucket R2"), self.cf_bucket)
        self.cf_data_url = QLineEdit()
        self.cf_data_url.setPlaceholderText(tr("facoltativo: https://dominio collegato al bucket"))
        cf.addRow(tr("Indirizzo dei dati"), self.cf_data_url)
        for widget in (self.cf_project, self.cf_bucket, self.cf_data_url):
            widget.textEdited.connect(lambda _t: self._cf_changed())
        self.cf_data.currentIndexChanged.connect(lambda _i: self._cf_changed())
        cl.addLayout(cf)
        self.cf_warning = QLabel(tr("Questo sito contiene le coordinate VERE del rilievo: pubblicandolo chiunque "
                                    "saprà dove si trova. Per pubblicarlo senza, costruiscilo di nuovo nel sistema "
                                    "locale."))
        self.cf_warning.setWordWrap(True)
        self.cf_warning.setStyleSheet(f"color: {PALETTE['red']}; font-weight: bold;")
        self.cf_warning.setVisible(False)
        cl.addWidget(self.cf_warning)
        self.cf_plan = QLabel("")
        self.cf_plan.setWordWrap(True)
        self.cf_plan.setTextFormat(Qt.TextFormat.RichText)
        self.cf_plan.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self.cf_plan)
        holder = QWidget()
        bar = FlowLayout(holder)
        self.b_cf_check = QPushButton(icon("search"), tr("Controlla"))
        self.b_cf_check.setToolTip(tr("Cerca wrangler, guarda se hai fatto l'accesso a Cloudflare e prepara il "
                                      "piano: cosa va dove, i limiti gratuiti, cosa diventa pubblico. Non invia "
                                      "nulla."))
        self.b_cf_check.clicked.connect(self.check_requested)
        self.b_publish = QPushButton(icon("publish"), tr("Pubblica..."))
        self.b_publish.clicked.connect(self.publish_requested)
        self.b_cf_cancel = QPushButton(icon("cancel"), tr("Annulla"))
        self.b_cf_cancel.clicked.connect(self.cancel_requested)
        self.b_site_open = QPushButton(icon("browser"), tr("Apri nel browser"))
        self.b_site_open.clicked.connect(self._open_published)
        self.b_site_copy = QPushButton(icon("copy"), tr("Copia l'indirizzo"))
        self.b_site_copy.clicked.connect(self._copy_published)
        self.b_open_folder = QPushButton(icon("folder"), tr("Apri la cartella"))
        self.b_open_folder.clicked.connect(self.folder_requested)
        for b in (self.b_cf_check, self.b_publish, self.b_cf_cancel, self.b_site_open, self.b_site_copy,
                  self.b_open_folder):
            bar.addWidget(b)
        cl.addWidget(holder)
        self.published_address = QLineEdit()
        self.published_address.setReadOnly(True)
        self.published_address.setPlaceholderText(tr("L'indirizzo del sito pubblicato"))
        self.published_address.editingFinished.connect(self._address_edited)
        cl.addWidget(self.published_address)
        step(3, "publish", tr("Pubblica"), body)
        lay.addStretch(1)
        self.preview_url = ""
        self.cf_status: site_publish.Status | None = None  # wrangler and the login, from the last check
        self.cf_plan_obj: site_publish.Plan | None = None
        self.cf_running = False
        self.cf_failure = ""
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

    def published(self) -> dict | None:
        """Where the site was published (``meta["web_site"]["published"]``), None before."""
        return site_publish.published_record(self.document) if self.document is not None else None

    def _open_published(self) -> None:
        pub = self.published()
        if pub and pub.get("address"):
            QDesktopServices.openUrl(QUrl(pub["address"]))

    def _copy_published(self) -> None:
        pub = self.published()
        if pub and pub.get("address"):
            QGuiApplication.clipboard().setText(pub["address"])

    def _address_edited(self) -> None:
        """The address typed where wrangler did not say it."""
        pub = self.published()
        text = self.published_address.text().strip()
        if pub and not self.published_address.isReadOnly() and text and text != (pub.get("address") or ""):
            self.address_typed.emit(text)

    def cf_options(self) -> dict:
        """The choices of the publication: project, where the data go, bucket, address of the data."""
        return {"project": self.cf_project.text().strip(), "data": self.cf_data.currentData() or "auto",
                "bucket": self.cf_bucket.text().strip() or None, "data_url": self.cf_data_url.text().strip() or None}

    def _cf_changed(self) -> None:
        """A choice of the publication changed: the plan shown no longer holds, check again."""
        self.cf_plan_obj = None
        self.update_states()

    def set_cloudflare(self, status, plan) -> None:
        """The result of a check: the state of wrangler and the plan."""
        self.cf_status, self.cf_plan_obj = status, plan
        self.cf_failure = ""
        self.update_states()

    def set_publishing(self, running: bool, failure: str = "") -> None:
        self.cf_running = running
        self.cf_failure = failure
        if not running:
            self.cf_plan_obj = None  # the next publication is planned again (what was sent changed)
        self.update_states()

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
        pub = site_publish.published_record(document) if document is not None else None
        if pub:
            self.cf_project.setText(str(pub.get("project") or ""))
            self.cf_bucket.setText(str(pub.get("bucket") or ""))
            k = self.cf_data.findData("r2" if pub.get("data") == "r2" else "auto")
            self.cf_data.setCurrentIndex(max(k, 0))
            address = pub.get("data_address") or ""
            self.cf_data_url.setText("" if ".r2.dev" in address else address)
        else:
            stem = Path(document.path).stem if document is not None and document.path else ""
            self.cf_project.setText(site_publish.default_project(stem) if stem else "")
            self.cf_bucket.setText("")
            self.cf_data.setCurrentIndex(0)
            self.cf_data_url.setText("")
        self.cf_status, self.cf_plan_obj, self.cf_failure = None, None, ""
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

        self._update_publish(built, build)

    def _update_publish(self, built: bool, build: str) -> None:
        """Step 4: not built / wrangler missing / not logged in / ready / publishing / published / failed."""
        pub = self.published()
        folder = site.site_folder(self.document, self.record) if built else None
        crs = built and site_publish.site_frame(folder) != "local"
        self.cf_warning.setVisible(bool(crs))
        status = self.cf_status.state if self.cf_status is not None else None
        plan = self.cf_plan_obj
        if self.cf_running:
            state, text = "running", tr("Pubblicazione in corso: i passi nel log. Annulla per fermarla (i file "
                                        "già inviati restano e non si inviano di nuovo).")
        elif self.cf_failure:
            state, text = "failed", tr("Non riuscito: {0}", self.cf_failure)
        elif not built:
            state, text = "todo", tr("Costruisci prima il sito.")
        elif status == "missing":
            state, text = "unavailable", tr("wrangler, il programma di Cloudflare, non è installato. Installa Node.js "
                                            "(nodejs.org), poi in un terminale: npm install -g wrangler e wrangler "
                                            "login. Il programma non lo installa da solo.")
        elif status == "logged_out":
            state, text = "unavailable", tr("wrangler non ha l'accesso a Cloudflare. In un terminale: wrangler login "
                                            "(si apre il browser; il programma non vede le credenziali), poi "
                                            "Controlla di nuovo.")
        elif pub:
            stale = site_publish.stale_publication(self.document)
            state = "stale" if stale else "done"
            if pub.get("address"):
                text = tr("Pubblicato il {0} su {1}", when_text(pub.get("when")), pub.get("address"))
            else:
                text = tr("Pubblicato il {0}; l'indirizzo non si è letto nella risposta di wrangler: lo trovi nella "
                          "dashboard di Cloudflare (Workers & Pages) o con wrangler pages project list. Scrivilo qui "
                          "sotto.", when_text(pub.get("when")))
            if pub.get("data_address"):
                text += " " + tr("(dati su {0})", pub["data_address"])
            if stale:
                text = tr("Il sito è stato costruito di nuovo dopo la pubblicazione: pubblicalo di nuovo.") + " " + text
        elif status == "ready":
            state, text = "todo", tr("Pronto: {0}. Controlla il piano qui sopra, poi Pubblica.", self.cf_status.text)
        else:
            state, text = "todo", tr("Su Cloudflare Pages (gratuito; R2 per i dati grandi) con il tuo wrangler, dopo "
                                     "wrangler login in un terminale. Premi Controlla: non invia nulla.")
        self.step_states["publish"] = state
        self._title("publish", state)
        self.states["publish"].setText(text)
        self.states["publish"].setStyleSheet(f"color: {PALETTE['red']};" if state == "failed" else "")
        self.cf_plan.setText(plan_text(plan) if plan is not None and not self.cf_running else "")
        self.cf_plan.setVisible(plan is not None and not self.cf_running)
        idle = not self._busy and not self.cf_running and build != "running"
        for w in (self.cf_project, self.cf_data, self.cf_bucket, self.cf_data_url):
            w.setEnabled(not self.cf_running)
        self.b_cf_check.setEnabled(built and idle)
        self.b_publish.setEnabled(built and idle and status == "ready" and plan is not None and plan.ok)
        self.b_cf_cancel.setEnabled(self.cf_running)
        address = (pub or {}).get("address") or ""
        self.b_site_open.setEnabled(bool(address))
        self.b_site_copy.setEnabled(bool(address))
        typing = bool(pub) and not address and not self.cf_running  # the user writes the address wrangler did not say
        self.published_address.setReadOnly(not typing)
        self.published_address.setPlaceholderText(tr("https://... l'indirizzo del sito, dalla dashboard di Cloudflare")
                                                  if typing else tr("L'indirizzo del sito pubblicato"))
        if not self.published_address.hasFocus() or not typing:
            self.published_address.setText(address)
        self.b_open_folder.setEnabled(built)


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
        self.pane.check_requested.connect(self.check_cloudflare)
        self.pane.publish_requested.connect(self.publish)
        self.pane.cancel_requested.connect(self.cancel_publish)
        self.pane.address_typed.connect(self.set_address)
        self._publishing = False

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
        """The worker stopped: a build or a publication cancelled ends here (neither done nor failed)."""
        if self._building:
            self._building = False
            self.pane.set_running(False)
        if self._publishing:  # the worker says it stopped before it calls done or failed: look after them
            QTimer.singleShot(0, self._publish_ended)

    def _publish_ended(self) -> None:
        if self._publishing:  # neither done nor failed: cancelled
            self._publishing = False
            log.warning(tr("Pubblicazione annullata"))
            self.pane.set_publishing(False)

    # --------------------------------------------------------------- publication (Cloudflare)
    def _site_folder(self) -> Path | None:
        record = self.pane.record
        if record is None:
            return None
        folder = site.site_folder(self.window.doc, record)
        return folder if (folder / site.MANIFEST).exists() else None

    def check_cloudflare(self) -> bool:
        """wrangler, the login and the plan, in the worker (wrangler takes seconds; the plan reads the data files
        to tell what changed). Nothing is sent."""
        folder = self._site_folder()
        if folder is None:
            self.window._warn(tr("Costruisci prima il sito."))
            return False
        o = self.pane.cf_options()

        def job(progress):
            plan = site_publish.plan(folder, o["project"], bucket=o["bucket"], data=o["data"], data_url=o["data_url"])
            status = site_publish.Wrangler().status(progress)
            return status, plan

        def done(result):
            status, plan = result
            for line in plan.lines():
                log.info("Cloudflare: %s", line)
            self.pane.set_cloudflare(status, plan)

        def failed(summary, details=""):
            self.pane.set_cloudflare(None, None)
            self.window._error(summary, details)

        return bool(self.window.runner.run(tr("Controllo di Cloudflare"), job, done, failed, quiet=True))

    def _consent(self, plan) -> tuple[bool, bool]:
        """The explicit consent: the site becomes public; a second, red, one for true coordinates."""
        from PySide6.QtWidgets import QMessageBox

        yes, no = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
        where = tr("su Cloudflare Pages, progetto {0}", plan.project)
        if plan.data == "r2":
            where += " " + tr("e nel bucket R2 {0} (indirizzo pubblico)", plan.bucket)
        text = tr("Il sito diventa PUBBLICO: chiunque abbia l'indirizzo può aprirlo e scaricarne i file ({0}). Per "
                  "limitare l'accesso dopo, usa Cloudflare Access dalla dashboard (vedi la guida).\n\nPubblicare?",
                  where)
        if message(self.window, "question", tr("Pubblica sul web"), text, buttons=yes | no, default=no) != yes:
            return False, False
        if plan.frame == "local":
            return True, False
        text = (f"<p style='color:{PALETTE['red']}'><b>" + tr("Questo sito contiene le coordinate VERE del rilievo.") +
                "</b></p><p>" + tr("Chiunque lo apra saprà dove si trova il sito archeologico. Confermi di volerle "
                                   "pubblicare?") + "</p>")
        ok = message(self.window, "warning", tr("Coordinate vere"), text, buttons=yes | no, default=no) == yes
        return True, ok

    def publish(self) -> bool:
        pane = self.pane
        plan = pane.cf_plan_obj
        status = pane.cf_status
        folder = self._site_folder()
        if folder is None or plan is None or status is None or status.state != "ready" or not plan.ok:
            self.window._warn(tr("Premi prima Controlla: il piano va visto prima di pubblicare."))
            return False
        public, coordinates = self._consent(plan)
        if not public or (plan.frame != "local" and not coordinates):
            log.info(tr("Pubblicazione non confermata: nulla è stato inviato"))
            return False
        o = pane.cf_options()
        doc = self.window.doc
        wrangler = site_publish.Wrangler(status.command or None)

        def job(progress):
            fresh = site_publish.plan(folder, o["project"], bucket=o["bucket"], data=o["data"],
                                      data_url=o["data_url"])  # what is on the disk now
            if fresh.frame != plan.frame:
                raise site_publish.ConsentError("the site changed since it was checked: check it again")
            return site_publish.publish(fresh, public=public, coordinates=coordinates, wrangler=wrangler,
                                        progress=progress)

        def done(result):
            self._publishing = False
            for line in result.transcript:
                log.info("wrangler: %s", line)
            site_publish.record_published(doc, result)
            for note in result.unread:
                log.warning(tr("Non letto nella risposta di wrangler: {0}", note))
            for text in result.unread_output:  # whole, scrubbed: for the user to send to the developers
                log.warning("wrangler:\n%s", text)
            if result.address:
                log.info(tr("Sito pubblicato su {0}", result.address))
            else:
                log.warning(tr("Sito pubblicato; l'indirizzo è nella dashboard di Cloudflare o in wrangler pages "
                               "project list: scrivilo nel passo 4"))
            if doc.path:
                try:
                    doc.save()
                except Exception as exc:  # noqa: BLE001 - said in the log, the site is published anyway
                    log.error("Salvataggio non riuscito: %s", exc)
            else:
                self.window._dirty(True)
            pane.set_publishing(False)
            self.window.workspace.populate(doc)

        def failed(summary, details=""):
            self._publishing = False
            for line in wrangler.transcript:
                log.info("wrangler: %s", line)
            text = site_publish.scrub(summary.split(": ", 1)[-1] if summary else tr("errore"))
            pane.set_publishing(False, text)
            self.window._error(site_publish.scrub(summary), site_publish.scrub(details))

        self._publishing = True
        pane.set_publishing(True)
        if not self.window.runner.run(tr("Pubblicazione su Cloudflare"), job, done, failed):
            self._publishing = False
            pane.set_publishing(False)
            return False
        return True

    def set_address(self, text: str) -> bool:
        """The address typed in step 4 kept with the project."""
        doc = self.window.doc
        try:
            site_publish.set_address(doc, text)
        except ValueError:
            self.window._warn(tr("L'indirizzo del sito deve iniziare con https:// (per esempio "
                                 "https://nome.pages.dev)"))
            return False
        if doc.path:
            try:
                doc.save()
            except Exception as exc:  # noqa: BLE001 - said in the log
                log.error("Salvataggio non riuscito: %s", exc)
        else:
            self.window._dirty(True)
        self.pane.update_states()
        self.window.workspace.populate(doc)
        return True

    def cancel_publish(self) -> None:
        if self._publishing:
            self.window.runner.cancel()

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


__all__ = ["WebPane", "WebPublishing", "state_names", "product_names", "default_folder", "folder_problem",
           "plan_text", "issue_texts", "data_modes"]
