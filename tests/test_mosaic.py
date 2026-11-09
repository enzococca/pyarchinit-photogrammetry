"""The merged orthophoto and DEM of several tied chunks (engine/mosaic): three synthetic chunks with flat colours and
flat heights overlapping along x, merged by a list of priority or by areas, placed through their stamps."""
from __future__ import annotations

import json

import numpy as np
import pytest
import rasterio
from shapely.geometry import box
from typer.testing import CliRunner

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.cli import app
from pyarchinit_photogrammetry.engine import mosaic, ties
from pyarchinit_photogrammetry.engine.dem import NODATA, write_geotiff

CRS = "EPSG:3004"
RES = 0.05
X0, Y1 = 2480000.0, 4500004.0  # the synthetic origin of the trench
COLOURS = {"A": (200, 40, 40), "B": (40, 200, 40), "C": (40, 40, 200)}
HEIGHTS = {"A": 10.0, "B": 20.0, "C": 30.0}
STARTS = {"A": 0.0, "B": 2.0, "C": 4.0}  # x of the west edge (m from X0): each 4 m wide, 4 m tall


def _ortho(colour, w, h):
    arr = np.zeros((4, h, w), np.uint8)
    for i in range(3):
        arr[i] = colour[i]
    arr[3] = 255
    return arr


def project(tmp_path, crs=None, sizes=None):
    """A saved document with chunks A, B, C: orthophoto and DEM each 4 x 4 m, A at x 0-4, B 2-6, C 4-8."""
    crs = crs or {}
    tmp_path.mkdir(parents=True, exist_ok=True)
    doc = ppg.Document()
    doc.save(tmp_path / "p.ppg")
    chs = {}
    for label in ("A", "B", "C"):
        ch = doc.add_chunk(label)
        ch.crs = crs.get(label, CRS)
        w, h = (sizes or {}).get(label, (4.0, 4.0))
        n, m = int(round(w / RES)), int(round(h / RES))
        origin = (X0 + STARTS[label], Y1)
        file_crs = None if ch.crs == "LOCAL" else ch.crs
        ch._set_product("orthomosaic", write_geotiff(ch.data_dir / "ortho.tif", _ortho(COLOURS[label], n, m), origin,
                                                     RES, file_crs))
        ch._set_product("dem", write_geotiff(ch.data_dir / "dem.tif", np.full((m, n), HEIGHTS[label], np.float32),
                                             origin, RES, file_crs, nodata=NODATA))
        chs[label] = ch
    doc.save()
    return doc, chs


def read(doc, rec, which="mosaic"):
    with rasterio.open(mosaic.path_of(doc, rec, which)) as src:
        return src.read(), src.transform


def at(arr, tr, x, y):
    c, r = ~tr @ (X0 + x, Y1 - y)
    return arr[:, int(r), int(c)]


def test_priority_by_list(tmp_path):
    doc, c = project(tmp_path)
    rec = mosaic.build(doc, [c["B"], c["A"], c["C"]])
    img, tr = read(doc, rec)
    src, _ = read(doc, rec, "sources")
    assert img.shape[0] == 4 and img.dtype == np.uint8
    assert tuple(at(img, tr, 1.0, 2.0)) == (*COLOURS["A"], 255)
    assert tuple(at(img, tr, 3.0, 2.0)) == (*COLOURS["B"], 255)  # A and B: B first in the list
    assert tuple(at(img, tr, 5.0, 2.0)) == (*COLOURS["B"], 255)  # B and C: B
    assert tuple(at(img, tr, 7.0, 2.0)) == (*COLOURS["C"], 255)
    assert at(src, tr, 1.0, 2.0)[0] == 2 and at(src, tr, 3.0, 2.0)[0] == 1 and at(src, tr, 7.0, 2.0)[0] == 3
    assert rec["resolution"] == pytest.approx(RES)
    assert rec["chunks"] == [c["B"].key, c["A"].key, c["C"].key]
    n = int(round(4 / RES)) ** 2
    assert rec["counts"][str(c["B"].key)] == n and rec["counts"][str(c["A"].key)] == n // 2
    assert rec in mosaic.mosaics_of(doc) and rec["crs"] == CRS
    assert json.loads(mosaic.path_of(doc, rec, "record").read_text())["id"] == rec["id"]
    assert mosaic.source_at(doc, rec["id"], X0 + 3.0, Y1 - 2.0) == "B"
    assert mosaic.source_at(doc, rec["id"], X0 + 1.0, Y1 - 2.0) == "A"
    assert mosaic.source_at(doc, rec["id"], X0 + 100.0, Y1 - 2.0) is None
    assert rec["name"] == "Ortofoto unita 1" and mosaic.is_stale(doc, rec) == []


