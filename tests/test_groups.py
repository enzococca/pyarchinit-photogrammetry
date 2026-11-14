"""Groups of chunks (core/groups.py) and a group moved as one block by the ties (engine/ties.py, unknowns "group")."""
from __future__ import annotations

import json

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core import geometry, groups
from pyarchinit_photogrammetry.engine import ties
from pyarchinit_photogrammetry.engine.chunks import affine_inverse
from pyarchinit_photogrammetry.testing.synthetic import make_scene

CRS = "EPSG:3004"


def _doc(n=4):
    doc = ppg.Document()
    for label in "ABCDE"[:n]:
        doc.add_chunk(label)
    return doc


def test_groups_are_made_renamed_filled_and_removed():
    doc = _doc()
    g = groups.add_group(doc, chunks=["A", "B"])
    assert g["name"] == "Gruppo 1" and g["chunks"] == [0, 1]
    g2 = groups.add_group(doc, "2024", chunks=["B", "C"])  # B leaves its first group: a chunk is in one group only
    assert groups.group(doc, g["id"])["chunks"] == [0]
    assert groups.group_of(doc, 1)["name"] == "2024"
    assert [c.label for c in groups.members(doc, "2024")] == ["B", "C"]
    with pytest.raises(groups.GroupError, match="already"):
        groups.add_group(doc, "2024")
    with pytest.raises(groups.GroupError, match="already"):
        groups.rename_group(doc, g["id"], "2024")
    groups.rename_group(doc, g["id"], "2025")
    assert groups.next_name(doc) == "Gruppo 1"
    groups.assign(doc, ["D"], "2024", before="C")
    assert groups.group(doc, g2["id"])["chunks"] == [1, 3, 2]
    groups.assign(doc, ["B"], None)
    assert groups.group_of(doc, 1) is None
    gone = groups.remove_group(doc, "2025")
    assert gone["chunks"] == [0] and [c.label for c in doc.chunks] == ["A", "B", "C", "D"]
    with pytest.raises(groups.GroupError, match="no group"):
        groups.group(doc, "2025")
    groups.remove_group(doc, "2024")
    assert groups.META_KEY not in doc.meta  # no key when there are none


def test_groups_are_saved_and_chunks_gone_are_dropped_on_open(tmp_path):
    doc = _doc()
    groups.add_group(doc, "anni", chunks=["A", "C", "D"])
    doc.save(tmp_path / "p.ppg")
    doc.chunks = [c for c in doc.chunks if c.label != "C"]  # a chunk gone (as a removal leaves it)
    assert groups.group(doc, "anni")["chunks"] == [0, 3]  # read without it ...
    assert groups.stored(doc)[0]["chunks"] == [0, 2, 3]  # ... kept while the program runs (an undo brings it back)
    doc.save()
    again = ppg.Document.open(tmp_path / "p.ppg")
    assert groups.stored(again)[0]["chunks"] == [0, 3]  # dropped on open
    assert [c.label for c in again.chunks] == ["A", "B", "D"]


def test_a_project_with_groups_is_read_unchanged_without_the_groups(tmp_path):
    """An older version reads ``meta`` as a whole and the chunks apart: the groups are one key of ``meta``, the chunks
    carry nothing of them, so a version that does not know them loses no chunk and writes the key back as it was."""
    doc = _doc(3)
    groups.add_group(doc, "anni", chunks=["A", "B"])
    doc.save(tmp_path / "p.ppg")
    data = json.loads((tmp_path / "p.ppg").read_text())
    assert data["meta"]["chunk_groups"][0]["chunks"] == [0, 1]
    assert all("group" not in json.dumps(c.get("meta", {})) for c in data["chunks"])
    meta, chunks = data["meta"], data["chunks"]  # what an older version keeps, written back as it read it
    (tmp_path / "old.ppg").write_text(json.dumps({**data, "meta": meta, "chunks": chunks}))
    back = ppg.Document.open(tmp_path / "old.ppg")
    assert [c.label for c in back.chunks] == ["A", "B", "C"] and groups.group(back, "anni")["chunks"] == [0, 1]
    old = _doc(2)
    old.save(tmp_path / "plain.ppg")  # a project of before: no key, no group
    assert groups.groups_of(ppg.Document.open(tmp_path / "plain.ppg")) == []


# --------------------------------------------------------------------------- a group moved as one block
def _rot_z(deg, scale=1.0, shift=(0, 0, 0)):
    a = np.radians(deg)
    m = np.eye(4)
    m[:3, :3] = scale * np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    m[:3, 3] = shift
    return m


def _translate(t):
    m = np.eye(4)
    m[:3, 3] = t
    return m


@pytest.fixture(scope="module")
def scene():
    return make_scene(texel=0.01)


