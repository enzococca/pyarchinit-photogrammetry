"""Free drawings in 3D: the data model kept in ``chunk.meta["drawings"]["free"]``, its layers, and the drawings that
follow the transform of the chunk when the program changes it (not a new alignment)."""
from __future__ import annotations

import logging
import types

import numpy as np
import pytest

from pyarchinit_photogrammetry.core import free_drawings as fd
from pyarchinit_photogrammetry.core import geometry as geo


def _bare(tmp_path, label="A"):
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    ch = doc.add_chunk(label)
    doc.save(tmp_path / "bare.ppg")
    return doc, ch


def _internal(transform, xyz):
    return geo.apply(np.linalg.inv(np.asarray(transform, float)), np.asarray(xyz, float).reshape(-1, 3))


def test_add_update_remove_and_layers(tmp_path, caplog):
    _doc, ch = _bare(tmp_path)
    assert fd.features(ch) == [] and [x["name"] for x in fd.layers(ch)] == [fd.DEFAULT_LAYER]
    line = fd.add(ch, "line", [[0, 0, 0], [1, 0, 0.5]])
    poly = fd.add(ch, "polygon", [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 0, 0]], layer="Muri", name="muro",
                  attributes={"n": 1}, colour="#00ff00")
    point = fd.add(ch, "point", [[2, 2, 2]])
    assert line["name"] == "Linea 1" and point["name"] == "Punto 1" and poly["name"] == "muro"
    assert len(line["id"]) == 32 and line["layer"] == fd.DEFAULT_LAYER and line["attributes"] == {}
    assert line["created"] == line["modified"] and line["created"].endswith("+00:00")
    assert len(poly["xyz"]) == 3  # stored open
    assert fd.add(ch, "line", [[0, 0, 0], [3, 0, 0]])["name"] == "Linea 2"
    for bad in (("curve", [[0, 0, 0], [1, 1, 1]]), ("line", [[0, 0, 0]]), ("polygon", [[0, 0, 0], [1, 1, 1]]),
                ("point", [[0, np.nan, 0]]), ("point", [[0, 0]])):
        with pytest.raises(ValueError):
            fd.add(ch, *bad)
    stored = ch.meta["drawings"]["free"]
    assert len(stored) == 4
    assert ch.meta["drawings"]["free_basis"]["transform"] == np.eye(4).tolist()
    # copies: changing what features() gives changes nothing
    got = fd.features(ch)
    got[0]["xyz"][0][0] = 99.0
    assert stored[0]["xyz"][0][0] == 0.0
    assert [f["id"] for f in fd.features(ch, layer="Muri")] == [poly["id"]]
    # layers: the default one first, those of the features listed
    assert [x["name"] for x in fd.layers(ch)] == [fd.DEFAULT_LAYER, "Muri"]
    assert fd.colour_of(ch, poly) == "#00ff00" and fd.colour_of(ch, line) == fd.DEFAULT_COLOUR
    fd.set_layer(ch, fd.DEFAULT_LAYER, colour="#112233", visible=False)
    assert fd.colour_of(ch, line) == "#112233"
    assert fd.layers(ch)[0] == {"name": fd.DEFAULT_LAYER, "colour": "#112233", "visible": False}
    fd.set_layer(ch, "Muri", rename="Murature")
    assert fd.features(ch, layer="Murature")[0]["id"] == poly["id"] and not fd.features(ch, layer="Muri")
    with pytest.raises(ValueError):
        fd.set_layer(ch, "Murature", rename=fd.DEFAULT_LAYER)
    # update
    new = fd.update(ch, line["id"], name="asse", xyz=[[0, 0, 0], [2, 0, 0], [2, 1, 0]], attributes={"a": "b"})
    assert new["name"] == "asse" and len(new["xyz"]) == 3 and new["attributes"] == {"a": "b"}
    assert new["modified"] >= line["modified"]
    with pytest.raises(ValueError):
        fd.update(ch, point["id"], xyz=[[0, 0, 0], [1, 1, 1]])  # a point has one vertex... at least
    with pytest.raises(ValueError):
        fd.update(ch, line["id"], xyz=[[0, 0, 0]])
    with pytest.raises(KeyError):
        fd.update(ch, "nope", name="x")
    with pytest.raises(ValueError):
        fd.update(ch, line["id"], name="")
    with pytest.raises(ValueError):
        fd.set_layer(ch, fd.DEFAULT_LAYER, rename="")
    # a broken record is left out, said in the log
    ch.meta["drawings"]["free"].append({"id": "broken", "kind": "line", "xyz": "no"})
    ch.meta["drawings"]["free"].append("not a record")
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        assert "broken" not in {f["id"] for f in fd.features(ch)}
        assert "broken" in caplog.text and "not a record" in caplog.text
        caplog.clear()
        fd.features(ch)
        assert "broken" not in caplog.text  # once per record
    assert "x" not in [x["name"] for x in fd.layers(ch)]
    ch.meta["drawings"]["free"].append({"id": 5, "layer": "x", "kind": "line", "xyz": [[0, 0, 0], [1, 1, 1]]})
    assert "x" not in [x["name"] for x in fd.layers(ch)]  # the layer of a broken record is not listed
    ch.meta["drawings"]["free"] = [f for f in ch.meta["drawings"]["free"] if f != "not a record"
                                   and not (isinstance(f, dict) and f.get("id") == 5)]
    # remove
    assert fd.remove(ch, [line["id"], "broken"]) == 2
    assert fd.remove(ch, point["id"]) == 1
    assert {f["id"] for f in fd.features(ch)} == {poly["id"], fd.features(ch)[-1]["id"]}


