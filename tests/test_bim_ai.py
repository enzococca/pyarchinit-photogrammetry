import cv2
import numpy as np
import pytest
from shapely.geometry import box

from pyarchinit_photogrammetry.ai import classify, sam
from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon, export_units

pytestmark = pytest.mark.slow
X0, Y0 = 2480000.0, 4500000.0


class BrightSegmenter:
    """Stand-in for SAM 2.1: segments bright (white) regions. Same interface."""

    def _bright(self, image):
        return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) > 200

    def segment_points(self, image, points, labels):
        m = self._bright(image)
        n, lab = cv2.connectedComponents(m.astype(np.uint8))
        x, y = np.round(points[0]).astype(int)
        return lab == lab[y, x] if m[y, x] else np.zeros_like(m)

    def segment_box(self, image, box_):
        m = self._bright(image)
        out = np.zeros_like(m)
        x0, y0, x1, y1 = np.round(box_).astype(int)
        out[y0:y1, x0:x1] = m[y0:y1, x0:x1]
        return out

    def segment_auto(self, image):
        n, lab = cv2.connectedComponents(self._bright(image).astype(np.uint8))
        return [lab == i for i in range(1, n)]


def test_ifc_with_units(processed, tmp_path):
    ifcopenshell = pytest.importorskip("ifcopenshell")
    from pyarchinit_photogrammetry.bim.ifc import export_chunk_ifc

    _, ch, _ = processed
    wall = UnitPolygon(box(X0 + 1.7, Y0 + 1.3, X0 + 2.3, Y0 + 1.5), us="2", area="1", tipo="USM")
    path = export_chunk_ifc(ch, tmp_path / "m.ifc", units=[wall], us_records={"2": {"d_stratigrafica": "Muro"}},
                            site="Saggio")
    f = ifcopenshell.open(str(path))
    names = [e.Name for e in f.by_type("IfcGeographicElement")]
    assert "USM 2" in names
    conv = f.by_type("IfcMapConversion")[0]
    assert conv.Eastings > 2_479_000
    assert f.by_type("IfcProjectedCRS")[0].Name == "EPSG:3004"


def test_classification_finds_wall(processed):
    _, ch, _ = processed
    pc = classify.classify_chunk(ch)
    w = ch.to_world(pc.xyz)
    wall = (abs(w[:, 0] - X0 - 2.0) < 0.25) & (abs(w[:, 1] - Y0 - 1.4) < 0.07)
    codes, counts = np.unique(pc.classification[wall], return_counts=True)
    assert codes[np.argmax(counts)] == classify.MASONRY
    assert (pc.classification == classify.GROUND).mean() > 0.5


def test_sam_interface_on_orthomosaic(processed, scene, tmp_path):
    _, ch, _ = processed
    x, y, _ = scene.targets[2]
    # click on the white quiet zone around the target (18 cm plate, 12 cm black square)
    unit = sam.segment_orthomosaic(ch, BrightSegmenter(), points=[(x + 0.075, y)], us="99", area="1")
    assert unit.geometry.area == pytest.approx(0.18 ** 2 - 0.12 ** 2, rel=0.3)
    assert unit.geometry.centroid.distance(__import__("shapely").geometry.Point(x, y)) < 0.01
    units = sam.auto_units(ch, BrightSegmenter(), min_area_m2=0.01)
    assert len(units) >= 6
    assert export_units([unit], tmp_path / "u.geojson", crs=ch.crs).exists()


def test_masks_to_point_classes(processed):
    _, ch, _ = processed
    cam = ch.aligned_cameras[0]
    seg = BrightSegmenter()
    img = cv2.cvtColor(cv2.imread(cam.path), cv2.COLOR_BGR2RGB)
    mask = seg._bright(img)
    n = sam.classify_from_masks(ch, {cam.key: mask}, 68, label="Targets")
    assert n > 50
