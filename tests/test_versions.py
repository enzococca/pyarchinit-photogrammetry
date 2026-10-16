"""Versions of the products: keep the active one, build again, show, activate, compare."""
from __future__ import annotations

import numpy as np
import pytest
import rasterio

from pyarchinit_photogrammetry.core import geometry, versions
from pyarchinit_photogrammetry.core.pointcloud import PointCloud
from pyarchinit_photogrammetry.testing.synthetic import make_classified_cloud


@pytest.fixture
def excavation(tmp_path):
    import pyarchinit_photogrammetry as ppg

    xyz, rgb, codes = make_classified_cloud(spacing=0.04)
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    ch.crs = "EPSG:3004"
    ch.transform = geometry.compose_similarity(1.0, np.eye(3), np.round(xyz.mean(0)))
    doc.save(tmp_path / "scavo.ppg")
    ch.point_cloud = PointCloud(ch.to_internal(xyz), rgb, classification=codes)
    return ch


def _read(path):
    with rasterio.open(path) as src:
        return src.read(1, masked=True).filled(np.nan)


def test_dem_versions_compare_and_switch(excavation):
    ch = excavation
    dsm = _read(ch.build_dem(resolution=0.05))
    assert versions.versions(ch, "dem") == [] and versions.active_info(ch, "dem")["name"].startswith("DEM del ")
    kept = versions.keep_active(ch, "dem", move=True)
    assert ch.product_path("dem") is None and kept["params"]["classes"] is None
    ch.build_dem(resolution=0.05, classes=[2])
    versions.set_active_name(ch, "dem", "DTM solo terreno")
    dtm = _read(ch.dem_path)
    (old,) = versions.versions(ch, "dem")
    assert np.allclose(_read(versions.version_file(ch, "dem", old["id"])), dsm, equal_nan=True)
    assert not np.allclose(dtm, dsm, equal_nan=True)
    # DTM - DSM: zero on the ground, minus the height of the wall where it stands
    diff = _read(versions.dem_difference(ch, old["id"]))
    finite = np.isfinite(diff)
    assert np.median(np.abs(diff[finite])) < 0.01 and diff[finite].min() < -0.6
    # the DSM back: the DTM is kept in its place
    previous = versions.activate(ch, "dem", old["id"])
    assert previous["name"] == "DTM solo terreno" and np.allclose(_read(ch.dem_path), dsm, equal_nan=True)
    assert ch.meta["dem"]["classes"] is None and [v["name"] for v in versions.versions(ch, "dem")] == ["DTM solo terreno"]
    versions.rename(ch, "dem", previous["id"], "Terreno")
    versions.rename(ch, "dem", None, "Superficie")
    assert versions.active_info(ch, "dem")["name"] == "Superficie"
    assert versions.versions(ch, "dem")[0]["name"] == "Terreno"
    folder = versions.version_file(ch, "dem", previous["id"]).parent
    versions.delete(ch, "dem", previous["id"])
    assert versions.versions(ch, "dem") == [] and not folder.exists()
    with pytest.raises(KeyError):
        versions.delete(ch, "dem", previous["id"])


