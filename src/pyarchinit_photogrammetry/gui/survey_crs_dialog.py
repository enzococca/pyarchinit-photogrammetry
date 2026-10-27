"""The coordinate system of a survey, chosen where the survey is: the systems :func:`..gnss.heights.survey_crs` proposes
for the frames of its positions - UTM on WGS 84 for positions in WGS 84 or ITRF2020, RDN2008 / UTM in Italy (ETRS89 /
UTM elsewhere in Europe) for the European frames - or another one written, with the heights above the geoid EGM2008 or
without heights of their own; and what putting a chunk into a system does, in words
(:func:`..gnss.heights.survey_crs_changes`), for the window to ask before it does it."""
from __future__ import annotations

from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
                               QRadioButton, QVBoxLayout)

from ..core import crs as crs_mod
from ..gnss import heights
from .i18n import number, tr

EGM2008 = "3855"  # the EPSG code of the heights above EGM2008 ("EGM2008 height")


def with_geoid(crs: str) -> str:
    """``crs`` with heights above EGM2008: an EPSG code without heights of its own gets ``+3855``; a system with heights
    of its own (compound, 3D) stays as it is."""
    crs = crs.strip()
    if heights.height_kind(crs) != heights.NONE or not crs.upper().startswith("EPSG:") or "+" in crs:
        return crs
    return f"{crs}+{EGM2008}"


# what survey_crs_changes counts, converts and keeps, as the window says it
def _converted_names() -> dict[str, str]:
    return {"markers": tr("marker"), "units": tr("unità disegnate"), "quote": tr("quote"),
            "points": tr("punti di misura"), "stones": tr("pietre"), "sections": tr("sezioni"),
            "patches": tr("patch dell'ortofoto"), "texture_patches": tr("patch della texture"),
            "region": tr("regione della ricostruzione"), "colour_chart": tr("carta dei colori")}


def _kept_names() -> dict[str, str]:
    return {"versions": tr("versioni di DEM e ortofoto"), "elevations": tr("prospetti"),
            "multispectral": tr("prodotti multispettrali"), "thermal": tr("prodotti termici"),
            "satellite": tr("scene satellitari"), "relief": tr("rilievi del DEM"),
            "calculator": tr("risultati del calcolatore raster"), "proxies": tr("proxy delle US"),
            "stones": tr("pietre su altri raster"), "sections": tr("sezioni"),
            "section_drawings": tr("disegni delle sezioni (SVG, DXF)"),
            "drawn": tr("disegni e marker, che restano dove sono (il chunk era in coordinate locali)")}


def product_names() -> dict[str, str]:
    """The products built in the coordinates of the system of a chunk (:data:`..gnss.heights.IN_THE_SYSTEM`), as the
    window names them."""
    return {"dem": tr("DEM"), "orthomosaic": tr("ortofoto"), "dem_precision": tr("precisione del DEM"),
            "coverage": tr("copertura")}


def relief_text(changes: dict) -> str | None:
    """What the conversion of a georeference by a similarity does to the heights of the model over its relief
    (``transform_relief``, ``transform_vertical`` of :func:`..gnss.heights.survey_crs_changes` and
    :func:`..gnss.heights.set_survey_crs`), None below a millimetre."""
    vertical, relief = changes.get("transform_vertical"), changes.get("transform_relief")
    if vertical is None or relief is None or vertical < 0.001:
        return None
    return tr("Sui {0} m di dislivello le quote del modello si scostano fino a {1} mm da quelle dei disegni: nel nuovo "
              "sistema cambia la scala della pianta, come in ogni georeferenziazione.", number(relief, 0),
              number(vertical * 1000, 1))


def changes_text(old: str, changes: dict, redraw=()) -> str:
    """What putting a chunk from ``old`` into the system of ``changes`` (:func:`..gnss.heights.survey_crs_changes`)
    does: the system, the georeference fitted again (or converted, when its references cannot fit it after all, as
    :func:`..gnss.heights.set_survey_crs` does) or converted (by how much the similarity misses, and what it does to
    the heights over the relief: :func:`relief_text`), what is converted,
    the products to build again, what stays in the old system, the lines the window holds only - ``redraw``, the
    names of their layers: taken away, to draw again -, the heights that pass as they are and those that go onto the
    geoid with the chunk."""
    lines = [tr("Sistema: {0} → {1}.", old, changes["crs"])]
    if changes.get("refit"):
        lines.append(tr("La georeferenziazione viene calcolata di nuovo nel nuovo sistema, sui marker e sulle "
                        "posizioni delle foto; se non bastano a calcolarla, viene convertita con una similitudine."))
    elif changes.get("transform_rms") is not None:
        lines.append(tr("La georeferenziazione viene convertita nel nuovo sistema con una similitudine adattata "
                        "all'estensione del chunk (scarto {0} mm): modello e nuvole restano con i disegni.",
                        number(changes["transform_rms"] * 1000, 1)))
        relief = relief_text(changes)
        if relief:
            lines.append(relief)
    names = _converted_names()
    converted = [f"{names.get(kind, kind)} {n}" for kind, n in (changes.get("convert") or {}).items() if n]
    if converted:
        lines.append(tr("Vengono convertiti: {0}.", ", ".join(converted)))
    products = product_names()
    if changes.get("rebuild"):
        lines.append(tr("Da costruire di nuovo dopo (sono nel sistema di prima): {0}.",
                        ", ".join(products.get(name, name) for name in changes["rebuild"])))
    kept_names = _kept_names()
    for kept in changes.get("kept") or []:
        shown = ", ".join(kept["names"][:6]) + (" ..." if len(kept["names"]) > 6 else "")
        lines.append(tr("Restano nel sistema di prima: {0} ({1}).", kept_names.get(kept["what"], kept["what"]), shown))
    if redraw:
        lines.append(tr("Da ridisegnare dopo: {0} (linee che sono solo nella finestra, nel sistema di prima: vengono "
                        "tolte).", ", ".join(redraw)))
    if changes.get("heights_unchanged"):
        lines.append(tr("Il sistema di prima non ha quote proprie: le quote di marker, quote e punti di misura passano "
                        "come sono nel nuovo, giuste solo se erano sul livello del mare."))
    if changes.get("heights_from_ellipsoid"):
        lines.append(tr("Le quote del chunk sono sull'ellissoide (posizioni GNSS delle foto): quote, punti di misura e "
                        "unità disegnate vanno sul geoide con il modello; le quote dei marker passano come sono."))
    return "\n".join(lines)


