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


def _dem(chunk, cell: float, z: float = 0.0) -> None:
    from pyarchinit_photogrammetry.engine.dem import write_geotiff

    n = int(round(40 / cell))
    arr = np.full((n, n), z, np.float32)
    chunk._set_product("dem", write_geotiff(chunk.data_dir / "dem.tif", arr, (-20.0, 20.0), cell, None,
                                            nodata=-9999.0))


def test_an_end_on_one_photo_takes_two_dem_cells(tmp_path):
    doc = ppg.Document(tmp_path / "foto.ppg")
    ch = _photo_chunk(doc)
    m = ch.add_marker("P", [0.0, 0.0, 0.0])  # placed on the surface from one photo (as triangulate_markers does)
    px, _ = ch.cameras[0].project(ch.sensor(ch.cameras[0]), np.zeros((1, 3)))
    m.projections[0] = Projection(float(px[0, 0]), float(px[0, 1]), True)
    assert ties.end_precision(ch, m) == pytest.approx(0.05)  # no DEM: the table default, never 1 cm
    _dem(ch, 0.1)
    assert ties.end_precision(ch, m) == pytest.approx(0.2)  # two cells of the DEM
    assert ties.raster_precision(ch, "dem") == pytest.approx(0.2)


def test_an_end_on_one_photo_without_a_surface_has_no_position():
    doc = ppg.Document()
    ch = _photo_chunk(doc)
    m = ch.add_marker("P")  # measured on one photo, no cloud to put it on
    m.projections[0] = Projection(500.0, 400.0, True)
    assert ties.end_internal(ch, m) is None and ties.end_precision(ch, m) is None


def test_an_end_on_one_photo_lands_on_the_surface():
    from pyarchinit_photogrammetry.core.pointcloud import PointCloud

    doc = ppg.Document()
    ch = _photo_chunk(doc)
    g = np.mgrid[-3:3:0.05, -3:3:0.05].reshape(2, -1).T
    ch.tie_points = PointCloud(np.c_[g, np.full(len(g), 0.4)])
    m = ch.add_marker("P")
    px, _ = ch.cameras[0].project(ch.sensor(ch.cameras[0]), np.array([[0.2, 0.1, 0.4]]))
    m.projections[0] = Projection(float(px[0, 0]), float(px[0, 1]), True)
    np.testing.assert_allclose(ties.end_internal(ch, m), [0.2, 0.1, 0.4], atol=0.01)
    assert m.position is None  # the marker is not changed


def test_a_tie_never_follows_a_reused_marker_key():
    doc, a, b = _two_chunks()
    tie = ties.add_tie(doc, a, [1, 2, 3], b, [4, 5, 6])
    b.remove_markers([tie.b[1]])  # removed from the Riferimenti pane, not with remove_tie
    other = b.add_marker("altro", [9, 9, 9])
    assert other.key == tie.b[1]  # the key comes back for another point
    (got,) = ties.ties_of(doc)
    assert not got.active and got.broken
    assert ties.end_world(doc, *got.b, uid=got.b_uid) is None
    report = ties.check_ends(doc)
    assert [r["id"] for r in report] == [tie.id] and "b" in report[0]["end"]
    assert not ties.ties_of(doc)[0].active
    # a renamed marker is still the same end; a marker found under another key is followed
    t2 = ties.add_tie(doc, a, [0, 0, 0], b, [1, 1, 1])
    b.marker_by_key(t2.b[1]).label = "rinominato"
    assert ties.ties_of(doc)[1].active
    np.testing.assert_allclose(ties.end_world(doc, *t2.b, uid=t2.b_uid), b.to_world(np.array([[1.0, 1, 1]]))[0])


def test_marker_uid_persists(tmp_path):
    doc, a, b = _two_chunks(tmp_path)
    tie = ties.add_tie(doc, a, [1, 2, 3], b, [4, 5, 6])
    doc.save()
    again = ppg.Document.open(tmp_path / "scavo.ppg")
    assert again.chunks[0].marker_by_key(tie.a[1]).uid == tie.a_uid
    assert ties.ties_of(again)[0].active


def test_known_coordinates_follow_the_marker_reference():
    doc, a, b = _two_chunks()
    tie = ties.add_known(doc, b, [0, 0, 0], [10.0, 20.0, 1.0], "GNSS 1", 0.02)
    m = b.marker_by_key(tie.a[1])
    m.reference.location = [11.0, 20.0, 1.0]  # edited in the Riferimenti pane
    m.reference.accuracy = [0.03, 0.03, 0.03]
    got = ties.ties_of(doc)[0]
    assert got.known == [11.0, 20.0, 1.0] and got.precision == pytest.approx(0.03)
    ties.update_known(doc, tie.id, known=[12.0, 20.0, 1.0], precision=0.04)  # edited in the Legami pane
    assert m.reference.location == [12.0, 20.0, 1.0] and m.reference.accuracy == [0.04] * 3
    assert ties.ties_of(doc)[0].known == [12.0, 20.0, 1.0]


