"""The window's side of the drawings kept with an elevation or a section (:mod:`..engine.elevation_drawings`): shown
in the 3D view on their planes, read back from the elevation's own QGIS project, given to another elevation.

The drawing itself - draw, vertices, delete, layers, attributes, snapping, undo - is the plan's own code working on
the sets of the elevation shown (``MainWindow.shown_units``...): nothing of it is here."""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction

from .i18n import tr

log = logging.getLogger("pyarchinit_photogrammetry")

SETTING_3D = "view/elevation_drawings_3d"
COLOUR_3D = "#00c8ff"


class ElevationDrawingActions:
    """Methods of the main window (it provides ``chunk``, ``settings``, ``model_view``, ``drawings_frame``,
    ``_store_drawings``, ``_restore_frame``, ``_redraw_overlays``, ``_frame_entry``, ``_dirty``, ``_warn``, ``_info``,
    ``_qgis_dir``, ``log_pane``)."""

    # ------------------------------------------------------------ menus
    def _build_elevation_drawing_actions(self, view_menu, qgis_menu) -> None:
        """*Vista > Disegni dei prospetti nella vista 3D* and *QGIS > Rileggi il prospetto da QGIS*."""
        self.a_elevation_drawings_3d = QAction(tr("Disegni dei prospetti nella vista 3D"), self, checkable=True)
        self.a_elevation_drawings_3d.setStatusTip(tr("Mostra nella vista 3D, sui loro piani, i disegni fatti sui "
                                                     "prospetti e sulle sezioni"))
        self.a_elevation_drawings_3d.setChecked(self.settings.value(SETTING_3D, "true") in ("true", True))
        self.a_elevation_drawings_3d.toggled.connect(self._elevation_drawings_3d_toggled)
        view_menu.addAction(self.a_elevation_drawings_3d)
        self._act(qgis_menu, tr("Rileggi il prospetto da QGIS"), self.read_elevation_from_qgis, icon_name="refresh",
                  tip=tr("Le modifiche fatte in QGIS ai disegni del prospetto mostrato (Apri il prospetto in QGIS) "
                         "tornano qui"))

    # ------------------------------------------------------------ 3D view
    def _elevation_drawings_3d_toggled(self, on: bool) -> None:
        self.settings.setValue(SETTING_3D, "true" if on else "false")
        self._draw_elevation_drawings_3d()

    def _schedule_elevation_drawings_3d(self) -> None:
        """The drawings of the elevations in the 3D view drawn again soon (once for many edits in a row)."""
        timer = getattr(self, "_elev3d_timer", None)
        if timer is None:
            timer = self._elev3d_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(0)
            timer.timeout.connect(self._draw_elevation_drawings_3d)
        timer.start()

    def _draw_elevation_drawings_3d(self) -> None:
        """The drawings of every elevation of the active chunk on their planes in the 3D view (read only), when
        *Vista > Disegni dei prospetti nella vista 3D* is ticked; those of the elevation shown as they are now."""
        mv, ch = getattr(self, "model_view", None), self.chunk
        if mv is None or not hasattr(mv, "set_guests"):
            return
        action = getattr(self, "a_elevation_drawings_3d", None)
        if ch is None or mv.chunk is not ch or (action is not None and not action.isChecked()):
            mv.set_guests([])
            return
        from ..engine import elevation_drawings as ED

        data = None
        if self.drawings_frame is not None:
            data = {self.drawings_frame: self._shown_frame_data()}
        try:
            mv.set_guests(ED.world_features(ch, data=data), COLOUR_3D)
        except Exception as exc:  # noqa: BLE001 - a view of the drawings: never in the way of the work
            log.warning("Disegni dei prospetti non mostrati in 3D: %s", exc)
            mv.set_guests([])

    def _shown_frame_data(self) -> dict:
        """The drawings of the elevation shown as the chunk will keep them (``_store_chunk_drawings``), without writing
        them into the chunk."""
        class _Holder:
            meta: dict = {}

        holder = _Holder()
        holder.meta = {}
        self._store_chunk_drawings(holder, self.frame_units, self.frame_quote, self.frame_points,
                                   frame=self.drawings_frame,
                                   stones={k: [g.wkt for g in v] for k, v in self.frame_stones.items() if v},
                                   lines=self.frame_lines)
        return ((holder.meta.get("drawings") or {}).get("frames") or {}).get(self.drawings_frame) or {}

    # ------------------------------------------------------------ QGIS, the elevation unfolded
    def _elevation_qgis_paths(self, entry: dict) -> tuple[Path, Path, str]:
        """The folder, the GeoPackage of the drawings and the stem of the QGIS project of ``entry``."""
        from ..engine.elevation import _safe_stem

        stem = _safe_stem(entry["name"]) + "_" + entry["id"]
        folder = self._qgis_dir() / "prospetti" / stem
        return folder, folder / f"{stem}_disegni.gpkg", stem

    def _read_back_elevation(self, entry: dict, quiet: bool = False) -> dict | None:
        """The edits made in QGIS on the GeoPackage of the elevation ``entry`` into its drawings: one step of
        Modifica > Annulla. Returns how many of each kind, None when there was nothing to read."""
        from ..engine import elevation_drawings as ED

        _folder, path, _stem = self._elevation_qgis_paths(entry)
        if not path.exists() or not ED.snapshot_path(path).exists():
            return None
        changes = ED.read_unfolded(path)
        if ED._empty_changes(changes):
            return None
        self._store_drawings()
        done = ED.apply_unfolded(self.chunk, entry["id"], path, changes)
        if self.drawings_frame == entry["id"]:
            self._restore_frame()
            self._redraw_overlays()
        self._dirty(True)
        n = sum(done.values())
        self.log_pane.info(tr("Prospetto «{0}»: {1} modifiche lette da QGIS (unità {2}, quote {3}, punti {4}, linee "
                              "{5}, pietre {6})", entry["name"], n, done["units"], done["quote"], done["points"],
                              done["lines"], done["stones"]))
        if not quiet:
            self.statusBar().showMessage(tr("{0} modifiche lette da QGIS", n), 8000)
        return done

    def read_elevation_from_qgis(self) -> None:
        """*QGIS > Rileggi il prospetto da QGIS*: what was changed in QGIS in the drawings of the elevation shown -
        its own project, *Apri il prospetto in QGIS* - comes back here (units, spot heights, points, lines, stones
        drawn, moved, changed or deleted there); what was changed here meanwhile and not there stays."""
        entry = self._frame_entry()
        if entry is None:
            self._warn(tr("Mostra prima un prospetto o una sezione (doppio clic nell'area di lavoro)."))
            return
        _folder, path, _stem = self._elevation_qgis_paths(entry)
        if not path.exists():
            self._warn(tr("Il prospetto «{0}» non è ancora stato aperto in QGIS (QGIS > Apri il prospetto in QGIS).",
                          entry["name"]))
            return
        if self._read_back_elevation(entry) is None:
            self.statusBar().showMessage(tr("Nessuna modifica in QGIS ai disegni del prospetto"), 8000)

    # ------------------------------------------------------------ the drawings of one elevation on another
    def transfer_elevation_drawings(self, entry: dict, chunk=None) -> None:
        """*Disegni da un altro prospetto...* on an elevation of the workspace: the drawings of another elevation of
        the chunk copied (or moved) onto this one through their world positions - an elevation made again on another
        plane gets the drawings of the old one. One step of Modifica > Annulla."""
        from ..engine import elevation_drawings as ED
        from .dialogs import Field, ParamDialog

        ch = chunk or self.chunk
        if ch is not self.chunk:
            self._warn(tr("Rendi attivo il chunk del prospetto prima."))
            return
        self._store_drawings()
        others = [(e["id"], e.get("name") or e["id"]) for e in ED.entries(ch)
                  if e["id"] != entry["id"] and ED.has_drawings(ch, e["id"])]
        if not others:
            self._warn(tr("Nessun altro prospetto di questo chunk ha disegni."))
            return
        v = ParamDialog.ask(self, "Disegni da un altro prospetto", [
            Field("source", "Prospetto da cui prenderli", others[0][0], choices=others),
            Field("move", "Spostali (toglili dall'altro prospetto)", False)])
        if not v:
            return
        try:
            done = ED.transfer(ch, v["source"], entry["id"], copy_=not v["move"])
        except (KeyError, ValueError) as exc:
            self._warn(str(exc))
            return
        if self.drawings_frame in (entry["id"], v["source"]):
            self._restore_frame()
            self._redraw_overlays()
        self._dirty(True)
        self.workspace.populate(self.doc)
        self._schedule_elevation_drawings_3d()
        self.log_pane.info(tr("Prospetto «{0}»: {1} disegni presi da un altro prospetto", entry["name"],
                              sum(done.values())))
