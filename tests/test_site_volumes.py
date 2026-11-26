"""Unit volumes thinned for the published web site: closed, with their volume and their thickness."""
from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import box

from pyarchinit_photogrammetry.archaeo import proxies
from pyarchinit_photogrammetry.engine import meshing
from pyarchinit_photogrammetry.engine.dem import write_geotiff
from pyarchinit_photogrammetry.io import site_volumes


@pytest.fixture
def thin_layer(tmp_path):
    """A layer 3 cm thick on a 2 x 1 m DEM at 1 cm, gently sloping (about 80,000 faces)."""
    x = (np.arange(200) + 0.5) * 0.01
    z = np.tile(10.0 + 0.05 * x, (100, 1)).astype(np.float32)
    dem = write_geotiff(tmp_path / "dem.tif", z, (0.0, 1.0), 0.01, "EPSG:3004", nodata=-32767.0)
    return proxies.unit_proxy(dem, box(0.0, 0.0, 2.0, 1.0), thickness=0.03, us="5").mesh


def _z_range(m):
    return float(m.bounds[1][2] - m.bounds[0][2])


def _assert_sound(new, old, target):
    assert new is not None
    assert len(new.faces) <= 1.1 * target
    assert new.is_watertight
    assert abs(abs(new.volume) - abs(old.volume)) <= 0.1 * abs(old.volume)
    assert abs(_z_range(new) - _z_range(old)) <= 0.005
    assert abs(new.bounds[0][2] - old.bounds[0][2]) <= 0.005 and abs(new.bounds[1][2] - old.bounds[1][2]) <= 0.005


def test_prism_refactor_keeps_the_volume(thin_layer):
    assert thin_layer.is_watertight
    assert abs(thin_layer.volume) == pytest.approx(0.06, abs=0.002)
    assert len(thin_layer.faces) > 70_000


def test_a_thin_layer_stays_closed(thin_layer):
    new, how = site_volumes.thin_volume(thin_layer, 2000)
    assert how in ("quadric", "regrid")
    _assert_sound(new, thin_layer, 2000)


def test_the_rebuild_when_quadric_is_not_there(thin_layer, monkeypatch):
    monkeypatch.setattr(meshing, "_decimate", lambda m, n: m)
    new, how = site_volumes.thin_volume(thin_layer, 2000)
    assert how == "regrid"
    _assert_sound(new, thin_layer, 2000)


def test_small_volume_kept(thin_layer):
    new, how = site_volumes.thin_volume(thin_layer, len(thin_layer.faces))
    assert new is thin_layer and how == "kept"


def test_targets():
    assert site_volumes.VOLUME_FACES == 20_000 and site_volumes.CHUNK_VOLUME_FACES == 600_000


def test_a_volume_that_cannot_be_rebuilt_is_dropped(thin_layer, monkeypatch):
    monkeypatch.setattr(meshing, "_decimate", lambda m, n: m)
    monkeypatch.setattr(site_volumes, "_regrid", lambda m, n: None)
    assert site_volumes.thin_volume(thin_layer, 2000) == (None, "dropped")


def test_regrid_keeps_to_the_cap_at_small_targets(thin_layer):
    # the walls count too: a 2 x 1 m layer asked for 200 faces once came out at 236
    for target in (60, 200, 500):
        new = site_volumes._regrid(thin_layer, target)
        assert new is not None and new.is_watertight
        assert len(new.faces) <= 1.1 * target


def test_volume_unit_of_its_own_polygon():
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.io.site_records import volume_unit

    polys = [UnitPolygon(box(0, 0, 1, 1), us="12", area="1", tipo="US", sito="Sito A")]
    # no site of the project: the polygon's own site is the one
    assert volume_unit({"us": "12", "tipo": "US", "sito": "Altro"}, polys, None) == ("Sito A", "1", "US", "12")
    # a unit drawn in two pieces of one site and area is one unit
    two = polys + [UnitPolygon(box(2, 0, 3, 1), us="12", area="1", tipo="US", sito="Sito A")]
    assert volume_unit({"us": "12", "tipo": "US"}, two, "Progetto") == ("Sito A", "1", "US", "12")
    # pieces that disagree on the site: no polygon decides, the project's site
    other = polys + [UnitPolygon(box(2, 0, 3, 1), us="12", area="1", tipo="US", sito="Sito B")]
    assert volume_unit({"us": "12", "tipo": "US", "area": "1"}, other, "Progetto") == ("Progetto", "1", "US", "12")


