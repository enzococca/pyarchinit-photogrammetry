"""Editing the DEM: an area filled again from the ground around it, the DEM as it was kept."""
from __future__ import annotations

import shutil

import numpy as np
import pytest
import rasterio

from pyarchinit_photogrammetry.engine import dem_edit

ORIGIN = np.array([2480000.0, 4500000.0, 100.0])  # the synthetic trench (testing.synthetic.make_scene)


def _copy(processed, tmp_path, name="copia"):
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path / name
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    out = ppg.Document.open(d / doc.path.name)
    return out, out.chunk


def test_new_heights(rng):
    known = np.column_stack([rng.uniform(0, 10, 200), rng.uniform(0, 10, 200), np.zeros(200)])
    known[:, 2] = 2.0 + 0.1 * known[:, 0] - 0.05 * known[:, 1]
    target = rng.uniform(2, 8, (50, 2))
    truth = 2.0 + 0.1 * target[:, 0] - 0.05 * target[:, 1]
    assert np.allclose(dem_edit.new_heights(known, target, "plane"), truth)
    assert np.allclose(dem_edit.new_heights(known, target, "linear"), truth, atol=1e-6)
    assert np.abs(dem_edit.new_heights(known, target, "idw") - truth).max() < 0.3
    assert np.allclose(dem_edit.new_heights(known, target, "constant", 5.0), 5.0)
    assert np.allclose(dem_edit.new_heights(known, target, "constant"), np.median(known[:, 2]))
    with pytest.raises(ValueError):
        dem_edit.new_heights(known, target, "spline")


def _read(path):
    with rasterio.open(path) as src:
        return src.read(1), src.transform


def test_wall_stub_removed_from_the_dem(processed, tmp_path):
    """The wall stub standing 0.30 m on the floor of the trench filled from the floor around it."""
    from shapely import contains_xy
    from shapely.geometry import box

    from pyarchinit_photogrammetry.core import versions

    doc, ch = _copy(processed, tmp_path)
    before, t = _read(ch.dem_path)
    area = box(*(ORIGIN[:2] + [1.62, 1.22]), *(ORIGIN[:2] + [2.38, 1.58]))
    stub = box(*(ORIGIN[:2] + [1.75, 1.35]), *(ORIGIN[:2] + [2.25, 1.45]))
    rows, cols = np.mgrid[0:before.shape[0], 0:before.shape[1]]
    x = t.c + (cols + 0.5) * t.a
    y = t.f + (rows + 0.5) * t.e
    in_stub, in_area = contains_xy(stub, x, y), contains_xy(area, x, y)
    floor = ORIGIN[2] - 0.35
    assert np.nanmedian(before[in_stub]) > floor + 0.2  # the stub is there
    info = ch.edit_dem(area, "plane")
    assert info["cells"] == int(in_area.sum()) and info["dz_max"] > 0.2
    after, _ = _read(ch.dem_path)
    assert abs(np.nanmedian(after[in_stub]) - floor) < 0.03
    assert np.array_equal(after[~in_area], before[~in_area], equal_nan=True)
    kept = versions.versions(ch, "dem")
    assert len(kept) == 1 and "modificato" in versions.active_info(ch, "dem")["name"]
    old, _ = _read(versions.version_file(ch, "dem", kept[0]["id"]))
    assert np.array_equal(old, before, equal_nan=True)
    assert ch.meta["dem_edits"][-1]["method"] == "plane"
    # a constant height and a breakline kept at its heights
    line = np.array([ORIGIN[:2] + [1.62, 1.40], ORIGIN[:2] + [2.38, 1.40]])
    ch.edit_dem(area, "constant", value=floor + 0.5, breaklines=[np.column_stack([line, [floor, floor]])],
                keep_version=False)
    after2, _ = _read(ch.dem_path)
    near_line = in_area & (np.abs(y - (ORIGIN[1] + 1.40)) < 0.01)
    assert np.nanmedian(after2[in_area & ~near_line]) == pytest.approx(floor + 0.5, abs=1e-3)
    info = ch.edit_dem(area, "linear", breaklines=[np.column_stack([line, [floor, floor]])], keep_version=False)
    after3, _ = _read(ch.dem_path)
    assert np.nanmax(np.abs(after3[near_line] - floor)) < 0.05


def test_interface_dem_edit_tool(processed, tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.core import versions
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, ch = _copy(processed, tmp_path)
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {
        "method": "plane", "value": None, "name": "Senza muretto"}))
    ring = ORIGIN[:2] + np.array([[1.62, 1.22], [2.38, 1.22], [2.38, 1.58], [1.62, 1.58]])
    w.set_draw_mode("dem_edit")
    w.ortho_view.dem_edit_finished.emit(ring)
    w.runner.wait()
    assert versions.active_info(w.chunk, "dem")["name"] == "Senza muretto"
    assert len(versions.versions(w.chunk, "dem")) == 1
    w.setWindowModified(False)
    w.close()
    app.processEvents()
