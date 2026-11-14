"""The free drawings of the window (:mod:`..core.free_drawings`, since 1.7.2): the menu of *Disegno libero*, the
drawings ended in the 3D view and on the 2D view, their edits from the 3D view, the redraw of both views, the layer
visibility link with the Layer pane, the form, *Misura area*, and *File > Esporta / Importa > Disegni 3D*. A mixin of
:class:`.main_window.MainWindow` (split from it in 1.7.3: the methods and their names unchanged)."""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMenu

from ..io.qgis_link import ID_FIELD
from .i18n import tr

log = logging.getLogger("pyarchinit_photogrammetry.gui")

LAYER_PREFIX = "Disegni 3D: "  # the layers of the 2D view that show the free drawings of a user layer (core.free_drawings)
FREE_KINDS = (("line", "Linea"), ("polygon", "Poligono"), ("point", "Punto"))  # the kinds of Disegno libero


def _runs(mask, closed: bool = False) -> list:
    """The runs of consecutive True of ``mask`` as index arrays; ``closed``: the last and the first are neighbours
    (a run across the end joined)."""
    mask = np.asarray(mask, bool)
    n = len(mask)
    runs, cur = [], []
    for i in range(n):
        if mask[i]:
            cur.append(i)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    if closed and len(runs) > 1 and runs[0][0] == 0 and runs[-1][-1] == n - 1:
        runs = [runs[-1] + runs[0]] + runs[1:-1]
    return [np.array(r) for r in runs]



class LayersShown:
    """The step of Modifica > Annulla of *Tutti* / *Nessuno*: the layers of the 2D view it changed shown as they were
    (undo) or as it left them (redo) - the free drawings' layers in the chunk too."""

    def __init__(self, window, changed: dict):
        self.window, self.changed = window, dict(changed)

    def _apply(self, which: int) -> None:
        w = self.window
        free = False
        for name, states in self.changed.items():
            shown = bool(states[which])
            w.ortho_view.set_layer_visible(name, shown)
            free = w._set_free_layer_shown(name, shown) or free
        if free:
            w._draw_free_3d()
            w.setWindowModified(True)
        w.layers_pane.rebuild()

    def undo(self) -> None:
        self._apply(0)

    def redo(self) -> None:
        self._apply(1)

