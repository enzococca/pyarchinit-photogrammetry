"""The pane *Acqua: livelli e bacini*: the table of levels per date, basins from a level and from points picked on the
shoreline, their polygons drawn or from a layer, the factor estimated on markers with the residuals in a table and
applied, the correction of the cloud, every change one step of Annulla. The synthetic pond of ``test_water``."""
from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from shapely.geometry import box  # noqa: E402
from test_gui_water import _close, _window, qapp  # noqa: E402,F401
from test_water import LEVEL, X0, Y0, basin, project  # noqa: E402

from pyarchinit_photogrammetry.engine import water  # noqa: E402


def test_basins_factor_and_correction_from_the_pane(qapp, tmp_path, monkeypatch):  # noqa: F811
    from pyarchinit_photogrammetry.archaeo.timeline import set_phase_date

    doc, a = project(tmp_path)
    set_phase_date(a, "2024-09-02")
    doc.save()
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        pane = w.water_pane
        w.show_water_pane()
        pane.refresh()
        assert pane.step_states["table"] == "todo"
        table = tmp_path / "livelli.csv"
        table.write_text("date;level;n\n2024-09-02;0,80;1,34\n", encoding="utf-8")
        n0 = len(w.history.entries)
        assert pane.load_table(str(table))
        assert water.level_of(a)["level"] == pytest.approx(0.80) and pane.step_states["table"] == "done"
        assert len(w.history.entries) == n0 + 1
        # a basin from the level written, everywhere
        pane.e_name.setText("Resto")
        pane.s_level.setValue(0.78)
        pane.s_n.setValue(1.33)
        assert pane.add_from_level()
        assert pane.basins.rowCount() == 1 and pane.step_states["basins"] == "done"
        # a basin from three points clicked on the shore: the plane through their heights on the DEM
        pane.e_name.setText("Est")
        r = 2.0
        pts = np.array([[X0 + r, Y0], [X0, Y0 + r], [X0 - r, Y0], [X0, Y0 - r]])
        assert pane.shore_picked(pts) is not False  # a second basin without polygon: the drawing is asked for
        drawn = box(X0, Y0 - 3, X0 + 3, Y0 + 3)
        pane._polygon_drawn("Est", np.asarray(drawn.exterior.coords)[:-1],
                            water.basin("Est", n=1.33, shore=np.column_stack([pts, basin(pts[:, 0], pts[:, 1])])))
        names = [b["name"] for b in water.basins_of(a)]
        assert names == ["Resto", "Est"]
        est = water.basins_of(a)[1]
        assert est["level"] == pytest.approx(LEVEL, abs=0.01) and est["polygon"] is not None
        # the factor of the basin chosen in the table
        pane._select("Est")
        pane.s_n.setValue(1.31)
        pane.s_level.setValue(0.80)
        assert pane.set_selected()
        assert water.basins_of(a)[1]["n"] == pytest.approx(1.31)
        steps = len(w.history.entries)
        assert pane.remove_selected() and [b["name"] for b in water.basins_of(a)] == ["Resto"]
        assert len(w.history.entries) == steps + 1
        w.undo()
        assert [b["name"] for b in water.basins_of(a)] == ["Resto", "Est"]
        # the factor from markers triangulated through the water
        for k, (dx, dy) in enumerate([(0.3, 0.2), (-0.4, 0.3), (0.5, -0.5)]):
            x, y = X0 + dx, Y0 + dy
            app = basin(x, y)
            m = a.add_marker(f"T{k}", position=a.to_internal(np.array([[x, y, app]]))[0])
            m.reference.location = [x, y, LEVEL - 1.32 * (LEVEL - app)]
        pane.refresh()
        assert pane.markers.count() == 3
        for i in range(pane.markers.count()):
            pane.markers.item(i).setCheckState(pane.markers.item(i).checkState().Checked)
        pane.c_basin.setCurrentIndex(pane.c_basin.findData("Est"))
        fit = pane.estimate()
        assert fit is not None and fit["n"] == pytest.approx(1.32, abs=1e-6)
        assert pane.residuals.rowCount() == 3 and pane.residuals.item(0, 0).text() == "T0"
        assert pane.apply() and water.basins_of(a)[1]["n"] == pytest.approx(1.32, abs=1e-6)
        assert pane.step_states["factor"] == "done"
        # the correction in the worker
        assert pane.correct()
        w.runner.wait()
        qapp.processEvents()
        assert (a.meta.get("dense") or {}).get("refraction")
        assert pane.step_states["correct"] == "done"
    finally:
        _close(w)