def test_store_chunk_drawings_keeps_free_drawings():
    pytest.importorskip("PySide6")
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    free = [{"id": "f", "layer": fd.DEFAULT_LAYER, "kind": "point", "name": "Punto 1", "xyz": [[1.0, 2.0, 3.0]],
             "attributes": {}, "created": "x", "modified": "x"}]
    layers = [{"name": "Muri", "colour": "#ff0000", "visible": True}]
    basis = {"alignment": None, "transform": np.eye(4).tolist()}
    ch = types.SimpleNamespace(meta={"drawings": {"units": [], "quote": [{"x": 1, "y": 2}], "free": free,
                                                  "free_layers": layers, "free_basis": basis}})
    MainWindow._store_chunk_drawings(ch, [], [], [])
    assert ch.meta["drawings"] == {"free": free, "free_layers": layers, "free_basis": basis}
    MainWindow._store_chunk_drawings(ch, [], [{"x": 5, "y": 6}], [], frame="e1")
    assert ch.meta["drawings"]["free"] == free and ch.meta["drawings"]["frames"]["e1"]["quote"] == [{"x": 5, "y": 6}]
    MainWindow._store_chunk_drawings(ch, [], [], [], frame="e1")
    assert ch.meta["drawings"] == {"free": free, "free_layers": layers, "free_basis": basis}


def _marker(ch, label, position, location):
    from pyarchinit_photogrammetry.core.camera import Reference

    m = ch.add_marker(label)
    m.position = np.array(position, float)
    m.reference = Reference(location=list(map(float, location)), accuracy=[0.01] * 3)
    return m


def _georeferenced(tmp_path, label="A", shift=(0.0, 0.0, 0.0)):
    """A chunk with three control markers whose references are a rotation and a shift of their positions."""
    doc, ch = _bare(tmp_path, label)
    t = geo.identity()
    c, s = np.cos(0.3), np.sin(0.3)
    t[:3, :3] = [[c, -s, 0], [s, c, 0], [0, 0, 1]]
    t[:3, 3] = np.array([100.0, 200.0, 10.0]) + shift
    for i, p in enumerate(([0.0, 0, 0], [4.0, 0, 0.2], [0.0, 3, -0.1])):
        _marker(ch, f"M{i}", p, geo.apply(t, np.array([p]))[0])
    ch.update_transform()
    return doc, ch


