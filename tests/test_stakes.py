"""Stakes and benchmarks found near known points (engine/stakes): a synthetic trench whose texture has dark round heads
and dark blotches (roots, large patches) is written as an orthophoto and a DEM; the heads are found where the known
points fall, the blotches never; a few photos rendered from known poses confirm them; nothing is stored until
accepted."""
from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core.geometry import look_at
from pyarchinit_photogrammetry.engine import stakes, ties
from pyarchinit_photogrammetry.engine.dem import NODATA, write_geotiff
from pyarchinit_photogrammetry.testing import synthetic

CRS = "EPSG:3004"
ORTHO_CELL = 0.005
DEM_CELL = 0.02
HEADS = [(5.0, 1.0), (7.0, 1.4), (6.8, 4.8), (5.2, 4.6), (6.1, 3.0)]  # local, far from the targets of the scene


@pytest.fixture(scope="module")
def scene():
    sc = synthetic.make_scene(size=(8.0, 6.0), texel=0.004, crs=CRS)
    truth = synthetic.add_stakes(sc, HEADS)
    blotches = synthetic.add_blotches(sc, 14, np.random.default_rng(3))
    return sc, truth, blotches


def write_products(sc, folder, cell=ORTHO_CELL):
    """The scene's texture as an orthophoto (RGBA, ``cell`` metres) and its heights as a DEM, in the world of the
    scene."""
    w, h = int(round(sc.size[0] / cell)), int(round(sc.size[1] / cell))
    rgb = cv2.resize(sc.texture, (w, h), interpolation=cv2.INTER_AREA)[..., ::-1]
    rgba = np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)])
    top_left = (sc.origin[0], sc.origin[1] + sc.size[1])
    ortho = write_geotiff(folder / "orthomosaic.tif", np.moveaxis(rgba, -1, 0), top_left, cell, CRS)
    dw, dh = int(round(sc.size[0] / DEM_CELL)), int(round(sc.size[1] / DEM_CELL))
    z = cv2.resize(sc.heights, (dw, dh), interpolation=cv2.INTER_AREA) + np.float32(sc.origin[2])
    dem = write_geotiff(folder / "dem.tif", z.astype(np.float32), top_left, DEM_CELL, CRS, nodata=NODATA)
    return ortho, dem


def project(tmp_path, sc, cell=ORTHO_CELL):
    """A document with one chunk (internal frame = the local frame of the scene) and the orthophoto and DEM."""
    doc = ppg.Document()
    ch = doc.add_chunk("Scavo")
    ch.crs = CRS
    t = np.eye(4)
    t[:3, 3] = sc.origin
    ch.transform = t
    doc.save(tmp_path / "p.ppg")
    ortho, dem = write_products(sc, ch.data_dir, cell)
    ch._set_product("orthomosaic", ortho)
    ch._set_product("dem", dem)
    return doc, ch


def points_from(truth, shift=(0.0, 0.0, 0.0), wrong=None):
    out = []
    for name, xyz in truth.items():
        p = np.asarray(xyz, float) + np.asarray(shift, float)
        if name == wrong:
            p = p + np.array([0.0, -1.0, 0.0])  # 1 m south: towards the edge, away from the targets
        out.append(stakes.KnownPoint(name, p, CRS, 0.02))
    return out


def near_blotch(blotches, cand):
    return any(np.hypot(*(cand.xy_world - np.asarray(b["centre"]))) < b["extent"] + 0.05 for b in blotches)


def test_heads_found_within_2_cm_and_blotches_left_out(tmp_path, scene):
    sc, truth, blotches = scene
    assert len(blotches) == 14 and {b["kind"] for b in blotches} == {"elongated", "large"}
    doc, ch = project(tmp_path, sc)
    known = points_from(truth, shift=(0.25, -0.2, 0.0))  # the known points a little off, as a GNSS point may be
    props = stakes.find(doc, ch, known, radius=1.0)
    assert [p.point.name for p in props] == list(truth)
    for p in props:
        assert p.best is not None, p.flags
        assert np.linalg.norm(p.best.xy_world - np.asarray(truth[p.point.name])[:2]) < 0.02
        assert abs(p.best.world[2] - truth[p.point.name][2]) < 0.03
        assert p.best.ortho_only and "ortho_only" in p.flags and "no_aligned_photos" in p.flags
        assert p.best.precision == pytest.approx(ties.RASTER_PIXELS * ORTHO_CELL)
        assert 0.04 < p.best.diameter < 0.06 and p.best.circularity >= stakes.MIN_CIRCULARITY
        assert np.allclose(p.displacement[:2], (-0.25, 0.2), atol=0.02)
    assert not any(near_blotch(blotches, c) for p in props for c in [p.best, *p.others][:1])
    # the blotches are dark enough to be seen: they are left out by their shape and size, not their contrast
    for b in blotches:
        x, y = b["centre"]
        bp = stakes.find(doc, ch, [stakes.KnownPoint("b", np.array([x, y, np.nan]), CRS, 0.02)], radius=0.3)[0]
        assert bp.best is None or not near_blotch([b], bp.best), b
    json.dumps([p.to_dict() for p in props])  # plain JSON
    back = stakes.Proposal.from_dict(json.loads(json.dumps(props[0].to_dict())))
    assert np.allclose(back.best.internal, props[0].best.internal) and back.point.name == props[0].point.name