def _world(scene, xy):
    xy = np.asarray(xy, float).reshape(-1, 2)
    X, Y = xy[:, 0] + scene.origin[0], xy[:, 1] + scene.origin[1]
    return np.column_stack([X, Y, scene.world_height(X, Y)])


def _block_doc(scene):
    """A fixed; B and C a group of two chunks whose relative pose is right, both moved by the same wrong similarity
    (a year of flights placed by a GPS that was off); ties only between A and B, one tie between B and C."""
    doc = ppg.Document()
    origin = _translate(scene.origin)
    truth = {"A": origin, "B": origin @ _rot_z(20, 1.0, (0.5, 0.1, 0)), "C": origin @ _rot_z(-35, 1.0, (2.0, 0.3, 0))}
    centre = np.array(scene.origin) + (2.0, 1.5, 0.0)
    error = _translate(centre) @ _rot_z(2.0, 1.003, (0.20, -0.12, 0.05)) @ _translate(-centre)
    chunks = {}
    for label in "ABC":
        ch = doc.add_chunk(label)
        ch.crs = CRS
        ch.transform = truth[label] if label == "A" else error @ truth[label]
        chunks[label] = ch
    groups.add_group(doc, "2025", chunks=["B", "C"])

    def internal(label, w):
        return geometry.apply(affine_inverse(truth[label]), np.atleast_2d(w))[0]

    for xy in ((0.6, 0.5), (1.8, 2.4), (2.9, 0.8), (1.2, 1.6), (2.4, 1.9)):
        w = _world(scene, xy)
        ties.add_tie(doc, "A", internal("A", w), "B", internal("B", w), precision=0.005)
    w = _world(scene, (3.3, 2.0))
    ties.add_tie(doc, "B", internal("B", w), "C", internal("C", w), name="inside", precision=0.005)
    return doc, chunks, truth


def test_a_group_moves_as_one_block_keeping_its_relative_poses(scene):
    doc, chunks, truth = _block_doc(scene)
    rel_before = affine_inverse(chunks["B"].transform) @ chunks["C"].transform
    sol = ties.solve(doc, {"A": "fixed", "B": "similarity", "C": ties.GROUP})
    assert sol.unknowns[chunks["C"].key] == ties.GROUP and sol.per_chunk[chunks["C"].key]["moves_with"] == 1
    assert sol.per_chunk[chunks["C"].key]["group"] == "2025"
    out = {o["name"]: o for o in sol.left_out}
    assert out["inside"]["messages"][0]["code"] == "out_same_group"  # a tie inside the block moves nothing
    for label in "BC":
        np.testing.assert_allclose(sol.transforms[chunks[label].key], truth[label], atol=1e-6)
    changed = ties.apply(doc, sol)
    np.testing.assert_allclose(affine_inverse(chunks["B"].transform) @ chunks["C"].transform, rel_before, atol=1e-9)
    assert chunks["C"].meta["chunk_alignment"]["kind"] == ties.GROUP
    ties.restore(doc, changed)
    assert not np.allclose(chunks["C"].transform, truth["C"])


def test_a_fixed_group_holds_all_its_chunks_and_the_others_tie_to_it(scene):
    doc, chunks, truth = _block_doc(scene)
    # B and C fixed as a group (where they are), A moves onto them: A takes the error of the group
    sol = ties.solve(doc, {"B": "fixed", "C": ties.GROUP, "A": "similarity"})
    assert sol.unknowns[chunks["C"].key] == "fixed"
    np.testing.assert_allclose(sol.transforms[chunks["C"].key], chunks["C"].transform, atol=0)
    expected = chunks["B"].transform @ affine_inverse(truth["B"]) @ truth["A"]
    np.testing.assert_allclose(sol.transforms[chunks["A"].key], expected, atol=1e-6)


def test_group_unknowns_are_refused_without_a_group_or_a_chunk_of_its_own(scene):
    doc, chunks, _truth = _block_doc(scene)
    with pytest.raises(ties.TieError, match="no chunk with unknowns of its own") as err:
        ties.solve(doc, {"A": "fixed", "B": ties.GROUP, "C": ties.GROUP})
    assert "nessun chunk con incognite proprie" in err.value.italian
    groups.assign(doc, ["C"], None)
    with pytest.raises(ties.TieError, match="in no group"):
        ties.solve(doc, {"A": "fixed", "B": "similarity", "C": ties.GROUP})
    groups.assign(doc, ["C"], "2025")
    chunks["C"].crs = "LOCAL"
    with pytest.raises(ties.TieError, match="one block only in one system"):
        ties.solve(doc, {"A": "fixed", "B": "similarity", "C": ties.GROUP})