def test_versions_of_clouds_models_and_outside_files(excavation, tmp_path):
    ch = excavation
    n = len(ch.point_cloud)
    kept = versions.keep_active(ch, "point_cloud")  # a copy: the active cloud stays
    assert len(ch.point_cloud) == n and len(PointCloud.load(versions.version_file(ch, "point_cloud", kept["id"]))) == n
    ch.point_cloud = ch.point_cloud.subset(np.arange(n) < n // 2)
    versions.activate(ch, "point_cloud", kept["id"])
    assert len(ch.point_cloud) == n and len(versions.versions(ch, "point_cloud")) == 1
    # the model with its preview; a failed build puts the active one back
    ch.build_model(surface="height_field")
    (ch.data_dir / "model_preview.ply").write_bytes((ch.data_dir / "model.ply").read_bytes())
    faces = len(ch.model.faces)
    kept = versions.keep_active(ch, "model", move=True)
    assert ch.model is None and not (ch.data_dir / "model_preview.ply").exists()
    versions.restore(ch, "model", kept)
    assert len(ch.model.faces) == faces and (ch.data_dir / "model_preview.ply").exists()
    assert versions.versions(ch, "model") == []
    # an orthomosaic exported by another program is never moved, only copied
    outside = tmp_path / "ortofoto_metashape.tif"
    outside.write_bytes(b"not really a tif")
    ch._set_product("orthomosaic", outside)
    kept = versions.keep_active(ch, "orthomosaic", move=True)
    assert outside.exists() and versions.version_file(ch, "orthomosaic", kept["id"]).read_bytes() == b"not really a tif"
    with pytest.raises(ValueError):
        versions.versions(ch, "tie_points")


def test_versions_in_the_interface(excavation, tmp_path, monkeypatch):
    """Build a DEM, keep it and build a terrain model; the workspace lists the kept version,
    which can be shown, compared, made active and deleted."""
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    app = QApplication.instance() or QApplication([])
    from pyarchinit_photogrammetry.gui import main_window as mw

    ch = excavation
    w = mw.MainWindow(ch.document, autoconnect=False)
    w.runner.show_dialog = False
    monkeypatch.setattr(w, "_need_chunk", lambda *a, **k: True)  # a cloud without photos is enough here
    monkeypatch.setattr(w, "_warn", lambda text: pytest.fail(text))
    monkeypatch.setattr(w, "_error", lambda summary, details="", logged=True: pytest.fail(f"{summary}\n{details}"))
    try:
        asked = []

        def ask(answers):
            def fake(cls, parent, title, fields, intro=""):
                asked.append({f.name: f for f in fields})
                return answers
            return classmethod(fake)

        monkeypatch.setattr(mw.ParamDialog, "ask", ask({"source": "point_cloud", "resolution": 0.05,
                                                         "interpolate": True, "version_name": "Superficie"}))
        w.build_dem()
        w.runner.wait()
        assert "keep" not in asked[-1] and versions.active_info(ch, "dem")["name"] == "Superficie"
        monkeypatch.setattr(mw.ParamDialog, "ask", ask({"source": "point_cloud", "resolution": 0.05,
                                                         "interpolate": True, "classes": [2], "keep": "keep",
                                                         "version_name": "Terreno"}))
        w.build_dem()
        w.runner.wait()
        app.processEvents()
        assert asked[-1]["keep"].choices[0][1].endswith("(Superficie)")
        (kept,) = versions.versions(ch, "dem")
        assert kept["name"] == "Superficie" and versions.active_info(ch, "dem")["name"] == "Terreno"
        labels = []

        def walk(item):
            labels.append(item.text(0))
            for i in range(item.childCount()):
                walk(item.child(i))

        walk(w.workspace.topLevelItem(0))
        assert any(t.startswith("DEM  -  Terreno") for t in labels) and any(t.startswith("Superficie") for t in labels)
        w.show_version("dem", kept["id"])
        assert w.ortho_view.path == versions.version_file(ch, "dem", kept["id"])
        w.dem_difference(kept["id"])
        w.runner.wait()
        app.processEvents()
        assert w.ortho_view.kind == "diff" and w.ortho_view.diff_limit > 0.3  # the wall is gone from the terrain
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Superficie (DSM)", True))
        w.rename_version("dem", kept["id"])
        w.activate_version("dem", kept["id"])
        w.runner.wait()
        app.processEvents()
        assert versions.active_info(ch, "dem")["name"] == "Superficie (DSM)" and ch.meta["dem"]["classes"] is None
        (other,) = versions.versions(ch, "dem")
        monkeypatch.setattr(mw, "message", lambda *a, **k: QMessageBox.StandardButton.Yes)
        w.delete_version("dem", other["id"])
        assert versions.versions(ch, "dem") == []
        # a kept copy of the dense cloud shown in the 3D view, then the active one again
        w.keep_version_copy("point_cloud")
        w.runner.wait()
        (cloud,) = versions.versions(ch, "point_cloud")
        w.show_version("point_cloud", cloud["id"])
        mv = w.model_view
        assert mv.paths and mv.version_label.isVisibleTo(mv) and not mv.b_lasso.isVisibleTo(mv)
        mv.combo.setCurrentIndex(mv.combo.findData("cameras"))
        assert not mv.paths and not mv.version_label.isVisibleTo(mv)
    finally:
        w.setWindowModified(False)
        w.close()