# ------------------------------------------------------------------------------------------------ in the site
SITE = "Sito di prova"


def _with_volumes(processed, folder):
    """A copy of the trench with two unit polygons (US 12 and US 14 of SITE) and their volumes as the program
    writes them: US 14's entry names another site, a third entry (US 99) has lost its PLY."""
    import shutil
    from pathlib import Path

    import rasterio

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    doc, _ch, _ = processed
    folder.mkdir(parents=True)
    shutil.copy2(doc.path, folder / doc.path.name)
    shutil.copytree(doc.files_dir, folder / doc.files_dir.name)
    doc = ppg.Document.open(folder / doc.path.name)
    ch = doc.chunk
    with rasterio.open(ch.dem_path) as src:
        b = src.bounds
    cx, cy = (b.left + b.right) / 2, (b.bottom + b.top) / 2
    units = [UnitPolygon(box(cx - 0.5, cy - 0.3, cx - 0.05, cy + 0.3), us="12", area="1", sito=SITE),
             UnitPolygon(box(cx + 0.05, cy - 0.3, cx + 0.5, cy + 0.3), us="14", area="1", sito=SITE)]
    ch.meta.setdefault("drawings", {})["units"] = [u.to_dict() for u in units]
    folder_p = ch.data_dir / "proxies"
    entries = []
    for px, site in zip(proxies.proxies_for_units(ch.dem_path, units, thickness=0.05), (SITE, "Altro sito")):
        path = proxies.save_proxy(px, folder_p, "ply")
        entries.append({"name": px.name, "us": px.us, "area": px.area, "tipo": px.tipo, "sito": site,
                        "file": str(path.relative_to(ch.data_dir)), "volume_m3": px.volume_m3,
                        "area_m2": px.area_m2, "z_top": list(px.z_top), "z_bottom": list(px.z_bottom),
                        "base": "thickness", "thickness": 0.05, "ppg_id": "ppg-segreto-4471",
                        "base_dem": str(Path.home() / "scavi" / "dem_segreto.tif")})
    lost = dict(entries[0], name="US99", us="99", file="proxies/US99.ply")
    entries.append(lost)
    ch.meta["proxies"] = entries
    doc.save()
    return doc, ch


@pytest.fixture(scope="module")
def volumes_doc(processed, tmp_path_factory):
    return _with_volumes(processed, tmp_path_factory.mktemp("volumi") / "progetto")


def _glb_meshes(path):
    """``{node name: (vertices Z-up, faces)}`` of ``proxies.glb``."""
    from test_site import _glb

    gltf, blob = _glb(path)
    out = {}
    for node in gltf["nodes"]:
        prim = gltf["meshes"][node["mesh"]]["primitives"][0]
        acc = gltf["accessors"][prim["attributes"]["POSITION"]]
        view = gltf["bufferViews"][acc["bufferView"]]
        v = np.frombuffer(blob, np.float32, acc["count"] * 3, view.get("byteOffset", 0)).reshape(-1, 3)
        ia = gltf["accessors"][prim["indices"]]
        iv = gltf["bufferViews"][ia["bufferView"]]
        f = np.frombuffer(blob, np.uint32, ia["count"], iv.get("byteOffset", 0)).reshape(-1, 3)
        out[node["name"]] = (np.column_stack([v[:, 0], -v[:, 2], v[:, 1]]), f, acc)
    return out


def _site(doc, folder, **kw):
    import json

    path = doc.export_site(folder, **kw)
    return path.parent, json.loads(path.read_text(encoding="utf-8"))