def _disc(d_px, ox, oy, size=80, sub=8, ground=130, head=25):
    """A grey image with a dark disc of ``d_px`` pixels across, its centre ``ox, oy`` from the centre of a pixel,
    each pixel the mean of ``sub`` x ``sub`` samples (as a camera or an orthophoto sees it)."""
    c = size // 2 + ox, size // 2 + oy
    ys, xs = np.mgrid[0:size * sub, 0:size * sub]
    inside = ((xs + 0.5) / sub - 0.5 - c[0]) ** 2 + ((ys + 0.5) / sub - 0.5 - c[1]) ** 2 <= (d_px / 2) ** 2
    cover = inside.reshape(size, sub, size, sub).mean(axis=(1, 3))
    return np.round(ground * (1 - cover) + head * cover).astype(np.uint8), c


@pytest.mark.parametrize("d_px", [3.0, 4.0, 5.0, 6.0, 7.0, 9.0])
@pytest.mark.parametrize("offset", [(0.0, 0.0), (0.5, 0.5), (0.25, 0.7)])
def test_small_heads_are_found_at_any_offset_in_the_pixel(d_px, offset):
    """A head of a few pixels (5 cm on a drone orthophoto of 1-2 cm, or seen from afar in a photo): its outline on
    the pixels depends on where its centre falls in the pixel, never enough to leave it out."""
    img, (cx, cy) = _disc(d_px, *offset)
    found = stakes.detect(img, None, (0.6 * d_px, 2.4 * d_px), 5 * 2.4 * d_px, 25)
    assert len(found) == 1, (d_px, offset)
    assert np.hypot(found[0]["x"] - cx, found[0]["y"] - cy) < 0.25


def test_a_3_cm_head_on_a_1_cm_orthophoto(tmp_path):
    """Three centimetres on an orthophoto of one centimetre a pixel: three pixels across, found within a pixel."""
    sc = synthetic.make_scene(size=(4.0, 3.0), texel=0.002, crs=CRS)
    truth = synthetic.add_stakes(sc, [(1.6, 2.5), (3.2, 2.2), (2.8, 0.8)], diameter=0.03)
    doc, ch = project(tmp_path, sc, cell=0.01)
    props = stakes.find(doc, ch, [stakes.KnownPoint(n, np.asarray(xyz) + np.array([0.1, 0.05, 0.0]), CRS, 0.02)
                                  for n, xyz in truth.items()], radius=0.3)
    for p in props:
        assert p.best is not None, (p.point.name, p.flags)
        assert np.linalg.norm(p.best.xy_world - np.asarray(truth[p.point.name])[:2]) < 0.01, p.point.name
        assert 0.02 < p.best.diameter < 0.045


def test_a_point_with_no_head_nearby_has_no_proposal(tmp_path, scene):
    sc, truth, blotches = scene
    doc, ch = project(tmp_path, sc)
    empty = (sc.origin[0] + 6.0, sc.origin[1] + 0.3, np.nan)  # bare ground (no z: read from the DEM)
    assert all(np.hypot(empty[0] - x, empty[1] - y) > 0.8 for x, y, _ in truth.values())
    assert all(np.hypot(empty[0] - b["centre"][0], empty[1] - b["centre"][1]) > 0.5 for b in blotches)
    p, = stakes.find(doc, ch, [stakes.KnownPoint("vuoto", np.array(empty), None, 0.02)], radius=0.4)
    assert p.best is None and p.others == [] and "no_candidate" in p.flags and p.displacement is None
    outside, = stakes.find(doc, ch, [stakes.KnownPoint("fuori", np.array([sc.origin[0] - 50, sc.origin[1], 100.0]),
                                                       None, 0.02)], radius=1.0)
    assert outside.best is None and "outside_orthophoto" in outside.flags