class SurveyCrsDialog(QDialog):
    """The system for the survey: one of ``choices`` (``(crs, label)``: the systems proposed where the survey is),
    ``default`` the one chosen at first, or another one written; *Quote sul geoide EGM2008* gives it heights above the
    geoid (an EPSG code gets ``+3855``). ``current`` is the system of the chunk, ``note`` what the dialog says of the
    positions, ``geoid`` whether the grid of EGM2008 is there (the dialog says it is downloaded first)."""

    def __init__(self, current: str, choices: list[tuple[str, str]], default: str | None = None, note: str = "",
                 geoid: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Sistema del rilievo"))
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        intro = QLabel(tr("Il sistema di coordinate del rilievo, dove si trova, per il sistema delle sue posizioni; il "
                          "chunk ora è in {0}. Marker e disegni vengono convertiti, DEM e ortofoto vanno costruiti di "
                          "nuovo.", current) + (" " + note if note else ""))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.group = QButtonGroup(self)
        self.options: list[tuple[QRadioButton, str]] = []
        for crs, label in choices:
            button = QRadioButton(label)
            self.group.addButton(button)
            self.options.append((button, crs))
            lay.addWidget(button)
            if crs == default:
                button.setChecked(True)
        other_row = QHBoxLayout()
        self.other = QRadioButton(tr("Altro:"))
        self.group.addButton(self.other)
        self.other_crs = QLineEdit()
        self.other_crs.setPlaceholderText(tr("EPSG:7792, o un WKT"))
        other_row.addWidget(self.other)
        other_row.addWidget(self.other_crs, 1)
        lay.addLayout(other_row)
        if self.group.checkedButton() is None:
            (self.options[0][0] if self.options else self.other).setChecked(True)
        self.other_crs.textEdited.connect(lambda _text: self.other.setChecked(True))
        self.geoid = QCheckBox(tr("Quote sul geoide EGM2008"))
        self.geoid.setChecked(True)
        lay.addWidget(self.geoid)
        grid = QLabel(tr("La griglia del geoide EGM2008 è installata.") if geoid else
                      tr("La griglia del geoide EGM2008 ({0} MB) non è installata: il programma la scarica prima, se "
                         "la vuoi.", round(heights.GEOID_BYTES / 1e6)))
        grid.setWordWrap(True)
        grid.setStyleSheet("color: #6c7a89")
        lay.addWidget(grid)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #b91c1c; font-weight: bold")
        lay.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def chosen(self) -> str:
        """The system chosen or written, as it is (without the heights of the box)."""
        if self.other.isChecked():
            return self.other_crs.text().strip()
        return next((crs for button, crs in self.options if button.isChecked()), "")

    def values(self) -> dict:
        """``{"crs", "plain"}``: the system chosen, with heights above EGM2008 when the box says so, and as it was
        chosen (without the heights of the box)."""
        crs = self.chosen()
        return {"crs": with_geoid(crs) if self.geoid.isChecked() and crs else crs, "plain": crs}

    def accept(self) -> None:
        if not self.chosen():
            self.error.setText(tr("Scrivi il sistema di coordinate (un codice EPSG, o un WKT)."))
            return
        try:
            crs_mod.to_pyproj(self.chosen())
        except Exception as exc:  # noqa: BLE001 - PROJ's own words
            self.error.setText(tr("PROJ non conosce questo sistema: {0}", exc))
            return
        super().accept()

    @classmethod
    def ask(cls, parent, current: str, choices: list[tuple[str, str]], default: str | None = None, note: str = "",
            geoid: bool = False) -> dict | None:
        """The answer of the dialog (:meth:`values`), None when it is cancelled."""
        dialog = cls(current, choices, default, note, geoid, parent)
        try:
            return dialog.values() if dialog.exec() == QDialog.DialogCode.Accepted else None
        finally:
            dialog.deleteLater()  # a child of the window: not kept until it closes