def test_tie_operations_never_rewrite_the_reference_of_a_known_point():
    doc, a, b = _two_chunks()
    tie = ties.add_known(doc, b, [0, 0, 0], [10.0, 20.0, 1.0], "GNSS 1", 0.02)
    m = b.marker_by_key(tie.a[1])
    m.reference.accuracy = [0.02, 0.02, 0.05]  # per axis, set by the user
    ties.add_tie(doc, a, [1, 1, 1], b, [2, 2, 2])  # an unrelated tie reads and saves all of them
    ties.save_ties(doc, ties.ties_of(doc))
    assert m.reference.accuracy == [0.02, 0.02, 0.05] and m.reference.location == [10.0, 20.0, 1.0]


def test_a_cleared_reference_leaves_no_known_point():
    doc, a, b = _two_chunks()
    tie = ties.add_known(doc, b, [0, 0, 0], [10.0, 20.0, 1.0], "GNSS 1", 0.02)
    m = b.marker_by_key(tie.a[1])
    m.reference.location = None  # cleared in the Riferimenti pane
    ties.add_tie(doc, a, [1, 1, 1], b, [2, 2, 2])
    assert m.reference.location is None  # never restored
    got = next(t for t in ties.ties_of(doc) if t.id == tie.id)
    assert not got.active and "reference" in got.broken
    assert [r["id"] for r in ties.check_ends(doc)] == [tie.id]
    assert m.reference.location is None


def test_a_given_name_never_duplicates_a_marker_label():
    doc, a, b = _two_chunks()
    a.add_marker("P1", [0, 0, 0])
    tie = ties.add_tie(doc, a, [1, 1, 1], b, [2, 2, 2], name="P1")
    assert tie.name == "P1"
    assert [m.label for m in a.markers].count("P1") == 1
    assert a.marker_by_key(tie.a[1]).label == "P1 (legame)" and b.marker_by_key(tie.b[1]).label == "P1"


def test_raster_precision_chooses_the_raster_and_skips_a_stale_one(tmp_path):
    from pyarchinit_photogrammetry.engine.dem import write_geotiff

    doc, a, _ = _two_chunks(tmp_path)
    _dem(a, 0.1)
    a._set_product("orthomosaic", write_geotiff(a.data_dir / "orthomosaic.tif", np.zeros((3, 400, 400), np.uint8),
                                                (-20.0, 20.0), 0.01, None))
    assert ties.raster_precision(a) == pytest.approx(0.02)
    assert ties.raster_precision(a, "dem") == pytest.approx(0.2)
    assert ties.raster_precision(a, "ortho") == pytest.approx(0.02)
    from pyarchinit_photogrammetry.core import stamps

    stamps.stamp(a, "dem")
    a.transform = _rot_z(5.0)  # the DEM is now to be rebuilt: its cells say nothing of this system
    assert stamps.is_stale(a, "dem") and ties.raster_precision(a, "dem") is None


def test_ties_survive_a_save_by_an_older_version(tmp_path):
    """A project with ties saved by 1.7.5 or earlier: the markers lose their identity (the old ``Marker.to_dict``
    does not know ``uid``), the ties keep theirs. The marker at the key with the end's label is the end again."""
    doc, a, b = _two_chunks(tmp_path)
    t1 = ties.add_tie(doc, a, [1, 2, 3], b, [4, 5, 6])
    ma, mb = a.add_marker("P1", [0, 0, 0]), b.add_marker("P1", [1, 0, 0])
    t2 = ties.link_markers(doc, a, ma, b, mb)
    doc.save()
    old = ppg.Document.open(tmp_path / "scavo.ppg")
    for ch in old.chunks:
        for m in ch.markers:
            m.uid = None  # what an older version writes: no "uid" in the markers
    old.save()
    again = ppg.Document.open(tmp_path / "scavo.ppg")
    assert all(m.uid is None for ch in again.chunks for m in ch.markers)
    got = {t.id: t for t in ties.ties_of(again)}
    for t in (t1, t2):
        assert got[t.id].active and not got[t.id].broken, got[t.id].broken
    assert again.chunks[1].marker_by_key(t1.b[1]).uid == t1.b_uid  # the identity given back
    np.testing.assert_allclose(ties.end_world(again, *t1.b, uid=t1.b_uid),
                               again.chunks[1].to_world(np.array([[4.0, 5, 6]]))[0])
    again.save()
    assert ties.ties_of(ppg.Document.open(tmp_path / "scavo.ppg"))[0].active
    # a marker without identity at the key but with another label is another point: never the end
    doc2, a2, b2 = _two_chunks()
    t = ties.add_tie(doc2, a2, [1, 2, 3], b2, [4, 5, 6])
    b2.remove_markers([t.b[1]])
    other = b2.add_marker("altro", [9, 9, 9])
    assert other.key == t.b[1] and other.uid is None
    (got2,) = ties.ties_of(doc2)
    assert got2.broken and other.uid is None


