"""The web site of a survey beyond the plan and the model (io/site.py, io/site_cloud.py, io/site_sections.py,
io/site_plugins.py): the point cloud, the sections and elevations, several dates in one page, the size budget and
what plugins add. Synthetic data only: the trench of testing.synthetic."""
from __future__ import annotations

import json
import re
import shutil
import struct
import tempfile
from pathlib import Path

import numpy as np
import pytest

from test_site import SECRET, Browser, _glb, _leaves, _serving, _with_drawings, find_browser

GPS_PICTURE_TEXT = "Fotocamera di prova"


# ------------------------------------------------------------------------------------------------ the project
def _section(ch, origin):
    """A section across the synthetic trench at x = 2 m, seen from the west, with a spot height and a unit drawn
    on it, as the window stores them (``(u, v)`` of the section)."""
    from shapely.geometry import box

    from pyarchinit_photogrammetry.engine import elevation as E

    a, b = origin[:2] + [2.0, 0.4], origin[:2] + [2.0, 2.6]
    frame = E.plane_from_line(ch, a, b, side=origin[:2] + [0.5, 1.5])
    entry = E.build_elevation(ch, frame, resolution=0.01, depth=(-1.0, 0.5), max_angle=89.0, kind="section",
                              name="Sezione A-A")
    u0, v0, u1, v1 = E.bounds_of(ch, entry)
    um, vm = (u0 + u1) / 2, (v0 + v1) / 2
    frames = ch.meta.setdefault("drawings", {}).setdefault("frames", {})
    frames[entry["id"]] = {
        "units": [{"wkt": box(um - 0.2, vm - 0.1, um + 0.2, vm + 0.1).wkt, "us": "7", "tipo": "US",
                   "note": str(Path.home() / SECRET)}],
        "quote": [{"u": um, "v": vm, "quota_q": vm, "us_q": "7"}]}
    return entry


def _picture_with_gps(path: Path) -> Path:
    """A JPEG with EXIF: a camera name and a GPS position."""
    from PIL import Image

    im = Image.new("RGB", (64, 48), (120, 80, 40))
    exif = Image.Exif()
    exif[0x0110] = GPS_PICTURE_TEXT  # Model
    exif[0x8825] = {1: "N", 2: (43.0, 30.0, 0.0), 3: "E", 4: (11.0, 15.0, 0.0)}  # GPSInfo
    im.save(path, "JPEG", exif=exif)
    return path


class Finds:
    """A tiny plugin of the tests: a layer of finds with a legend, a catalogue with a picture, a page of text
    with things a page must never run."""

    name = "finds-test"

    def __init__(self, picture: Path, centre):
        self.picture, self.centre = picture, centre
        self.calls = []

    def contributions(self, chunk):
        self.calls.append(chunk.label)
        cx, cy, z = self.centre
        return [
            {"kind": "layer", "id": "finds", "title": {"it": "Reperti", "en": "Finds"},
             "features": {"type": "FeatureCollection", "features": [
                 {"type": "Feature", "geometry": {"type": "Point", "coordinates": [cx + 0.1, cy, z]},
                  "properties": {"n": 1, "classe": "anfora", "_private": "x", "path": str(Path.home() / SECRET)}},
                 {"type": "Feature", "geometry": {"type": "Point", "coordinates": [cx - 0.1, cy, z]},
                  "properties": {"n": 2, "classe": "moneta"}}]},
             "colour": "#1f77b4",
             "legend": {"field": "classe", "items": [{"value": "anfora", "title": {"it": "Anfore", "en": "Amphorae"},
                                                      "colour": "#d62728"},
                                                     {"value": "moneta", "title": "Monete", "colour": "#2ca02c"}]},
             "fields": {"classe": {"it": "Classe", "en": "Class"}}},
            {"kind": "table", "id": "catalogo", "title": "Catalogo",
             "columns": [{"key": "n", "title": "N."}, {"key": "descrizione", "title": {"it": "Descrizione",
                                                                                      "en": "Description"}}],
             "rows": [{"n": 1, "descrizione": "Anfora", "image": str(self.picture)},
                      {"n": 2, "descrizione": "Moneta"}]},
            {"kind": "html", "id": "note", "title": "Note",
             "html": '<h3>Note di scavo</h3><p onclick="alert(1)">Testo <a href="javascript:alert(1)">x</a> '
                     '<a href="https://example.org/">y</a></p><script>alert(2)</script><style>p{}</style>'
                     '<img src="pianta.jpg" onerror="alert(3)"><img src="https://example.org/t.png">'
                     '<iframe src="https://example.org"></iframe>',
             "images": {"pianta.jpg": str(self.picture)}},
            {"kind": "map", "id": "boh"},  # not understood: left out, said in the notes
        ]


def _broken(chunk):
    raise RuntimeError("the plugin fell")


class OnElevation:
    """A plugin of the second form of the hook: told the frame of the site, it puts a layer on the section in its
    ``(u, v)``, gives values in both languages and a height through the frame; with two mistakes the site refuses."""

    name = "frame-test"

    def __init__(self, z):
        self.z = z  # a true height of the trench
        self.sites = []

    def contributions(self, chunk, site):
        from shapely.geometry import box, mapping

        from pyarchinit_photogrammetry.engine import elevation as E

        self.sites.append(site)
        entry = E.elevations(chunk)[0]
        u0, v0, u1, v1 = E.bounds_of(chunk, entry)
        um, vm = (u0 + u1) / 2, (v0 + v1) / 2
        decay = box(um + 0.4, vm - 0.1, um + 0.7, vm + 0.1)
        term = {"it": "Esfoliazione", "en": "Exfoliation"}
        return [
            {"kind": "layer", "id": "decay", "elevation": entry["id"], "title": {"it": "Degrado", "en": "Decay"},
             "features": [{"type": "Feature", "geometry": mapping(decay),
                           "properties": {"term": term, "label": {"it": "esf."}, "top": site.uv(entry["id"], um, vm
                                                                                                 + 0.1)[1]}}],
             "legend": {"field": "term", "items": [{"value": "Esfoliazione", "colour": "#aa00ff",
                                                    "title": term}]},
             "fields": {"term": {"it": "Termine", "en": "Term"}, "top": {"it": "Sommità", "en": "Top"}}},
            {"kind": "layer", "id": "decay", "elevation": "not-there", "features": [
                {"type": "Feature", "geometry": mapping(decay), "properties": {}}]},
            {"kind": "layer", "id": "world", "elevation": entry["id"], "features": [  # plan coordinates: refused
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [2480002.0, 4500001.0]},
                 "properties": {}}]},
            {"kind": "table", "id": "terms", "title": {"it": "Termini", "en": "Terms"},
             "columns": [{"key": "term", "title": {"it": "Termine", "en": "Term"}},
                         {"key": "quota", "title": {"it": "Quota", "en": "Height"}},
                         {"key": "only", "title": "Solo"}],
             "rows": [{"term": term, "quota": site.height(self.z), "only": {"en": "English only"},
                       "empty": {"it": ""}}]},
            {"kind": "html", "id": "guida", "title": "Guida",
             "html": {"it": "<p>Pagina italiana</p>", "en": "<p>English page</p><script>x</script>"}},
        ]