def test_volumes_in_the_frame_of_the_site(volumes_doc, tmp_path):
    import json

    from test_site import _coordinate_leaks, _frame_shift

    doc, ch = volumes_doc
    site, manifest = _site(doc, tmp_path / "site", products=("dem", "units", "proxies"))
    layer = next(x for x in manifest["chunks"][0]["layers"] if x["id"] == "volumes")
    assert layer["type"] == "volumes" and layer["url"] == "data/proxies.glb" and layer["count"] == 2
    assert layer["data"] == "data/proxies.json" and "origin" not in layer and len(layer["box"]) == 6
    assert [x["id"] for x in manifest["chunks"][0]["layers"]].index("volumes") < \
        [x["id"] for x in manifest["chunks"][0]["layers"]].index("units")
    text = (site / "data" / "proxies.json").read_text(encoding="utf-8")
    data = json.loads(text)
    gltf_text = (site / "data" / "proxies.glb").read_bytes()[20:].split(b"BIN\0")[0].decode("utf-8", "replace")
    for t in (text, gltf_text, json.dumps(layer)):
        assert _coordinate_leaks(t) == []
        for word in ("ppg_id", "ppg-segreto", "Altro sito", "sito", "file", "base_dem", "dem_segreto", "/Users",
                     "proxies/US"):
            assert word not in t, word
    dz = _frame_shift(ch, site)[2]
    meshes = _glb_meshes(site / "data" / "proxies.glb")
    by_name = {e["name"]: e for e in ch.meta["proxies"]}
    assert sorted(meshes) == sorted(v["key"] for v in data["volumes"]) == ["US12", "US14"]
    for v in data["volumes"]:
        entry = by_name[v["key"]]
        assert v["z_bottom"][0] == pytest.approx(entry["z_bottom"][0] - dz, abs=0.001)
        assert v["z_top"][1] == pytest.approx(entry["z_top"][1] - dz, abs=0.001)
        xyz, _f, acc = meshes[v["key"]]
        assert all(abs(x) < 100 for x in acc["min"] + acc["max"])
        assert xyz[:, 2].min() == pytest.approx(entry["z_bottom"][0] - dz, abs=0.005)
        assert set(v) >= {"key", "label", "us", "area", "tipo", "volume_m3", "area_m2", "z_top", "z_bottom",
                          "colour"}
        assert "record" not in v  # no records asked for
    assert any("US99" in n and "left out" in n for n in manifest["build"]["notes"])


def test_volumes_under_the_limits(volumes_doc, tmp_path, monkeypatch):
    import trimesh

    from pyarchinit_photogrammetry.io import site_publish

    doc, ch = volumes_doc
    before = max(len(trimesh.load(str(ch.data_dir / e["file"]), force="mesh").faces)
                 for e in ch.meta["proxies"][:2])
    monkeypatch.setattr(site_volumes, "VOLUME_FACES", 500)
    assert before > 550  # the volumes of the trench must be thinned
    site, _manifest = _site(doc, tmp_path / "site", products=("proxies",))
    glb = site / "data" / "proxies.glb"
    assert glb.stat().st_size < site_publish.PAGES_FILE_BYTES
    meshes = _glb_meshes(glb)
    assert len(meshes) == 2
    for xyz, f, _acc in meshes.values():
        assert len(f) <= 1.1 * 500
        assert trimesh.Trimesh(xyz.astype(float), f, process=True).is_watertight


def test_volume_takes_the_record_of_its_polygon(volumes_doc, tmp_path):
    import json

    doc, _ch = volumes_doc
    rows = [{"sito": SITE, "area": "1", "us": "12", "unita_tipo": "US", "d_stratigrafica": "Strato"},
            {"sito": SITE, "area": "1", "us": "14", "unita_tipo": "US", "d_stratigrafica": "Riempimento"}]
    site, manifest = _site(doc, tmp_path / "site", products=("units", "proxies"), unit_records=rows)
    units = json.loads((site / "data" / "units.geojson").read_text(encoding="utf-8"))["features"]
    unit_key = {f["properties"]["us"]: f["properties"]["record"] for f in units}
    volumes = json.loads((site / "data" / "proxies.json").read_text(encoding="utf-8"))["volumes"]
    vol_key = {v["us"]: v["record"] for v in volumes}
    assert vol_key == unit_key and set(vol_key) == {"12", "14"}  # US 14's entry names "Altro sito"
    records = json.loads((site / "data" / "records.json").read_text(encoding="utf-8"))
    assert set(records) == set(unit_key.values())


def test_volumes_alone_give_a_3d_view(volumes_doc, tmp_path):
    import json
    import math

    doc, ch = volumes_doc
    site, manifest = _site(doc, tmp_path / "site", products=("proxies",))
    assert [x["id"] for x in manifest["chunks"][0]["layers"]] == ["volumes"]
    data = json.loads((site / "data" / "proxies.json").read_text(encoding="utf-8"))
    lowest = min(e["z_bottom"][0] for e in ch.meta["proxies"][:2])
    meshes = _glb_meshes(site / "data" / "proxies.glb")
    # dz is the whole metre under the lowest point of the volumes
    low = min(v["z_bottom"][0] for v in data["volumes"])
    assert 0 <= low < 1 and low == pytest.approx(lowest - math.floor(lowest), abs=0.001)
    assert min(xyz[:, 2].min() for xyz, _f, _a in meshes.values()) >= -0.005


