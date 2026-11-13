"""A survey as a web site (io/site.py): the folder, its two frames, the local server and the page in a browser."""
from __future__ import annotations

import base64
import contextlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pytest

SECRET = "appunti riservati.txt"


# ------------------------------------------------------------------------------------------------ the project
def _with_drawings(processed, folder: Path):
    """A copy of the processed trench with a unit, a spot height, a break line, a measuring point and a free
    drawing on its plan; the unit has a private key and a text that names a folder of this computer."""
    import rasterio
    from shapely.geometry import box

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.archaeo import breaklines
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.core import free_drawings

    doc, _ch, _ = processed
    folder.mkdir()
    shutil.copy2(doc.path, folder / doc.path.name)
    shutil.copytree(doc.files_dir, folder / doc.files_dir.name)
    doc = ppg.Document.open(folder / doc.path.name)
    ch = doc.chunk
    with rasterio.open(ch.dem_path) as src:
        b = src.bounds
        dem = src.read(1, masked=True)
        col, row = np.argwhere(~np.ma.getmaskarray(dem))[len(dem.compressed()) // 2][::-1]
        x, y = src.xy(int(row), int(col))
        z = float(dem[int(row), int(col)])
    cx, cy = (b.left + b.right) / 2, (b.bottom + b.top) / 2
    unit = UnitPolygon(box(cx - 0.4, cy - 0.3, cx + 0.4, cy + 0.3), us="12", area="1", sito="Sito di prova",
                       attributes={"d_stratigrafica": "Strato", "note": str(Path.home() / "scavi" / SECRET),
                                   "_private": "x", "ppg_id": "abc"})
    d = ch.meta.setdefault("drawings", {})
    d["units"] = [unit.to_dict() | {"attributes": dict(unit.attributes)}]
    d["quote"] = [{"x": x, "y": y, "quota_q": z, "us_q": "12", "area_q": "1"}]
    d["lines"] = [breaklines.new_record("breakline", [(cx - 0.5, cy, z), (cx + 0.5, cy + 0.1, z + 0.05)],
                                        {"etichetta": "ciglio"})]
    d["points"] = [{"x": cx + 0.2, "y": cy + 0.2, "z": z, "label": "P1"}]
    free_drawings.add(ch, "line", [[cx - 0.3, cy - 0.2, z], [cx + 0.3, cy - 0.2, z]], layer="Limiti", name="limite",
                      attributes={"fase": "II"})
    doc.save()
    return doc, ch, {"x": x, "y": y, "z": z, "cx": cx, "cy": cy}


@pytest.fixture(scope="module")
def trench_site(processed, tmp_path_factory):
    """``(doc, chunk, facts, folder, manifest)``: the trench with drawings and its site in the local frame."""
    base = tmp_path_factory.mktemp("sito")
    doc, ch, facts = _with_drawings(processed, base / "progetto")
    # small tiles: the model has a coarse root and finer tiles, as a real one
    path = doc.export_site(base / "site", title="Saggio di prova", model_faces=8000)
    return doc, ch, facts, path.parent, json.loads(path.read_text(encoding="utf-8"))


def _glb(path: Path) -> tuple[dict, bytes]:
    """The JSON and the binary chunk of a GLB."""
    import struct

    data = path.read_bytes()
    size = struct.unpack_from("<I", data, 12)[0]
    return json.loads(data[20:20 + size]), data[20 + size + 8:]


def _tile_points(path: Path) -> np.ndarray:
    """The vertices of a GLB tile, back to Z up (x east, y north, z up)."""
    gltf, blob = _glb(path)
    out = []
    for prim in gltf["meshes"][0]["primitives"]:
        acc = gltf["accessors"][prim["attributes"]["POSITION"]]
        view = gltf["bufferViews"][acc["bufferView"]]
        v = np.frombuffer(blob, np.float32, acc["count"] * 3, view.get("byteOffset", 0)).reshape(-1, 3)
        out.append(np.column_stack([v[:, 0], -v[:, 2], v[:, 1]]))
    return np.vstack(out)


def _leaves(node: dict) -> list[dict]:
    return [n for k in node.get("children", []) for n in _leaves(k)] if node.get("children") else [node]


def _frame_shift(ch, site: Path, dem: str = "data/dem.tif") -> np.ndarray:
    """What the local frame of a site took off the coordinates of the chunk: from its DEM, as a reader would."""
    import math

    import rasterio

    with rasterio.open(ch.dem_path) as a, rasterio.open(site / dem) as b:
        return np.array([a.bounds.left - b.bounds.left, a.bounds.top - b.bounds.top,
                         math.floor(float(a.read(1, masked=True).min()))])


def _layer(manifest, name):
    return next(x for x in manifest["chunks"][0]["layers"] if x["id"] == name)


# ------------------------------------------------------------------------------------------------ the folder
def test_local_site_folder_and_manifest(trench_site):
    import rasterio

    import pyarchinit_photogrammetry as ppg

    _doc, ch, _facts, site, manifest = trench_site
    assert manifest["format"] == "ppg-site" and manifest["version"] == 1
    assert manifest["generator"] == {"name": "pyarchinit-photogrammetry", "version": ppg.__version__}
    assert manifest["title"] == "Saggio di prova" and manifest["languages"] == ["it", "en"]
    assert manifest["frame"] == "local" and manifest["crs"] is None and manifest["unit"] == "m"
    chunk = manifest["chunks"][0]
    assert chunk["label"] == ch.label and [x["id"] for x in chunk["layers"]] == [
        "ortho", "hillshade", "dem", "model", "units", "drawings"]
    for layer in chunk["layers"]:
        assert (site / layer["url"]).is_file() and set(layer["title"]) == {"it", "en"}
    for name in ("index.html", "app/main.js", "app/cog.js", "app/i18n.js", "app/style.css", "app/vendor/ol.js",
                 "app/vendor/ol.css", "app/vendor/OPENLAYERS-LICENSE.md", "app/vendor/RBUSH-LICENSE.txt",
                 "app/vendor/QUICKSELECT-LICENSE.txt", "app/vendor/EARCUT-LICENSE.txt",
                 "app/vendor/PBF-LICENSE.txt", "app/THIRD_PARTY.txt", "app/view3d.js",
                 "app/vendor/three/three.module.js", "app/vendor/three/addons/loaders/GLTFLoader.js",
                 "app/vendor/3d-tiles-renderer/index.three.js", "app/vendor/THREE-LICENSE.txt",
                 "app/vendor/3D-TILES-RENDERER-LICENSE.txt", "data/model/tileset.json"):
        assert (site / name).is_file(), name
    assert "<title>Saggio di prova</title>" in (site / "index.html").read_text(encoding="utf-8")
    assert not [p.name for p in site.iterdir() if p.name.startswith(".")]  # the working folder is gone

    with rasterio.open(site / "data/ortho.tif") as cog, rasterio.open(ch.orthomosaic_path) as src:
        assert cog.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG" and cog.profile["compress"] == "webp"
        assert cog.count == 4 and (cog.width, cog.height) == (src.width, src.height)
        assert float((cog.dataset_mask() == 0).mean()) == pytest.approx(float((src.dataset_mask() == 0).mean()),
                                                                        abs=0.01)
    with rasterio.open(site / "data/dem.tif") as cog, rasterio.open(ch.dem_path) as src:
        a, b = cog.read(1, masked=True), src.read(1, masked=True)
        assert cog.dtypes[0] == "float32" and cog.profile["compress"] == "deflate"
        assert cog.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG"
        assert np.array_equal(np.ma.getmaskarray(a), np.ma.getmaskarray(b))
        lift = float(np.floor(b.min()))
        assert np.allclose(a.compressed(), b.compressed() - lift, atol=1e-4)  # heights from the whole metre below
        assert 0 <= float(a.min()) < 1
        assert _layer(manifest, "dem")["range"] == pytest.approx([float(a.min()), float(a.max())], abs=2e-3)
        empty = np.ma.getmaskarray(a)
    with rasterio.open(site / "data/dem_hillshade.tif") as cog:
        assert cog.count == 4 and cog.profile["compress"] == "webp"
        assert float(((cog.read(4) == 0) != empty).mean()) < 0.01  # transparent where the DEM is empty


def test_local_frame_gives_no_place_and_no_path(trench_site, tmp_path):
    import rasterio

    doc, ch, facts, site, manifest = trench_site
    private = [str(Path.home()), str(Path(doc.path).parent), tempfile.gettempdir(),
               str(Path(tempfile.gettempdir()).resolve()), str(tmp_path.parent)]
    for f in site.rglob("*"):
        if not f.is_file():
            continue
        data = f.read_bytes()
        for p in private:
            assert p.encode() not in data, f"{f.relative_to(site)} names {p}"
    for name in ("ortho.tif", "dem.tif", "dem_hillshade.tif"):
        with rasterio.open(site / "data" / name) as src:
            assert src.crs is None and not src.tags()
            assert max(abs(v) for v in src.bounds) < 100  # around the origin, not in Gauss-Boaga
        data = (site / "data" / name).read_bytes()
        for word in (b"EPSG", b"Monte Mario", b"PROJCS", b"3004", b".vrt", b"chunk_0"):
            assert word not in data, f"{name} holds {word!r}"
    text = "".join((site / n).read_text(encoding="utf-8") for n in
                   ("site.json", "data/units.geojson", "data/drawings.geojson", "index.html"))
    tileset_text = (site / "data/model/tileset.json").read_text(encoding="utf-8")
    tileset = json.loads(tileset_text)
    assert "transform" not in tileset_text and set(tileset.get("extras", {})) <= {"textures"}
    # its numbers are the boxes and errors of the tiles, long decimals in which the digits of an easting turn up
    # by chance: they are checked as numbers (small, around the origin), its words as words
    assert not any(w in tileset_text for w in ("EPSG", "crs", '"origin"'))
    assert all(abs(float(n)) < 100 for n in re.findall(r"-?\d+\.\d+(?:[eE][-+]?\d+)?", tileset_text))
    for f in (site / "data/model/tiles").glob("*.glb"):
        header = _glb(f)[0]  # the text of a tile: nothing of the place of the site
        assert not any(w in json.dumps(header) for w in ("EPSG", "crs", "origin", "extras\": {\"c")), f.name
        assert max(abs(v) for a in header["accessors"] for v in a.get("max", []) + a.get("min", [])) < 100
    for word in ("EPSG", "3004", "2480", "4500", "crs\":{", '"origin"'):
        assert word not in text, word
    extent = manifest["chunks"][0]["extent"]
    assert max(abs(v) for v in extent) < 100 and extent[0] < 0 < extent[2] and extent[1] < 0 < extent[3]

    units = json.loads((site / "data/units.geojson").read_text(encoding="utf-8"))
    assert "crs" not in units and len(units["features"]) == 1
    p = units["features"][0]["properties"]
    assert p["label"] == "US 12" and p["us"] == "12" and p["area"] == "1" and p["sito"] == "Sito di prova"
    assert p["d_stratigrafica"] == "Strato" and p["area_m2"] == pytest.approx(0.48, abs=1e-3)
    assert p["note"] == SECRET and "_private" not in p and "ppg_id" not in p  # the path cut to its last name
    ring = np.array(units["features"][0]["geometry"]["coordinates"][0])
    assert np.abs(ring).max() < 100 and np.ptp(ring[:, 0]) == pytest.approx(0.8, abs=1e-3)

    drawings = json.loads((site / "data/drawings.geojson").read_text(encoding="utf-8"))
    by_layer = {f["properties"]["layer"]: f for f in drawings["features"]}
    assert set(by_layer) == {"Quote", "Linee di rottura", "Punti di misura", "Limiti"}
    groups = {g["name"]: g for g in _layer(manifest, "drawings")["groups"]}
    assert set(groups) == set(by_layer) and all(g["count"] == 1 for g in groups.values())
    with rasterio.open(ch.dem_path) as src:
        lift = float(np.floor(src.read(1, masked=True).min()))
    quota = by_layer["Quote"]["properties"]
    assert quota["quota"] == pytest.approx(facts["z"] - lift, abs=1e-3) and quota["us_q"] == "12"
    # three decimals of the height written, which is the height asked within a millimetre: not its own rounding,
    # that falls on either side when the height sits half way between two millimetres
    assert len(quota["label"].split(".")[1]) == 3 and float(quota["label"]) == pytest.approx(quota["quota"], abs=6e-4)
    line = by_layer["Linee di rottura"]
    assert line["properties"]["label"] == "ciglio" and line["geometry"]["type"] == "LineString"
    assert line["geometry"]["coordinates"][0][2] == pytest.approx(facts["z"] - lift, abs=1e-3)
    assert by_layer["Limiti"]["properties"]["fase"] == "II" and by_layer["Limiti"]["properties"]["label"] == "limite"
    assert by_layer["Punti di misura"]["properties"]["label"] == "P1"


def test_model_in_the_frame_of_the_plan(trench_site, tmp_path):
    """The model of the site: 3D Tiles in the metres of the plan (the same origin and the same base of the heights
    as the DEM), no transform to the globe, the texture in pages of a capped size shared by the leaves; in the
    ``crs`` frame the tileset of the program, on the globe, with its centre in the manifest."""
    from PIL import Image

    from pyarchinit_photogrammetry.io.web import _world_mesh

    _doc, ch, _facts, site, manifest = trench_site
    layer = _layer(manifest, "model")
    assert layer["type"] == "tiles" and layer["url"] == "data/model/tileset.json" and "origin" not in layer
    assert layer["title"] == {"it": "Modello 3D", "en": "3D model"} and layer["textured"] is True
    tileset = json.loads((site / layer["url"]).read_text(encoding="utf-8"))
    assert tileset["asset"]["version"] == "1.1" and "transform" not in tileset["root"]
    assert tileset["extras"] == {"textures": ["textures/page_0.jpg"]}
    leaves = _leaves(tileset["root"])
    assert len(leaves) > 1 and tileset["root"]["children"]  # a coarse root and finer tiles
    world = _world_mesh(ch)[0]
    assert layer["faces"] == len(ch.model.faces)
    points = np.vstack([_tile_points(site / "data/model" / n["content"]["uri"]) for n in leaves])
    expected = world - _frame_shift(ch, site)
    assert points.min(0) == pytest.approx(expected.min(0), abs=1e-4)
    assert points.max(0) == pytest.approx(expected.max(0), abs=1e-4)
    assert layer["box"] == pytest.approx([*expected.min(0), *expected.max(0)], abs=1e-3)
    assert 0 <= _layer(manifest, "dem")["range"][0] < 1 and expected[:, 2].min() > -0.5  # the base of the DEM
    leaf, _ = _glb(site / "data/model" / leaves[0]["content"]["uri"])
    assert leaf["materials"][0]["extras"] == {"page": 0} and "images" not in leaf
    assert "TEXCOORD_0" in leaf["meshes"][0]["primitives"][0]["attributes"]
    root, _ = _glb(site / "data/model" / tileset["root"]["content"]["uri"])
    assert "COLOR_0" in root["meshes"][0]["primitives"][0]["attributes"]  # coarse tiles: vertex colours
    with Image.open(site / "data/model/textures/page_0.jpg") as im:
        assert max(im.size) <= 4096 and not im.info.get("exif")

    # a smaller texture, no texture at all
    doc = trench_site[0]
    small = doc.export_site(tmp_path / "piccolo", products="model", model_texture=512).parent
    with Image.open(small / "data/model/textures/page_0.jpg") as im:
        assert max(im.size) == 512
    plain = json.loads(doc.export_site(tmp_path / "colori", products="model", model_texture=None)
                       .read_text(encoding="utf-8"))
    assert _layer(plain, "model")["textured"] is False
    assert not (tmp_path / "colori/data/model/textures").exists()
    assert _layer(plain, "model")["box"][2] < 1  # without a DEM the base is the lowest vertex of the model

    # the crs frame: the tileset on the globe, its centre in the manifest
    crs = json.loads(doc.export_site(tmp_path / "crs", frame="crs", products=("dem", "model"))
                     .read_text(encoding="utf-8"))
    layer = _layer(crs, "model")
    centre = (world.min(0) + world.max(0)) / 2
    assert layer["origin"] == pytest.approx(list(centre), abs=1e-6)
    assert layer["box"] == pytest.approx([*world.min(0), *world.max(0)], abs=1e-3)
    tiles = json.loads((tmp_path / "crs/data/model/tileset.json").read_text(encoding="utf-8"))
    assert len(tiles["root"]["transform"]) == 16 and tiles["extras"]["crs"] == "EPSG:3004"


def test_crs_frame_keeps_the_reference_system(trench_site, tmp_path):
    import rasterio

    doc, ch, facts, _site, _ = trench_site
    path = doc.export_site(tmp_path / "site", frame="crs", language="en", products=("dem", "units"))
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["frame"] == "crs" and manifest["crs"] == "EPSG:3004" and manifest["languages"] == ["en"]
    assert [x["id"] for x in manifest["chunks"][0]["layers"]] == ["hillshade", "dem", "units"]
    assert _layer(manifest, "hillshade")["visible"] is True  # no orthophoto: the relief is shown
    with rasterio.open(path.parent / "data/dem.tif") as cog, rasterio.open(ch.dem_path) as src:
        assert cog.crs.to_epsg() == 3004 and tuple(cog.bounds) == pytest.approx(tuple(src.bounds))
        assert np.allclose(cog.read(1, masked=True).compressed(), src.read(1, masked=True).compressed())
    units = json.loads((path.parent / "data/units.geojson").read_text(encoding="utf-8"))
    assert units["crs"]["properties"]["name"] == "urn:ogc:def:crs:EPSG::3004"
    ring = np.array(units["features"][0]["geometry"]["coordinates"][0])
    assert ring[:-1, 0].mean() == pytest.approx(facts["cx"], abs=1e-3)
    assert not (path.parent / "data/ortho.tif").exists()
    assert str(Path.home()).encode() not in (path.parent / "data/dem.tif").read_bytes()


def test_site_folder_is_never_someone_elses(trench_site, tmp_path):
    doc, *_ = trench_site
    other = tmp_path / "documenti"
    other.mkdir()
    (other / "tesi.txt").write_text("mia", encoding="utf-8")
    with pytest.raises(FileExistsError):
        doc.export_site(other)
    assert [p.name for p in other.iterdir()] == ["tesi.txt"]
    site = doc.export_site(tmp_path / "site", products="units").parent
    (site / "CNAME").write_text("esempio.org", encoding="utf-8")  # a file of the host: kept when built again
    (site / "data" / "vecchio.txt").write_text("x", encoding="utf-8")
    doc.export_site(site, products="drawings")
    assert (site / "CNAME").exists() and not (site / "data" / "vecchio.txt").exists()
    assert sorted(p.name for p in (site / "data").iterdir()) == ["drawings.geojson"]
    with pytest.raises(ValueError, match="frame"):
        doc.export_site(tmp_path / "x", frame="wgs84")
    with pytest.raises(ValueError, match="language"):
        doc.export_site(tmp_path / "x", language="de")
    with pytest.raises(ValueError, match="no chunk"):
        doc.export_site(tmp_path / "x", chunks=["Saggio 9"])
    assert not (tmp_path / "x").exists()


def test_several_chunks_share_one_frame(tmp_path):
    import pyarchinit_photogrammetry as ppg
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    doc = ppg.Document()
    for n, x in enumerate((1000.0, 1010.0)):
        ch = doc.add_chunk(f"Saggio {n + 1}")
        ch.crs = "EPSG:32633"
        ch.meta["drawings"] = {"units": [UnitPolygon(box(x, 5000.0, x + 2, 5002.0), us=str(n + 1)).to_dict()]}
    doc.add_chunk("vuoto")
    doc.save(tmp_path / "due.ppg")
    path = doc.export_site(tmp_path / "site")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert [c["label"] for c in manifest["chunks"]] == ["Saggio 1", "Saggio 2"]  # the empty chunk left out
    assert [c["layers"][0]["url"] for c in manifest["chunks"]] == ["data/1/units.geojson", "data/2/units.geojson"]
    assert manifest["chunks"][0]["extent"] == [-6, -1, -4, 1] and manifest["chunks"][1]["extent"] == [4, -1, 6, 1]
    assert manifest["title"] == "due"
    one = doc.export_site(tmp_path / "uno", chunks=["Saggio 2"], frame="crs")
    assert json.loads(one.read_text(encoding="utf-8"))["chunks"][0]["extent"] == [1010, 5000, 1012, 5002]
    doc.chunks[1].crs = "EPSG:32632"
    with pytest.raises(ValueError, match="different reference systems"):
        doc.export_site(tmp_path / "site")
    assert json.loads(path.read_text(encoding="utf-8")) == manifest  # the site built before is still there
    doc.chunks[0].crs = doc.chunks[1].crs = "EPSG:4326"
    with pytest.raises(ValueError, match="degrees"):
        doc.export_site(tmp_path / "gradi")
    with pytest.raises(ValueError, match="nothing to publish"):
        doc.export_site(tmp_path / "gradi", chunks=["vuoto"])


def test_local_heights_start_under_the_true_minimum(tmp_path):
    """A wide DEM with a pit one cell across, which a reduced reading misses: the heights of the local frame
    start under it all the same, and no cell is under zero. A drawing at height zero (made without heights) does
    not move the base, or the true heights would be published; without a DEM the drawn heights give the base."""
    import rasterio
    from rasterio.transform import from_origin

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.archaeo.terrain import dem_range
    from pyarchinit_photogrammetry.io import site as site_mod

    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    ch.crs = "EPSG:32633"
    doc.save(tmp_path / "fossa.ppg")
    heights = np.full((64, 3300), 100.25, dtype="float32")
    heights[:, :300] = -9999.0
    heights[36, 1600] = 97.3
    dem = ch.own_product_path("dem")
    dem.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(dem, "w", driver="GTiff", width=3300, height=64, count=1, dtype="float32",
                       nodata=-9999.0, crs="EPSG:32633", transform=from_origin(500000.0, 4500000.0, 0.01, 0.01),
                       tiled=True, blockxsize=256, blockysize=256, compress="deflate") as dst:
        dst.write(heights, 1)
    ch._set_product("dem", dem)
    assert dem_range(ch.dem_path)[0] > 100  # the reduced reading does not see the pit
    assert site_mod._dem_heights(ch.dem_path) == pytest.approx((97.3, 100.25), abs=1e-4)

    path = doc.export_site(tmp_path / "site", products=["dem", "drawings"])
    layer = _layer(json.loads(path.read_text(encoding="utf-8")), "dem")
    assert layer["range"] == pytest.approx([0.3, 3.25], abs=1e-3)
    with rasterio.open(path.parent / "data/dem.tif") as src:
        cells = src.read(1, masked=True)
    assert cells.min() == pytest.approx(0.3, abs=1e-4) and cells.min() >= 0 and cells.count() == 64 * 3000

    ch.meta["drawings"] = {"quote": [{"x": 500010.0, "y": 4499999.8, "quota_q": 98.4}],
                           "points": [{"x": 500011.0, "y": 4499999.8, "z": 0.0, "label": "P1"}]}
    path = doc.export_site(tmp_path / "site", products=["dem", "drawings"])
    layer = _layer(json.loads(path.read_text(encoding="utf-8")), "dem")
    assert layer["range"] == pytest.approx([0.3, 3.25], abs=1e-3)  # the base is still the one of the DEM
    drawn = json.loads((path.parent / "data/drawings.geojson").read_text(encoding="utf-8"))["features"]
    assert sorted(f["properties"].get("quota", f["properties"].get("z")) for f in drawn) == pytest.approx([-97, 1.4])
    ch.meta["drawings"]["points"][0]["z"] = 97.9
    path = doc.export_site(tmp_path / "site", products=["drawings"])  # no DEM: the lowest drawn height
    drawn = json.loads((path.parent / "data/drawings.geojson").read_text(encoding="utf-8"))["features"]
    assert sorted(f["properties"].get("quota", f["properties"].get("z")) for f in drawn) == pytest.approx([0.9, 1.4])
    assert not (path.parent / "data/dem.tif").exists()  # the site built again without the DEM


def test_names_typed_by_the_user_carry_no_path(tmp_path):
    """The names the user typed - of the chunk, of a layer of free drawings, of a drawing, of an attribute, the
    title and the description - lose the path of a computer as the values of the attributes do, also when the
    path is of another computer or of another disk."""
    from shapely.geometry import box

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.core import free_drawings
    from pyarchinit_photogrammetry.io import site as site_mod

    home = str(Path.home())
    doc = ppg.Document()
    ch = doc.add_chunk(f"{home}/Rilievi/Saggio 3")
    ch.crs = "EPSG:32633"
    unit = UnitPolygon(box(1000.0, 5000.0, 1002.0, 5002.0), us="1",
                       attributes={"/Volumes/Disco esterno/campagna/schede.xlsx": "vista",
                                   "foto": "C:\\Users\\scavo\\Desktop\\us1.jpg",
                                   "rete": "\\\\server\\scavi\\us1.pdf", "link": "file:///home/scavo/us1.pdf",
                                   "nota": "taglio 1/2, quota -0.35 m; rapporto: copre/taglia"})
    ch.meta["drawings"] = {"units": [unit.to_dict() | {"attributes": dict(unit.attributes)}]}
    free_drawings.add(ch, "line", [[1000.0, 5000.0, 10.0], [1001.0, 5001.0, 10.0]],
                      layer=f"{home}/Desktop/limiti di scavo", name="/Volumes/Disco esterno/campagna/limite nord")
    free_drawings.add(ch, "point", [[1001.0, 5001.5, 10.2]], layer="~/Documents/reperti", name="chiodo")
    doc.save(tmp_path / "nomi.ppg")
    path = doc.export_site(tmp_path / "site", title="/Volumes/Disco esterno/campagna/Tell di prova",
                           description=f"vedi {home}/Documents/relazione finale.pdf")
    site = path.parent
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["title"] == "Tell di prova" and manifest["description"] == "relazione finale.pdf"
    assert manifest["chunks"][0]["label"] == "Saggio 3"
    groups = [g["name"] for g in _layer(manifest, "drawings")["groups"]]
    assert sorted(groups) == ["limiti di scavo", "reperti"]
    assert all(g["title"] == {"it": g["name"], "en": g["name"]} for g in _layer(manifest, "drawings")["groups"])
    drawn = json.loads((site / "data/drawings.geojson").read_text(encoding="utf-8"))["features"]
    assert sorted(f["properties"]["layer"] for f in drawn) == ["limiti di scavo", "reperti"]
    assert sorted(f["properties"]["label"] for f in drawn) == ["chiodo", "limite nord"]
    p = json.loads((site / "data/units.geojson").read_text(encoding="utf-8"))["features"][0]["properties"]
    assert p["schede.xlsx"] == "vista" and p["foto"] == "us1.jpg" and p["rete"] == "us1.pdf"
    assert p["link"] == "us1.pdf"
    assert p["nota"] == "taglio 1/2, quota -0.35 m; rapporto: copre/taglia"  # words with a slash are not paths
    for f in site.rglob("*"):
        if f.is_file() and f.suffix in (".json", ".geojson", ".html"):
            text = f.read_text(encoding="utf-8")
            for word in (home, "/Volumes", "Disco esterno", "Desktop", "Documents", "C:", "server", "file:"):
                assert word not in text, f"{f.name} holds {word}"
    # the rule itself: what is a path and what is not
    for text in ("/Users/x/y.txt", "in ~/scavo/a", "D:/dati/a.tif", "(\\\\nas\\a)", "x=/tmp/a", "file:/a"):
        assert site_mod._scrub(text, []) != text, text
    for text in ("US 12/13", "scala 1:20", "km/h", "N/A", "a: b", "http e https", "~10 cm", "1/2"):
        assert site_mod._scrub(text, []) == text, text


# ------------------------------------------------------------------------------------------------ the server
@contextlib.contextmanager
def _serving(folder, **options):
    """The port of a local server of ``folder``, running while the block lasts."""
    from pyarchinit_photogrammetry.io import site as site_mod

    httpd = site_mod.server(folder, **options)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


@pytest.fixture()
def served(trench_site):
    with _serving(trench_site[3]) as port:
        yield trench_site[3], port


def _get(port, path, headers=None, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
        r = conn.getresponse()
        return r.status, dict((k.lower(), v) for k, v in r.getheaders()), r.read()
    finally:
        conn.close()


def test_server_answers_range_requests(served):
    from pyarchinit_photogrammetry.io import site as site_mod

    folder, port = served
    assert site_mod.server(folder).server_address[0] == "127.0.0.1"  # this computer alone, unless asked
    data = (folder / "data/dem.tif").read_bytes()
    status, head, body = _get(port, "/data/dem.tif")
    assert status == 200 and body == data and head["accept-ranges"] == "bytes" and head["content-type"] == "image/tiff"
    status, head, body = _get(port, "/data/dem.tif", {"Range": "bytes=100-299"})
    assert status == 206 and body == data[100:300] and head["content-range"] == f"bytes 100-299/{len(data)}"
    assert head["content-length"] == "200" and head["content-type"] == "image/tiff"
    status, head, body = _get(port, "/data/dem.tif", {"Range": f"bytes={len(data) - 10}-{len(data) + 5000}"})
    assert status == 206 and body == data[-10:] and head["content-range"].startswith(f"bytes {len(data) - 10}-")
    status, _, body = _get(port, "/data/dem.tif", {"Range": "bytes=-16"})
    assert status == 206 and body == data[-16:]
    status, _, body = _get(port, "/data/dem.tif", {"Range": "bytes=50-"})
    assert status == 206 and body == data[50:]
    status, head, _ = _get(port, "/data/dem.tif", {"Range": f"bytes={len(data)}-"})
    assert status == 416 and head["content-range"] == f"bytes */{len(data)}"
    assert _get(port, "/data/dem.tif", {"Range": "bytes=0-1,5-6"})[0] == 416  # one range at a time
    status, head, body = _get(port, "/data/dem.tif", {"Range": "bytes=0-9"}, method="HEAD")
    assert status == 206 and body == b"" and head["content-length"] == "10"

    status, head, body = _get(port, "/")
    assert status == 200 and head["content-type"].startswith("text/html") and b"app/main.js" in body
    assert _get(port, "/app/main.js")[1]["content-type"].startswith("text/javascript")
    assert _get(port, "/site.json")[1]["content-type"].startswith("application/json")
    assert _get(port, "/data/units.geojson")[1]["content-type"].startswith("application/geo+json")
    assert _get(port, "/data/")[0] == 404  # no list of a folder
    assert _get(port, "/../../etc/passwd")[0] == 404 and _get(port, "/nulla.tif")[0] == 404


def test_server_without_ranges_gives_whole_files(trench_site):
    """``ranges=False``: the server of a host that ignores ``Range``, to try the page against it."""
    folder = trench_site[3]
    data = (folder / "data/dem.tif").read_bytes()
    with _serving(folder, ranges=False) as port:
        status, head, body = _get(port, "/data/dem.tif", {"Range": "bytes=100-299"})
        assert status == 200 and body == data and "accept-ranges" not in head and "content-range" not in head
        assert head["content-length"] == str(len(data))
        assert _get(port, "/data/dem.tif", {"Range": f"bytes={len(data)}-"})[0] == 200
        assert _get(port, "/data/")[0] == 404


def test_web_commands(trench_site, tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc = trench_site[0]
    run = CliRunner()
    r = run.invoke(app, ["web", "build", str(doc.path), str(tmp_path / "site"), "--product", "units", "--title",
                         "Prova", "--language", "en"])
    assert r.exit_code == 0, r.output
    manifest = json.loads((tmp_path / "site/site.json").read_text(encoding="utf-8"))
    assert manifest["title"] == "Prova" and manifest["languages"] == ["en"] and manifest["frame"] == "local"
    assert "ppg web serve" in r.output and "real coordinates" not in r.output
    r = run.invoke(app, ["web", "build", str(doc.path), str(tmp_path / "site"), "--frame", "crs", "--product", "units"])
    assert r.exit_code == 0 and "real coordinates" in r.output
    (tmp_path / "altro").mkdir()
    (tmp_path / "altro/file.txt").write_text("x", encoding="utf-8")
    r = run.invoke(app, ["web", "build", str(doc.path), str(tmp_path / "altro")])
    assert r.exit_code == 1 and "left as it is" in r.output
    r = run.invoke(app, ["web", "serve", str(tmp_path / "altro")])
    assert r.exit_code == 1 and "not a site" in r.output


def test_viewer_needs_nothing_from_the_network():
    import re

    from pyarchinit_photogrammetry.io import site as site_mod

    page = (site_mod.VIEWER / "index.html").read_text(encoding="utf-8")
    assert "Content-Security-Policy" in page and "default-src 'self'" in page
    for name in ("index.html", "app/main.js", "app/view3d.js", "app/cog.js", "app/i18n.js", "app/style.css",
                 "app/vendor/ol.css"):
        text = (site_mod.VIEWER / name).read_text(encoding="utf-8")
        assert not re.search(r"https?://|//cdn|@import|innerHTML|eval\(", text), name
    notice = (site_mod.VIEWER / "app/THIRD_PARTY.txt").read_text(encoding="utf-8")
    assert "OpenLayers 10.11.0" in notice and "BSD 2-Clause" in notice
    assert "BSD 2-Clause License" in (site_mod.VIEWER / "app/vendor/OPENLAYERS-LICENSE.md").read_text(encoding="utf-8")
    import hashlib

    digest = hashlib.sha256((site_mod.VIEWER / "app/vendor/ol.js").read_bytes()).hexdigest()
    assert digest in notice  # the file is the one of the release named in the notice
    assert (site_mod.VIEWER / "app/vendor/ol.js").read_bytes().rstrip().endswith(b"sourceMappingURL=ol.js.map")
    # the libraries inside the build: each with the licence of its package, and its copyright line in the notice
    for name, version, licence in (("rbush", "4.0.1", "MIT License"), ("quickselect", "3.0.0", "ISC License"),
                                   ("earcut", "3.2.4", "ISC License"), ("pbf", "5.1.2", "Redistribution")):
        text = (site_mod.VIEWER / f"app/vendor/{name.upper()}-LICENSE.txt").read_text(encoding="utf-8")
        owner = next(line.strip() for line in text.splitlines() if line.startswith("Copyright"))
        assert licence in text and f"{name} {version}" in notice and owner in notice, name
        assert f"vendor/{name.upper()}-LICENSE.txt" in notice


def test_vendored_3d_libraries_are_the_released_files():
    """three.js and 3DTilesRendererJS: every file in their folders is named in the notice with the sha256 of the
    file of the npm release, beside the licences; the import map that names them is the one the page allows."""
    import base64
    import hashlib
    import re

    from pyarchinit_photogrammetry.io import site as site_mod

    app = site_mod.VIEWER / "app"
    notice = (app / "THIRD_PARTY.txt").read_text(encoding="utf-8")
    listed = dict((path, digest) for digest, path in re.findall(r"sha256 ([0-9a-f]{64})  (vendor/\S+)", notice))
    files = sorted(str(f.relative_to(app)).replace(os.sep, "/") for d in ("three", "3d-tiles-renderer")
                   for f in (app / "vendor" / d).rglob("*") if f.is_file())
    assert len(files) == 9 and sorted(listed) == files
    for path, digest in listed.items():
        assert hashlib.sha256((app / path).read_bytes()).hexdigest() == digest, path
    assert "three.js 0.187.0" in notice and "3DTilesRendererJS 0.5.4" in notice
    assert "MIT License" in (app / "vendor/THREE-LICENSE.txt").read_text(encoding="utf-8")
    licence = (app / "vendor/3D-TILES-RENDERER-LICENSE.txt").read_text(encoding="utf-8")
    assert "Apache License" in licence and "Copyright 2020 California Institute of Technology" in licence
    assert "Copyright 2020 California Institute of Technology" in notice
    # the import map: the only inline script, allowed by its hash and by nothing else
    page = (site_mod.VIEWER / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script([^>]*)>(.*?)</script>", page, re.S)
    inline = [body for attrs, body in scripts if body.strip()]
    assert len(inline) == 1 and json.loads(inline[0])["imports"]["three"] == "./app/vendor/three/three.module.js"
    digest = base64.b64encode(hashlib.sha256(inline[0].encode()).digest()).decode()
    policy = re.search(r'Content-Security-Policy" content="([^"]+)"', page).group(1)
    assert f"script-src 'self' 'sha256-{digest}';" in policy and "unsafe" not in policy.split("style-src")[0]
    for target in json.loads(inline[0])["imports"].values():
        assert (site_mod.VIEWER / target).exists(), target


# ------------------------------------------------------------------------------------------------ the browser
def find_browser() -> str | None:
    """A Chromium to look at the page with: ``PPG_BROWSER``, the one Playwright downloaded, Chrome or Chromium
    where they are usually installed. None on Windows (the pipe of the DevTools protocol needs POSIX handles)."""
    if sys.platform == "win32":
        return None
    asked = os.environ.get("PPG_BROWSER")
    if asked:
        return asked if Path(asked).exists() else None
    if os.environ.get("CI"):
        return None  # on a runner only when asked for
    found = []
    for cache in (Path.home() / "Library/Caches/ms-playwright", Path.home() / ".cache/ms-playwright"):
        if cache.is_dir():
            for pattern in ("chromium_headless_shell-*/*/chrome-headless-shell",
                            "chromium_headless_shell-*/*/headless_shell",
                            "chromium-*/*/Chromium.app/Contents/MacOS/Chromium",
                            "chromium-*/*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                            "chromium-*/*/chrome"):
                found += sorted(cache.glob(pattern), reverse=True)
    found += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
              Path("/Applications/Chromium.app/Contents/MacOS/Chromium")]
    found += [Path(p) for p in (shutil.which("chromium"), shutil.which("chromium-browser"),
                                shutil.which("google-chrome")) if p]
    return next((str(p) for p in found if p.is_file() and os.access(p, os.X_OK)), None)


class Browser:
    """A headless Chromium driven through the DevTools protocol on a pipe (no package needed): one page, the
    errors of its console and the requests that failed kept in ``errors``."""

    def __init__(self, executable: str, profile: Path, size=(1100, 700)):
        to_browser_r, to_browser_w = os.pipe()
        from_browser_r, from_browser_w = os.pipe()
        for fd in (to_browser_r, from_browser_w):
            os.set_inheritable(fd, True)
        args = [executable, "--headless=new", "--remote-debugging-pipe", f"--user-data-dir={profile}",
                f"--window-size={size[0]},{size[1]}", "--no-first-run", "--no-default-browser-check",
                "--disable-background-networking", "--disable-component-update", "--disable-sync",
                "--disable-default-apps", "--disable-extensions", "--mute-audio", "--hide-scrollbars",
                "--force-device-scale-factor=1", "--use-mock-keychain", "--password-store=basic"]
        if os.environ.get("CI") or (hasattr(os, "geteuid") and os.geteuid() == 0):
            args.append("--no-sandbox")
        # the browser reads its commands on handle 3 and answers on 4
        self.process = subprocess.Popen(
            ["/bin/sh", "-c", f'exec "$@" 3<&{to_browser_r} 4>&{from_browser_w}', "sh", *args],
            close_fds=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(to_browser_r)
        os.close(from_browser_w)
        self._out, self._in = to_browser_w, from_browser_r
        self._buffer = b""
        self._id = 0
        self.errors: list[str] = []
        self.requests: list[str] = []
        self.session = None
        target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        for domain in ("Runtime", "Log", "Page", "Network"):
            self.call(f"{domain}.enable")

    def _read(self, timeout: float) -> dict | None:
        import select

        end = time.monotonic() + timeout
        while b"\0" not in self._buffer:
            left = end - time.monotonic()
            if left <= 0 or not select.select([self._in], [], [], left)[0]:
                return None
            block = os.read(self._in, 1 << 16)
            if not block:
                raise RuntimeError("the browser closed")
            self._buffer += block
        message, self._buffer = self._buffer.split(b"\0", 1)
        return json.loads(message)

    def _event(self, m: dict) -> None:
        method, p = m.get("method"), m.get("params") or {}
        if method == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails") or {}
            self.errors.append("exception: " + str((d.get("exception") or {}).get("description") or d.get("text")))
        elif method == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
            self.errors.append("console: " + " ".join(str(a.get("value", a.get("description"))) for a in p["args"]))
        elif method == "Log.entryAdded" and p["entry"].get("level") == "error":
            self.errors.append(f"log: {p['entry'].get('text')} {p['entry'].get('url', '')}")
        elif method == "Network.loadingFailed" and not p.get("canceled"):
            self.errors.append(f"request failed: {p.get('errorText')}")
        elif method == "Network.requestWillBeSent":
            self.requests.append(p["request"]["url"])

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._id += 1
        message = {"id": self._id, "method": method, "params": params or {}}
        if self.session:
            message["sessionId"] = self.session
        os.write(self._out, json.dumps(message).encode() + b"\0")
        end = time.monotonic() + timeout
        while True:
            m = self._read(max(0.0, end - time.monotonic()))
            if m is None:
                raise TimeoutError(method)
            if m.get("id") == self._id:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result") or {}
            self._event(m)

    def js(self, expression: str, timeout: float = 30.0):
        """The value of a JavaScript expression of the page (a promise is awaited)."""
        r = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True},
                      timeout)
        if "exceptionDetails" in r:
            raise RuntimeError(str(r["exceptionDetails"].get("exception", {}).get("description")
                                   or r["exceptionDetails"]))
        return r["result"].get("value")

    def wait(self, expression: str, timeout: float = 30.0) -> None:
        end = time.monotonic() + timeout
        while True:
            try:
                if self.js(expression):
                    return
            except RuntimeError:  # between two pages there is no document to ask
                pass
            if time.monotonic() > end:
                raise TimeoutError(f"{expression}; errors: {self.errors}")
            time.sleep(0.1)

    def click(self, x: float, y: float) -> None:
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1})

    def screenshot(self) -> np.ndarray:
        import cv2

        png = base64.b64decode(self.call("Page.captureScreenshot", {"format": "png"})["data"])
        return cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)

    def close(self) -> None:
        try:
            self.call("Browser.close", timeout=5)
        except Exception:  # noqa: BLE001 - closing anyway
            pass
        for fd in (self._out, self._in):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            self.process.kill()


def test_page_in_a_browser(served, trench_site, tmp_path):
    """The page loads with no error, shows the orthophoto, reads the DEM at a click as rasterio does, shows the
    attributes of the unit clicked, measures, changes language - and asks nothing outside its own server."""
    import rasterio

    executable = find_browser()
    if executable is None:
        pytest.skip("no Chromium found (PPG_BROWSER names one)")
    _doc, _ch, _facts, _site, manifest = trench_site
    folder, port = served
    origin = f"http://127.0.0.1:{port}"
    browser = Browser(executable, tmp_path / "profilo")
    try:
        browser.call("Page.navigate", {"url": origin + "/"})
        browser.wait("['ready', 'error'].includes(document.documentElement.dataset.state)", 60)
        assert browser.js("document.documentElement.dataset.state") == "ready", browser.errors
        assert browser.js("document.title") == "Saggio di prova"
        assert browser.js("ppgSite.map.getView().getProjection().getUnits()") == "m"
        assert browser.js("document.documentElement.dataset.ranges") == "served"
        assert browser.js("document.getElementById('notice').hidden") is True

        # the heights of the DEM, read by the page through range requests
        with rasterio.open(folder / "data/dem.tif") as src:
            dem = src.read(1, masked=True)
            cells = np.argwhere(~np.ma.getmaskarray(dem))
            for row, col in cells[:: max(1, len(cells) // 7)][:7]:
                x, y = (float(v) for v in src.xy(int(row), int(col)))
                assert browser.js(f"ppgSite.heightAt({x!r}, {y!r})") == pytest.approx(float(dem[row, col]), abs=1e-5)
            hole = np.argwhere(np.ma.getmaskarray(dem))
            if len(hole):
                x, y = (float(v) for v in src.xy(int(hole[0][0]), int(hole[0][1])))
                assert browser.js(f"ppgSite.heightAt({x!r}, {y!r})") is None
        assert browser.js("ppgSite.heightAt(1e6, 1e6)") is None

        # the picture: where the orthophoto is, the map is not its empty background
        browser.wait("document.querySelector('#map canvas') !== null")
        time.sleep(0.5)
        shot = browser.screenshot()
        box = browser.js("(() => { const r = document.getElementById('map').getBoundingClientRect();"
                         " return [r.left, r.top, r.width, r.height]; })()")
        cx, cy = int(box[0] + box[2] / 2), int(box[1] + box[3] / 2)
        patch = shot[cy - 60:cy + 60, cx - 60:cx + 60].astype(float)
        background = np.array([0xe6, 0xe3, 0xdf], float)  # #dfe3e6 in BGR
        assert np.abs(patch - background).mean() > 12 and patch.std() > 5, "the map is empty"

        # a click on the unit: its attributes and the height in the side
        unit = json.loads((folder / "data/units.geojson").read_text(encoding="utf-8"))["features"][0]
        ring = np.array(unit["geometry"]["coordinates"][0])[:-1]
        ux, uy = float(ring[:, 0].mean()) + 0.25, float(ring[:, 1].mean()) + 0.2  # off the lines drawn in it
        px = browser.js(f"ppgSite.map.getPixelFromCoordinate([{ux!r}, {uy!r}])")
        browser.click(box[0] + px[0], box[1] + px[1])
        browser.wait("document.querySelector('#info table') !== null", 10)
        info = browser.js("document.getElementById('info').innerText")
        assert "US 12" in info and "Strato" in info and SECRET in info and "Sito di prova" in info
        with rasterio.open(folder / "data/dem.tif") as src:
            expected = float(next(src.sample([(ux, uy)]))[0])
        assert _shown_height(info) == pytest.approx(expected, abs=2e-3)  # the pixel clicked, not the point

        # the words follow the language; the measure is on the plane
        first = browser.js("document.getElementById('layers-title').textContent")
        browser.js("document.getElementById('language').click()")
        second = browser.js("document.getElementById('layers-title').textContent")
        assert {first, second} == {"Livelli", "Layers"}
        assert "5.000 m" in browser.js("ppgSite.measured(new ol.geom.LineString([[0, 0], [3, 4]]))")
        assert "12.00 m²" in browser.js("ppgSite.measured(new ol.geom.Polygon([[[0,0],[4,0],[4,3],[0,3],[0,0]]]))")
        layers = browser.js("document.getElementById('layers').innerText")
        words = ("Orthophoto", "Shaded relief", "Stratigraphic units", "Break lines") if second == "Layers" else (
            "Ortofoto", "Rilievo ombreggiato", "Unità stratigrafiche", "Linee di rottura")
        for title in (*words, "Limiti"):
            assert title in layers, (title, layers)

        assert not browser.errors, browser.errors
        outside = [u for u in browser.requests if not u.startswith((origin, "about:", "blob:", "data:"))]
        assert not outside, outside  # nothing asked outside the folder served
    finally:
        browser.close()



def test_page_on_a_host_without_ranges(trench_site, tmp_path):
    """A host that answers a request of a part with the whole file: the page says so in its two languages and
    works on the files taken whole; a file over the limit is not downloaded, and its layer is named and left
    out while the drawings stay."""
    import rasterio

    executable = find_browser()
    if executable is None:
        pytest.skip("no Chromium found (PPG_BROWSER names one)")
    folder = trench_site[3]
    sizes = {n: (folder / "data" / n).stat().st_size for n in ("ortho.tif", "dem.tif", "dem_hillshade.tif")}
    browser = Browser(executable, tmp_path / "profilo")
    try:
        with _serving(folder, ranges=False) as port:
            origin = f"http://127.0.0.1:{port}"
            # no cache: a browser answers the request of a part from a whole file it already holds
            browser.call("Network.setCacheDisabled", {"cacheDisabled": True})
            browser.call("Page.navigate", {"url": origin + "/"})
            browser.wait("['ready', 'error'].includes(document.documentElement.dataset.state)", 60)
            assert browser.js("document.documentElement.dataset.state") == "ready", browser.errors
            assert browser.js("document.documentElement.dataset.ranges") == "none"
            assert browser.js("document.getElementById('notice').hidden") is False
            words = {browser.js("document.getElementById('notice').innerText")}
            browser.js("document.getElementById('language').click()")
            words.add(browser.js("document.getElementById('notice').innerText"))
            assert any("richieste HTTP Range" in w for w in words) and any("HTTP range requests" in w for w in words)
            assert browser.js("document.querySelector('#notice ul')") is None  # small files: nothing left out
            assert browser.js("ppgSite.state.dems.length") == 1 and browser.js("ppgSite.state.left.length") == 0
            with rasterio.open(folder / "data/dem.tif") as src:
                dem = src.read(1, masked=True)
                row, col = np.argwhere(~np.ma.getmaskarray(dem))[dem.count() // 3]
                x, y = (float(v) for v in src.xy(int(row), int(col)))
            assert browser.js(f"ppgSite.heightAt({x!r}, {y!r})") == pytest.approx(float(dem[row, col]), abs=1e-5)
            assert not browser.errors, browser.errors

            # the same host with files over the limit (lowered to the smallest of the three, less one byte)
            limit = min(sizes.values()) - 1
            browser.requests.clear()
            browser.call("Page.navigate", {"url": f"{origin}/?whole-limit={limit}"})
            browser.wait("document.documentElement.dataset.ranges === 'none'"
                         " && ['ready', 'error'].includes(document.documentElement.dataset.state)", 60)
            assert browser.js("document.documentElement.dataset.state") == "ready", browser.errors
            assert browser.js("ppgSite.state.left.map((x) => x.def.id)") == ["ortho", "hillshade", "dem"]
            assert browser.js("ppgSite.state.left.map((x) => x.size)") == [
                sizes["ortho.tif"], sizes["dem_hillshade.tif"], sizes["dem.tif"]]
            assert browser.js("document.querySelectorAll('#notice li').length") == 3
            notice = browser.js("document.getElementById('notice').innerText")
            assert ("Too large" in notice and "Orthophoto" in notice) or ("Troppo grandi" in notice
                                                                          and "Ortofoto" in notice)
            assert browser.js("ppgSite.state.entries.map((e) => e.def.id)") == ["units", "drawings"]
            assert browser.js("ppgSite.state.dems.length") == 0
            assert browser.js(f"ppgSite.heightAt({x!r}, {y!r})") is None
            assert not browser.errors, browser.errors
    finally:
        browser.close()


def _targets(processed) -> dict[str, np.ndarray]:
    """The coded targets of the synthetic trench: their true coordinates (the file the scene was made with)."""
    rows = (Path(processed[0].path).parent / "gcp.csv").read_text(encoding="utf-8").splitlines()
    return {r.split(",")[0]: np.array([float(v) for v in r.split(",")[1:4]]) for r in rows if r[:1].isdigit()}


def _open_model(browser: Browser) -> None:
    browser.js("document.getElementById('tab-3d').click()")
    browser.wait("['ready', 'error'].includes(document.documentElement.dataset.model)", 60)
    assert browser.js("document.documentElement.dataset.model") == "ready", browser.errors
    browser.wait("ppgSite.state.view3d.ready()", 60)


def _click_on_model(browser: Browser, point, close: bool = True, count: int = 1) -> None:
    """A click on the model where ``point`` (frame of the site) is; ``close``: seen from 0.6 m first, as one
    zooms in to pick a point, with the finest tiles loaded."""
    x, y, z = (float(v) for v in point)
    if close:
        browser.js(f"(() => {{ const v = ppgSite.state.view3d, c = v.centre;"
                   f" v.controls.target.set({x!r} - c.x, {y!r} - c.y, {z!r} - c.z);"
                   f" v.camera.position.set({x!r} - c.x, {y!r} - c.y - 0.2, {z!r} - c.z + 0.6);"
                   " v.controls.update(); })()")
        time.sleep(0.3)
        browser.wait("ppgSite.state.view3d.ready()", 30)
        time.sleep(0.2)
    px, py = browser.js(f"ppgSite.state.view3d.project({x!r}, {y!r}, {z!r})")
    for kind in ("mousePressed", "mouseReleased"):
        browser.call("Input.dispatchMouseEvent", {"type": kind, "x": px, "y": py, "button": "left",
                                                  "clickCount": count})


def test_model_in_a_browser(served, trench_site, processed, tmp_path):
    """The 3D tab: the tiles load with no error and nothing asked outside the server; a click gives the point of
    the model, at the height the DEM of the plan gives there; the distance between two targets and the area of the
    rectangle of four are those of the true coordinates within a few millimetres; Esc and Clear drop a measure."""
    import rasterio

    executable = find_browser()
    if executable is None:
        pytest.skip("no Chromium found (PPG_BROWSER names one)")
    _doc, ch, _facts, _site, manifest = trench_site
    folder, port = served
    shift = _frame_shift(ch, folder)
    targets = {k: v - shift for k, v in _targets(processed).items()}  # in the frame of the site
    origin = f"http://127.0.0.1:{port}"
    browser = Browser(executable, tmp_path / "profilo", size=(1200, 800))
    try:
        browser.call("Page.navigate", {"url": origin + "/"})
        browser.wait("['ready', 'error'].includes(document.documentElement.dataset.state)", 60)
        assert browser.js("document.documentElement.dataset.state") == "ready", browser.errors
        assert browser.js("document.getElementById('tabs').hidden") is False
        assert not any("/vendor/three/" in u for u in browser.requests)  # three.js only when the tab is opened
        _open_model(browser)
        assert browser.js("document.getElementById('map').hidden") is True
        assert browser.js("ppgSite.state.view3d.models[0].tiles.stats.failed") == 0

        # the model is drawn: the middle of the view is not its empty background
        time.sleep(0.5)
        shot = browser.screenshot()
        box = browser.js("(() => { const r = document.getElementById('view3d').getBoundingClientRect();"
                         " return [r.left, r.top, r.width, r.height]; })()")
        cx, cy = int(box[0] + box[2] / 2), int(box[1] + box[3] / 2)
        patch = shot[cy - 60:cy + 60, cx - 60:cx + 60].astype(float)
        assert np.abs(patch - np.array([0xe6, 0xe3, 0xdf], float)).mean() > 12 and patch.std() > 5, "no model"

        # a point: its coordinates in the side, its height the one of the DEM of the plan
        _click_on_model(browser, targets["4"])
        browser.wait("document.querySelector('#info table') !== null", 10)
        info = browser.js("document.getElementById('info').innerText")
        with rasterio.open(folder / "data/dem.tif") as src:
            dem = float(next(src.sample([tuple(targets["4"][:2])]))[0])
        assert _shown_height(info) == pytest.approx(dem, abs=3e-3)
        assert _shown_height(info) == pytest.approx(targets["4"][2], abs=3e-3)
        textured = browser.js("(() => { let n = 0; for (const t of ppgSite.state.view3d.models[0].tiles.visibleTiles)"
                              " t.engineData.scene.traverse((o) => { if (o.material && o.material.map) n++; });"
                              " return n; })()")
        assert textured > 0  # close up, the finest tiles: they carry the texture

        # a distance: two targets 3.2 m apart
        browser.js("document.getElementById('distance').click()")
        for name in ("0", "1"):
            _click_on_model(browser, targets[name])
        browser.js("document.getElementById('stop').click()")
        result = browser.js("ppgSite.state.view3d.result()")
        true = targets["1"] - targets["0"]
        assert result["kind"] == "distance" and result["points"] == 2
        assert result["length"] == pytest.approx(np.linalg.norm(true), abs=3e-3)
        assert result["plan"] == pytest.approx(np.linalg.norm(true[:2]), abs=3e-3)
        assert result["rise"] == pytest.approx(true[2], abs=3e-3)
        picked = browser.js("ppgSite.state.view3d.points.map((p) => [p.x, p.y, p.z])")
        assert np.abs(np.array(picked) - [targets["0"], targets["1"]]).max() < 3e-3
        out = browser.js("document.getElementById('measure-out').innerText")
        assert ("3D length" in out and "Plan length" in out) or ("Lunghezza 3D" in out and "Dislivello" in out), out

        # an area: the rectangle of the four corner targets, ended by a double click
        browser.js("document.getElementById('area').click()")
        corners = [targets[k] for k in ("0", "1", "2", "3")]
        for p in corners:
            _click_on_model(browser, p)
        _click_on_model(browser, corners[-1], close=False, count=2)
        assert browser.js("ppgSite.state.view3d.mode") is None and browser.js("ppgSite.state.view3d.closed")
        result = browser.js("ppgSite.state.view3d.result()")
        ring = np.array(corners)
        plan = 0.5 * abs(np.dot(ring[:, 0], np.roll(ring[:, 1], -1)) - np.dot(ring[:, 1], np.roll(ring[:, 0], -1)))
        assert result["kind"] == "area" and result["points"] == 4
        assert result["planArea"] == pytest.approx(plan, abs=0.01) and plan == pytest.approx(7.0464, abs=1e-3)
        assert plan <= result["area"] < plan * 1.001  # gently sloping: the 3D area is a little larger
        assert result["length"] == pytest.approx(2 * (3.2 + 2.202), abs=0.01)
        out = browser.js("document.getElementById('measure-out').innerText")
        assert "m²" in out and ("Plan area" in out or "Superficie in pianta" in out), out
        assert browser.js("ppgSite.state.view3d.scene.children.at(-1).children.length") == 2  # marks and line

        # Esc drops the measure in progress, Clear the one ended
        browser.js("document.getElementById('distance').click()")
        _click_on_model(browser, targets["5"])
        assert browser.js("ppgSite.state.view3d.points.length") == 1
        browser.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Escape", "code": "Escape",
                                                "windowsVirtualKeyCode": 27})
        assert browser.js("ppgSite.state.view3d.points.length") == 0 and browser.js("ppgSite.state.view3d.mode") is None
        assert browser.js("document.getElementById('measure-out').textContent") == ""
        browser.js("document.getElementById('area').click()")
        for name in ("4", "5"):
            _click_on_model(browser, targets[name])
        browser.js("document.getElementById('clear').click()")
        assert browser.js("ppgSite.state.view3d.points.length") == 0
        assert browser.js("ppgSite.state.view3d.scene.children.at(-1).children.length") == 0

        # the words of the tab in both languages, the plan back
        words = set()
        for _ in range(2):
            words.add(browser.js("document.getElementById('fit').textContent"))
            browser.js("document.getElementById('language').click()")
        assert words == {"Vista iniziale", "Home view"}
        browser.js("document.getElementById('fit').click()")
        browser.js("document.getElementById('tab-plan').click()")
        assert browser.js("document.getElementById('view3d').hidden") is True
        assert browser.js("ppgSite.state.view3d.active") is False

        assert not browser.errors, browser.errors
        outside = [u for u in browser.requests if not u.startswith((origin, "about:", "blob:", "data:"))]
        assert not outside, outside
    finally:
        browser.close()


def _shown_height(info: str) -> float:
    """The height in the text of the side panel (the line after the coordinates)."""
    line = next(x for x in info.splitlines() if x.startswith(("Quota (m)", "Height (m)")))
    return float(line.split("\t")[-1])


def _two_trenches(processed, folder: Path, models: bool = False):
    """A project of two chunks made of the processed trench: the second is the first moved 6 m east and lifted
    0.5 m, each with a stratigraphic unit, the second with a free drawing too; ``models``: each with the textured
    model of the trench, moved as its rasters."""
    import rasterio
    from shapely.geometry import box

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.core import free_drawings

    source = processed[1]
    doc = ppg.Document()
    doc.save(folder / "due_saggi.ppg")
    centres = []
    for n, (east, lift) in enumerate(((0.0, 0.0), (6.0, 0.5))):
        ch = doc.add_chunk(f"Saggio {'AB'[n]}")
        ch.crs = source.crs
        for product, path in (("orthomosaic", source.orthomosaic_path), ("dem", source.dem_path)):
            out = ch.own_product_path(product)
            out.parent.mkdir(parents=True, exist_ok=True)
            with rasterio.open(path) as src:
                profile = src.profile
                profile["transform"] = src.transform.translation(east, 0.0) @ src.transform
                with rasterio.open(out, "w", **profile) as dst:
                    for band in range(1, src.count + 1):
                        cells = src.read(band)
                        if product == "dem" and lift:
                            cells = np.where(cells == src.nodata, cells, cells + lift).astype(cells.dtype)
                        dst.write(cells, band)
                    if product == "orthomosaic" and src.count == 3:
                        dst.write_mask(src.dataset_mask())
                    b = dst.bounds
            ch._set_product(product, out)
        if models:
            for name in ("model", "model_textured"):
                out = ch.data_dir / source.product_path(name).name
                shutil.copy2(source.product_path(name), out)
                ch._set_product(name, out)
            move = np.eye(4)
            move[:3, 3] = (east, 0.0, lift)
            ch.transform = move @ source.transform
        cx, cy = (b.left + b.right) / 2, (b.bottom + b.top) / 2
        unit = UnitPolygon(box(cx - 0.4, cy - 0.3, cx + 0.4, cy + 0.3), us=str(n + 1), sito="Sito di prova")
        ch.meta["drawings"] = {"units": [unit.to_dict()]}
        if n:
            free_drawings.add(ch, "line", [[cx - 0.6, cy - 0.5, 0.0], [cx + 0.6, cy - 0.5, 0.0]], layer="Limiti",
                              name="limite")
        centres.append((cx, cy))
    doc.save()
    return doc, centres


def test_models_of_two_chunks(processed, tmp_path):
    """Two chunks with a model each: two model layers in one frame, both in the 3D tab under the name of their
    chunk; a click on each gives its own point, the second 6 m east and 0.5 m higher; a model hidden is not picked."""
    executable = find_browser()
    doc, _centres = _two_trenches(processed, tmp_path / "progetto", models=True)
    path = doc.export_site(tmp_path / "site", title="Due saggi", products=("dem", "model"), model_texture=1024)
    folder = path.parent
    manifest = json.loads(path.read_text(encoding="utf-8"))
    a, b = (next(x for x in c["layers"] if x["id"] == "model") for c in manifest["chunks"])
    assert [a["url"], b["url"]] == ["data/1/model/tileset.json", "data/2/model/tileset.json"]
    assert np.subtract(b["box"], a["box"]) == pytest.approx([6, 0, 0.5, 6, 0, 0.5], abs=2e-3)
    if executable is None:
        pytest.skip("no Chromium found (PPG_BROWSER names one)")
    shift = _frame_shift(doc.chunks[0], folder, "data/1/dem.tif")
    target = _targets(processed)["4"] - shift
    browser = Browser(executable, tmp_path / "profilo", size=(1200, 800))
    try:
        with _serving(folder) as port:
            origin = f"http://127.0.0.1:{port}"
            browser.call("Page.navigate", {"url": origin + "/"})
            browser.wait("['ready', 'error'].includes(document.documentElement.dataset.state)", 60)
            _open_model(browser)
            rows = browser.js("[...document.getElementById('layers').children].map((n) => n.innerText)")
            names = [r.split("\t")[0].split("\n")[0] for r in rows]
            assert names in (["Saggio A", "3D model", "Saggio B", "3D model"],
                             ["Saggio A", "Modello 3D", "Saggio B", "Modello 3D"]), rows
            assert browser.js("ppgSite.state.view3d.models.map((m) => m.tiles.stats.loaded > 0)") == [True, True]
            for n, move in enumerate(([0, 0, 0], [6, 0, 0.5])):
                _click_on_model(browser, target + move)
                browser.wait("document.querySelector('#info table') !== null", 10)
                point = browser.js("ppgSite.state.last")
                assert [point["x"], point["y"], point["height"]] == pytest.approx(list(target + move), abs=3e-3)
                assert point["model"] == f"Saggio {'AB'[n]}"
            # the second model hidden: the same pixel finds nothing of it
            browser.js("document.querySelectorAll('#layers input')[1].click()")
            x, y, z = (float(v) for v in target + [6, 0, 0.5])
            px, py = browser.js(f"ppgSite.state.view3d.project({x!r}, {y!r}, {z!r})")
            hit = browser.js(f"ppgSite.state.view3d.pick({px!r}, {py!r})")
            assert hit is None or hit["chunk"]["label"] == "Saggio A"
            assert not browser.errors, browser.errors
            outside = [u for u in browser.requests if not u.startswith((origin, "about:", "blob:", "data:"))]
            assert not outside, outside
    finally:
        browser.close()


def test_page_of_two_chunks(processed, tmp_path):
    """Two chunks in one page: a heading and the layers of each, both orthophotos on the map, and a click that
    reads the DEM and the unit of the chunk it falls on."""
    import rasterio

    executable = find_browser()
    if executable is None:
        pytest.skip("no Chromium found (PPG_BROWSER names one)")
    doc, centres = _two_trenches(processed, tmp_path / "progetto")
    path = doc.export_site(tmp_path / "site", title="Due saggi")
    folder = path.parent
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert [c["label"] for c in manifest["chunks"]] == ["Saggio A", "Saggio B"]
    assert [[x["id"] for x in c["layers"]] for c in manifest["chunks"]] == [
        ["ortho", "hillshade", "dem", "units"], ["ortho", "hillshade", "dem", "units", "drawings"]]
    a, b = (c["extent"] for c in manifest["chunks"])
    assert b[0] - a[0] == pytest.approx(6.0, abs=1e-3) and a[0] < 0 < b[2]  # one frame for both
    assert _layer({"chunks": manifest["chunks"][1:]}, "dem")["range"][0] == pytest.approx(
        _layer(manifest, "dem")["range"][0] + 0.5, abs=1e-3)

    browser = Browser(executable, tmp_path / "profilo")
    try:
        with _serving(folder) as port:
            origin = f"http://127.0.0.1:{port}"
            browser.call("Page.navigate", {"url": origin + "/"})
            browser.wait("['ready', 'error'].includes(document.documentElement.dataset.state)", 60)
            assert browser.js("document.documentElement.dataset.state") == "ready", browser.errors

            # the list: a heading for each chunk, then its layers; the drawings of the second with their group
            rows = browser.js("[...document.getElementById('layers').children].map((n) => n.tagName === 'H3'"
                              " ? '# ' + n.textContent : (n.classList.contains('group') ? 'group' : 'layer'))")
            assert rows == ["# Saggio A", "layer", "layer", "layer",
                            "# Saggio B", "layer", "layer", "layer", "layer", "group"]
            shown = browser.js("ppgSite.state.entries.map((e) => [e.chunk.label, e.def.id, e.layer.getVisible()])")
            assert shown == [["Saggio A", "ortho", True], ["Saggio A", "hillshade", False], ["Saggio A", "units", True],
                             ["Saggio B", "ortho", True], ["Saggio B", "hillshade", False], ["Saggio B", "units", True],
                             ["Saggio B", "drawings", True]]
            assert browser.js("ppgSite.state.dems.length") == 2
            assert browser.js("document.getElementById('tabs').hidden") is True  # no model: no 3D tab

            # both orthophotos are on the map: neither centre shows the empty background
            browser.wait("document.querySelector('#map canvas') !== null")
            time.sleep(0.5)
            shot = browser.screenshot()
            box = browser.js("(() => { const r = document.getElementById('map').getBoundingClientRect();"
                             " return [r.left, r.top, r.width, r.height]; })()")
            background = np.array([0xe6, 0xe3, 0xdf], float)
            spots = []
            for n in (1, 2):
                unit = json.loads((folder / f"data/{n}/units.geojson").read_text(encoding="utf-8"))["features"][0]
                ring = np.array(unit["geometry"]["coordinates"][0])[:-1]
                ux, uy = float(ring[:, 0].mean()) + 0.25, float(ring[:, 1].mean()) + 0.2
                px = browser.js(f"ppgSite.map.getPixelFromCoordinate([{ux!r}, {uy!r}])")
                spots.append((ux, uy, box[0] + px[0], box[1] + px[1]))
                # beside the unit, on the orthophoto alone
                ox, oy = browser.js(f"ppgSite.map.getPixelFromCoordinate([{ux + 0.5!r}, {uy + 0.4!r}])")
                ox, oy = int(box[0] + ox), int(box[1] + oy)
                patch = shot[oy - 12:oy + 12, ox - 12:ox + 12].astype(float)
                assert patch.size and np.abs(patch - background).mean() > 12, f"chunk {n}: the map is empty"

            # a click on each chunk: its own unit and the height of its own DEM
            heights = []
            for n, (ux, uy, px, py) in enumerate(spots, 1):
                with rasterio.open(folder / f"data/{n}/dem.tif") as src:
                    expected = float(next(src.sample([(ux, uy)]))[0])
                heights.append(expected)
                assert browser.js(f"ppgSite.heightAt({ux!r}, {uy!r})") == pytest.approx(expected, abs=1e-5)
                browser.click(px, py)
                browser.wait(f"document.getElementById('info').innerText.includes('US {n}')", 10)
                info = browser.js("document.getElementById('info').innerText")
                assert f"US {n}" in info and f"US {3 - n}" not in info, info
                assert _shown_height(info) == pytest.approx(expected, abs=2e-3)  # the pixel clicked, not the point
            assert heights[1] == pytest.approx(heights[0] + 0.5, abs=1e-4)  # the second trench is the lifted one
            assert 0 <= heights[0] < 5  # heights of the frame, not of the reference system

            assert not browser.errors, browser.errors
            outside = [u for u in browser.requests if not u.startswith((origin, "about:", "blob:", "data:"))]
            assert not outside, outside
    finally:
        browser.close()
