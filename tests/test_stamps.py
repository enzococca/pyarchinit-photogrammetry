"""Products stamped with the chunk transform they were built in; the stale ones listed."""
from __future__ import annotations

import numpy as np
import pytest

from pyarchinit_photogrammetry.core import geometry, stamps, versions
from pyarchinit_photogrammetry.core.pointcloud import PointCloud
from pyarchinit_photogrammetry.testing.synthetic import make_classified_cloud

T0 = geometry.compose_similarity(1.0, np.eye(3), np.array([100.0, 200.0, 10.0]))


def _rotation_z(deg):
    a = np.radians(deg)
    return np.array([[np.cos(a), -np.sin(a), 0.0], [np.sin(a), np.cos(a), 0.0], [0.0, 0.0, 1.0]])


@pytest.fixture
def bare(tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    doc.save(tmp_path / "scavo.ppg")
    ch.transform = T0.copy()
    for name in stamps.GRID_PRODUCTS:
        path = ch.data_dir / f"{name}.tif"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        ch._set_product(name, path)
    return ch


@pytest.fixture
def excavation(tmp_path):
    import pyarchinit_photogrammetry as ppg

    xyz, rgb, codes = make_classified_cloud(spacing=0.04)
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    ch.crs = "EPSG:3004"
    ch.transform = geometry.compose_similarity(1.0, np.eye(3), np.round(xyz.mean(0)))
    doc.save(tmp_path / "scavo.ppg")
    ch.point_cloud = PointCloud(ch.to_internal(xyz), rgb, classification=codes)
    return ch


def test_unstamped_products_are_current(bare):
    assert "built_in" not in bare.meta
    assert stamps.stale_products(bare) == []
    assert not stamps.is_stale(bare, "dem")
    bare.transform = geometry.compose_similarity(2.0, np.eye(3), np.zeros(3))
    assert stamps.stale_products(bare) == []  # projects before 1.7.2 read as current


def test_a_changed_transform_makes_them_stale(bare):
    stamps.stamp(bare, "dem")
    assert bare.meta["built_in"]["dem"] == bare.transform.tolist()
    assert not stamps.is_stale(bare, "dem")
    moved = bare.transform.copy()
    moved[0, 3] += 1e-6
    bare.transform = moved
    assert stamps.is_stale(bare, "dem")
    assert stamps.stale_products(bare) == [{"kind": "product", "product": "dem", "name": "DEM", "id": None}]
    bare.transform = T0.copy()
    bare.transform[:3, 3] *= 1 + 1e-12
    assert not stamps.is_stale(bare, "dem")
    # a rotation far from the origin of the internal frame shows even with a large translation
    big = geometry.compose_similarity(1.0, np.eye(3), np.array([2480000.0, 4500000.0, 100.0]))
    bare.transform = big
    stamps.stamp(bare, "orthomosaic")
    bare.transform = geometry.compose_similarity(1.0, _rotation_z(0.01), big[:3, 3])
    assert stamps.is_stale(bare, "orthomosaic")
    assert stamps.same_transform(big, big + 1e-12 * np.abs(big))
    shifted = big.copy()
    shifted[0, 3] += 0.006  # 6 mm at Gauss-Boaga size shows
    assert not stamps.same_transform(big, shifted)


def test_elevations_stale_only_with_a_scale(bare):
    """Moved with a rigid change and re-stamped (the orientation's contract): current; a scale, or a change that leaves
    the frames where they were: stale."""
    entry = {"id": "e1", "name": "Prospetto"}
    stamps.stamp_entry(bare, entry)
    bare.meta["elevations"] = [entry]
    rigid = geometry.compose_similarity(1.0, _rotation_z(30), np.array([5.0, -3.0, 2.0]))
    bare.transform = rigid @ T0
    assert not stamps.entry_stale(bare, entry, scale_only=True)  # its pixels stay valid: the frame may follow
    assert stamps.stale_products(bare) == [{"kind": "elevation", "product": "elevation", "name": "Prospetto",
                                            "id": "e1"}]  # not moved: stale
    assert stamps.restamp_entries(bare, ("elevations",)) == 1  # the frames moved with it
    assert stamps.stale_products(bare) == []
    bare.transform = geometry.compose_similarity(1.001, np.eye(3), np.zeros(3)) @ bare.transform
    assert stamps.entry_stale(bare, entry, scale_only=True)
    assert [s["kind"] for s in stamps.stale_products(bare)] == ["elevation"]


def test_levelling_leaves_an_elevation_stale(bare, monkeypatch):
    from pyarchinit_photogrammetry.engine import dem, meshing

    bare.crs = "LOCAL"
    entry = stamps.stamp_entry(bare, {"id": "e1", "name": "Prospetto"})
    bare.meta["elevations"] = [entry]
    up = np.array([0.2, 0.1, 1.0])
    monkeypatch.setattr(meshing, "up_vector_internal", lambda chunk: up / np.linalg.norm(up))
    dem.ensure_level(bare)
    assert bare.meta.get("levelled")
    assert [s["id"] for s in stamps.stale_products(bare) if s["kind"] == "elevation"] == ["e1"]


def test_derived_products_take_the_stamp_of_their_input(excavation):
    from pyarchinit_photogrammetry.engine import rastercalc

    ch = excavation
    ch.build_dem(resolution=0.05)
    t1 = ch.transform.copy()
    t1[:3, 3] += [0.5, 0.0, 0.0]
    ch.transform = t1  # the DEM is now stale
    ch.build_relief("slrm")
    rastercalc.calculate(ch, "dem * 2", "doppio")
    stale = [(s["kind"], s["product"]) for s in stamps.stale_products(ch)]
    assert ("product", "dem") in stale and ("relief", "slrm") in stale and ("calculator", "calculator") in stale
    assert stamps.same_transform(ch.meta["relief"]["slrm"]["built_in"], ch.meta["built_in"]["dem"])
    # a derived product of a current input is current; without a stamp on the input it is the chunk's transform
    stamps.inherit(ch, "slope", "aspect")
    assert not stamps.is_stale(ch, "slope")
    ch.build_dem(resolution=0.05)
    ch.build_relief("slrm")
    rastercalc.calculate(ch, "dem * 2", "doppio")
    assert stamps.stale_products(ch) == []


def test_stamps_follow_versions_and_undo(excavation):
    ch = excavation
    t0 = ch.transform.copy()
    ch.build_dem(resolution=0.05)
    assert stamps.same_transform(ch.meta["built_in"]["dem"], t0)
    t1 = t0.copy()
    t1[:3, 3] += [1.0, 0.0, 0.0]
    ch.transform = t1
    assert stamps.is_stale(ch, "dem")
    kept = versions.keep_active(ch, "dem", move=True)
    assert stamps.same_transform(kept["built_in"], t0)
    ch.build_dem(resolution=0.05)
    assert not stamps.is_stale(ch, "dem")
    assert stamps.stale_products(ch) == [{"kind": "version", "product": "dem", "name": kept["name"],
                                          "id": kept["id"]}]
    previous = versions.activate(ch, "dem", kept["id"])
    assert previous["stale"] is True  # the version made active is da rifare
    assert "stale" not in versions.versions(ch, "dem")[0]  # never written into the project
    assert stamps.is_stale(ch, "dem")
    assert [s["kind"] for s in stamps.stale_products(ch)] == ["product"]
    ch.transform = t0  # the orientation undone: the old DEM is current again, the one built in T1 is not
    assert not stamps.is_stale(ch, "dem")
    assert stamps.stale_products(ch) == [{"kind": "version", "product": "dem", "name": previous["name"],
                                          "id": previous["id"]}]
    back = versions.activate(ch, "dem", previous["id"])
    assert back["stale"] is True and stamps.is_stale(ch, "dem")  # the DEM built in T1, active again in T0
    # saved and read again with the chunk
    from pyarchinit_photogrammetry.core.chunk import Chunk

    again = Chunk.from_dict(ch.to_dict(), ch.document)
    assert again.meta["built_in"] == ch.meta["built_in"]
    # an entry without a stamp (a version kept before 1.7.2) takes the stamp away when it becomes active
    entry = versions.versions(ch, "dem")[0]
    entry.pop("built_in")
    versions.activate(ch, "dem", entry["id"])
    assert "dem" not in ch.meta["built_in"] and not stamps.is_stale(ch, "dem")
    # no active DEM before: None, as always, even for a stale version (is_stale says it)
    versions.keep_active(ch, "dem", move=True)
    assert versions.activate(ch, "dem", versions.versions(ch, "dem")[0]["id"]) is None  # the DEM built in T1
    assert stamps.is_stale(ch, "dem")


def test_builders_stamp_their_products(processed):
    _doc, ch, _report = processed
    for name in ("dem", "orthomosaic"):
        assert stamps.same_transform(ch.meta["built_in"][name], ch.transform)  # the session chunk: never moved here
    assert stamps.stale_products(ch) == []


def test_order_of_the_rebuild(bare):
    for name in ("slope", "aspect"):
        path = bare.data_dir / "terrain" / f"{name}.tif"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        bare._set_product(name, path)
    for name in stamps.GRID_PRODUCTS:
        stamps.stamp(bare, name)
    stamps.inherit(bare, "slope", "dem")
    stamps.inherit(bare, "aspect", "dem")
    meta = bare.meta
    rec = {}
    for key in ("elevation", "relief", "calc", "refl", "index", "zones", "thermal", "anomaly", "scene", "version"):
        rec[key] = {}
        stamps.stamp_entry(bare, rec[key])
    meta["elevations"] = [{"id": "e1", "name": "Prospetto", **rec["elevation"]}]
    meta["relief"] = {"slrm": rec["relief"]}
    meta["calculator"] = {"ndvi2": {"file": "calculator/ndvi2.tif", **rec["calc"]}}
    meta["multispectral"] = {"reflectance": "multispectral/reflectance.tif", **rec["refl"],
                             "indices": {"NDVI": {"file": "multispectral/NDVI.tif", **rec["index"]}},
                             "zoning": {"NDVI": {"file": "z.gpkg", **rec["zones"]}}}
    meta["thermal"] = {"file": "multispectral/thermal.tif", **rec["thermal"],
                       "anomaly": {"file": "multispectral/thermal_anomaly.tif", **rec["anomaly"]}}
    meta["satellite"] = {"scenes": [{"name": "WV3", "version": "v1", **rec["scene"]}]}
    meta["versions"] = {"orthomosaic": [{"id": "v1", "name": "Satellite: WV3", "files": {}, **rec["version"]}]}
    bare.transform = geometry.compose_similarity(1.01, np.eye(3), np.zeros(3)) @ T0
    got = [(s["kind"], s["product"], s["id"]) for s in stamps.stale_products(bare)]
    assert got == [("product", "dem", None), ("product", "orthomosaic", None), ("product", "dem_precision", None),
                   ("product", "coverage", None), ("elevation", "elevation", "e1"), ("relief", "slrm", "slrm"),
                   ("product", "slope", None), ("product", "aspect", None), ("product", "tin", None), ("calculator", "calculator", "ndvi2"),
                   ("multispectral", "reflectance", "reflectance"), ("multispectral", "index", "NDVI"),
                   ("multispectral", "zoning", "NDVI"), ("thermal", "thermal", "temperature"),
                   ("thermal", "thermal_anomaly", "anomalies"), ("satellite", "scenes", "v1"),
                   ("version", "orthomosaic", "v1")]
    # a product no longer in the chunk is not listed even with its stamp
    bare.products.pop("coverage")
    assert ("product", "coverage", None) not in [(s["kind"], s["product"], s["id"])
                                                 for s in stamps.stale_products(bare)]
