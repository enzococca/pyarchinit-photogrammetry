import cv2
import numpy as np
import pytest

from pyarchinit_photogrammetry.markers import targets


def test_sheet_roundtrip(tmp_path):
    files = targets.generate_sheet(tmp_path / "sheet.png", "aruco_4x4_50", codes=range(6), marker_size_mm=30, dpi=150)
    img = cv2.imread(str(files[0]), cv2.IMREAD_GRAYSCALE)
    found = sorted(d["code"] for d in targets.detect(img, "aruco_4x4_50"))
    assert found == list(range(6))


def test_pdf_sheet_has_pages(tmp_path):
    files = targets.generate_sheet(tmp_path / "sheet.pdf", "apriltag_36h11", codes=range(30), marker_size_mm=40)
    assert files[0].exists() and files[0].stat().st_size > 1000


def test_center_is_diagonal_intersection():
    q = np.array([[0, 0], [10, 0], [12, 9], [-1, 10]], float)
    c = targets._diagonal_intersection(q)
    # c lies on both diagonals
    for a, b in ((q[0], q[2]), (q[1], q[3])):
        u, v = b - a, c - a
        assert abs(u[0] * v[1] - u[1] * v[0]) < 1e-9


def test_turntable_layout_geometry(tmp_path):
    files, layout = targets.generate_turntable_mat(tmp_path / "mat.png", n_targets=8, diameter_mm=200, dpi=100)
    pts = np.array(list(layout.values()))
    assert np.allclose(np.linalg.norm(pts[:, :2], axis=1), 0.1)
    img = cv2.imread(str(files[0]), cv2.IMREAD_GRAYSCALE)
    assert len(targets.detect(img)) == 8


def test_bad_code():
    with pytest.raises(ValueError):
        targets.target_image("aruco_4x4_50", 99)


def _three_photos(doc):
    """A chunk with three aligned pinhole photos 10 m above the origin, looking down."""
    from pyarchinit_photogrammetry.core.camera import Camera, Sensor

    ch = doc.add_chunk("foto")
    ch.sensors[0] = Sensor(key=0, label="s", width=1000, height=800, model="PINHOLE",
                           params=[1000.0, 1000.0, 500.0, 400.0])
    for k, x in enumerate((-1.0, 0.0, 1.0)):
        pose = np.diag([1.0, -1.0, -1.0, 1.0])
        pose[:3, 3] = [x, 0.0, 10.0]
        ch.cameras.append(Camera(key=k, label=f"c{k}", path=f"c{k}.jpg", sensor_key=0, transform=pose))
    return ch


def test_triangulate_markers_skips_the_rays_that_cannot_be_unprojected(monkeypatch, caplog):
    """One measurement that the lens model cannot invert (NaN from ``Sensor.unproject``) used to
    stop ``triangulate_markers`` for every marker with ``LinAlgError: SVD did not converge``. Now
    that ray is dropped with a line naming marker and photo, a marker left with one ray goes on
    the surface like any single measurement, one with none is reported, the others are
    triangulated."""
    import logging

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.camera import Sensor
    from pyarchinit_photogrammetry.core.marker import Projection

    ch = _three_photos(ppg.Document())
    truth = {"A": [0.0, 0.0, 0.0], "B": [0.5, 0.2, 0.0], "C": [-0.3, 0.1, 0.0], "D": [0.2, -0.4, 0.0]}
    for label, p in truth.items():
        m = ch.add_marker(label)
        for cam in ch.cameras:
            px, _ = cam.project(ch.sensor(cam), np.asarray(p)[None])
            m.projections[cam.key] = Projection(float(px[0, 0]), float(px[0, 1]), True)
    by_label = {m.label: m for m in ch.markers}
    # the pixels the lens model cannot invert: A in c2, B in c1 and c2, D everywhere
    bad = {(round(by_label[l].projections[k].x, 3), round(by_label[l].projections[k].y, 3))
           for l, k in (("A", 2), ("B", 1), ("B", 2), ("D", 0), ("D", 1), ("D", 2))}
    unproject = Sensor.unproject

    def folded(self, pixels):
        out = unproject(self, pixels)
        for i, (x, y) in enumerate(np.atleast_2d(np.asarray(pixels, float))):
            if (round(float(x), 3), round(float(y), 3)) in bad:
                out[i] = np.nan
        return out

    monkeypatch.setattr(Sensor, "unproject", folded)
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        ch.triangulate_markers()
    assert np.allclose(by_label["A"].position, truth["A"], atol=1e-6)  # from c0 and c1
    assert np.allclose(by_label["C"].position, truth["C"], atol=1e-6)  # untouched by the bad ray of A
    assert by_label["B"].position is None  # one ray and no surface to put it on
    assert by_label["D"].position is None
    lines = [r.getMessage() for r in caplog.records]
    assert any(t.startswith("marker A") and "c2" in t for t in lines), lines
    assert any(t.startswith("marker B") and "c1" in t for t in lines)
    assert any(t.startswith("marker B") and "c2" in t for t in lines)
    assert any(t.startswith("marker D") and "nessuna" in t for t in lines), lines
    assert not any(t.startswith("marker C") for t in lines)


def test_unproject_gives_nan_where_the_lens_model_folds():
    """What the guards are for: pycolmap answers NaN, not an error, for a pixel beyond the radius
    where a strong polynomial stops growing (a Metashape calibration imported without its k4)."""
    from pyarchinit_photogrammetry.core.camera import Sensor

    s = Sensor(key=0, label="DJI", width=8064, height=6048, model="FULL_OPENCV",
               params=[5376.0, 5376.0, 4032.0, 3024.0, -0.09, 0.35, 0.0, 0.0, -1.2, 0.0, 0.0, 0.0])
    out = s.unproject(np.array([[4032.0, 3024.0], [10.0, 10.0]]))
    assert np.allclose(out[0], 0.0) and np.isnan(out[1]).all()