def test_rasters_built_before_a_move_are_read_where_they_were_built(tmp_path, scene):
    """The chunk moved 0.5 m east and 0.1 m up after its orthophoto and DEM were built: the heads are found where
    they are now (Review Focus 1, the rasters carried with the chunk through their stamps)."""
    sc, truth, _ = scene
    doc, ch = project(tmp_path, sc)
    built = np.asarray(ch.transform, float).copy()
    ch.meta.setdefault("built_in", {}).update(orthomosaic=built.tolist(), dem=built.tolist())
    moved = built.copy()
    moved[:3, 3] += (0.5, 0.0, 0.1)
    ch.transform = moved
    now = {n: np.asarray(xyz) + np.array([0.5, 0.0, 0.1]) for n, xyz in truth.items()}
    props = stakes.find(doc, ch, [stakes.KnownPoint(n, p + np.array([0.1, 0.1, 0.0]), CRS, 0.02)
                                  for n, p in now.items()], radius=0.5)
    for p in props:
        assert np.linalg.norm(p.best.world - now[p.point.name]) < 0.03, p.point.name
        assert np.allclose(ch.to_world(p.best.internal[None])[0], p.best.world)


def test_check_names_the_wrong_point(tmp_path, scene):
    sc, truth, _ = scene
    doc, ch = project(tmp_path, sc)
    props = stakes.find(doc, ch, points_from(truth, wrong="S1"), radius=1.5)
    assert all(p.best is not None for p in props)
    result = stakes.check(props)
    assert result["suspects"] == ["S1"]
    assert result["outliers"] == ["S1"]
    assert np.allclose(result["displacement_median"][:2], (0.0, 0.0), atol=0.02)
    assert len(result["pairs"]) == 10 and sum(not c["ok"] for c in result["pairs"]) == 4


def test_coherent_displacement_is_reported(tmp_path, scene):
    sc, truth, _ = scene
    doc, ch = project(tmp_path, sc)
    props = stakes.find(doc, ch, points_from(truth, shift=(-0.7, 0.0, 0.0)), radius=1.2)
    result = stakes.check(props)
    assert np.allclose(result["displacement_median"][:2], (0.7, 0.0), atol=0.02)
    assert result["suspects"] == [] and result["outliers"] == []
    assert all(c["ok"] for c in result["pairs"])