@pytest.fixture(scope="module")
def full_site(processed, tmp_path_factory):
    """``(doc, chunk, facts, folder, manifest, plugin)``: the trench with drawings, a section, a plugin, its site in
    the local frame with the point cloud."""
    from pyarchinit_photogrammetry.io import site_plugins

    base = tmp_path_factory.mktemp("sito_pieno")
    doc, ch, facts = _with_drawings(processed, base / "progetto")
    shutil.copytree(processed[0].path.parent / "photos", base / "progetto" / "photos")  # the section reads them
    origin = np.array([2480000.0, 4500000.0, 100.0])
    _section(ch, origin)
    doc.save()
    plugin = Finds(_picture_with_gps(base / "foto.jpg"), (facts["cx"], facts["cy"], facts["z"]))
    framed = OnElevation(facts["z"])
    entries = [site_plugins.register(plugin.contributions, owner=plugin.name),
               site_plugins.register(_broken, owner="rotto"),
               site_plugins.register(framed.contributions, owner=framed.name, with_site=True)]
    try:
        path = doc.export_site(base / "site", title="Saggio completo", model_faces=8000,
                               products=["orthomosaic", "dem", "units", "drawings", "model", "cloud", "sections"],
                               cloud_points=200_000)
    finally:
        for e in entries:
            site_plugins.unregister(e)
    plugin.framed = framed
    return doc, ch, facts, path.parent, json.loads(path.read_text(encoding="utf-8")), plugin


def _layer(chunk, name):
    return next(x for x in chunk["layers"] if x["id"] == name)


def _shift(ch, site: Path) -> np.ndarray:
    import math

    import rasterio

    with rasterio.open(ch.dem_path) as a, rasterio.open(site / "data/dem.tif") as b:
        return np.array([a.bounds.left - b.bounds.left, a.bounds.top - b.bounds.top,
                         math.floor(float(a.read(1, masked=True).min()))])


