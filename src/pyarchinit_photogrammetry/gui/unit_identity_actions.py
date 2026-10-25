"""The desktop's bulk identifier action, using one undoable drawing update."""
from __future__ import annotations

from ..archaeo.unit_identity import apply_changes
from .i18n import tr
from .unit_identity_dialog import UnitIdentityDialog


def reserved_units(window, units=None):
    """Reserve saved drawings and connected forms; never silently ignore an archive error."""
    reserved = []
    doc = window.doc
    for chunk in doc.chunks if doc else ():
        if chunk is window.chunk:
            continue
        for row in (chunk.meta.get("drawings") or {}).get("units", []):
            reserved.append(row)
    pa = window.pa
    if pa is not None:
        sites = {u.sito for u in (units or window.units) if u.sito}
        if window.project_site():
            sites.add(window.project_site())
        for site in sites:
            reserved.extend(pa.stratigraphic_units(site))
        if pa.db.has_table("ut_table"):
            reserved.extend(pa.topographic_units())
    return reserved


def identify(window):
    if not window.units:
        window._warn(tr("Crea prima le unità dalle proposte SAM o disegnale sul canvas."))
        return
    selected = {int(a["_unit"]) for _, _, a in window.ortho_view.selected_features() if "_unit" in a}
    try:
        reserved = reserved_units(window)
        changes = UnitIdentityDialog.ask(window.units, selected, parent=window, reserved=reserved,
                                          defaults={"progetto": window.project_site() or
                                                    (window.doc.path.stem if window.doc and window.doc.path else "")})
        if changes is None:
            return
        apply_to_window(window, changes)
    except (ValueError, RuntimeError, OSError) as exc:
        window._warn(str(exc))
    except Exception as exc:
        window._exc("Identificazione delle unità non riuscita", exc)


def apply_to_window(window, changes):
    changes = list(changes)
    if not changes:
        return 0
    before = [(window.units[c.index], (c.before.tipo, c.before.us, c.before.area), False) for c in changes]
    count = apply_changes(window.units, changes)
    for change in changes:
        u = window.units[change.index]
        gid = (u.attributes or {}).get("gid")
        if gid is not None:
            window.pa_pending[int(gid)] = u
    window._update_pa_save_button()
    window._redraw_overlays()
    window._store_drawings()
    window.setWindowModified(True)
    if window.history is not None:
        window.history.record(tr("Identificativi e gruppi: {0} unità", count))
    window._schedule_qgis()
    window._follow_proxies(before)
    window.log_pane.info(tr("Identificativi e gruppi aggiornati: {0} unità", count))
    window.statusBar().showMessage(tr("{0} unità aggiornate; Modifica > Annulla ripristina il gruppo precedente", count),
                                    12000)
    # Keep the edited group selected after the redraw, including every multipart feature.
    indices = {change.index for change in changes}
    items = [item for layer in window.ortho_view.layers.values() if layer["visible"]
             for item, _, attrs in layer["features"] if attrs.get("_unit") in indices]
    window.ortho_view.select_items(items)
    return count


def publish_ut_records(window):
    """An explicit user action creates UT forms; geometry stays in the project/GeoPackage."""
    if not window._need_pa() or not window._need_chunk():
        return
    selected = {a["_unit"] for _, _, a in window.ortho_view.selected_features() if "_unit" in a}
    units = [u for i, u in enumerate(window.units) if u.tipo == "UT" and (not selected or i in selected)]
    if not units:
        window._warn(tr("Seleziona unità topografiche oppure crea prima delle UT."))
        return
    if any(not u.us or not (u.attributes or {}).get("progetto") for u in units):
        window._warn(tr("Assegna prima numero e progetto alle UT con Identifica e raggruppa unità."))
        return
    prepared = []
    for u in units:
        a = u.attributes or {}
        fields = {"def_ut": a.get("def_ut") or a.get("samgeo_prompt") or "",
                  "descrizione_ut": a.get("gruppo") or a.get("nome") or ""}
        project, name = a["progetto"], a.get("ut_letterale") or a.get("nome") or ""
        linked = window.pa.db.query("ut_table", id_ut=a["id_ut"]) if a.get("id_ut") is not None else []
        record = linked[0] if linked else window.pa.ut_record(project, u.us, name)
        prepared.append((u, project, name, fields, record))
    # Ask only as the final step before actually writing external records.
    from PySide6.QtWidgets import QMessageBox
    answer = QMessageBox.question(window, "PyArchInit — UT",
        tr("Salvare {0} schede in ut_table? Le geometrie restano nel progetto e sono esportabili in GeoPackage.", len(units)))
    if answer != QMessageBox.StandardButton.Yes:
        return
    for u, project, name, fields, record in prepared:
        if record:
            window.pa.update_ut(record["id_ut"], progetto=project, nr_ut=u.us, ut_letterale=name, **fields)
            u.attributes["id_ut"] = int(record["id_ut"])
        else:
            u.attributes["id_ut"] = window.pa.create_ut(project, u.us, name, **fields)
    window._dirty(True)
    window._store_drawings()
    window._redraw_overlays()
    window.log_pane.info(tr("{0} schede UT salvate in PyArchInit", len(units)))


def open_ut_form(window, unit):
    if not window._need_pa():
        return
    from .dialogs import Field, ParamDialog

    a = unit.attributes or {}
    project = a.get("progetto") or unit.sito or window.project_site() or ""
    linked = window.pa.db.query("ut_table", id_ut=a["id_ut"]) if a.get("id_ut") is not None else []
    record = (linked[0] if linked else window.pa.ut_record(project, unit.us,
              a.get("ut_letterale") or a.get("nome") or "") if unit.us and project else None)
    defaults = record or a
    values = ParamDialog.ask(window, tr("Scheda UT — PyArchInit"), [
        Field("progetto", tr("Progetto"), project),
        Field("nr_ut", tr("Numero UT"), int(unit.us) if unit.us else window.pa.next_ut(project) if project else 1, minimum=1),
        Field("ut_letterale", tr("Nome"), a.get("ut_letterale") or a.get("nome") or ""),
        Field("def_ut", tr("Definizione UT"), defaults.get("def_ut") or a.get("samgeo_prompt") or ""),
        Field("descrizione_ut", tr("Descrizione"), defaults.get("descrizione_ut") or "")],
        intro=tr("La scheda viene salvata in ut_table. La geometria rimane nel progetto, esportabile in GeoPackage."))
    if values is None:
        return
    try:
        from ..archaeo.unit_identity import plan_units

        changes = plan_units(window.units, [next(i for i, u in enumerate(window.units) if u is unit)],
                             number_mode="keep", progetto=values["progetto"], name_mode="same", name=values["ut_letterale"])
        # UT forms can be edited only under their existing identity here; use the bulk action for renumbering.
        if str(values["nr_ut"]) != str(unit.us or values["nr_ut"]):
            raise ValueError(tr("Per cambiare il numero usa Identifica e raggruppa unità."))
        if record:
            window.pa.update_ut(record["id_ut"], **values)
            identifier = record["id_ut"]
        else:
            identifier = window.pa.create_ut(**values)
        apply_to_window(window, changes)
        unit.attributes["id_ut"] = int(identifier)
        unit.us = str(values["nr_ut"])
        unit.attributes["nr_ut"] = int(values["nr_ut"])
        window._store_drawings()
        window._redraw_overlays()
        window._dirty(True)
    except (ValueError, RuntimeError, OSError, FileExistsError) as exc:
        window._warn(str(exc))