def with_photos(tmp_path, sc, truth, name="S5", n=6, unreadable=()):
    """The project of :func:`project` with ``n`` photos rendered around a head from known poses (no alignment
    needed: the poses are the truth), ``unreadable`` of them emptied as an online-only placeholder."""
    doc, ch = project(tmp_path, sc)
    folder = tmp_path / "photos"
    folder.mkdir()
    size, focal = (640, 480), 700.0
    K = np.array([[focal, 0, size[0] / 2], [0, focal, size[1] / 2], [0, 0, 1]])
    head = np.asarray(truth[name], float) - np.asarray(sc.origin)
    poses = []
    for i in range(n):
        a = 2 * np.pi * i / n
        eye = head + np.array([0.5 * np.cos(a), 0.5 * np.sin(a), 1.8])
        poses.append(look_at(eye, head + np.array([0.1 * np.sin(a), 0.1 * np.cos(a), 0.0]), up=(0, 1, 0)))
        cv2.imwrite(str(folder / f"IMG_{i:02d}.jpg"), synthetic.render(sc, poses[-1], K, size, k1=0.0),
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
    cams = ch.add_photos([folder])
    sensor = ch.sensor(cams[0])
    sensor.model, sensor.params = "PINHOLE", [focal, focal, size[0] / 2, size[1] / 2]
    for cam, pose in zip(sorted(cams, key=lambda c: c.label), poses):
        cam.transform = pose
    for i in unreadable:
        (folder / f"IMG_{i:02d}.jpg").write_bytes(b"")
    return doc, ch


def test_photos_confirm_a_head(tmp_path, scene):
    sc, truth, _ = scene
    doc, ch = with_photos(tmp_path, sc, truth)
    p, = stakes.find(doc, ch, [points_from(truth)[4]], radius=0.5)
    assert p.best is not None and not p.best.ortho_only and p.best.agreeing >= 5, p.best
    assert np.linalg.norm(p.best.world - np.asarray(truth["S5"])) < 0.01
    assert p.best.precision >= stakes.MIN_PRECISION
    assert all(ph["residual_px"] < 2.0 for ph in p.best.photos if ph["agrees"])
    pic = stakes.preview(ch, p, size=128)
    assert pic.shape == (128, 256, 3) and pic.dtype == np.uint8
    assert pic[:, 128:].std() > 5  # the photo, not the grey of a missing one


def test_unreadable_photos_fall_back_to_the_orthophoto(tmp_path, scene):
    sc, truth, _ = scene
    doc, ch = with_photos(tmp_path, sc, truth, unreadable=(0, 1, 2, 3))  # two left: fewer than three agree
    p, = stakes.find(doc, ch, [points_from(truth)[4]], radius=0.5)
    assert p.best is not None and p.best.ortho_only and "photos_unreadable" in p.flags and "ortho_only" in p.flags
    assert np.linalg.norm(p.best.xy_world - np.asarray(truth["S5"])[:2]) < 0.02
    for k in (4, 5):
        (tmp_path / "photos" / f"IMG_{k:02d}.jpg").write_bytes(b"")
    p, = stakes.find(doc, ch, [points_from(truth)[4]], radius=0.5)  # none readable: no crash either
    assert p.best is not None and p.best.ortho_only and "photos_unreadable" in p.flags
    assert stakes.preview(ch, p, size=64).shape == (64, 128, 3)


def test_accept_makes_known_ties_and_nothing_before(tmp_path, scene):
    sc, truth, _ = scene
    doc, ch = with_photos(tmp_path, sc, truth)
    before = json.dumps(doc.meta.get("ties"))
    markers = len(ch.markers)
    props = stakes.find(doc, ch, points_from(truth), radius=0.5)
    assert json.dumps(doc.meta.get("ties")) == before and len(ch.markers) == markers  # nothing stored
    made = stakes.accept(doc, ch, props, names=["S5", "S2"])
    assert [t.name for t in made] == ["S2", "S5"]
    tied = {t.name: t for t in ties.ties_of(doc)}
    assert set(tied) == {"S2", "S5"} and all(t.b is None for t in tied.values())
    s5 = next(m for m in ch.markers if m.key == tied["S5"].a[1])
    assert len(s5.projections) >= 5 and all(pr.pinned is True for pr in s5.projections.values())
    assert np.linalg.norm(ch.to_world(s5.position[None])[0] - np.asarray(truth["S5"])) < 0.01
    s2 = next(m for m in ch.markers if m.key == tied["S2"].a[1])
    assert not s2.projections  # orthophoto only: no photo measurements
    assert np.allclose(tied["S5"].known, truth["S5"])
    with pytest.raises(ValueError, match="no head|no proposal"):
        stakes.accept(doc, ch, props, names=["Z9"])
    ch.meta["alignment_id"] = "another"
    with pytest.raises(ValueError, match="aligned again"):
        stakes.accept(doc, ch, props, names=["S1"])


def test_accept_leaves_the_other_markers_where_they_are(tmp_path, scene):
    """Accepting a stake triangulates its own marker only: a marker whose position was adjusted with the block (it
    differs from what its photos give), a marker measured on one photo, a ground control point and the end of an
    existing tie keep their positions."""
    sc, truth, _ = scene
    doc, ch = with_photos(tmp_path, sc, truth)
    cams = sorted(ch.aligned_cameras, key=lambda c: c.label)
    p = ch.to_internal(np.array([truth["S5"]]))[0] + np.array([0.15, -0.1, 0.0])

    def seen(cam):
        px, _d = cam.project(ch.sensor(cam), p[None])
        return float(px[0, 0]), float(px[0, 1])

    adjusted = ch.add_marker("adjusted", p + np.array([0.004, -0.003, 0.006]))
    for cam in cams[:3]:
        ch.add_marker_projection(adjusted, cam, *seen(cam), pinned=True)
    one = ch.add_marker("one photo", p + np.array([0.0, 0.0, 0.2]))
    ch.add_marker_projection(one, cams[0], *seen(cams[0]), pinned=True)
    gcp_marker = ch.add_marker("GCP", p + np.array([0.05, 0.05, 0.0]), reference=truth["S1"])
    for cam in cams[3:]:
        ch.add_marker_projection(gcp_marker, cam, *seen(cam), pinned=False)
    other = ties.add_known(doc, ch, p + np.array([-0.1, 0.0, 0.0]), truth["S3"], name="esistente")
    end = next(m for m in ch.markers if m.key == other.a[1])
    ch.add_marker_projection(end, cams[1], *seen(cams[1]), pinned=True)
    before = {m.label: m.position.copy() for m in ch.markers}
    props = stakes.find(doc, ch, [points_from(truth)[4]], radius=0.5)
    stakes.accept(doc, ch, props)
    for m in ch.markers:
        if m.label in before:
            assert np.array_equal(m.position, before[m.label]), m.label


def test_a_coded_target_is_not_a_head(tmp_path):
    """The black cells of a coded target are dark and square: never proposed, also when the known point is the
    target itself; a head next to it is found."""
    sc = synthetic.make_scene(size=(4.0, 3.0), texel=0.004, crs=CRS)
    tx, ty, _ = sc.targets[7]
    truth = synthetic.add_stakes(sc, [(tx - sc.origin[0], ty - sc.origin[1] + 0.35)])
    doc, ch = project(tmp_path, sc)
    for code, (x, y, z) in sc.targets.items():
        p, = stakes.find(doc, ch, [stakes.KnownPoint(f"T{code}", np.array([x, y, z]), CRS, 0.02)], radius=0.15)
        assert p.best is None, (code, p.best)
    p, = stakes.find(doc, ch, [stakes.KnownPoint("T7", np.array(sc.targets[7]), CRS, 0.02)], radius=0.6)
    assert p.best is not None and np.linalg.norm(p.best.xy_world - np.asarray(truth["S1"])[:2]) < 0.02
    assert all(np.hypot(*(c.xy_world - np.asarray(sc.targets[7])[:2])) > 0.1 for c in [p.best, *p.others])
    # a target detected in the chunk: its footprint is left out by itself too
    m = ch.add_marker("target 7", ch.to_internal(np.array([sc.targets[7]]))[0])
    m.code = 7
    centred = stakes.find(doc, ch, [stakes.KnownPoint("T7", np.array(sc.targets[7]), CRS, 0.02)], radius=0.6,
                          head=(0.01, 0.12))[0]  # smaller heads let single cells through the size test
    assert all(np.hypot(*(c.xy_world - np.asarray(sc.targets[7])[:2])) > 0.1
               for c in ([centred.best] if centred.best else []) + centred.others)


def test_check_compares_distances_in_the_chunks_system(tmp_path, scene):
    """Known points given in another projected system (Monte Mario zone 1 for a chunk in zone 2: a scale factor far
    from 1 there): both distances of a pair are measured in the chunk's system."""
    from pyarchinit_photogrammetry.core import crs as crs_mod

    sc, truth, _ = scene
    doc, ch = project(tmp_path, sc)
    names = list(truth)
    other = crs_mod.transform_points(np.array([truth[n] for n in names]), CRS, "EPSG:3003")
    pts = [stakes.KnownPoint(n, other[i], "EPSG:3003", 0.02) for i, n in enumerate(names)]
    props = stakes.find(doc, ch, pts, radius=0.3)
    assert all(p.best is not None for p in props)
    assert all(np.allclose(p.known, truth[p.point.name], atol=1e-3) for p in props)
    result = stakes.check(props)
    assert all(abs(c["difference"]) < 0.005 for c in result["pairs"]), [c["difference"] for c in result["pairs"]]
    back = stakes.Proposal.from_dict(json.loads(json.dumps(props[0].to_dict())))
    assert np.allclose(back.known, props[0].known)


def test_known_points_from_csv_chunk_and_ties(tmp_path, scene):
    sc, truth, _ = scene
    doc, ch = project(tmp_path, sc)
    csv_path = tmp_path / "punti.csv"
    csv_path.write_text("nome;x;y;z;precisione\nS1;10,0;20,0;;0,01\nS2;11.5;21.5;100.2\n")  # decimal commas
    pts = stakes.known_points(csv_path, crs=CRS)
    assert [p.name for p in pts] == ["S1", "S2"] and np.isnan(pts[0].xyz[2]) and pts[0].precision == 0.01
    assert pts[1].precision == ties.TABLE_PRECISION and pts[1].crs == CRS
    m = ch.add_marker("GPS 1", None, reference=truth["S1"])
    m.reference.accuracy = [0.01, 0.01, 0.03]
    from_chunk = stakes.known_points(ch)
    assert [p.name for p in from_chunk] == ["GPS 1"] and from_chunk[0].precision == 0.03
    ties.add_known(doc, ch, ch.to_internal(np.array([truth["S2"]]))[0], truth["S2"], name="S2", precision=0.02)
    assert [p.name for p in stakes.known_points("ties", doc=doc)] == ["S2"]
    local = stakes.KnownPoint("loc", np.array([1.0, 2.0, 3.0]), "LOCAL", 0.02)
    with pytest.raises(ties.TieError):
        stakes.find(doc, ch, [local])


def test_cli_find_writes_json_and_previews(tmp_path, scene):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    sc, truth, _ = scene
    doc, ch = project(tmp_path, sc)
    doc.save()
    csv_path = tmp_path / "punti.csv"
    csv_path.write_text("name,x,y,z\n" + "".join(f"{n},{x:.3f},{y:.3f},{z:.3f}\n" for n, (x, y, z) in truth.items())
                        + f"vuoto,{sc.origin[0] + 6.0:.3f},{sc.origin[1] + 0.3:.3f}\n")
    out = tmp_path / "res" / "proposals.json"
    r = CliRunner().invoke(app, ["stakes", "find", str(tmp_path / "p.ppg"), "Scavo", str(csv_path), "--radius", "0.4",
                                 "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "S1" in r.output and "vuoto" in r.output and "ortho" in r.output
    data = json.loads(out.read_text())
    assert [d["point"]["name"] for d in data["proposals"]] == ["S1", "S2", "S3", "S4", "S5", "vuoto"]
    for n in ["S1", "S5", "vuoto"]:
        assert (out.parent / f"{n}.png").exists()
    assert not ppg.Document.open(tmp_path / "p.ppg").meta.get("ties")
    r = CliRunner().invoke(app, ["stakes", "accept", str(tmp_path / "p.ppg"), "Scavo", str(out), "--ids", "S1,S3"])
    assert r.exit_code == 0, r.output
    assert not ppg.Document.open(tmp_path / "p.ppg").meta.get("ties")  # without --yes nothing changes
    r = CliRunner().invoke(app, ["stakes", "accept", str(tmp_path / "p.ppg"), "Scavo", str(out), "--ids", "S1,S3",
                                 "--yes"])
    assert r.exit_code == 0, r.output
    assert [t.name for t in ties.ties_of(ppg.Document.open(tmp_path / "p.ppg"))] == ["S1", "S3"]
    r = CliRunner().invoke(app, ["stakes", "accept", str(tmp_path / "p.ppg"), "Scavo", str(out)])
    assert r.exit_code != 0  # --ids or --agreeing


@pytest.mark.parametrize("d_px", [8.625, 8.75, 8.875, 9.0])
def test_a_head_of_9_pixels_centred_on_a_pixel_corner(d_px):
    """A disc of about 9 pixels across centred on a pixel corner fills 0.95 of its enclosing rectangle on the pixels:
    still a head (a square cell of a coded target fills 1.0 and stays out)."""
    img, (cx, cy) = _disc(d_px, 0.5, 0.5, sub=1)  # each pixel in or out by its centre: the hardest outline
    found = stakes.detect(img, None, (0.6 * d_px, 2.4 * d_px), 5 * 2.4 * d_px, 25)
    assert len(found) == 1
    assert np.hypot(found[0]["x"] - cx, found[0]["y"] - cy) < 0.25
    square = np.full((80, 80), 130, np.uint8)
    square[36:45, 36:45] = 25
    assert stakes.detect(square, None, (0.6 * 9, 2.4 * 9), 5 * 2.4 * 9, 25) == []


def test_next_candidate_cycles_the_others(tmp_path, scene):
    sc, truth, _blotches = scene
    doc, ch = project(tmp_path, sc)
    name, xyz = next(iter(truth.items()))
    p = stakes.find(doc, ch, [stakes.KnownPoint(name, np.asarray(xyz, float), CRS, 0.02)], radius=3.0)[0]
    assert p.best is not None and p.others
    q = stakes.next_candidate(p)
    assert q.best is p.others[0] and q.others[-1] is p.best and len(q.others) == len(p.others)
    assert np.allclose(q.displacement, q.best.world - p.known)
    back = p
    for _ in range(len(p.others) + 1):
        back = stakes.next_candidate(back)
    assert back.best is p.best  # round the cycle
    alone = stakes.Proposal(p.point, p.chunk, p.best, [], p.displacement, p.flags, p.alignment, p.known)
    assert stakes.next_candidate(alone) is alone
