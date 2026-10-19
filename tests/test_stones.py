"""Stone-by-stone drawing: SAM's masks filtered by size and shape, exported for a drawing."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from pyarchinit_photogrammetry.archaeo import stones as st
from pyarchinit_photogrammetry.engine.dem import write_geotiff

RES = 0.005  # a 2 x 1.5 m wall face, 5 mm pixels


class WallSegmenter:
    """SAM-like masks of a wall face: every stone, the mortar around them and a few specks."""

    def segment_auto(self, image, **options):
        dark = (image[..., 0] < 100).astype(np.uint8)
        n, lab = cv2.connectedComponents(dark)
        masks = [lab == i for i in range(1, n)]
        masks.append(dark == 0)  # the mortar: one big mask
        speck = np.zeros(dark.shape, bool)
        speck[5:8, 5:8] = True  # a 1.5 cm speck
        masks.append(speck)
        return masks


@pytest.fixture
def wall(tmp_path):
    rng = np.random.default_rng(3)
    img = np.full((300, 400, 3), 190, np.uint8)  # mortar
    centres = []
    for r in range(40, 300, 70):
        for c in range(45, 400, 85):
            a, b = rng.integers(22, 34), rng.integers(16, 26)
            cv2.ellipse(img, (int(c), int(r)), (int(a), int(b)), float(rng.integers(0, 30)), 0, 360, (70, 60, 55), -1)
            centres.append((c, r))
    path = write_geotiff(tmp_path / "prospetto.tif", np.moveaxis(img, -1, 0), (0.0, 1.5), RES, None)
    return path, len(centres)


def test_keep_stones_by_size_and_shape():
    from shapely.geometry import Point, Polygon

    stone = Point(0, 0).buffer(0.1)  # 20 cm
    speck = Point(1, 1).buffer(0.01)
    face = Polygon([(0, 0), (3, 0), (3, 2), (0, 2)])  # the whole wall: too big
    ring = Point(5, 5).buffer(0.2).difference(Point(5, 5).buffer(0.17))  # a thin crescent: not solid
    kept = st.keep_stones([stone, speck, face, ring], min_size=0.05, max_size=1.0)
    assert kept == [stone]


def test_find_and_export_stones(wall, tmp_path):
    import ezdxf

    path, n = wall
    stones = st.find_stones(path, WallSegmenter(), min_size=0.1, max_size=0.6, resolution=RES)
    assert len(stones) == n  # every stone once, no mortar, no speck
    assert all(0.1 <= st.size_of(s) <= 0.6 for s in stones)
    x0, y0, x1, y1 = path_bounds = (0.0, 0.0, 2.0, 1.5)
    assert all(x0 <= s.bounds[0] and s.bounds[2] <= x1 and y0 <= s.bounds[1] and s.bounds[3] <= y1 for s in stones)
    dxf = st.export_stones(stones, tmp_path / "pietre.dxf")
    lines = [e for e in ezdxf.readfile(dxf).modelspace() if e.dxftype() == "LWPOLYLINE"]
    assert len(lines) == n and all(e.closed and e.dxf.layer == "PIETRE" for e in lines)
    svg = st.export_stones(stones, tmp_path / "pietre.svg", scale=20).read_text()
    width = (max(s.bounds[2] for s in stones) - min(s.bounds[0] for s in stones)) * 50 + 20  # 1:20, 1 cm margins
    assert svg.count("<path") == n and f'width="{width:.1f}mm"' in svg
    gj = st.export_stones(stones, tmp_path / "pietre.geojson").read_text()
    assert gj.count('"Feature"') == n
    part = st.find_stones(path, WallSegmenter(), bounds=(0.0, 0.75, 1.0, 1.5), min_size=0.1, max_size=0.6,
                          resolution=RES)
    assert 0 < len(part) < n and path_bounds


def test_stones_in_the_window(wall, tmp_path, monkeypatch):
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    import shutil

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    import pyarchinit_photogrammetry as ppg

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    path, n = wall
    doc = ppg.Document()
    ch = doc.add_chunk("Muro")
    doc.save(tmp_path / "muro.ppg")
    ch.data_dir.mkdir(parents=True, exist_ok=True)
    ortho = shutil.copy(path, ch.data_dir / "orthomosaic.tif")
    ch._set_product("orthomosaic", ortho)
    doc.save()
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.show()
    app.processEvents()
    monkeypatch.setattr(w, "_segmenter", lambda: WallSegmenter())
    monkeypatch.setattr(w, "_sam_available", lambda then=None: True)
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(
        lambda cls, parent, title, fields, intro="": {"min_size": 0.1, "max_size": 0.6, "thoroughness": "normale",
                                                      "scale": 20.0}))
    w.draw_stones(None)
    w.runner.wait()
    app.processEvents()
    key = "orthomosaic.tif"
    assert len(w.stones[key]) == n and len(w.ortho_view.layers[mw.STONES_LAYER]["features"]) == n
    w.ortho_view.selection = [w.ortho_view.layers[mw.STONES_LAYER]["features"][0][0]]
    assert w.delete_selected(confirm=False) == 1
    assert len(w.stones[key]) == n - 1 and len(w.ortho_view.layers[mw.STONES_LAYER]["features"]) == n - 1
    out = tmp_path / "pietre.dxf"
    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    w.export_stones_drawing()
    assert out.exists()
    w.save_document()
    w.setWindowModified(False)
    w.close()
    again = mw.MainWindow(ppg.Document.open(tmp_path / "muro.ppg"), autoconnect=False, check_updates=False)
    assert len(again.stones[key]) == n - 1
    again.setWindowModified(False)
    again.close()