def test_free_drawings_follow_transform_changes_not_realignment(tmp_path):
    from pyarchinit_photogrammetry.engine import chunks, orient

    doc, ch = _georeferenced(tmp_path)
    seen = []
    stop = orient.on_transform_changed(lambda c, m: seen.append((c, m)))
    try:
        xyz = [[100.0, 201.0, 10.5], [102.0, 203.0, 9.5], [101.0, 202.0, 10.0]]
        rec = fd.add(ch, "polygon", xyz)
        t_old = ch.transform.copy()
        # a fourth marker that pulls the fit: update_transform moves the transform, the drawing follows
        _marker(ch, "M3", [3.0, 3.0, 0.3], [103.5, 203.2, 10.4])
        ch.update_transform()
        assert not np.allclose(ch.transform, t_old)
        got = fd.features(ch)[0]["xyz"]
        assert np.abs(_internal(ch.transform, got) - _internal(t_old, xyz)).max() < 1e-9
        assert seen == []  # the listeners are told of the motions that carry the stores (an orientation) only
        assert fd.follow(ch) is None  # nothing more: once
        # a new alignment: the drawings stay in the coordinates of the chunk, the basis takes it
        ch.meta["alignment_id"] = "new"
        before = fd.features(ch)[0]["xyz"]
        ch.markers[3].reference.location = [103.0, 202.0, 10.1]
        ch.update_transform()
        assert fd.features(ch)[0]["xyz"] == before
        assert ch.meta["drawings"]["free_basis"]["alignment"] == "new"
        assert np.allclose(ch.meta["drawings"]["free_basis"]["transform"], ch.transform)
        # aligned to another chunk: followed
        other_doc_chunk = doc.add_chunk("B")
        t_ref = geo.identity()
        t_ref[:3, 3] = [50.0, -20.0, 1.0]
        for m in ch.markers[:3]:
            mm = other_doc_chunk.add_marker(m.label)
            mm.position = np.array(m.position, float)  # the same points, placed by another transform
        other_doc_chunk.transform = t_ref
        # the reference is B: A moves into its frame
        before_t, before_xyz = ch.transform.copy(), fd.features(ch)[0]["xyz"]
        chunks.align_chunks(doc, other_doc_chunk, [ch], method="markers")
        assert not np.allclose(ch.transform, before_t)
        assert np.abs(_internal(ch.transform, fd.features(ch)[0]["xyz"]) - _internal(before_t, before_xyz)).max() \
            < 1e-9
        assert np.allclose(ch.meta["drawings"]["free_basis"]["transform"], ch.transform)
    finally:
        stop()
    assert rec["id"] == fd.features(ch)[0]["id"]


def test_orientation_moves_them_once(tmp_path):
    from pyarchinit_photogrammetry.engine import orient

    _doc, ch = _bare(tmp_path)
    ch.transform = geo.identity()
    ch.transform[:3, 3] = [1.0, 2.0, 3.0]
    xyz = [[1.0, 2.0, 3.0], [2.0, 2.0, 3.0]]
    fd.add(ch, "line", xyz)
    t_old = ch.transform.copy()
    m = geo.identity()
    m[:3, :3] = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    m[:3, 3] = [5.0, 0.0, 1.0]
    result = orient.apply(ch, m)
    assert result["moved"]["free"] == 1
    got = fd.features(ch)[0]["xyz"]
    assert np.abs(np.array(got) - geo.apply(m, np.array(xyz))).max() < 1e-12  # by M, once
    assert np.abs(_internal(ch.transform, got) - _internal(t_old, xyz)).max() < 1e-9
    assert np.allclose(ch.meta["drawings"]["free_basis"]["transform"], ch.transform)
    assert fd.follow(ch) is None


def test_a_singular_basis_is_not_followed(tmp_path):
    _doc, ch = _bare(tmp_path)
    fd.add(ch, "point", [[1.0, 2.0, 3.0]])
    bad = np.eye(4)
    bad[2, 2] = 1e-14  # nearly singular: no motion from it, the basis taken again
    ch.meta["drawings"]["free_basis"]["transform"] = bad.tolist()
    ch.transform = geo.identity()
    ch.transform[:3, 3] = [1.0, 0.0, 0.0]
    assert fd.follow(ch) is None
    assert fd.features(ch)[0]["xyz"] == [[1.0, 2.0, 3.0]]
    assert np.allclose(ch.meta["drawings"]["free_basis"]["transform"], ch.transform)