def test_a_new_alignment_leaves_ends_without_photos_out_until_placed_again_or_confirmed(tmp_path):
    """An alignment of the photos is a new internal frame: an end placed without photos keeps its old internal
    numbers, wrong by the same motion as all the others of its chunk - no check could see it. It is stale: left out of
    the solve and said, until placed again or confirmed. An end measured on photos is triangulated again: current."""
    doc, a, b = _two_chunks(tmp_path)
    true = [np.array(p, float) for p in ([0, 0, 0], [6, 0, 0], [0, 5, 1], [6, 5, 0.5])]
    made = [ties.add_tie(doc, a, p, b, geometry_inv(b, a.to_world(p[None])[0]), precision=0.01) for p in true]
    assert all(t.b_alignment == "" for t in made)
    b.meta["alignment_id"] = "nuovo"  # what Chunk.align_cameras / reset_alignment do
    got = ties.ties_of(doc)
    assert all(t.stale == ["b"] for t in got) and all(t.active and not t.broken for t in got)
    stale = ties.stale_ends(doc)
    assert [s["id"] for s in stale] == [t.id for t in made] and stale[0]["end"] == "b"
    assert "riallineamento" in ties.render(stale[0]["message"], "it")
    assert "new alignment" in ties.render(stale[0]["message"])
    assert ties._usable_ends(doc) == []
    with pytest.raises(ties.TieError):
        ties.solve(doc, {a.key: "fixed", b.key: "shift"})  # nothing left to place B
    doc.save()
    assert all(t.stale == ["b"] for t in ties.ties_of(ppg.Document.open(tmp_path / "scavo.ppg")))

    # three ends placed again in the new frame; the fourth still out, said why
    for t, p in zip(made[:3], true[:3]):
        ties.place_end(doc, t.id, "b", geometry_inv(b, a.to_world(p[None])[0]))
    sol = ties.solve(doc, {a.key: "fixed", b.key: "shift"})
    assert sorted(r["id"] for r in sol.residuals) == sorted(t.id for t in made[:3])
    (out,) = sol.left_out
    assert out["id"] == made[3].id and out["messages"][0]["code"] == "out_stale"
    # confirmed: in again
    assert ties.confirm_ends(doc, [made[3].id]) == [made[3].id]
    assert ties.stale_ends(doc) == [] and not ties.solve(doc, {a.key: "fixed", b.key: "shift"}).left_out
    assert ties.confirm_ends(doc) == []

    # an end measured on photos is triangulated again by the new alignment: never stale
    ch = _photo_chunk(doc)
    m = ch.add_marker("F", [0.0, 0.0, 0.0])
    for cam in ch.cameras:
        px, _ = cam.project(ch.sensor(cam), np.zeros((1, 3)))
        m.projections[cam.key] = Projection(float(px[0, 0]), float(px[0, 1]), True)
    tf = ties.link_markers(doc, a, a.add_marker("F", [0, 0, 0]), ch, m)
    ch.meta["alignment_id"] = "altro"
    got = ties.tie_by_id(doc, tf.id)
    assert not got.stale and got.b_alignment == "altro"


def geometry_inv(chunk, world) -> np.ndarray:
    """The internal point of ``chunk`` at the world point ``world``."""
    return chunk.to_internal(np.asarray(world, float)[None])[0]


def test_align_cameras_and_reset_give_a_new_alignment():
    doc, a, _b = _two_chunks()
    a.reset_alignment()
    first = a.meta["alignment_id"]
    a.reset_alignment()
    assert first and a.meta["alignment_id"] != first


def test_import_errors_name_the_line_of_the_file(tmp_path):
    doc, a, b = _two_chunks()
    table = tmp_path / "legami.csv"
    table.write_text("chunk_a,x,y,z,chunk_b,x,y,z\n\n2024,1,2,3,2025,1,2,3\n2024,1,2,3,nessuno,1,2,3\n",
                     encoding="utf-8")
    with pytest.raises(ValueError, match="line 4"):
        ties.import_ties(doc, table)
