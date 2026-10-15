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
