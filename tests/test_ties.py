"""Ties between chunks: two markers (one per chunk) linked in ``doc.meta["ties"]`` (engine/ties.py)."""
from __future__ import annotations

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core.camera import Camera, Sensor
from pyarchinit_photogrammetry.core.marker import Projection
from pyarchinit_photogrammetry.engine import ties


def _rot_z(deg: float) -> np.ndarray:
    a = np.radians(deg)
    m = np.eye(4)
    m[:2, :2] = [[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]]
    return m


def _two_chunks(tmp_path=None):
    doc = ppg.Document(None if tmp_path is None else tmp_path / "scavo.ppg")
    a, b = doc.add_chunk("2024"), doc.add_chunk("2025")
    b.transform = _rot_z(10.0)
    b.transform[:3, 3] = [5.0, -2.0, 0.5]
    return doc, a, b


def test_a_tie_is_two_markers_and_survives_a_new_transform():
    doc, a, b = _two_chunks()
    pa, pb = np.array([1.0, 2.0, 0.3]), np.array([-3.0, 4.0, 0.1])
    tie = ties.add_tie(doc, a, pa, b, pb)
    assert tie.name == "legame 1" and tie.active and tie.b is not None and tie.known is None
    ma, mb = a.marker_by_key(tie.a[1]), b.marker_by_key(tie.b[1])
    assert ma.label == mb.label == "legame 1"
    np.testing.assert_allclose(ma.position, pa)
    np.testing.assert_allclose(mb.position, pb)
    assert tie.a[0] == a.key and tie.b[0] == b.key
    assert [t.id for t in ties.ties_of(doc)] == [tie.id]

    before = ties.end_world(doc, *tie.b)
    np.testing.assert_allclose(before, b.to_world(pb[None])[0])
    b.transform = _rot_z(-30.0) @ b.transform
    after = ties.end_world(doc, *tie.b)
    assert np.linalg.norm(after - before) > 0.5
    np.testing.assert_allclose(b.marker_by_key(tie.b[1]).position, pb)  # internal positions do not move

    second = ties.add_tie(doc, a, pa + 1, b, pb + 1, precision=0.02)
    assert second.name == "legame 2" and second.precision == 0.02 and second.id != tie.id
    named = ties.add_tie(doc, a, pa - 1, b, pb - 1, name="picchetto 7")
    assert a.marker_by_key(named.a[1]).label == "picchetto 7"
    assert ties.add_tie(doc, a, pa, b, pb).name == "legame 3"  # the next free number in the document


def test_a_tie_needs_two_different_chunks():
    doc, a, _ = _two_chunks()
    with pytest.raises(ValueError):
        ties.add_tie(doc, a, [0, 0, 0], a, [1, 1, 1])


def test_an_end_without_a_position_has_no_world_point():
    doc, a, b = _two_chunks()
    ma, mb = a.add_marker("P1"), b.add_marker("P1")  # neither triangulated nor placed
    tie = ties.link_markers(doc, a, ma, b, mb)
    assert ties.end_world(doc, *tie.a) is None and ties.end_world(doc, *tie.b) is None
    assert ties.end_world(doc, a.key, 999) is None and ties.end_world(doc, 999, 0) is None


def test_ties_from_markers_with_the_same_label():
    doc, a, b = _two_chunks()
    for label, p in (("T1", [0, 0, 0]), ("T2", [4, 0, 0]), ("solo A", [1, 1, 0])):
        a.add_marker(label, p)
    for label, p in (("T2", [3, 1, 0]), ("T1", [0, 1, 0]), ("solo B", [2, 2, 0])):
        b.add_marker(label, p)
    count = len(a.markers), len(b.markers)
    made = ties.ties_from_markers(doc, a, b)
    assert sorted(t.name for t in made) == ["T1", "T2"]
    assert (len(a.markers), len(b.markers)) == count  # no new markers
    t1 = next(t for t in made if t.name == "T1")
    assert a.marker_by_key(t1.a[1]).label == b.marker_by_key(t1.b[1]).label == "T1"
    assert ties.ties_from_markers(doc, b, a) == []  # the same pairs are not tied twice
    assert len(ties.ties_of(doc)) == 2


def test_import_ties_from_a_table(tmp_path):
    from pyarchinit_photogrammetry.engine.dem import write_geotiff

    doc, a, b = _two_chunks(tmp_path)
    doc.save()
    yy, xx = np.mgrid[0:200, 0:200]
    z = (10.0 + 0.0 * xx + 0.0 * yy).astype(np.float32) + 0.25
    dem = write_geotiff(b.data_dir / "dem.tif", z, (-50.0, 50.0), 0.5, None, nodata=-9999.0)
    b._set_product("dem", dem)
    table = tmp_path / "legami.csv"
    table.write_text("chunk_a,x_a,y_a,z_a,chunk_b,x_b,y_b,z_b,precision,name\n"
                     "2024,1,2,0.3,2025,6,0,0.8,,\n"
                     "2024,3,1,0.2,2025,7.5,-1,,0.02,p2\n"
                     "2024,0,0,0,2025,5,-2,0.5,0.1,\n")
    made = ties.import_ties(doc, table)
    assert len(made) == 3
    assert [t.precision for t in made] == [0.05, 0.02, 0.1]
    assert made[1].name == "p2" and made[0].name.startswith("legame ")
    np.testing.assert_allclose(ties.end_world(doc, *made[0].a), [1, 2, 0.3])
    np.testing.assert_allclose(ties.end_world(doc, *made[0].b), [6, 0, 0.8], atol=1e-9)
    np.testing.assert_allclose(ties.end_world(doc, *made[1].b), [7.5, -1, 10.25], atol=1e-6)  # z from the DEM
    internal = b.marker_by_key(made[1].b[1]).position
    np.testing.assert_allclose(b.to_world(internal[None])[0], [7.5, -1, 10.25], atol=1e-6)