def test_areas_override_the_list_and_fade_outside(tmp_path):
    doc, c = project(tmp_path)
    area = box(X0 + 4.5, Y1 - 3.0, X0 + 5.5, Y1 - 1.0)  # inside B and C: C there
    rec = mosaic.build(doc, ["A", "B", "C"], areas=[("C", area)])
    img, tr = read(doc, rec)
    assert tuple(at(img, tr, 5.0, 2.0)) == (*COLOURS["C"], 255)
    faded = tuple(int(round(v + mosaic.FADE * (255 - v))) for v in COLOURS["B"])
    assert tuple(at(img, tr, 5.0, 3.5)) == (*faded, 255)  # outside the area: the list (B), faded
    faded_a = tuple(int(round(v + mosaic.FADE * (255 - v))) for v in COLOURS["A"])
    assert tuple(at(img, tr, 1.0, 2.0)) == (*faded_a, 255)
    # an area of a chunk without data where it lies: the list there, not faded
    rec2 = mosaic.build(doc, ["A", "B", "C"], areas=[("A", box(X0 + 6.5, Y1 - 3, X0 + 7.5, Y1 - 1))])
    img2, tr2 = read(doc, rec2)
    assert tuple(at(img2, tr2, 7.0, 2.0)) == (*COLOURS["C"], 255)
    from pyarchinit_photogrammetry.io import gpkg

    rows = gpkg.read_table(mosaic.path_of(doc, rec, "areas"), "aree")
    assert [(r[1]["chunk"], r[1]["priority"]) for r in rows] == [("C", 1)]
    assert rec["areas"][0]["chunk"] == c["C"].key and rec["background"] == "faded"
    with pytest.raises(mosaic.MosaicError, match="not in the list"):
        mosaic.build(doc, ["A", "B"], areas=[("C", area)])


def test_background_none_is_transparent(tmp_path):
    doc, c = project(tmp_path)
    area = box(X0 + 1.0, Y1 - 3.0, X0 + 3.0, Y1 - 1.0)
    rec = mosaic.build(doc, ["A", "B"], areas=[("B", area)], background="none")
    img, tr = read(doc, rec)
    src, _ = read(doc, rec, "sources")
    assert rec["bounds"][0] == pytest.approx(X0 + 1.0) and rec["bounds"][2] == pytest.approx(X0 + 3.0)
    assert tuple(at(img, tr, 2.5, 2.0)) == (*COLOURS["B"], 255)
    assert tuple(at(img, tr, 1.5, 2.0)) == (*COLOURS["A"], 255)  # inside the area, B has no data: the list
    assert img[3, 0, 0] == 255  # the grid covers the area only
    rec_d = mosaic.build(doc, ["A", "B"], product="dem", areas=[("B", box(X0 + 2.5, Y1 - 3, X0 + 3.5, Y1 - 1))],
                         background="none", resolution=0.1)
    z, trd = read(doc, rec_d)
    srcd, _ = read(doc, rec_d, "sources")
    assert at(z, trd, 3.0, 2.0)[0] == pytest.approx(20.0)
    assert (srcd[0] == 2).all()  # the grid is the box of the area
    rec_e = mosaic.build(doc, ["A", "B"], areas=[("B", box(X0 + 2.5, Y1 - 3, X0 + 3.5, Y1 - 1))],
                         background="none")
    img_e, tr_e = read(doc, rec_e)
    src_e, _ = read(doc, rec_e, "sources")
    # the grid is the box of the area: every cell inside it is B's
    assert (src_e[0] == 2).all() and (img_e[3] == 255).all()
    rec_f = mosaic.build(doc, ["A", "B"], areas=[("B", box(X0 + 2.5, Y1 - 3, X0 + 3.5, Y1 - 2.5)),
                                                  ("A", box(X0 + 1.5, Y1 - 1.5, X0 + 2.0, Y1 - 1.0))],
                         background="none")
    img_f, tr_f = read(doc, rec_f)
    src_f, _ = read(doc, rec_f, "sources")
    hole = at(img_f, tr_f, 2.2, 2.0)  # between the two areas: transparent, no source
    assert hole[3] == 0 and at(src_f, tr_f, 2.2, 2.0)[0] == 0
    assert mosaic.source_at(doc, rec_f["id"], X0 + 2.2, Y1 - 2.0) is None