class FreeDrawingActions:
    """The free-drawing methods of the main window (it provides ``chunk``, ``settings``, ``model_view``,
    ``ortho_view``, ``log_pane``, ``_dirty``, ``_warn``, ``_info``, ``_exc``, ``_act`` and the other helpers used)."""

    def _build_free_menu(self) -> None:
        """The menu of *Disegno libero*: the kind drawn next, the layer it goes in, a new layer, the form."""
        from ..core import free_drawings

        self.free_menu = QMenu(tr("Disegno libero"), self)
        kinds = QActionGroup(self)
        current = self.settings.value("draw/free_kind", "line")
        self.free_kind = current if current in ("line", "polygon", "point") else "line"
        self.free_kind_actions: dict[str, QAction] = {}
        for key, label in FREE_KINDS:
            a = self.free_menu.addAction(tr(label))
            a.setCheckable(True)
            a.setChecked(key == self.free_kind)
            a.triggered.connect(lambda _=False, k=key: self.set_free_kind(k))
            kinds.addAction(a)
            self.free_kind_actions[key] = a
        self.free_menu.addSeparator()
        self.free_layer = str(self.settings.value("draw/free_layer", free_drawings.DEFAULT_LAYER)
                              or free_drawings.DEFAULT_LAYER)
        self.free_layer_menu = self.free_menu.addMenu(tr("Layer"))
        self.free_layer_menu.aboutToShow.connect(self._fill_free_layer_menu)
        self._act(self.free_menu, tr("Nuovo layer..."), self._new_free_layer)
        self.free_menu.addSeparator()
        self.a_free_props = self._act(self.free_menu, tr("Proprietà..."), lambda: self.free_properties())
        self.a_free_props.setToolTip(tr("Nome, layer, colore, attributi, quote e misure del disegno libero selezionato "
                                        "(nella vista 2D o nella vista 3D)"))

    def _fill_free_layer_menu(self) -> None:
        from ..core import free_drawings

        self.free_layer_menu.clear()
        names = [x["name"] for x in free_drawings.layers(self.chunk)] if self.chunk is not None else []
        if self.free_layer not in names:
            names.append(self.free_layer)
        group = QActionGroup(self.free_layer_menu)
        for name in names:
            a = self.free_layer_menu.addAction(name)
            a.setCheckable(True)
            a.setChecked(name == self.free_layer)
            a.setProperty("ppg_no_translate", True)
            a.triggered.connect(lambda _=False, n=name: self.set_free_layer(n))
            group.addAction(a)

    def set_free_kind(self, kind: str) -> None:
        """The kind of the free drawings drawn next in 2D (line, polygon, point); the tool is taken up."""
        if kind not in ("line", "polygon", "point"):
            return
        self.free_kind = kind
        self.settings.setValue("draw/free_kind", kind)
        a = self.free_kind_actions.get(kind)
        if a is not None and not a.isChecked():
            a.setChecked(True)
        if self.ortho_view.tr is not None:
            self.set_draw_mode("free_" + kind)

    def set_free_layer(self, name: str) -> None:
        """The layer the next free drawings go in (2D and 3D), remembered between sessions."""
        name = str(name or "").strip()
        if not name:
            return
        self.free_layer = name
        self.settings.setValue("draw/free_layer", name)
        self.statusBar().showMessage(tr("I disegni liberi vanno nel layer {0}", name), 10000)

    def _new_free_layer(self) -> None:
        name, ok = QInputDialog.getText(self, tr("Nuovo layer"), tr("Nome del layer dei disegni liberi:"))
        if ok and name.strip():
            if self.chunk is not None:
                from ..core import free_drawings

                if name.strip() not in {x["name"] for x in free_drawings.layers(self.chunk)}:
                    free_drawings.set_layer(self.chunk, name.strip())
                    self._dirty(True)
            self.set_free_layer(name.strip())

    def _free_record(self, fid: str) -> dict | None:
        from ..core import free_drawings

        if self.chunk is None or not fid:
            return None
        return next((f for f in free_drawings.features(self.chunk) if f["id"] == fid), None)

    def _free_add(self, kind: str, xyz) -> dict | None:
        """A free drawing of ``kind`` with the vertices ``xyz`` (chunk coordinates) in the current layer, one step of
        *Modifica > Annulla*, drawn in both views."""
        from ..core import free_drawings

        ch = self.chunk
        if ch is None:
            return None
        try:
            rec = free_drawings.add(ch, kind, np.asarray(xyz, float).tolist(), layer=self.free_layer)
        except ValueError as exc:
            self.statusBar().showMessage(tr("Disegno non aggiunto: {0}", str(exc)), 15000)
            return None
        self._dirty(True)
        self._draw_free()
        self.log_pane.info(tr("{0} aggiunto al layer {1}", rec["name"], rec["layer"]))
        return rec

    def _free_from_3d(self, kind: str, internal) -> None:
        """A free drawing ended in the 3D view: its vertices (internal) in the chunk's coordinates."""
        if self.chunk is None:
            return
        self._free_add(kind, self.chunk.to_world(np.asarray(internal, float).reshape(-1, 3)))

    def _free_from_2d(self, kind: str, coords) -> None:
        """A free drawing ended on the 2D view: on the plan each vertex takes the height of the DEM there, on an
        elevation its (u, v) goes to the chunk's coordinates at the depth of the relief there; a vertex without a value
        takes the one interpolated along the drawing from its neighbours; none at all: refused."""
        ch = self.chunk
        if ch is None:
            return
        from ..core import free_drawings

        xy = np.asarray(coords, float).reshape(-1, 2)
        if self._shown_raster_stale():
            self._warn(tr("Questo raster va rifatto prima di disegnarci sopra (Flusso di lavoro > Ricostruisci i "
                          "prodotti da rifare)"))
            return
        entry = self._frame_entry()
        if entry is None:
            zs = free_drawings.fill_missing(self._dem_values(xy), xy)
            if zs is None:
                self._warn(tr("Il disegno libero prende le quote dal DEM: serve il DEM sotto almeno un vertice"))
                return
            xyz = np.column_stack([xy, zs])
        else:
            from ..engine import elevation as E

            ds = free_drawings.fill_missing(self._relief_values(entry, xy), xy)
            if ds is None:
                self._warn(tr("Il disegno libero prende la profondità dal rilievo del prospetto: serve il rilievo "
                              "sotto almeno un vertice"))
                return
            xyz = np.array([E.world_of(entry, u, v, d) for (u, v), d in zip(xy, ds)], float)
        self._free_add(kind, xyz)

    def _relief_values(self, entry: dict, uv) -> list:
        """The depth of the relief of the elevation ``entry`` at each (u, v), None where it has none (one reading of
        the file for every vertex; :func:`..engine.elevation.relief_at` per point)."""
        from ..core import free_drawings
        from ..engine import elevation as E

        _ortho, relief = E.elevation_paths(self.chunk, entry)
        return free_drawings.raster_values(relief, uv)

    def _free_selected_in_3d(self, fid: str) -> None:
        self._free_selected = fid or ""

    def _free_vertex_moved(self, fid: str, index: int, internal) -> None:
        from ..core import free_drawings

        rec = self._free_record(fid)
        if rec is None or not 0 <= index < len(rec["xyz"]):
            return
        xyz = [list(p) for p in rec["xyz"]]
        xyz[index] = [float(v) for v in self.chunk.to_world(np.asarray(internal, float).reshape(1, 3))[0]]
        free_drawings.update(self.chunk, fid, xyz=xyz)
        self._dirty(True)
        self._draw_free()

    def _free_delete_requested(self, fid: str, index: int) -> None:
        """Delete from the 3D view: the drawing (``index`` -1) or one vertex, refused when a polygon would keep fewer
        than three vertices or a line fewer than two (the drawing is deleted whole instead)."""
        from ..core import free_drawings

        rec = self._free_record(fid)
        if rec is None:
            return
        if index < 0 or rec["kind"] == "point":
            free_drawings.remove(self.chunk, fid)
            self._dirty(True)
            self._draw_free()
            self.log_pane.info(tr("{0} eliminato", rec["name"]))
            return
        if not 0 <= index < len(rec["xyz"]):
            return
        if len(rec["xyz"]) - 1 < free_drawings.MIN_VERTICES[rec["kind"]]:
            self.statusBar().showMessage(
                tr("Un poligono ha almeno tre vertici: elimina il disegno intero") if rec["kind"] == "polygon" else
                tr("Una linea ha almeno due vertici: elimina il disegno intero"), 15000)
            return
        xyz = [p for i, p in enumerate(rec["xyz"]) if i != index]
        free_drawings.update(self.chunk, fid, xyz=xyz)
        self._dirty(True)
        self._draw_free()

    def _model_busy_message(self, text: str) -> None:
        """A line of the 3D view for the status bar (the model being prepared for drawing); "": cleared."""
        if text:
            self.statusBar().showMessage(text, 15000)
        else:
            self.statusBar().clearMessage()

    def _free_draped(self, fid: str, internal) -> None:
        from ..core import free_drawings

        if self._free_record(fid) is None:
            return
        xyz = self.chunk.to_world(np.asarray(internal, float).reshape(-1, 3))
        free_drawings.update(self.chunk, fid, xyz=xyz.tolist())
        self._dirty(True)
        self._draw_free()

    def _draw_free(self) -> None:
        """The free drawings of the active chunk shown again in the 3D view and on the 2D view."""
        from ..core import free_drawings

        ch = self.chunk
        if ch is None:
            self._draw_free_3d()
            self._draw_free_2d()
            return
        read = (free_drawings.layers(ch), free_drawings.features(ch))  # read once for both views
        self._draw_free_3d(read)
        self._draw_free_2d(read)

    def _draw_free_3d(self, read=None) -> None:
        """``read``: the layers and the drawings of the active chunk already read (:meth:`_draw_free`)."""
        from ..core import free_drawings

        ch = self.chunk
        if ch is None or self.model_view.chunk is not ch:
            return
        # once per redraw: reading them per drawing made a redraw quadratic
        layers, feats = read or (free_drawings.layers(ch), free_drawings.features(ch))
        hidden = {x["name"] for x in layers if not x.get("visible", True)}
        shown = [f for f in feats if f["layer"] not in hidden]
        self.model_view.set_free(shown, {f["id"]: free_drawings.colour_of(ch, f, layers) for f in shown})
        schedule = getattr(self, "_schedule_elevation_drawings_3d", None)
        if schedule is not None:  # the drawings of the elevations, beside them (gui.elevation_drawing_actions)
            schedule()

    def _draw_free_2d(self, read=None) -> None:
        """Per user layer a layer :data:`LAYER_PREFIX` + its name on the 2D view: on the plan the drawings by their x,
        y; on an elevation the (u, v) of the parts whose depth is within the elevation's depth (+- two cells)."""
        from ..core import free_drawings

        v, ch = self.ortho_view, self.chunk
        if not hasattr(self, "free_layer"):  # the window is being built
            return
        for name in [n for n in v.layer_order if n.startswith(LAYER_PREFIX)]:
            v.remove_layer(name)
        if ch is None or v.tr is None:
            return
        entry = self._frame_entry()
        frame, band = None, None
        if entry is not None:
            from ..engine.elevation import frame_from_dict

            frame = frame_from_dict(entry["frame"])
            lo, hi = entry.get("depth") or (-np.inf, np.inf)
            margin = 2.0 * float(entry.get("resolution") or 0.0)
            band = (float(lo) - margin, float(hi) + margin)
        # the layers and the drawings read once per redraw (``read``: already read by :meth:`_draw_free`)
        layers, feats = read or (free_drawings.layers(ch), free_drawings.features(ch))
        by_layer: dict[str, list] = {}
        for rec in feats:
            by_layer.setdefault(rec["layer"], []).append(rec)
        with v.batch():
            for lay in layers:
                feats = by_layer.get(lay["name"])
                if not feats:
                    continue
                name = LAYER_PREFIX + lay["name"]
                for rec in feats:
                    colour = free_drawings.colour_of(ch, rec, layers)
                    attrs = {ID_FIELD: rec["id"], "_free": True, "name": rec["name"]}
                    xyz = np.asarray(rec["xyz"], float)
                    if frame is None:
                        self._add_free_part(name, rec["kind"], xyz[:, :2], colour, rec["name"], attrs, closed=True)
                        continue
                    uvd = frame.coords(xyz)
                    inside = (uvd[:, 2] >= band[0]) & (uvd[:, 2] <= band[1])
                    if inside.all():
                        self._add_free_part(name, rec["kind"], uvd[:, :2], colour, rec["name"], attrs, closed=True)
                        continue
                    if rec["kind"] == "point" or not inside.any():
                        continue
                    for run in _runs(inside, closed=rec["kind"] == "polygon"):  # the parts on the face: lines
                        if len(run) >= 2:
                            self._add_free_part(name, "line", uvd[run, :2], colour, rec["name"], attrs, closed=False)
                if name in v.layers and not lay.get("visible", True):
                    v.set_layer_visible(name, False)

    def _add_free_part(self, layer: str, kind: str, xy, colour: str, label: str, attrs: dict, closed: bool) -> None:
        v = self.ortho_view
        if kind == "point":
            v.add_point(float(xy[0][0]), float(xy[0][1]), colour, label, layer=layer, attrs=dict(attrs))
        elif kind == "polygon" and closed:
            v.add_polygon(xy, label, colour, fill_alpha=40, layer=layer, attrs=dict(attrs))
        else:
            v.add_line(xy, colour, label, layer=layer, attrs=dict(attrs), width=2.0)

    def _layer_visibility_changed(self, name: str, visible: bool) -> None:
        """A layer of the free drawings shown or hidden in the Layer pane: kept in the chunk, the 3D view follows."""
        if self._set_free_layer_shown(name, visible):
            self._draw_free_3d()
            self._dirty(True)

    def _set_free_layer_shown(self, name: str, visible: bool) -> bool:
        """The layer ``name`` of the view, if it is one of the free drawings, shown or hidden in the chunk; whether it
        changed."""
        if not name.startswith(LAYER_PREFIX) or self.chunk is None:
            return False
        from ..core import free_drawings

        layer = name[len(LAYER_PREFIX):]
        if next((x for x in free_drawings.layers(self.chunk) if x["name"] == layer), {}).get("visible", True) == visible:
            return False
        free_drawings.set_layer(self.chunk, layer, visible=visible)
        return True

    def _layers_visibility_set(self, changed: dict) -> None:
        """*Tutti* / *Nessuno* in the Layer pane (``{layer: (before, now)}``): the free drawings' layers kept in the
        chunk, and ONE step of Modifica > Annulla for all the layers changed, free or not."""
        free = False
        for name, (_before, now) in changed.items():
            free = self._set_free_layer_shown(name, now) or free
        if free:
            self._draw_free_3d()
            self.setWindowModified(True)
        if self.history is not None and changed and not self.history.applying:
            self.history.push(tr("Visibilità dei layer"), LayersShown(self, changed), document=False)

    def _feature_double_clicked(self, layer: str, attrs: dict) -> None:
        if attrs.get("_free") and attrs.get(ID_FIELD):
            self.free_properties(str(attrs[ID_FIELD]))

    def _selected_free_id(self) -> str:
        """The free drawing selected: in the 2D view (one), else in the 3D view."""
        free = [a for _l, _i, a in self.ortho_view.selected_features() if a.get("_free")]
        if len(free) == 1:
            return str(free[0].get(ID_FIELD))
        return self.model_view.selected_free or self._free_selected or ""

    def free_properties(self, fid: str | None = None) -> bool:
        """The form of the free drawing ``fid`` (default: the one selected in 2D or 3D); its changes written as one
        step of *Modifica > Annulla*. Whether something changed."""
        from ..core import free_drawings
        from . import free_drawing_dialog as fdd

        fid = fid or self._selected_free_id()
        rec = self._free_record(fid)
        if rec is None:
            self.statusBar().showMessage(tr("Seleziona un disegno libero (nella vista 2D o 3D)"), 10000)
            return False
        values = fdd.FreeDrawingDialog.ask(self, self.chunk, rec)
        if not values:
            return False
        ch = self.chunk
        names = {x["name"] for x in free_drawings.layers(ch)}
        fields = {k: values[k] for k in ("name", "layer", "attributes", "xyz")}
        if values.get("colour") == "":  # back to the layer's colour
            fields["colour"] = None
        elif values.get("colour"):
            fields["colour"] = values["colour"]
        try:
            free_drawings.update(ch, fid, **fields)
        except ValueError as exc:
            self._warn(tr("Disegno non modificato: {0}", str(exc)))
            return False
        if values["layer"] not in names:  # a new layer, kept with the change (one step)
            free_drawings.set_layer(ch, values["layer"])
        self._dirty(True)
        self._draw_free()
        return True

    def export_free_drawings(self) -> None:
        """*File > Esporta > Disegni 3D...*: the free drawings of the active chunk (all, or one layer) into a DXF 3D, a
        GeoPackage with Z, a GeoJSON or a CSV of the vertices, by the suffix of the file."""
        if not self._need_chunk():
            return
        from ..core import free_drawings
        from ..io import free_drawings as fio

        if not free_drawings.features(self.chunk):
            self._warn(tr("Nessun disegno libero: disegnali con Disegno libero (vista 2D) o Disegna (vista 3D)."))
            return
        path, _f = QFileDialog.getSaveFileName(
            self, tr("Esporta i disegni 3D"), str(Path(self._last_dir()) / "disegni_3d.dxf"),
            "DXF (*.dxf);;GeoPackage (*.gpkg);;GeoJSON (*.geojson);;CSV (*.csv)")
        if not path:
            return
        if Path(path).suffix.lower() not in fio.SUFFIXES:
            path += ".dxf"
        names = [x["name"] for x in free_drawings.layers(self.chunk) if free_drawings.features(self.chunk, x["name"])]
        everything = tr("Tutti i layer")
        layer = None
        if len(names) > 1:
            chosen, ok = QInputDialog.getItem(self, tr("Esporta i disegni 3D"), tr("Layer:"), [everything, *names], 0,
                                              False)
            if not ok:
                return
            layer = None if chosen == everything else chosen
        try:
            out = fio.export(self.chunk, path, layer=layer)
        except Exception as exc:
            self._exc("Esportazione dei disegni 3D non riuscita", exc)
            return
        n = len(free_drawings.features(self.chunk, layer))
        self.log_pane.info(tr("{0} disegni 3D esportati in {1}", n, out))
        self.statusBar().showMessage(tr("{0} disegni 3D esportati in {1}", n, out), 15000)

    def import_free_drawings(self) -> None:
        """*File > Importa > Disegni 3D...*: lines, polygons and points of a GeoPackage or a GeoJSON as free drawings of
        the active chunk (z from the DEM when the file has none), one step of *Modifica > Annulla*."""
        if not self._need_chunk():
            return
        path, _f = QFileDialog.getOpenFileName(self, tr("Importa disegni 3D"), self._last_dir(),
                                               "GeoPackage, GeoJSON, DXF (*.gpkg *.geojson *.json *.dxf)")
        if not path:
            return
        from ..core import free_drawings

        from_file = tr("Dal file (colonna layer, altrimenti il nome del file)")
        names = [x["name"] for x in free_drawings.layers(self.chunk)]
        chosen, ok = QInputDialog.getItem(self, tr("Importa disegni 3D"), tr("Layer:"), [from_file, *names], 0, True)
        if not ok:
            return
        chosen = str(chosen or "").strip()
        self._import_free_file(path, None if not chosen or chosen == from_file else chosen)

    def _import_free_file(self, path, layer) -> None:
        """The free drawings of ``path`` imported into the active chunk in ``layer`` (None: the file's), said in the
        log; a missing grid of the geoid offered for download, then the import again."""
        from ..gnss import heights
        from ..io import free_drawings as fio

        try:
            got = fio.import_features(self.chunk, path, layer=layer)
        except heights.GridMissing as exc:
            self._grid_missing(exc, tr("Importa disegni 3D"), retry=lambda: self._import_free_file(path, layer))
            return
        except fio.StaleDem:
            self._warn(tr("I disegni del file non hanno quote e il DEM va rifatto prima di prenderle (Flusso di "
                          "lavoro > Ricostruisci i prodotti da rifare): nessun disegno importato"))
            return
        except fio.ForeignCrs as exc:
            if exc.reason == "unreadable":
                self._warn(tr("Il file nomina un sistema di riferimento che non si riesce a leggere ({0}): nessun "
                              "disegno importato", str(exc.file_crs)))
            elif exc.reason == "chunk_local":
                self._warn(tr("I disegni del file sono in {0} e il chunk è in un sistema locale: nessun disegno "
                              "importato", str(exc.file_crs)))
            else:
                self._warn(tr("Il file non dice in quale sistema di riferimento sono i disegni e il chunk è in {0}: "
                              "nessun disegno importato", str(exc.chunk_crs)))
            return
        except Exception as exc:
            self._exc("Importazione dei disegni 3D non riuscita", exc)
            return
        if got["added"]:
            self._dirty(True)
            self._draw_free()
        text = tr("{0} disegni 3D importati da {1} ({2} con le quote del DEM, {3} saltati)",
                  got["added"], Path(path).name, got["dem_z"], got["skipped"])
        if got.get("rfc7946"):
            text += tr("; GeoJSON senza sistema letto come WGS 84 (RFC 7946) e convertito in {0}", self.chunk.crs)
        if got.get("holes_dropped"):
            text += tr("; {0} buchi di poligoni lasciati fuori (un disegno libero ha un solo contorno)",
                       got["holes_dropped"])
        self.log_pane.info(text)
        self.statusBar().showMessage(text, 15000)

    def _free_measures(self, fid: str) -> None:
        """*Misura area* on a selected free drawing: its measures in 3D (on the model too, for a polygon: taken in the
        background, the model read and let go, the result shown when it is ready)."""
        from . import free_drawing_dialog as fdd

        rec = self._free_record(fid)
        if rec is None:
            return
        from ..engine.orient import toward_cameras

        doc, chunk = self.doc, self.chunk

        def show(text: str) -> None:
            if self.doc is not doc or self.chunk is not chunk:  # another project or chunk meanwhile: not its measures
                log.info("Misure di %s lasciate: il chunk misurato non è più quello attivo", rec["name"])
                return
            self.log_pane.info(f"{rec['name']}: {text}")
            self._info(tr("Misure del disegno libero"), f"{rec['name']}\n{text}")

        if fdd.measure_later(self, self.chunk, rec, toward_cameras(self.chunk, np.asarray(rec["xyz"], float)), show):
            self.statusBar().showMessage(tr("Misure di {0} in calcolo...", rec["name"]), 10000)