def test_import_ties_refuses_a_bad_table_whole(tmp_path):
    doc, a, b = _two_chunks(tmp_path)
    table = tmp_path / "legami.csv"
    table.write_text("2024;1,5;2;0,3;2025;6;0;0,8\n2024;3;1;0.2;nessuno;7;-1;0\n")
    with pytest.raises(ValueError, match="nessuno"):
        ties.import_ties(doc, table)
    assert ties.ties_of(doc) == [] and not a.markers and not b.markers
    table.write_text("2024;1,5;2;0,3;2025;6;0;\n")  # no z and no DEM
    with pytest.raises(ValueError):
        ties.import_ties(doc, table)
    table.write_text("2024;1,5;2;0,3;2025;6;0;0,8\n")  # semicolons and decimal commas
    (t,) = ties.import_ties(doc, table)
    np.testing.assert_allclose(ties.end_world(doc, *t.a), [1.5, 2, 0.3])


def test_ties_persist_with_the_document(tmp_path):
    doc, a, b = _two_chunks(tmp_path)
    t1 = ties.add_tie(doc, a, [1, 2, 3], b, [4, 5, 6], precision=0.03)
    t2 = ties.add_known(doc, b, [0.5, 0.5, 0.5], [100.0, 200.0, 10.0], "GNSS 1", 0.02)
    t1.active = False
    ties.save_ties(doc, [t1, t2])
    doc.save()
    again = ppg.Document.open(tmp_path / "scavo.ppg")
    got = ties.ties_of(again)
    assert got == [t1, t2]
    assert got[1].b is None and got[1].known == [100.0, 200.0, 10.0] and got[1].crs == b.crs
    mb = again.chunks[1].marker_by_key(got[1].a[1])
    assert mb.label == "GNSS 1" and mb.reference.location == [100.0, 200.0, 10.0]
    assert mb.reference.accuracy == [0.02, 0.02, 0.02]
    np.testing.assert_allclose(ties.end_world(again, *got[0].b), again.chunks[1].to_world(np.array([[4, 5, 6.0]]))[0])


def test_remove_tie_keeps_or_drops_its_markers():
    doc, a, b = _two_chunks()
    t1 = ties.add_tie(doc, a, [0, 0, 0], b, [1, 1, 1])
    t2 = ties.add_tie(doc, a, [2, 0, 0], b, [3, 1, 1])
    ties.remove_tie(doc, t1.id)
    assert [t.id for t in ties.ties_of(doc)] == [t2.id]
    assert [m.label for m in a.markers] == ["legame 2"] and [m.label for m in b.markers] == ["legame 2"]
    ties.remove_tie(doc, t2.id, keep_markers=True)
    assert ties.ties_of(doc) == [] and len(a.markers) == len(b.markers) == 1
    # markers the user had before (same label in both) are never deleted with the tie
    a.add_marker("T1", [0, 0, 0])
    b.add_marker("T1", [0, 0, 0])
    t3 = next(t for t in ties.ties_from_markers(doc, a, b) if t.name == "T1")
    ties.remove_tie(doc, t3.id)
    assert "T1" in [m.label for m in a.markers] and "T1" in [m.label for m in b.markers]
    with pytest.raises(KeyError):
        ties.remove_tie(doc, "nessuno")


def test_remove_tie_keeps_a_marker_another_tie_uses():
    doc, a, b = _two_chunks()
    c = doc.add_chunk("2026")
    t1 = ties.add_tie(doc, a, [0, 0, 0], b, [1, 1, 1])
    mb = b.marker_by_key(t1.b[1])
    mc = c.add_marker("C", [0, 0, 0])
    t2 = ties.link_markers(doc, b, mb, c, mc)
    ties.remove_tie(doc, t1.id)
    assert b.marker_by_key(mb.key) is mb and ties.ties_of(doc) == [t2]


def _photo_chunk(doc):
    """A chunk with two aligned pinhole photos 10 m above a point at the origin."""
    ch = doc.add_chunk("foto")
    ch.sensors[0] = Sensor(key=0, label="s", width=1000, height=800, model="PINHOLE",
                           params=[1000.0, 1000.0, 500.0, 400.0])
    for k, x in enumerate((-1.0, 1.0)):
        pose = np.diag([1.0, -1.0, -1.0, 1.0])  # looking down
        pose[:3, 3] = [x, 0.0, 10.0]
        ch.cameras.append(Camera(key=k, label=f"c{k}", path=f"c{k}.jpg", sensor_key=0, transform=pose))
    return ch


def test_end_precision_from_the_photos_in_metres():
    doc = ppg.Document()
    ch = _photo_chunk(doc)
    point = np.array([0.0, 0.0, 0.0])
    m = ch.add_marker("P")
    for cam in ch.cameras:
        px, _ = cam.project(ch.sensor(cam), point[None])
        m.projections[cam.key] = Projection(float(px[0, 0]), float(px[0, 1]), True)
    ch.triangulate_markers()
    assert ties.end_precision(ch, m) == pytest.approx(0.01)  # exact: the minimum of 1 cm
    m.projections[0] = Projection(m.projections[0].x, m.projections[0].y + 10.0, True)  # 10 px across the base
    ch.triangulate_markers()
    p = ties.end_precision(ch, m)
    assert 0.02 < p < 0.08  # a few pixels at 10 m with f = 1000 px: centimetres
    assert ties.end_precision(ch, ch.add_marker("Q", [0, 0, 0])) is None  # not measured on photos