def test_dem_mosaic_keeps_heights_and_sources_band(tmp_path):
    doc, c = project(tmp_path)
    rec = mosaic.build(doc, ["C", "B", "A"], product="dem",
                       areas=[("A", box(X0 + 2.5, Y1 - 3, X0 + 3.5, Y1 - 1))])  # faded: the DEM keeps its heights
    z, tr = read(doc, rec)
    src, _ = read(doc, rec, "sources")
    assert z.dtype == np.float32
    assert at(z, tr, 1.0, 2.0)[0] == pytest.approx(10.0)
    assert at(z, tr, 3.0, 2.0)[0] == pytest.approx(10.0)  # inside A's area
    assert at(z, tr, 3.0, 3.5)[0] == pytest.approx(20.0)  # B before A in the list
    assert at(z, tr, 5.0, 2.0)[0] == pytest.approx(30.0)
    assert at(src, tr, 5.0, 2.0)[0] == 1 and at(src, tr, 3.0, 2.0)[0] == 3 and at(src, tr, 3.0, 3.5)[0] == 2
    assert set(np.unique(src)) <= {1, 2, 3}
    vals = z[0][src[0] > 0]
    assert set(np.round(np.unique(vals), 4)) <= {10.0, 20.0, 30.0}
    assert rec["name"] == "DEM unito 1"