def test_levelling_keeps_them_on_the_model(tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.engine import dem, meshing

    _doc, ch = _bare(tmp_path)
    xyz = [[1.0, 0.0, 0.0], [3.0, 1.0, 0.5]]
    fd.add(ch, "line", xyz)
    t = np.radians(17.0)
    monkeypatch.setattr(meshing, "up_vector_internal", lambda c: np.array([np.sin(t), 0.0, np.cos(t)]))
    dem.ensure_level(ch)
    assert not np.allclose(ch.transform, np.eye(4))
    got = fd.features(ch)[0]["xyz"]
    assert np.abs(_internal(ch.transform, got) - np.array(xyz)).max() < 1e-9


def test_an_optimisation_that_fits_again_keeps_them_on_the_model(tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.engine import sfm

    _doc, ch = _georeferenced(tmp_path)
    xyz = [[100.0, 201.0, 10.5], [102.0, 203.0, 9.5]]
    fd.add(ch, "line", xyz)
    t_old = ch.transform.copy()

    def adjusted(chunk, *args, **kw):  # the adjustment: the georeference fitted again on a moved block
        chunk.transform = chunk.transform @ np.array(
            [[1, 0, 0, 0.05], [0, 1, 0, -0.02], [0, 0, 1, 0.03], [0, 0, 0, 1]], float)
        return 0.5

    monkeypatch.setattr(sfm, "_optimize_colmap", adjusted)
    assert ch.optimize_cameras() == 0.5
    got = fd.features(ch)[0]["xyz"]
    assert not np.allclose(ch.transform, t_old)
    assert np.abs(_internal(ch.transform, got) - _internal(t_old, xyz)).max() < 1e-9


def test_a_failed_chunk_alignment_puts_them_back(tmp_path, monkeypatch):
    """align_chunks levels an unlevelled local reference (its drawings follow) and then fails: the transform comes back
    and the drawings with it, their basis that transform."""
    from pyarchinit_photogrammetry.core.chunk import Chunk
    from pyarchinit_photogrammetry.engine import chunks, meshing

    doc, ref = _bare(tmp_path)
    other = doc.add_chunk("B")
    xyz = [[1.0, 0.0, 0.0], [3.0, 1.0, 0.5]]
    fd.add(ref, "line", xyz)
    t = np.radians(17.0)
    monkeypatch.setattr(meshing, "up_vector_internal", lambda c: np.array([np.sin(t), 0.0, np.cos(t)]))
    camera = types.SimpleNamespace(center=np.zeros(3))  # an aligned photo: the reference is levelled
    monkeypatch.setattr(Chunk, "aligned_cameras", property(lambda self: [camera]))
    with pytest.raises(ValueError, match="markers in common"):
        chunks.align_chunks(doc, ref, [other], method="markers")
    assert np.array_equal(ref.transform, np.eye(4)) and not ref.meta.get("levelled")
    assert np.abs(np.array(fd.features(ref)[0]["xyz"]) - np.array(xyz)).max() < 1e-12
    assert np.allclose(ref.meta["drawings"]["free_basis"]["transform"], ref.transform)


def test_a_drawing_made_after_a_new_alignment_does_not_move_the_older_ones(tmp_path):
    """The probe of the 1.7.2 review: draw, re-align, draw another, fit the georeference -> the first drawing stays in
    the coordinates of the chunk (ruling 7: a new alignment leaves it there), the second follows the fit."""
    _doc, ch = _georeferenced(tmp_path)
    first = [[100.0, 201.0, 10.5], [102.0, 203.0, 9.5]]
    fd.add(ch, "line", first)
    ch.meta["alignment_id"] = "new"  # a new alignment: another frame, the transform reset, no fit yet
    ch.transform = geo.identity()
    second = [[1.0, 2.0, 0.5], [3.0, 2.5, 0.2]]
    fd.add(ch, "line", second)
    assert all("basis" not in f for f in fd.features(ch))  # the bases are not part of the drawings given out
    ch.update_transform()
    assert not np.allclose(ch.transform, geo.identity())
    got = {f["name"]: f["xyz"] for f in fd.features(ch)}
    assert got["Linea 1"] == first
    assert np.abs(_internal(ch.transform, got["Linea 2"]) - np.array(second)).max() < 1e-9
    # both in the new alignment now: one basis again, and a later fit moves both
    assert all("basis" not in f for f in ch.meta["drawings"]["free"])
    assert ch.meta["drawings"]["free_basis"]["alignment"] == "new"
    t_old = ch.transform.copy()
    before = {f["name"]: f["xyz"] for f in fd.features(ch)}
    _marker(ch, "M3", [3.0, 3.0, 0.3], [103.5, 203.2, 10.4])
    ch.update_transform()
    for name, xyz in before.items():
        now = next(f["xyz"] for f in fd.features(ch) if f["name"] == name)
        assert np.abs(_internal(ch.transform, now) - _internal(t_old, xyz)).max() < 1e-9


def test_a_file_of_1_7_2_without_record_bases_is_read(tmp_path):
    _doc, ch = _georeferenced(tmp_path)
    t_old = ch.transform.copy()
    xyz = [[100.0, 201.0, 10.5], [102.0, 203.0, 9.5]]
    ch.meta["drawings"] = {"free": [{"id": "a", "layer": fd.DEFAULT_LAYER, "kind": "line", "name": "L", "xyz": xyz,
                                     "attributes": {}}],
                           "free_basis": {"alignment": ch.meta.get("alignment_id"), "transform": t_old.tolist()}}
    _marker(ch, "M3", [3.0, 3.0, 0.3], [103.5, 203.2, 10.4])
    ch.update_transform()
    now = fd.features(ch)[0]["xyz"]
    assert np.abs(_internal(ch.transform, now) - _internal(t_old, xyz)).max() < 1e-9