def test_volumes_make_the_site_stale(volumes_doc, tmp_path):
    import os

    from pyarchinit_photogrammetry.io import site as site_mod

    doc, ch = volumes_doc
    assert "proxies" in site_mod.chunk_products(ch)
    sources = site_mod.site_sources(doc, None, ["proxies"])
    manifest = doc.export_site(tmp_path / "site", products=("proxies",))
    record = site_mod.record_site(doc, manifest, {"products": ["proxies"], "frame": "local"}, sources)
    assert site_mod.site_stale(doc, record) == []
    path = ch.data_dir / ch.meta["proxies"][0]["file"]
    path.write_bytes(path.read_bytes())
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert site_mod.site_stale(doc, record) == [f"{ch.label}: proxies"]


def test_glb_names_its_nodes():
    import hashlib

    from test_site import _glb as read

    from pyarchinit_photogrammetry.io.web import _glb

    pos = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float)
    prims = [{"position": pos, "index": [0, 1, 2]},
             {"position": pos + 1, "index": [0, 2, 1], "colour": np.full((3, 3), 200)}]
    assert hashlib.sha256(_glb(prims)).hexdigest() == \
        "1bda987a88327aa87da579e222f8ab49f063a546e319309edcf9128199a6f877"  # the tiles as before
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "n.glb"
        path.write_bytes(_glb(prims, names=["a", "b"]))
        gltf, _blob = read(path)
    assert [n["name"] for n in gltf["nodes"]] == ["a", "b"]
    assert [n["mesh"] for n in gltf["nodes"]] == [0, 1] and len(gltf["meshes"]) == 2
    assert gltf["scenes"][0]["nodes"] == [0, 1]


def test_a_volume_dropped_by_thinning_has_no_record(volumes_doc, tmp_path, monkeypatch):
    """Thinning runs before the records: a volume it leaves out has no record and no line in the report."""
    import json

    doc, _ch = volumes_doc
    rows = [{"sito": SITE, "area": "1", "us": "12", "unita_tipo": "US", "d_stratigrafica": "Strato"},
            {"sito": SITE, "area": "1", "us": "14", "unita_tipo": "US", "d_stratigrafica": "Riempimento"}]
    thin, calls = site_volumes.thin_volume, []

    def second_dropped(mesh, target):
        calls.append(target)
        return (None, "dropped") if len(calls) == 2 else thin(mesh, target)

    monkeypatch.setattr(site_volumes, "thin_volume", second_dropped)
    site, manifest = _site(doc, tmp_path / "site", products=("proxies",), unit_records=rows)
    volumes = json.loads((site / "data" / "proxies.json").read_text(encoding="utf-8"))["volumes"]
    assert [v["us"] for v in volumes] == ["12"]
    records = json.loads((site / "data" / "records.json").read_text(encoding="utf-8"))
    assert set(records) == {volumes[0]["record"]}
    notes = manifest["build"]["notes"]
    assert any("US14" in n and "thinned" in n for n in notes)
    assert any("records of 1 of 1 units" in n for n in notes), notes