# ------------------------------------------------------------------------------------------------ the cloud
def test_cloud_as_tiles_of_points_and_copc(full_site):
    import laspy

    doc, ch, _facts, site, manifest, _ = full_site
    chunk = manifest["chunks"][0]
    assert [x["id"] for x in chunk["layers"]] == ["ortho", "hillshade", "dem", "model", "cloud", "sections",
                                                  "units", "drawings", "plugin:finds-test:finds"]
    layer = _layer(chunk, "cloud")
    assert layer["type"] == "points" and layer["visible"] is False  # the model shown first
    assert layer["download"] == "data/cloud.copc.laz" and "origin" not in layer
    tileset = json.loads((site / layer["url"]).read_text(encoding="utf-8"))
    assert "transform" not in tileset["root"] and "extras" not in tileset
    assert tileset["root"]["refine"] == "ADD" and tileset["root"].get("children")
    assert all(n["geometricError"] == 0 for n in _leaves(tileset["root"]))
    world = ch.point_cloud.transformed(ch.transform)
    shift = _shift(ch, site)
    pts = []
    for f in (site / "data/cloud/tiles").glob("*.glb"):
        gltf, blob = _glb(f)
        prim = gltf["meshes"][0]["primitives"][0]
        assert prim["mode"] == 0 and gltf["accessors"][1]["normalized"]
        acc = gltf["accessors"][prim["attributes"]["POSITION"]]
        v = np.frombuffer(blob, np.float32, acc["count"] * 3, 0).reshape(-1, 3)
        pts.append(np.column_stack([v[:, 0], -v[:, 2], v[:, 1]]))
    pts = np.vstack(pts)
    assert len(pts) == layer["points"] == min(len(world), 200_000)
    assert np.abs(pts).max() < 100  # around the origin of the site
    # every point of the tiles is a point of the cloud less the shift of the site (within float32)
    from scipy.spatial import cKDTree

    d, _ = cKDTree(world.xyz - shift).query(pts[:: max(1, len(pts) // 2000)].astype(np.float64))
    assert d.max() < 1e-4
    with laspy.open(site / layer["download"]) as f:
        las = f.read()
        assert len(las.points) == layer["points"] and f.header.parse_crs() is None
        assert np.abs(np.asarray(las.x)).max() < 100
    box = layer["box"]
    assert box[0] == pytest.approx(pts[:, 0].min(), abs=0.01) and box[5] == pytest.approx(pts[:, 2].max(), abs=0.01)


def test_cloud_thinned_and_in_the_crs_frame(processed, tmp_path):
    from pyarchinit_photogrammetry.io import site_cloud

    doc, ch, _ = processed
    path = doc.export_site(tmp_path / "s", products=["cloud"], cloud_points=50_000, cloud_copc=False, frame="crs")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    layer = _layer(manifest["chunks"][0], "cloud")
    assert layer["points"] == 50_000 and "download" not in layer and layer["visible"] is True
    assert site_cloud.tiles_points(tmp_path / "s/data/cloud") == 50_000
    tileset = json.loads((tmp_path / "s/data/cloud/tileset.json").read_text(encoding="utf-8"))
    assert len(tileset["root"]["transform"]) == 16 and tileset["extras"]["crs"] == ch.crs
    assert layer["origin"][0] > 2e6  # the true coordinates, as asked
    reduced = manifest["build"]["reduced"]
    assert reduced == [{"chunk": ch.label, "product": "cloud", "what": "points", "from": len(ch.point_cloud),
                        "to": 50_000, "why": "browser"}]


# ------------------------------------------------------------------------------------------------ the sections
def test_sections_with_their_orthophoto_drawing_and_trace(full_site):
    import rasterio

    from pyarchinit_photogrammetry.engine import elevation as E

    _doc, ch, _facts, site, manifest, _ = full_site
    chunk = manifest["chunks"][0]
    (sec,) = chunk["sections"]
    entry = E.elevations(ch)[0]
    shift = _shift(ch, site)
    assert sec["id"] == "1" and sec["name"] == "Sezione A-A" and sec["kind"] == "section" and sec["heights"]
    u0, v0, u1, v1 = E.bounds_of(ch, entry)
    assert sec["extent"][1] == pytest.approx(v0 - shift[2], abs=0.02)
    assert 0 <= sec["extent"][1] < 5  # local heights: never the true ones
    with rasterio.open(site / sec["ortho"]["url"]) as cog, rasterio.open(E.elevation_paths(ch, entry)[0]) as src:
        assert cog.crs is None and not cog.tags() and cog.profile["compress"] == "webp" and cog.count == 4
        assert cog.bounds.top == pytest.approx(src.bounds.top - shift[2], abs=1e-6)
        assert cog.bounds.left == pytest.approx(src.bounds.left, abs=1e-6)
    drawing = json.loads((site / sec["drawing"]["url"]).read_text(encoding="utf-8"))
    layers = {f["properties"]["layer"] for f in drawing["features"]}
    assert {"profile", "US", "quote"} <= layers
    quote = next(f for f in drawing["features"] if f["properties"]["layer"] == "quote")
    # the spot height of the section in the heights of the site, and where it is in the section
    assert quote["properties"]["quota"] == pytest.approx(quote["geometry"]["coordinates"][1], abs=1e-3)
    unit = next(f for f in drawing["features"] if f["properties"]["layer"] == "US")
    assert unit["properties"]["note"] == SECRET and unit["properties"]["label"]
    profile = [f for f in drawing["features"] if f["properties"]["layer"] == "profile"]
    lines = E.read_profile(ch, entry)
    assert len(profile) == len([x for x in lines if len(x) > 1])
    first = np.asarray(profile[0]["geometry"]["coordinates"])
    assert np.allclose(first[:, 1], lines[0][: len(first), 1] - shift[2], atol=1e-3)
    svg = (site / sec["svg"]).read_text(encoding="utf-8")
    assert svg.startswith("<svg") and "Sezione A-A" in svg and "<script" not in svg
    # the trace on the plan: the line x = 2 m of the trench, in the frame of the plan
    traces = json.loads((site / "data/sections.geojson").read_text(encoding="utf-8"))
    (f,) = traces["features"]
    assert f["properties"] == {"label": "Sezione A-A", "section": "1", "tipo": "section"}
    xy = np.asarray(f["geometry"]["coordinates"]) + shift[:2]
    assert np.allclose(xy[:, 0], 2480002.0, atol=1e-6) and sec["trace"] == f["geometry"]
    assert min(xy[:, 1]) > 4499999.0 and max(xy[:, 1]) < 4500004.0


def test_sections_of_a_site_in_the_crs_frame_keep_their_heights(full_site, tmp_path):
    from pyarchinit_photogrammetry.engine import elevation as E

    doc, ch, *_ = full_site
    manifest = json.loads(doc.export_site(tmp_path / "s", products=["sections"], frame="crs")
                          .read_text(encoding="utf-8"))
    (sec,) = manifest["chunks"][0]["sections"]
    assert sec["extent"][1] == pytest.approx(E.bounds_of(ch, E.elevations(ch)[0])[1], abs=0.02)
    assert [x["id"] for x in manifest["chunks"][0]["layers"]] == ["sections"]


# ------------------------------------------------------------------------------------------------ the plugins
def test_plugin_contributions_on_the_site(full_site):
    from PIL import Image

    _doc, ch, facts, site, manifest, plugin = full_site
    chunk = manifest["chunks"][0]
    assert plugin.calls == [ch.label]
    listed = {p["id"]: p for p in chunk["plugins"] if p["plugin"] == "finds-test"}
    assert set(listed) == {"finds", "catalogo", "note"}  # a provider of the first form, as before
    layer = _layer(chunk, "plugin:finds-test:finds")
    assert layer["url"] == "data/plugins/finds-test/finds.geojson" and layer["group_by"] == "classe"
    assert [(g["name"], g["count"]) for g in layer["groups"]] == [("anfora", 1), ("moneta", 1)]
    assert layer["fields"]["classe"] == {"it": "Classe", "en": "Class"}
    feats = json.loads((site / layer["url"]).read_text(encoding="utf-8"))["features"]
    shift = _shift(ch, site)
    x, y, z = feats[0]["geometry"]["coordinates"]
    assert (x + shift[0], y + shift[1], z + shift[2]) == pytest.approx((facts["cx"] + 0.1, facts["cy"], facts["z"]),
                                                                       abs=1e-3)
    assert "_private" not in feats[0]["properties"] and feats[0]["properties"]["path"] == SECRET
    table = json.loads((site / listed["catalogo"]["url"]).read_text(encoding="utf-8"))
    assert [c["key"] for c in table["columns"]] == ["n", "descrizione"]
    picture = site / table["rows"][0]["_image"]
    assert "_image" not in table["rows"][1]
    with Image.open(picture) as im:
        assert im.format == "JPEG" and not im.getexif()
    data = picture.read_bytes()
    assert GPS_PICTURE_TEXT.encode() not in data and b"Exif" not in data
    page = (site / listed["note"]["url"]).read_text(encoding="utf-8")
    assert "<h3>Note di scavo</h3>" in page and 'href="https://example.org/"' in page
    for bad in ("script", "alert", "onclick", "onerror", "javascript", "iframe", "style", "t.png"):
        assert bad not in page, bad
    assert re.search(r'<img src="note-1\.jpg">', page) and (site / "data/plugins/finds-test/note-1.jpg").exists()
    notes = manifest["build"]["notes"]
    assert any("rotto" in n and "the plugin fell" in n for n in notes)
    assert any("finds-test" in n and "'map'" in n for n in notes)


def test_plugin_told_the_frame_puts_a_layer_on_the_section(full_site):
    from pyarchinit_photogrammetry.engine import elevation as E

    _doc, ch, facts, site, manifest, plugin = full_site
    chunk = manifest["chunks"][0]
    shift = _shift(ch, site)
    (told,) = plugin.framed.sites
    entry = E.elevations(ch)[0]
    assert told.frame == "local" and told.local and told.crs is None
    assert told.shift == pytest.approx(tuple(shift))
    assert told.elevations == [{"elevation": entry["id"], "section": "1", "name": "Sezione A-A", "kind": "section",
                                "heights": True}]
    with pytest.raises(AttributeError):
        told.shift = (0, 0, 0)  # read only
    assert told.height(facts["z"]) == pytest.approx(facts["z"] - shift[2], abs=1e-4)
    assert told.xy(facts["cx"], facts["cy"]) == pytest.approx([facts["cx"] - shift[0], facts["cy"] - shift[1]],
                                                               abs=1e-4)
    with pytest.raises(KeyError):
        told.uv("not-there", 0, 0)
    # the layer on the section, in its (u, v) with v taken down as the heights of the section
    (sec,) = chunk["sections"]
    (layer,) = sec["layers"]
    assert layer["id"] == "plugin:frame-test:decay" and layer["group_by"] == "term"
    assert layer["groups"] == [{"name": "Esfoliazione", "title": {"it": "Esfoliazione", "en": "Exfoliation"},
                                "colour": "#aa00ff", "count": 1}]
    assert not any(x["id"].startswith("plugin:frame-test") for x in chunk["layers"])  # not on the plan
    (f,) = json.loads((site / layer["url"]).read_text(encoding="utf-8"))["features"]
    u0, v0, u1, v1 = E.bounds_of(ch, entry)
    um, vm = (u0 + u1) / 2, (v0 + v1) / 2
    ring = np.asarray(f["geometry"]["coordinates"][0])
    assert ring[:, 0].min() == pytest.approx(um + 0.4, abs=1e-4)
    assert ring[:, 1].max() == pytest.approx(vm + 0.1 - shift[2], abs=1e-4)
    assert f["properties"]["top"] == pytest.approx(ring[:, 1].max(), abs=1e-4)  # uv(): the same frame
    assert sec["extent"][1] <= ring[:, 1].min() and ring[:, 1].max() <= sec["extent"][3]
    assert f["properties"]["term"] == {"it": "Esfoliazione", "en": "Exfoliation"}
    listed = {p["id"]: p for p in chunk["plugins"] if p["plugin"] == "frame-test"}
    assert listed["decay"]["section"] == "1"
    notes = manifest["build"]["notes"]
    assert any("frame-test" in n and "not-there" in n for n in notes)
    assert any("frame-test" in n and "off the elevation" in n for n in notes)
    assert not (site / "data/plugins/frame-test/world.s1.geojson").exists()
    # values in both languages, one alone kept alone, an empty one dropped; a height in the frame of the site
    table = json.loads((site / listed["terms"]["url"]).read_text(encoding="utf-8"))
    (row,) = table["rows"]
    assert row == {"term": {"it": "Esfoliazione", "en": "Exfoliation"}, "quota": round(facts["z"] - shift[2], 4),
                   "only": {"en": "English only"}}
    # a page in each language, each cleaned
    urls = listed["guida"]["url"]
    assert set(urls) == {"it", "en"}
    assert (site / urls["it"]).read_text(encoding="utf-8") == "<p>Pagina italiana</p>"
    assert (site / urls["en"]).read_text(encoding="utf-8") == "<p>English page</p>"


def test_site_frame_in_the_crs_frame():
    from pyarchinit_photogrammetry.io.site import _Shift
    from pyarchinit_photogrammetry.io.site_plugins import Site

    site = Site(_Shift(), "crs", "EPSG:32633", elevations=[{"elevation": "e1", "section": "1", "name": "A",
                                                           "kind": "elevation", "heights": True}])
    assert site.frame == "crs" and not site.local and site.crs == "EPSG:32633" and site.shift == (0.0, 0.0, 0.0)
    assert site.position([500000.12341, 4500000.0, 12.5]) == [500000.1234, 4500000.0, 12.5]
    assert site.uv("e1", 1.0, 12.5) == [1.0, 12.5] and site.height("x") is None
    local = Site(_Shift(10, 20, 5), "local", "EPSG:32633", elevations=[{"elevation": "e1", "section": "1",
                                                                       "name": "A", "kind": "elevation",
                                                                       "heights": True}])
    assert local.crs is None  # a local site never names its system, nor does its frame
    assert local.uv("e1", 1.0, 12.5) == [1.0, 7.5] and local.position([11, 22, 6]) == [1.0, 2.0, 1.0]
    local.elevations[0]["heights"] = False  # a copy
    assert local.uv("e1", 1.0, 12.5) == [1.0, 7.5]


def test_html_of_a_plugin_is_cleaned():
    from pyarchinit_photogrammetry.io.site_plugins import clean_html

    assert clean_html("<p>a<b>b</p>c") == "<p>a<b>b</b></p>c"
    assert clean_html('<a href="mailto:x@y.z">m</a>') == \
        '<a href="mailto:x@y.z" rel="noopener noreferrer nofollow" target="_blank">m</a>'
    assert clean_html("<svg><script>x</script></svg><p>ok</p>") == "<p>ok</p>"
    assert clean_html('<td colspan="2" style="x">1</td><img src="a.jpg" width="9x">', ["a.jpg"]) == \
        '<td colspan="2">1</td><img src="a.jpg">'
    assert clean_html("&lt;script&gt;") == "&lt;script&gt;"


def test_plugin_hook_through_the_context(tmp_path):
    from pyarchinit_photogrammetry.io import site_plugins
    from pyarchinit_photogrammetry.plugins.api import Context

    before = len(site_plugins.providers())
    context = Context("prova-sito", tmp_path)
    context.add_site_contributions(lambda chunk: [])
    assert site_plugins.providers()[-1][0] == "prova-sito" and len(site_plugins.providers()) == before + 1
    context.add_site_contributions(lambda chunk, site: [], with_site=True)
    assert Context.site_hook == site_plugins.HOOK == 2
    context.release()
    assert len(site_plugins.providers()) == before


def test_a_provider_of_the_first_form_is_called_with_the_chunk_alone(tmp_path):
    """Backward compatible: a provider written for the first form of the hook gets one argument and works as
    before, beside one of the second form."""
    from types import SimpleNamespace

    from pyarchinit_photogrammetry.io import site_plugins
    from pyarchinit_photogrammetry.io.site import _Shift

    calls = []

    def first(chunk):
        calls.append(("first", chunk.label))
        return {"kind": "table", "id": "t", "title": "T", "rows": [{"a": 1}]}

    def second(chunk, site):
        calls.append(("second", chunk.label, site.frame))
        return [{"kind": "table", "id": "t", "rows": [{"a": {"it": "uno", "en": "one"}}]}]

    entries = [site_plugins.register(first, owner="primo"), site_plugins.register(second, "secondo", with_site=True)]
    try:
        notes = []
        layers, listed, _ = site_plugins.write(SimpleNamespace(label="c"), tmp_path, "data", _Shift(), dict,
                                               str, notes)
    finally:
        for e in entries:
            site_plugins.unregister(e)
    assert calls == [("first", "c"), ("second", "c", "local")] and not notes and not layers
    assert [(x["plugin"], x["id"]) for x in listed] == [("primo", "t"), ("secondo", "t")]
    rows = json.loads((tmp_path / "plugins/secondo/t.json").read_text(encoding="utf-8"))["rows"]
    assert rows == [{"a": {"it": "uno", "en": "one"}}]


# ------------------------------------------------------------------------------------------------ what goes out
def test_new_files_hold_no_place_and_no_path(full_site, tmp_path):
    import rasterio

    doc, ch, _facts, site, manifest, _ = full_site
    private = [str(Path.home()), str(Path(doc.path).parent), tempfile.gettempdir(),
               str(Path(tempfile.gettempdir()).resolve()), str(tmp_path.parent)]
    for f in site.rglob("*"):
        if not f.is_file():
            continue
        data = f.read_bytes()
        for p in private:
            assert p.encode() not in data, f"{f.relative_to(site)} names {p}"
        if f.suffix in (".json", ".geojson", ".svg", ".html"):
            text = data.decode("utf-8")
            for word in ("EPSG", "Monte Mario", "PROJCS"):
                assert word not in text, f"{f.relative_to(site)} holds {word}"
            # the coordinates of the place, not the digits of a long decimal that happen to be the same
            assert not re.search(r"(?<![\d.])(2480|4500)\d{3}", text), f.relative_to(site)
            if f.suffix != ".html":  # numbers: small, around the origin (the page of the viewer has none of ours)
                assert all(abs(float(n)) < 1000 for n in re.findall(r"-?\d+\.\d+(?:[eE][-+]?\d+)?", text)
                           if not f.name.endswith(".svg")), f.name
    for f in (site / "data/cloud/tiles").glob("*.glb"):
        header = _glb(f)[0]
        assert max(abs(v) for a in header["accessors"] for v in a.get("max", []) + a.get("min", [])) < 100
        assert "extras" not in json.dumps(header)
    with rasterio.open(site / manifest["chunks"][0]["sections"][0]["ortho"]["url"]) as src:
        assert src.crs is None and not src.tags()
    data = (site / "data/cloud.copc.laz").read_bytes()
    for word in (b"EPSG", b"PROJCS", b"Monte Mario", b"GEOGCS"):
        assert word not in data


# ------------------------------------------------------------------------------------------------ the budget
def test_budget_thins_the_model_and_the_cloud(processed, tmp_path):
    import laspy  # noqa: F401 - the COPC writer

    doc, ch, _ = processed
    whole = json.loads(doc.export_site(tmp_path / "intero", products=["model", "cloud"], budget_mb=None,
                                       cloud_points=None).read_text(encoding="utf-8"))
    big = whole["chunks"][0]
    assert whole["build"]["reduced"] == [] and whole["build"]["budget"] is None
    model_bytes = _layer(big, "model")["bytes"]
    cloud_bytes = _layer(big, "cloud")["bytes"]
    budget = (whole["build"]["size"] - (model_bytes + cloud_bytes) / 2) / 1e6  # half of what they take
    path = doc.export_site(tmp_path / "ridotto", products=["model", "cloud"], budget_mb=budget, cloud_points=None)
    small = json.loads(path.read_text(encoding="utf-8"))
    reduced = {(r["product"], r["what"]): r for r in small["build"]["reduced"]}
    assert ("cloud", "points") in reduced and reduced[("cloud", "points")]["why"] == "budget"
    assert ("model", "faces") in reduced or ("model", "texture") in reduced
    chunk = small["chunks"][0]
    assert _layer(chunk, "cloud")["points"] < _layer(big, "cloud")["points"]
    assert _layer(chunk, "model")["bytes"] + _layer(chunk, "cloud")["bytes"] < model_bytes + cloud_bytes
    assert small["build"]["size"] < whole["build"]["size"]
    assert small["build"]["notes"] and all(ch.label in n for n in small["build"]["notes"] if "reduced" in n)
    # a texture reduced keeps the model textured, a model with fewer faces too
    assert _layer(chunk, "model")["textured"]
    from pyarchinit_photogrammetry.io import site as site_mod

    assert site_mod.build_notes(path) == small["build"]["notes"]


def test_a_model_thinned_keeps_its_texture(processed, tmp_path):
    """export_3dtiles with max_total_faces: fewer faces, the texture coordinates of the new faces read the same
    colours of the texture as the faces of the model under them."""
    from pyarchinit_photogrammetry.io.web import _thinned, _world_mesh

    _doc, ch, _ = processed
    world, faces, colors, uv, pages, tex = _world_mesh(ch)
    v, f, c, uv2, page = _thinned(world, faces, colors, uv, tex, len(faces) // 4)
    assert len(f) < len(faces) / 2 and uv2.shape == (len(f), 3, 2)
    assert 0.0 <= uv2.min() and uv2.max() <= 1.0
    from scipy.spatial import cKDTree

    # the centre of each new face: its texture coordinate close to the one of the old face nearest to it
    centres = v[f].mean(1)
    tri = world[faces]
    near = cKDTree(tri.mean(1)).query(centres)[1]
    old = np.asarray(uv, float).reshape(-1, 3, 2)[near].mean(1)
    assert np.median(np.linalg.norm(uv2.mean(1) - old, axis=1)) < 0.01


# ------------------------------------------------------------------------------------------------ several dates
def _two_dates(processed, folder: Path):
    """Two surveys of the trench in one project: the second a week later, dug 0.2 m deeper in a square of a metre
    in the middle; both with the model, the second with a unit. Returns the document."""
    import rasterio

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from shapely.geometry import box

    source = processed[1]
    doc = ppg.Document()
    doc.save(folder / "date.ppg")
    for n, day in enumerate(("2025-06-02", "2025-06-09")):
        ch = doc.add_chunk(f"Rilievo {n + 1}")
        ch.crs = source.crs
        ch.meta["date"] = day
        for product, path in (("orthomosaic", source.orthomosaic_path), ("dem", source.dem_path)):
            out = ch.own_product_path(product)
            out.parent.mkdir(parents=True, exist_ok=True)
            with rasterio.open(path) as src:
                profile = src.profile
                with rasterio.open(out, "w", **profile) as dst:
                    for band in range(1, src.count + 1):
                        cells = src.read(band)
                        if product == "dem" and n:
                            r, c = cells.shape
                            sq = np.zeros_like(cells, bool)
                            sq[r // 2 - 20:r // 2 + 20, c // 2 - 20:c // 2 + 20] = True
                            cells = np.where(sq & (cells != src.nodata), cells - 0.2, cells).astype(cells.dtype)
                        dst.write(cells, band)
                    if product == "orthomosaic" and src.count == 3:
                        dst.write_mask(src.dataset_mask())
                    b = dst.bounds
            ch._set_product(product, out)
        for name in ("model", "model_textured"):
            out = ch.data_dir / source.product_path(name).name
            shutil.copy2(source.product_path(name), out)
            ch._set_product(name, out)
        ch.transform = np.array(source.transform)
        if n:
            cx, cy = (b.left + b.right) / 2, (b.bottom + b.top) / 2
            ch.meta["drawings"] = {"units": [UnitPolygon(box(cx - 0.3, cy - 0.3, cx + 0.3, cy + 0.3), us="9")
                                             .to_dict()]}
    doc.save()
    return doc


@pytest.fixture(scope="module")
def dated_site(processed, tmp_path_factory):
    base = tmp_path_factory.mktemp("date")
    doc = _two_dates(processed, base)
    path = doc.export_site(base / "site", model_faces=8000, timeline=True,
                           labels={"Rilievo 1": "Prima", "Rilievo 2": "Dopo"})
    return doc, base / "site", json.loads(path.read_text(encoding="utf-8"))


def test_dated_entries_share_one_frame(dated_site):
    import rasterio

    doc, site, manifest = dated_site
    assert manifest["timeline"] == [{"date": "2025-06-02", "label": "Prima", "chunks": ["1"]},
                                    {"date": "2025-06-09", "label": "Dopo", "chunks": ["2"]}]
    a, b = manifest["chunks"]
    assert (a["label"], a["date"], b["label"], b["date"]) == ("Prima", "2025-06-02", "Dopo", "2025-06-09")
    # one origin: the same cell has the same coordinates on both dates, and the dug square is 0.2 m lower
    with rasterio.open(site / "data/1/dem.tif") as one, rasterio.open(site / "data/2/dem.tif") as two:
        assert one.bounds == two.bounds
        d1, d2 = one.read(1, masked=True), two.read(1, masked=True)
        r, c = d1.shape
        assert float(d1[r // 2, c // 2] - d2[r // 2, c // 2]) == pytest.approx(0.2, abs=1e-4)
        assert float(d1[5 + r // 4, 5 + c // 4] - d2[5 + r // 4, 5 + c // 4]) == pytest.approx(0.0, abs=1e-6)
    assert _layer(a, "model")["box"] == _layer(b, "model")["box"]


def test_dates_given_found_or_missing(processed, tmp_path):
    doc = _two_dates(processed, tmp_path)
    for ch in doc.chunks:
        ch.meta.pop("date")
    with pytest.raises(ValueError, match="no date"):
        doc.export_site(tmp_path / "x", products=["units", "dem"], timeline=True)
    path = doc.export_site(tmp_path / "s", products=["dem"], dates=[("Rilievo 1", "2024-05-01"),
                                                                    (doc.chunks[1], "2026-05-01T10:00")])
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert [t["date"] for t in manifest["timeline"]] == ["2024-05-01", "2026-05-01"]
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        doc.export_site(tmp_path / "y", dates={"Rilievo 1": "maggio"})
    plain = json.loads(doc.export_site(tmp_path / "p", products=["dem"]).read_text(encoding="utf-8"))
    assert "timeline" not in plain and "date" not in plain["chunks"][0]


def test_chunks_of_two_projects_in_one_site(processed, tmp_path):
    import pyarchinit_photogrammetry as ppg

    one = _two_dates(processed, (tmp_path / "a").mkdir() or tmp_path / "a")
    other = ppg.Document.open(_two_dates(processed, (tmp_path / "b").mkdir() or tmp_path / "b").path)
    path = one.export_site(tmp_path / "s", chunks=[one.chunks[0], other.chunks[1]], products=["dem", "units"],
                           dates=[(one.chunks[0], "2025-01-01"), (other.chunks[1], "2026-01-01")])
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert [c["label"] for c in manifest["chunks"]] == ["Rilievo 1", "Rilievo 2"]
    assert [t["date"] for t in manifest["timeline"]] == ["2025-01-01", "2026-01-01"]
    assert str(tmp_path).encode() not in path.read_bytes()


def test_web_build_options(processed, tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc = _two_dates(processed, tmp_path)
    other = tmp_path / "altro"
    other.mkdir()
    shutil.copy2(doc.path, other / "altro.ppg")
    shutil.copytree(doc.files_dir, other / "altro.ppg.files")
    result = CliRunner().invoke(app, [
        "web", "build", str(doc.path), str(tmp_path / "s"), "--chunk", "Rilievo 1",
        "--chunk", f"{other / 'altro.ppg'}:Rilievo 2", "--date", "Rilievo 1=2025-06-02",
        "--date", "Rilievo 2=2025-07-01", "--label", "Rilievo 2=Luglio", "--product", "dem", "--product", "cloud",
        "--budget", "300", "--cloud-points", "1000"])
    assert result.exit_code == 0, result.output
    manifest = json.loads((tmp_path / "s/site.json").read_text(encoding="utf-8"))
    assert [(t["date"], t["label"]) for t in manifest["timeline"]] == [("2025-06-02", "Rilievo 1"),
                                                                       ("2025-07-01", "Luglio")]
    assert manifest["build"]["budget"] == 300_000_000
    result = CliRunner().invoke(app, ["web", "build", str(doc.path), str(tmp_path / "t"), "--date", "Rilievo 1=x"])
    assert result.exit_code == 1 and "YYYY-MM-DD" in result.output


# ------------------------------------------------------------------------------------------------ the page
def _browser(tmp_path):
    executable = find_browser()
    if executable is None:
        pytest.skip("no Chromium found (PPG_BROWSER names one)")
    return Browser(executable, tmp_path / "profilo", size=(1200, 760))


def _drag_free_click(browser, x, y, count=1):
    for kind in ("mousePressed", "mouseReleased"):
        browser.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                                  "clickCount": count})


def test_cloud_sections_and_plugins_in_a_browser(full_site, tmp_path):
    """The cloud drawn and picked in the 3D tab, the section with its orthophoto measured on its plane and linked
    to its trace on the plan both ways, the tabs of the plugin with a clean page - no error, nothing asked outside
    the server of the site."""
    _doc, _ch, _facts, site, manifest, _ = full_site
    browser = _browser(tmp_path)
    try:
        with _serving(site) as port:
            origin = f"http://127.0.0.1:{port}"
            browser.call("Page.navigate", {"url": origin + "/"})
            browser.wait("['ready', 'error'].includes(document.documentElement.dataset.state)", 60)
            browser.js("localStorage.setItem('ppg-site-language', 'it')")  # the words of the page in Italian
            browser.call("Page.reload")
            browser.wait("document.documentElement.lang === 'it' && "
                         "['ready', 'error'].includes(document.documentElement.dataset.state)", 60)
            assert browser.js("document.documentElement.dataset.state") == "ready", browser.errors
            tabs = browser.js("[...document.querySelectorAll('#tabs button')].filter(b => !b.hidden)"
                              ".map(b => b.textContent)")
            assert tabs == ["Pianta", "Modello 3D", "Sezioni", "Catalogo", "Note", "Termini", "Guida"]
            # the cloud alone, drawn by levels and picked on its points
            browser.js("ppgSite.setTab('3d')")
            browser.wait("ppgSite.state.view3d && ppgSite.state.view3d.ready()", 90)
            browser.js("document.querySelector('label.layer[data-layer=model] input').click()")
            browser.js("document.querySelector('label.layer[data-layer=cloud] input').click()")
            browser.wait("ppgSite.state.view3d.ready()", 90)
            view = "ppgSite.state.view3d"
            assert browser.js(f"{view}.models.map(m => m.holder.visible)") == [False, True]
            assert browser.js(f"{view}.models[1].tiles.visibleTiles.size") > 1  # finer nodes added near
            rect = browser.js("(() => { const r = document.querySelector('#view3d canvas')"
                              ".getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()")
            cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
            point = browser.js(f"(() => {{ const p = {view}.pick({cx}, {cy}); return p && [p.x, p.y, p.z]; }})()")
            assert point is not None and all(abs(v) < 10 for v in point)
            for dx in (-80, 80):
                browser.js(f"{view}.measure('distance')") if dx < 0 else None
                _drag_free_click(browser, cx + dx, cy)
            result = browser.js(f"{view}.result()")
            assert result["points"] == 2 and 0.5 < result["length"] < 5
            browser.js(f"{view}.clear()")
            # the section: its orthophoto, a measure on its plane, its trace on the plan
            browser.js("document.getElementById('tab-sections').click()")
            browser.wait("document.documentElement.dataset.section === '1'", 60)
            sec = manifest["chunks"][0]["sections"][0]
            assert browser.js("ppgSite.state.sectionMap.getView().getProjection().getCode()") == "PPG:SEZ:1"
            line = browser.js("ppgSite.measured(new ol.geom.LineString([[0, 0], [1.2, 0.5]]))")
            assert line == "Lunghezza: 1.300 m"
            u = (sec["extent"][0] + sec["extent"][2]) / 2
            px = browser.js(f"ppgSite.state.sectionMap.getPixelFromCoordinate([{u}, {sec['extent'][1] + 0.05}])")
            box = browser.js("(() => { const r = document.getElementById('section-map').getBoundingClientRect();"
                             " return [r.left, r.top]; })()")
            _drag_free_click(browser, box[0] + px[0], box[1] + px[1])
            browser.wait("document.getElementById('info').textContent.includes('u lungo la traccia')", 10)
            browser.js("document.getElementById('show-on-plan').click()")
            browser.wait("document.documentElement.dataset.highlight === '1'", 10)
            assert browser.js("ppgSite.state.tab") == "plan"
            assert browser.js("ppgSite.state.highlight.getSource().getFeatures().length") == 1
            browser.js("document.documentElement.dataset.section = ''")
            browser.js("ppgSite.map.renderSync()")
            c = sec["trace"]["coordinates"]
            mid = [(c[0][0] + c[-1][0]) / 2, (c[0][1] + c[-1][1]) / 2]
            px = browser.js(f"ppgSite.map.getPixelFromCoordinate({mid})")
            box = browser.js("(() => { const r = document.getElementById('map').getBoundingClientRect();"
                             " return [r.left, r.top]; })()")
            _drag_free_click(browser, box[0] + px[0], box[1] + px[1])
            browser.wait("document.documentElement.dataset.section === '1'", 20)
            assert browser.js("ppgSite.state.tab") == "sections"
            # the tabs of the plugin
            browser.js("document.querySelector(\"button[data-tab='x:finds-test:catalogo']\").click()")
            browser.wait("document.documentElement.dataset.extra === 'finds-test:catalogo'", 20)
            browser.wait("[...document.querySelectorAll('.card img')].every(i => i.complete && i.naturalWidth)", 20)
            assert browser.js("document.querySelectorAll('.card').length") == 2
            browser.js("document.querySelector(\"button[data-tab='x:finds-test:note']\").click()")
            browser.wait("document.documentElement.dataset.extra === 'finds-test:note'", 20)
            page = browser.js("document.getElementById('extra').innerHTML")
            assert "Note di scavo" in page and not any(w in page for w in ("script", "onerror", "javascript"))
            assert browser.js("document.querySelector('.plugin-page a[href]').rel") == "noopener noreferrer nofollow"
            _elevation_layer_and_languages(browser, site, sec)
            assert not browser.errors, browser.errors
            assert all(u.startswith(origin) or u.startswith(("data:", "blob:")) for u in browser.requests)
    finally:
        browser.close()


def _elevation_layer_and_languages(browser, site, sec):
    """The layer a plugin put on the section: in the list with its tick and legend, its attributes on a click in the
    language of the page; the values of a table and the page of a plugin in the other language once it is chosen."""
    browser.js("ppgSite.setTab('sections')")
    browser.wait("document.documentElement.dataset.section === '1'", 60)
    row = "document.querySelector(\"#layers label.layer[data-layer='plugin:frame-test:decay']\")"
    assert browser.js(f"{row}.textContent") == "Degrado"
    assert browser.js("[...document.querySelectorAll('#layers label.group')].map(l => l.textContent)"
                      ".includes('Esfoliazione1')")
    layer = "ppgSite.state.section.extra[0].layer"
    assert browser.js(f"{layer}.getSource().getFeatures().length") == 1
    browser.js(f"{row}.querySelector('input').click()")
    assert browser.js(f"{layer}.getVisible()") is False
    browser.js(f"{row}.querySelector('input').click()")
    assert browser.js(f"{layer}.getVisible()") is True
    ring = np.asarray(json.loads((site / sec["layers"][0]["url"]).read_text(encoding="utf-8"))
                      ["features"][0]["geometry"]["coordinates"][0])
    centre = [float(ring[:, 0].mean()), float(ring[:, 1].mean())]

    def click_centre():
        browser.js("ppgSite.state.sectionMap.renderSync()")
        px = browser.js(f"ppgSite.state.sectionMap.getPixelFromCoordinate({centre})")
        box = browser.js("(() => { const r = document.getElementById('section-map').getBoundingClientRect();"
                         " return [r.left, r.top]; })()")
        _drag_free_click(browser, box[0] + px[0], box[1] + px[1])

    click_centre()
    browser.wait("document.getElementById('info').textContent.includes('Esfoliazione')", 10)
    info = browser.js("document.getElementById('info').textContent")
    assert "Termine" in info and "Sommità" in info and "Exfoliation" not in info
    browser.js("document.getElementById('language').click()")
    browser.wait("ppgSite.state.language === 'en' && document.documentElement.dataset.section === '1'", 30)
    assert browser.js(f"{row}.textContent") == "Decay"
    click_centre()
    browser.wait("document.getElementById('info').textContent.includes('Exfoliation')", 10)
    info = browser.js("document.getElementById('info').textContent")
    assert "Term" in info and "Esfoliazione" not in info
    browser.js("document.querySelector(\"button[data-tab='x:frame-test:terms']\").click()")
    browser.wait("document.documentElement.dataset.extra === 'frame-test:terms'", 20)
    cards = browser.js("document.getElementById('extra').textContent")
    assert "Exfoliation" in cards and "English only" in cards and "Esfoliazione" not in cards
    browser.js("document.querySelector(\"button[data-tab='x:frame-test:guida']\").click()")
    browser.wait("document.documentElement.dataset.extra === 'frame-test:guida'", 20)
    assert "English page" in browser.js("document.getElementById('extra').textContent")
    browser.js("document.getElementById('language').click()")  # back to Italian: the page of that language
    browser.wait("document.getElementById('extra').textContent.includes('Pagina italiana')", 20)
    assert "English page" not in browser.js("document.getElementById('extra').textContent")


def test_dates_in_a_browser(dated_site, tmp_path):
    """The selector of the dates: the latest first; another date switches the plan and the 3D view, the view kept."""
    _doc, site, _manifest = dated_site
    browser = _browser(tmp_path)
    try:
        with _serving(site) as port:
            browser.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/"})
            browser.wait("['ready', 'error'].includes(document.documentElement.dataset.state)", 60)
            assert browser.js("document.documentElement.dataset.state") == "ready", browser.errors
            buttons = browser.js("[...document.querySelectorAll('#dates button.date')].map(b => b.textContent)")
            assert buttons == ["2025-06-02 · Prima", "2025-06-09 · Dopo"]
            shown = "ppgSite.state.entries.filter(e => e.layer.getVisible()).map(e => e.chunk.id + ':' + e.def.id)"
            assert browser.js(shown) == ["2:ortho", "2:units"]
            import rasterio

            with rasterio.open(site / "data/1/dem.tif") as src:  # the middle of the trench: dug on the second date
                rows, cols = src.shape
                x, y = src.xy(rows // 2, cols // 2)
            dem = browser.js(f"ppgSite.heightAt({x}, {y})")
            centre = browser.js("ppgSite.map.getView().getCenter()")
            browser.js("document.querySelector('#dates button.date').click()")
            browser.wait("document.documentElement.dataset.epoch === '0'", 10)
            assert browser.js(shown) == ["1:ortho"]
            assert browser.js(f"ppgSite.heightAt({x}, {y})") == pytest.approx(dem + 0.2, abs=1e-4)  # before it
            assert browser.js("ppgSite.map.getView().getCenter()") == centre
            browser.js("ppgSite.setTab('3d')")
            browser.wait("ppgSite.state.view3d && ppgSite.state.view3d.ready()", 90)
            models = "ppgSite.state.view3d.models.map(m => m.holder.visible)"
            assert browser.js(models) == [True, False]
            camera = browser.js("ppgSite.state.view3d.camera.position.toArray()")
            browser.js("(() => { const s = document.getElementById('date-slider'); s.value = '1';"
                       " s.dispatchEvent(new Event('input')); })()")
            browser.wait("document.documentElement.dataset.epoch === '1'", 10)
            assert browser.js(models) == [False, True]
            assert browser.js("ppgSite.state.view3d.camera.position.toArray()") == camera
            assert not browser.errors, browser.errors
    finally:
        browser.close()