def test_raster_built_in_another_transform_is_moved(tmp_path):
    doc, c = project(tmp_path, sizes={"B": (4.0, 2.0)})
    b = c["B"]
    built = np.eye(4)
    b.meta.setdefault("built_in", {})["dem"] = built.tolist()
    b.meta["built_in"]["orthomosaic"] = built.tolist()
    move = np.eye(4)
    move[:3, 3] = (1.0, 0.0, 0.5)  # a tie solve after the DEM: 1 m east, 0.5 m up
    b.transform = move
    rec = mosaic.build(doc, ["B", "A"], product="dem")
    z, tr = read(doc, rec)
    assert at(z, tr, 3.5, 1.0)[0] == pytest.approx(20.5)  # B now at x 3-7
    assert at(z, tr, 2.5, 1.0)[0] == pytest.approx(10.0)  # where B was built: A's
    assert rec["moved"] == [b.key]
    tilt = move.copy()
    tilt[2, 0] = 0.001  # the heights move with x too: z' = m20 x + z + m23, x where the cell was built
    b.transform = tilt
    rec_t = mosaic.build(doc, ["B", "A"], product="dem")
    zt, trt = read(doc, rec_t)
    assert at(zt, trt, 3.525, 1.025)[0] == pytest.approx(20.5 + 0.001 * (X0 + 2.525), abs=1e-3)
    # a quarter turn about the middle of B's file: 4 x 2 m becomes 2 x 4 m
    cx, cy = X0 + 2.0 + 2.0, Y1 - 1.0
    rot = np.eye(4)
    rot[:2, :2] = [[0.0, -1.0], [1.0, 0.0]]
    rot[:2, 3] = [cx + cy, cy - cx]
    b.transform = rot
    rec2 = mosaic.build(doc, ["B"], product="orthomosaic")
    w = rec2["bounds"][2] - rec2["bounds"][0]
    h = rec2["bounds"][3] - rec2["bounds"][1]
    assert w == pytest.approx(2.0, abs=RES) and h == pytest.approx(4.0, abs=RES)
    img, tr2 = read(doc, rec2)
    assert tuple(img[:, img.shape[1] // 2, img.shape[2] // 2]) == (*COLOURS["B"], 255)
    assert rec2["bounds"][0] == pytest.approx(cx - 1.0, abs=RES)


def test_local_and_projected_refused(tmp_path):
    doc, c = project(tmp_path, crs={"B": "LOCAL"})
    with pytest.raises(ties.TieError) as err:
        mosaic.build(doc, ["A", "B"])
    assert "not in the same frame" in str(err.value) and "non sono nello stesso sistema" in err.value.italian
    assert not mosaic.mosaics_of(doc)
    assert not (doc.files_dir / "mosaics").exists() or not any((doc.files_dir / "mosaics").iterdir())
    doc2, c2 = project(tmp_path / "local", crs={"A": "LOCAL", "B": "LOCAL", "C": "LOCAL"})
    rec = mosaic.build(doc2, ["A", "B", "C"])  # two local frames mix
    assert rec["crs"] == "LOCAL"


def test_blocks_give_the_same_result(tmp_path):
    doc, c = project(tmp_path, crs={"C": "EPSG:3003"}, sizes={"B": (4.0, 2.0)})
    c3 = c["C"]  # C's file in Monte Mario zone 1: a 3-degree system away, reprojected
    from pyarchinit_photogrammetry.core import crs as crs_mod

    p = crs_mod.transform_points(np.array([[X0 + 4.0, Y1, 0.0]]), CRS, "EPSG:3003")[0]
    c3._set_product("orthomosaic", write_geotiff(c3.data_dir / "ortho.tif", _ortho(COLOURS["C"], 80, 80),
                                                 (p[0], p[1]), RES, "EPSG:3003"))
    c3._set_product("dem", write_geotiff(c3.data_dir / "dem.tif", np.full((80, 80), 30.0, np.float32),
                                         (p[0], p[1]), RES, "EPSG:3003", nodata=NODATA))
    b = c["B"]
    b.meta.setdefault("built_in", {})["orthomosaic"] = np.eye(4).tolist()
    b.meta["built_in"]["dem"] = np.eye(4).tolist()
    m = np.eye(4)
    m[:2, :2] = [[np.cos(0.3), -np.sin(0.3)], [np.sin(0.3), np.cos(0.3)]]
    m[:3, 3] = (X0 + 4) * (1 - np.cos(0.3)) + (Y1 - 1) * np.sin(0.3), (Y1 - 1) * (1 - np.cos(0.3)) - (X0 + 4) * np.sin(0.3), 0.2
    m[2, 0] = 0.001  # a slight tilt: the heights move with x
    b.transform = m
    area = box(X0 + 3.0, Y1 - 3.0, X0 + 5.0, Y1 - 0.5)
    for product in ("orthomosaic", "dem"):
        big = mosaic.build(doc, ["A", "B", "C"], product=product, areas=[("B", area)])
        small = mosaic.build(doc, ["A", "B", "C"], product=product, areas=[("B", area)], block=64)
        for which in ("mosaic", "sources"):
            x, _ = read(doc, big, which)
            y, _ = read(doc, small, which)
            np.testing.assert_array_equal(x, y)
        assert big["counts"] == small["counts"]
        assert all(v > 0 for v in big["counts"].values())


def test_stale_after_a_transform_change(tmp_path):
    doc, c = project(tmp_path)
    rec = mosaic.build(doc, ["A", "B", "C"])
    assert mosaic.is_stale(doc, rec) == []
    t = np.eye(4)
    t[0, 3] = 0.01
    c["C"].transform = t
    assert mosaic.is_stale(doc, rec) == ["C"]
    gone = mosaic.remove(doc, rec["id"])
    assert gone["id"] == rec["id"] and not mosaic.mosaics_of(doc) and "mosaics" not in doc.meta
    assert not mosaic.folder(doc, rec["id"]).exists()
    with pytest.raises(mosaic.MosaicError):
        mosaic.remove(doc, rec["id"])


def test_cli_mosaic_build_and_list(tmp_path):
    from pyarchinit_photogrammetry.io import gpkg

    doc, c = project(tmp_path)
    areas = tmp_path / "aree.gpkg"
    gpkg.write_table(areas, "aree", "POLYGON", {"chunk": "TEXT", "priority": "INTEGER"},
                     [(box(X0 + 4.5, Y1 - 3, X0 + 5.5, Y1 - 1), {"chunk": "C", "priority": 1})], CRS)
    runner = CliRunner()
    pp = str(tmp_path / "p.ppg")
    res = runner.invoke(app, ["mosaic", "build", pp, "--chunks", "A,B,C", "--areas", str(areas), "--name", "Tre anni"])
    assert res.exit_code == 0, res.output
    assert "Tre anni" in res.output and "C:" in res.output
    res = runner.invoke(app, ["mosaic", "build", pp, "--chunks", "A,B", "--dem", "--resolution", "0.1"])
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["mosaic", "list", pp, "--json"])
    assert res.exit_code == 0, res.output
    recs = json.loads(res.output)
    assert [r["name"] for r in recs] == ["Tre anni", "DEM unito 1"] and recs[1]["resolution"] == pytest.approx(0.1)
    assert recs[0]["stale"] == []
    res = runner.invoke(app, ["mosaic", "list", pp])
    assert "Tre anni" in res.output and "1 area(s)" in res.output
    res = runner.invoke(app, ["mosaic", "remove", pp, recs[1]["id"]])
    assert res.exit_code == 0, res.output
    assert len(mosaic.mosaics_of(ppg.Document.open(pp))) == 1
    doc2, _ = project(tmp_path / "mixed", crs={"B": "LOCAL"})
    res = runner.invoke(app, ["mosaic", "build", str(tmp_path / "mixed" / "p.ppg"), "--chunks", "A,B"])
    assert res.exit_code == 1 and "not in the same frame" in res.output


def test_a_moved_dem_in_another_projected_system(tmp_path):
    from pyarchinit_photogrammetry.core import crs as crs_mod

    doc, c = project(tmp_path)
    ch = c["C"]
    ch.crs = "EPSG:3003"
    p = crs_mod.transform_points(np.array([[X0 + 4.0, Y1, 0.0]]), CRS, "EPSG:3003")[0]
    ch._set_product("dem", write_geotiff(ch.data_dir / "dem.tif", np.full((80, 80), 30.0, np.float32), (p[0], p[1]),
                                         RES, "EPSG:3003", nodata=NODATA))
    ch.meta.setdefault("built_in", {})["dem"] = np.eye(4).tolist()
    m = np.eye(4)
    m[:3, 3] = (1.0, 0.0, 0.5)
    ch.transform = m
    rec = mosaic.build(doc, ["A", "C"], product="dem")  # in the system of A
    z, tr = read(doc, rec)
    assert at(z, tr, 7.0, 2.0)[0] == pytest.approx(30.5, abs=1e-4)  # reprojected, moved and raised
    assert at(z, tr, 5.5, 2.0)[0] == pytest.approx(30.5, abs=1e-4)
    assert np.isnan(at(z, tr, 4.5, 2.0)[0])  # where C was built, before the move: nothing now
    assert rec["moved"] == [ch.key] and rec["crs"] == CRS