def test_volumes_over_the_size_have_no_record(volumes_doc, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.io import site_publish

    doc, _ch = volumes_doc
    rows = [{"sito": SITE, "area": "1", "us": "12", "unita_tipo": "US", "d_stratigrafica": "Strato"}]
    monkeypatch.setattr(site_publish, "PAGES_FILE_BYTES", 100)
    site, manifest = _site(doc, tmp_path / "site", products=("units", "proxies"), unit_records=rows)
    assert not (site / "data" / "proxies.glb").exists()
    assert "volumes" not in [x["id"] for x in manifest["chunks"][0]["layers"]]
    notes = manifest["build"]["notes"]
    assert any("over" in n and "MiB" in n for n in notes)
    assert any("records of 1 of 2 units" in n for n in notes), notes  # the two polygons, no volume


def test_a_malformed_volume_entry_is_left_out(volumes_doc, tmp_path):
    import copy
    import json

    doc, ch = volumes_doc
    saved = copy.deepcopy(ch.meta["proxies"])
    try:
        ch.meta["proxies"][1]["z_top"] = 12.5  # a scalar: not a pair
        site, manifest = _site(doc, tmp_path / "site", products=("proxies",))
    finally:
        ch.meta["proxies"] = saved
    volumes = json.loads((site / "data" / "proxies.json").read_text(encoding="utf-8"))["volumes"]
    assert [v["key"] for v in volumes] == ["US12"]
    assert any("US14" in n and "malformed" in n for n in manifest["build"]["notes"])


def test_an_unreadable_volume_is_left_out_an_open_one_kept(volumes_doc, tmp_path):
    """A PLY that is there but unreadable: left out of the site with a note, and not counted by the report of the
    matches. An open mesh (blender-em keeps some) is read and counted: small, it is published as given."""
    import copy

    import trimesh

    from pyarchinit_photogrammetry.io import site_records

    doc, ch = volumes_doc
    saved = copy.deepcopy(ch.meta["proxies"])
    broken = ch.data_dir / "proxies" / "US99.ply"
    open_ply = ch.data_dir / "proxies" / "US98.ply"
    try:
        broken.write_bytes(b"not a mesh at all")
        trimesh.Trimesh([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], [[0, 1, 2], [0, 1, 3]]).export(str(open_ply))
        ch.meta["proxies"].append(dict(saved[0], name="US98", us="98", file="proxies/US98.ply"))
        notes = []
        assert [e["us"] for e, _m in site_volumes.load(ch, notes)] == ["12", "14", "98"]
        assert any("US99" in n and "unreadable" in n for n in notes)
        assert not any("US98" in n for n in notes)
        rows = [{"sito": SITE, "area": "1", "us": u, "unita_tipo": "US"} for u in ("12", "14", "98", "99")]
        labels = [r["label"] for r in site_records.match_report(doc, None, rows) if r["what"] == "volume"]
        assert sorted(labels) == ["US12", "US14", "US98"]
        site, manifest = _site(doc, tmp_path / "site", products=("proxies",))
        meshes = _glb_meshes(site / "data" / "proxies.glb")
        assert sorted(meshes) == ["US12", "US14", "US98"] and len(meshes["US98"][1]) == 2  # as given
        assert any("US99" in n and "unreadable" in n for n in manifest["build"]["notes"])
    finally:
        ch.meta["proxies"] = saved
        broken.unlink(missing_ok=True)
        open_ply.unlink(missing_ok=True)


def _groups_provider(chunk, site):
    return [{"kind": "unit_groups", "id": "epoche", "title": {"it": "Epoche", "en": "Epochs"},
             "groups": [{"name": "fase-1", "title": "Fase 1", "colour": "#112233",
                         "units": [{"tipo": "US", "us": "12"}, {"tipo": "US", "us": "999"}]},
                        {"name": "fase-2", "colour": "red",
                         "units": [{"tipo": "us", "us": 12}, {"us": " 14 ", "area": "2"}, {"tipo": "US"}]}]},
            {"kind": "unit_groups", "id": "altre", "groups": [{"name": "x", "colour": "#000000",
                                                               "units": [{"us": "14"}]}]}]


def test_hook_3_groups_colour_the_volumes(volumes_doc, tmp_path):
    import json

    from pyarchinit_photogrammetry.archaeo.proxies import PROXY_COLOURS
    from pyarchinit_photogrammetry.io import site_plugins

    doc, _ch = volumes_doc
    entry = site_plugins.register(_groups_provider, owner="em", with_site=True)
    try:
        site, manifest = _site(doc, tmp_path / "site", products=("proxies",))
    finally:
        site_plugins.unregister(entry)
    data = json.loads((site / "data" / "proxies.json").read_text(encoding="utf-8"))
    by_us = {v["us"]: v for v in data["volumes"]}
    # US 12 is named by both groups: the first takes it; US 14 is in area 1, not 2: no group, the program colour
    assert by_us["12"]["group"] == "fase-1" and by_us["12"]["colour"] == "#112233"
    assert by_us["14"]["group"] is None and by_us["14"]["colour"] == PROXY_COLOURS[1]
    assert data["groups"] == [
        {"name": "fase-1", "title": {"it": "Fase 1", "en": "Fase 1"}, "colour": "#112233", "count": 1},
        {"name": "fase-2", "title": {"it": "fase-2", "en": "fase-2"}, "colour": PROXY_COLOURS[1], "count": 0},
        {"name": "", "title": {"it": "Senza gruppo", "en": "No group"}, "colour": None, "count": 1}]
    layer = next(x for x in manifest["chunks"][0]["layers"] if x["id"] == "volumes")
    assert layer["groups"] == data["groups"]
    assert not (site / "data" / "plugins").exists() and "plugins" not in manifest["chunks"][0]
    notes = manifest["build"]["notes"]
    assert any("plugin em" in n and "altre" in n and "one set of unit groups" in n for n in notes), notes


def test_unit_groups_need_volumes(volumes_doc, tmp_path):
    from pyarchinit_photogrammetry.io import site_plugins

    doc, _ch = volumes_doc
    entry = site_plugins.register(lambda chunk, site: _groups_provider(chunk, site)[:1], owner="em",
                                  with_site=True)
    try:
        site, manifest = _site(doc, tmp_path / "site", products=("dem",))
    finally:
        site_plugins.unregister(entry)
    assert not (site / "data" / "proxies.json").exists() and not (site / "data" / "plugins").exists()
    assert not any("plugin em" in n for n in manifest["build"]["notes"])


def test_unit_groups_without_a_list_are_left_out(tmp_path):
    from types import SimpleNamespace

    from pyarchinit_photogrammetry.io import site_plugins
    from pyarchinit_photogrammetry.io.site import _Shift

    entry = site_plugins.register(_groups_provider, owner="em", with_site=True)
    notes = []
    try:
        site_plugins.write(SimpleNamespace(label="C"), tmp_path, "data", _Shift(), lambda v: v, str, notes)
    finally:
        site_plugins.unregister(entry)
    assert len(notes) == 2 and all("unit groups" in n for n in notes)
    assert not (tmp_path / "plugins").exists()


def test_apply_groups():
    volumes = [{"tipo": "US", "us": "1", "area": None, "colour": "#aaaaaa", "group": None},
               {"tipo": "USM", "us": "1", "area": "3", "colour": "#bbbbbb", "group": None}]
    groups = {"plugin": "em", "id": "g", "title": {"it": "G", "en": "G"},
              "groups": [{"name": "a", "title": {"it": "A", "en": "A"}, "colour": "#010101",
                          "units": [{"tipo": "USM", "us": "1", "area": "2"}]},
                         {"name": "b", "title": {"it": "B", "en": "B"}, "colour": "#020202",
                          "units": [{"tipo": "USM", "us": "1", "area": "3"}, {"tipo": "US", "us": "1"}]}]}
    assert site_volumes.apply_groups([dict(v) for v in volumes], None) == []
    legend = site_volumes.apply_groups(volumes, groups)
    assert [(v["group"], v["colour"]) for v in volumes] == [("b", "#020202"), ("b", "#020202")]
    assert [(g["name"], g["count"]) for g in legend] == [("a", 0), ("b", 2)]  # no "Senza gruppo" row: none left


def _on_volume(site, key, n=12):
    """``n`` points of the surface of the volume ``key`` of ``proxies.glb`` well inside its outline, in the frame of
    the site: a ray toward one of them meets the volume."""
    xyz = _glb_meshes(site / "data" / "proxies.glb")[key][0]
    lo, hi = xyz.min(axis=0), xyz.max(axis=0)
    inside = xyz[(xyz[:, 0] > lo[0] + 0.08) & (xyz[:, 0] < hi[0] - 0.08) & (xyz[:, 1] > lo[1] + 0.08)
                 & (xyz[:, 1] < hi[1] - 0.08)]
    return [tuple(float(v) for v in p) for p in inside[np.linspace(0, len(inside) - 1, n).astype(int)]]


def test_volumes_in_the_page(volumes_doc, tmp_path):
    """The 3D tab draws the volumes over the model: a click on a volume opens the record of its unit (the volume
    wins over the surface of the model in front of it) and marks it; the legend has a switch a group, the last one
    named by the page; a group switched off is no longer picked; a record selected marks its volume; a measure
    takes the nearest surface (the model where it is in front); an unreadable volumes file leaves the model shown;
    a site with volumes alone opens on the 3D tab with a legend of the volumes."""
    import json

    from test_site import Browser, _serving, find_browser

    from pyarchinit_photogrammetry.io import site_plugins

    executable = find_browser()
    if executable is None:
        pytest.skip("no Chromium found (PPG_BROWSER names one)")
    doc, _ch = volumes_doc
    rows = [{"sito": SITE, "area": "1", "us": "12", "unita_tipo": "US", "d_stratigrafica": "Strato"},
            {"sito": SITE, "area": "1", "us": "14", "unita_tipo": "US", "d_stratigrafica": "Riempimento"}]
    entry = site_plugins.register(_groups_provider, owner="em", with_site=True)
    try:
        site, _manifest = _site(doc, tmp_path / "site", products=("dem", "model", "units", "proxies"),
                                unit_records=rows, model_faces=8000)
    finally:
        site_plugins.unregister(entry)
    data = json.loads((site / "data" / "proxies.json").read_text(encoding="utf-8"))
    volumes = {v["us"]: v for v in data["volumes"]}
    k12, k14 = volumes["12"]["record"], volumes["14"]["record"]
    on12 = _on_volume(site, "US12")
    c12, c14 = on12[len(on12) // 2], _on_volume(site, "US14")[6]
    pick = ("((p) => p && p.feature ? p.feature.properties.us : null)"
            "(ppgSite.state.view3d.pick(...ppgSite.state.view3d.project({}, {}, {})))")
    pick_any = ("((p) => p ? (p.feature ? p.feature.properties.us : 'model') : null)"
                "(ppgSite.state.view3d.pick(...ppgSite.state.view3d.project({}, {}, {})))")
    with _serving(site) as port:
        origin = f"http://127.0.0.1:{port}"
        browser = Browser(executable, tmp_path / "profilo", size=(1200, 800))
        try:
            browser.call("Page.navigate", {"url": origin + "/"})
            browser.wait("['ready', 'error'].includes(document.documentElement.dataset.state)", 60)
            assert browser.js("document.documentElement.dataset.state") == "ready", browser.errors
            browser.js("ppgSite.setTab('3d')")
            browser.wait("document.documentElement.dataset.model === 'ready' && ppgSite.state.view3d.ready()", 60)
            assert browser.js("ppgSite.state.view3d.models.map((m) => m.def.id)") == ["model", "volumes"]
            px, py = browser.js("ppgSite.state.view3d.project({}, {}, {})".format(*c12))
            for kind in ("mousePressed", "mouseReleased"):
                browser.call("Input.dispatchMouseEvent", {"type": kind, "x": px, "y": py, "button": "left",
                                                          "clickCount": 1})
            browser.wait("document.querySelector('#info .record') !== null", 10)
            info = browser.js("document.getElementById('info').innerText")
            assert "US 12 - Strato" in info, info
            assert browser.js("document.documentElement.dataset.highlight") == k12
            assert browser.js("ppgSite.state.view3d.selected()") == ["US12"]
            # where the model is under the pointer too (it has holes), the volume is picked, not the model
            index = browser.js("ppgSite.state.models.findIndex((m) => m.def.type === 'volumes')")
            both = 0
            for at in on12:
                browser.js(f"ppgSite.state.view3d.setVisible({index}, false)")
                model_here = browser.js(pick_any.format(*at))
                browser.js(f"ppgSite.state.view3d.setVisible({index}, true)")
                if model_here == "model":
                    both += 1
                    assert browser.js(pick.format(*at)) == "12", at
            assert both >= 3, both

            # a measure takes the nearest surface: where the model is in front of the volume, the point of the model
            z_of = ("((p) => p ? [p.z, Boolean(p.feature)] : null)"
                    "(ppgSite.state.view3d.pick(...ppgSite.state.view3d.project({}, {}, {}), {}))")
            front = []
            for at in on12:
                browser.js(f"ppgSite.state.view3d.setVisible({index}, false)")
                on_model = browser.js(z_of.format(*at, "{}"))
                browser.js(f"ppgSite.state.view3d.setVisible({index}, true)")
                on_volume = browser.js(z_of.format(*at, "{}"))
                nearest = browser.js(z_of.format(*at, "{preferVolumes: false}"))
                if on_model and on_volume[1]:
                    in_front = on_model if on_model[0] > on_volume[0] else on_volume
                    assert nearest[1] == in_front[1] and nearest[0] == pytest.approx(in_front[0], abs=1e-9), \
                        (at, on_model, on_volume, nearest)
                    if on_model[0] > on_volume[0] + 1e-4:
                        front.append((on_model[0] - on_volume[0], at))
            assert front, "no point of US 12 with the model in front"
            at = max(front)[1]
            mx, my = (round(v) for v in browser.js("ppgSite.state.view3d.project({}, {}, {})".format(*at)))
            browser.js(f"ppgSite.state.view3d.setVisible({index}, false)")
            z_model = browser.js(f"ppgSite.state.view3d.pick({mx}, {my}).z")
            browser.js(f"ppgSite.state.view3d.setVisible({index}, true)")
            z_volume = browser.js(f"ppgSite.state.view3d.pick({mx}, {my}).z")
            assert z_model > z_volume, (z_model, z_volume)
            browser.js("document.getElementById('distance').click()")
            for kind in ("mousePressed", "mouseReleased"):
                browser.call("Input.dispatchMouseEvent", {"type": kind, "x": mx, "y": my, "button": "left",
                                                          "clickCount": 1})
            assert browser.js("ppgSite.state.view3d.points.length") == 1
            assert browser.js("ppgSite.state.view3d.points[0].z") == pytest.approx(z_model, abs=1e-9)
            browser.js("document.getElementById('clear').click()")
            assert browser.js("ppgSite.state.view3d.mode") is None

            legend = browser.js("[...document.querySelectorAll('#layers [data-group]')]"
                                ".map((r) => [r.dataset.group, r.innerText.trim()])")
            assert [g for g, _t in legend] == ["fase-1", "fase-2", ""], legend
            assert "Fase 1" in legend[0][1] and "Senza gruppo" in legend[2][1], legend
            browser.js("document.getElementById('language').click()")
            assert "No group" in browser.js("document.querySelector('#layers [data-group=\"\"]').innerText")

            assert browser.js(pick.format(*c14)) == "14"
            browser.js("document.querySelector('#layers [data-group=\"fase-1\"] input').click()")
            assert browser.js(pick.format(*c12)) is None  # its group is off
            assert browser.js(pick.format(*c14)) == "14"  # in no group: still there

            browser.js(f"ppgSite.selectRecord(ppgSite.site.chunks[0], {k14!r})")
            assert browser.js("ppgSite.state.view3d.selected()") == ["US14"]
            assert not browser.errors, browser.errors
            outside = [u for u in browser.requests if not u.startswith((origin, "about:", "blob:", "data:"))]
            assert not outside, outside
        finally:
            browser.close()

    # a volumes file that cannot be read: the model is shown all the same, the layer list says so
    (site / "data" / "proxies.glb").write_bytes(b"glTF this is not a model")
    with _serving(site) as port:
        browser = Browser(executable, tmp_path / "profilo3", size=(1200, 800))
        try:
            browser.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/"})
            browser.wait("['ready', 'error'].includes(document.documentElement.dataset.state)", 60)
            browser.js("ppgSite.setTab('3d')")
            browser.wait("['ready', 'error'].includes(document.documentElement.dataset.model)", 60)
            assert browser.js("document.documentElement.dataset.model") == "ready", browser.errors
            browser.wait("ppgSite.state.view3d.ready()", 60)
            assert browser.js("ppgSite.state.view3d.models.map((m) => [m.def.id, Boolean(m.failed)])") == [
                ["model", False], ["volumes", True]]
            assert browser.js("ppgSite.state.view3d.models[0].tiles.visibleTiles.size") > 0
            assert "Volumi delle US non leggibili." in browser.js("document.getElementById('layers').innerText")
            assert browser.js(pick_any.format(*c12)) in ("model", None)  # never a volume
            assert not browser.errors, browser.errors
        finally:
            browser.close()

    alone, manifest = _site(doc, tmp_path / "alone", products=("proxies",))
    assert [x["id"] for x in manifest["chunks"][0]["layers"]] == ["volumes"]
    with _serving(alone) as port:
        browser = Browser(executable, tmp_path / "profilo2", size=(1200, 800))
        try:
            browser.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/"})
            browser.wait("['ready', 'error'].includes(document.documentElement.dataset.state)", 60)
            assert browser.js("document.documentElement.dataset.state") == "ready", browser.errors
            assert browser.js("ppgSite.state.tab") == "3d"
            browser.wait("ppgSite.state.view3d.ready()", 30)
            rows = browser.js("[...document.querySelectorAll('#layers [data-volume]')].map((r) => r.innerText.trim())")
            assert rows == ["US 12", "US 14"], rows
            assert browser.js(pick.format(*_on_volume(alone, "US14")[6])) == "14"
            assert not browser.errors, browser.errors
        finally:
            browser.close()
