"""The joint solve of several chunks from their ties (engine/ties.py ``solve``/``apply``).

The chunks are copies of the synthetic trench (``testing.synthetic.make_scene``): points of its surface seen by three
"surveys", each with its internal frame, put in the world by a transform that is a little wrong (as the GPS of a
drone puts it); the solve must find the true transforms back."""
from __future__ import annotations

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core import geometry
from pyarchinit_photogrammetry.engine import ties
from pyarchinit_photogrammetry.engine.chunks import affine_inverse, align_chunks
from pyarchinit_photogrammetry.testing.synthetic import make_scene

CRS = "EPSG:3004"


@pytest.fixture(scope="module")
def scene():
    return make_scene(texel=0.01)


def _similarity(deg_z=0.0, deg_x=0.0, scale=1.0, shift=(0.0, 0.0, 0.0)) -> np.ndarray:
    az, ax = np.radians(deg_z), np.radians(deg_x)
    rz = np.array([[np.cos(az), -np.sin(az), 0], [np.sin(az), np.cos(az), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, np.cos(ax), -np.sin(ax)], [0, np.sin(ax), np.cos(ax)]])
    m = np.eye(4)
    m[:3, :3] = scale * rz @ rx
    m[:3, 3] = shift
    return m


def _about(m: np.ndarray, centre) -> np.ndarray:
    """``m`` applied about ``centre`` (world)."""
    c = np.asarray(centre, float)
    return geometry_translate(c) @ m @ geometry_translate(-c)


def geometry_translate(t) -> np.ndarray:
    m = np.eye(4)
    m[:3, 3] = t
    return m


def _world(scene, xy) -> np.ndarray:
    """World points on the trench surface at local ``xy`` (metres from the corner of the trench)."""
    xy = np.asarray(xy, float).reshape(-1, 2)
    X, Y = xy[:, 0] + scene.origin[0], xy[:, 1] + scene.origin[1]
    return np.column_stack([X, Y, scene.world_height(X, Y)])


class Survey:
    """A chunk whose true transform is ``truth`` and whose transform is ``error @ truth`` (about the trench)."""

    def __init__(self, doc, label, truth, error=None, crs=CRS, centre=None):
        self.chunk = doc.add_chunk(label)
        self.truth = np.asarray(truth, float)
        err = np.eye(4) if error is None else _about(error, centre)
        self.chunk.transform = err @ self.truth
        self.chunk.crs = crs

    def internal(self, world) -> np.ndarray:
        return geometry.apply(affine_inverse(self.truth), np.atleast_2d(world))


def _truth(scene, deg=0.0, shift=(0.0, 0.0, 0.0), scale=1.0) -> np.ndarray:
    """A true transform: an internal frame of the survey (turned, scaled, near zero) to the world."""
    inner = _similarity(deg_z=deg, deg_x=0.0, scale=scale, shift=shift)
    return geometry_translate(scene.origin) @ inner


def _tie(doc, sa, sb, world, **kw):
    return ties.add_tie(doc, sa.chunk, sa.internal(world)[0], sb.chunk, sb.internal(world)[0], **kw)


def _close(a, b, atol=1e-6):
    np.testing.assert_allclose(np.asarray(a, float), np.asarray(b, float), rtol=0, atol=atol)


def _three(scene, errors=None):
    """Three surveys of the trench: A (fixed, right), B and C moved by known similarities."""
    doc = ppg.Document()
    centre = scene.origin + np.array([2.0, 1.5, 0.0])
    a = Survey(doc, "2024", _truth(scene))
    errors = errors or {}
    b = Survey(doc, "2025", _truth(scene, 20.0, (1.0, -0.5, 0.2), 1.0),
               errors.get("b", _similarity(deg_z=3.0, deg_x=0.4, scale=1.01, shift=(0.4, -0.3, 0.6))), centre=centre)
    c = Survey(doc, "2026", _truth(scene, -35.0, (-2.0, 1.0, -0.1), 0.5),
               errors.get("c", _similarity(deg_z=-2.0, scale=0.99, shift=(-0.5, 0.2, -0.4))), centre=centre)
    return doc, a, b, c


AB = [(0.6, 0.7), (1.8, 0.5), (1.5, 2.4), (0.5, 2.2)]
BC = [(2.6, 0.6), (3.5, 1.2), (3.2, 2.5)]
AC = [(3.6, 0.3)]


def test_similarity_recovers_known_moves(scene):
    doc, a, b, c = _three(scene)
    for xy in AB:
        _tie(doc, a, b, _world(scene, xy))
    for xy in BC:
        _tie(doc, b, c, _world(scene, xy))
    for xy in AC:
        _tie(doc, a, c, _world(scene, xy))
    before = {ch.key: ch.transform.copy() for ch in doc.chunks}
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "similarity", c.chunk.key: "similarity"})
    assert {ch.key: ch.transform.tolist() for ch in doc.chunks} == {k: v.tolist() for k, v in before.items()}
    assert sol.crs == CRS
    assert sol.redundancy == 3 * 8 - 14
    assert len(sol.residuals) == 8 and all(r["d"] < 1e-3 for r in sol.residuals)
    assert sol.rms < 1e-3
    _close(sol.transforms[a.chunk.key], a.truth)
    _close(sol.transforms[b.chunk.key], b.truth)
    _close(sol.transforms[c.chunk.key], c.truth)
    pc = sol.per_chunk[b.chunk.key]
    assert pc["kind"] == "similarity" and 2.5 < pc["rotation_deg"] < 3.5 and abs(pc["scale"] - 1 / 1.01) < 1e-6
    assert sol.per_chunk[a.chunk.key]["kind"] == "fixed" and sol.per_chunk[a.chunk.key]["rotation_deg"] == 0
    r = sol.residuals[0]
    assert {"id", "name", "kind", "chunks", "dx", "dy", "dz", "d", "precision", "normalised"} <= set(r)


def test_plan_with_two_ties_when_tilt_and_scale_are_right(scene):
    doc, a, b, _c = _three(scene, {"b": _similarity(deg_z=12.0, shift=(0.8, -0.6, 0.35))})
    _tie(doc, a, b, _world(scene, AB[0]))
    _tie(doc, a, b, _world(scene, AB[2]))
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "plan"})
    assert sol.redundancy == 6 - 4
    _close(sol.transforms[b.chunk.key], b.truth)
    assert abs(sol.per_chunk[b.chunk.key]["rotation_deg"] - 12.0) < 1e-6
    assert abs(sol.per_chunk[b.chunk.key]["scale"] - 1.0) < 1e-12


def test_plan_keeps_tilt_and_scale(scene):
    """With a tilt in the error, "plan" turns about the vertical only: the tilt stays, the residuals say it."""
    doc, a, b, _c = _three(scene, {"b": _similarity(deg_z=5.0, deg_x=1.0, shift=(0.3, 0.2, 0.1))})
    for xy in AB:
        _tie(doc, a, b, _world(scene, xy))
    old = b.chunk.transform.copy()
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "plan"})
    new = sol.transforms[b.chunk.key]
    delta = new @ affine_inverse(old)
    assert abs(delta[2, 2] - 1.0) < 1e-12 and np.allclose(delta[2, :2], 0) and np.allclose(delta[:2, 2], 0)
    assert abs(np.linalg.det(delta[:3, :3]) - 1.0) < 1e-12
    assert max(r["d"] for r in sol.residuals) > 0.005  # the tilt is not taken away


def test_similarity_with_two_ties_is_refused(scene):
    doc, a, b, _c = _three(scene)
    _tie(doc, a, b, _world(scene, AB[0]))
    _tie(doc, a, b, _world(scene, AB[2]))
    with pytest.raises(ties.TieError) as err:
        ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "similarity"})
    text = str(err.value)
    assert "2025" in text and "6 equations" in text and "7 unknowns" in text
    assert "2025" in err.value.italian and "6 equazioni" in err.value.italian


def test_rigid_with_points_on_a_line_is_refused(scene):
    doc, a, b, _c = _three(scene)
    for x in (0.5, 1.5, 2.5):
        _tie(doc, a, b, _world(scene, (x, 0.5)) * [1, 1, 0] + [0, 0, 100.0])  # a straight line
    with pytest.raises(ties.TieError) as err:
        ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "rigid"})
    assert "2025" in str(err.value) and "line" in str(err.value)


def test_chunks_not_linked_to_the_fixed_system_are_refused(scene):
    doc, a, b, c = _three(scene)
    for xy in BC:
        _tie(doc, b, c, _world(scene, xy))
    _tie(doc, b, c, _world(scene, AB[0]))
    with pytest.raises(ties.TieError) as err:
        ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "rigid", c.chunk.key: "rigid"})
    assert "2025" in str(err.value) and "2026" in str(err.value)


def test_nothing_fixes_the_system(scene):
    doc, a, b, _c = _three(scene)
    for xy in AB:
        _tie(doc, a, b, _world(scene, xy))
    with pytest.raises(ties.TieError) as err:
        ties.solve(doc, {a.chunk.key: "rigid", b.chunk.key: "rigid"})
    assert "fixed" in str(err.value) and "fisso" in err.value.italian


def test_references_fix_the_system_without_a_fixed_chunk(scene):
    doc, a, b, c = _three(scene)
    # known points (GNSS stakes) on chunk B, a reference marker of chunk C, ties B-C
    for xy in AB[:3]:
        w = _world(scene, xy)[0]
        ties.add_known(doc, b.chunk, b.internal(w)[0], w, precision=0.02)
    for xy in BC:
        _tie(doc, b, c, _world(scene, xy))
    w = _world(scene, AB[3])[0]
    m = c.chunk.add_marker("GCP 9", c.internal(w)[0], reference=w)
    m.reference.accuracy = [0.01, 0.01, 0.03]
    sol = ties.solve(doc, {b.chunk.key: "similarity", c.chunk.key: "similarity"})
    _close(sol.transforms[b.chunk.key], b.truth)
    _close(sol.transforms[c.chunk.key], c.truth)
    kinds = sorted(r["kind"] for r in sol.residuals)
    assert kinds == ["known"] * 3 + ["reference"] + ["tie"] * 3  # each known point once
    assert sol.redundancy == 3 * 7 - 14
    ref = next(r for r in sol.residuals if r["kind"] == "reference")
    assert ref["name"] == "GCP 9" and ref["precision"] == pytest.approx(0.03)
    with pytest.raises(ties.TieError):  # without the references C is not fixed by anything... nor B by known
        ties.solve(doc, {b.chunk.key: "similarity", c.chunk.key: "similarity"}, references=False,
                   ties=[t for t in ties.ties_of(doc) if t.b is not None])
    assert a.chunk.key not in sol.transforms


def test_a_tie_between_two_referenced_markers_counts_each_reference_once(scene):
    doc, a, b, _c = _three(scene)
    for xy in AB:
        _tie(doc, a, b, _world(scene, xy))
    w = _world(scene, BC[0])[0]
    ma = a.chunk.add_marker("P5", a.internal(w)[0], reference=w)
    mb = b.chunk.add_marker("P5", b.internal(w)[0], reference=w)
    ties.link_markers(doc, a.chunk, ma, b.chunk, mb, precision=0.01)
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "rigid"})
    named = [r for r in sol.residuals if r["name"] == "P5"]
    assert sorted(r["kind"] for r in named) == ["reference", "reference", "tie"]
    fitted = [r for r in sol.residuals if r["fitted"]]
    assert sum(1 for r in named if r["fitted"]) == 2  # the reference of the fixed chunk only reported
    assert sol.redundancy == 3 * len(fitted) - 6


def test_height_only(scene):
    doc, a, b, _c = _three(scene, {"b": _similarity(shift=(0.0, 0.0, -0.7))})
    for xy in AB[:2]:
        _tie(doc, a, b, _world(scene, xy))
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "height"})
    _close(sol.transforms[b.chunk.key], b.truth)
    pc = sol.per_chunk[b.chunk.key]
    assert pc["kind"] == "height" and pc["shift"] == pytest.approx(0.7, abs=1e-9) and pc["rotation_deg"] == 0
    assert sol.redundancy == 6 - 1


def test_shift_only(scene):
    doc, a, b, _c = _three(scene, {"b": _similarity(shift=(0.3, -0.2, 0.1))})
    _tie(doc, a, b, _world(scene, AB[1]))
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "shift"})
    _close(sol.transforms[b.chunk.key], b.truth)
    assert sol.redundancy == 0


def test_robust_downweights_a_wrong_tie(scene):
    doc, a, b, _c = _three(scene)
    pts = [(0.6, 0.7), (1.8, 0.5), (1.5, 2.4), (0.5, 2.2), (3.0, 1.0), (2.5, 2.7)]
    made = []
    for i, xy in enumerate(pts):
        w = _world(scene, xy)[0]
        wb = w + ([1.0, 0.0, 0.0] if i == 2 else 0.0)  # clicked on the wrong stake in B
        made.append(ties.add_tie(doc, a.chunk, a.internal(w)[0], b.chunk, b.internal(wb)[0], precision=0.02))
    plain = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "similarity"})
    robust = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "similarity"}, robust=True)

    def error(sol):
        probe = b.internal(_world(scene, [(x, y) for x in (0.5, 2, 3.5) for y in (0.5, 2.5)]))
        return np.max(np.linalg.norm(geometry.apply(sol.transforms[b.chunk.key], probe)
                                     - geometry.apply(b.truth, probe), axis=1))

    assert error(plain) > 0.05 and error(robust) < 0.2 * error(plain)
    wrong = next(r for r in robust.residuals if r["id"] == made[2].id)
    assert wrong["weight"] < 0.2 and wrong["d"] > 0.9
    assert all(r["weight"] == 1.0 for r in plain.residuals)
    assert robust.robust and not plain.robust


def test_ties_left_out_are_listed(scene):
    doc, a, b, c = _three(scene)
    for xy in AB:
        _tie(doc, a, b, _world(scene, xy))
    ma, mb = a.chunk.add_marker("nessuna posizione"), b.chunk.add_marker("nessuna posizione")
    no_pos = ties.link_markers(doc, a.chunk, ma, b.chunk, mb)
    off = _tie(doc, a, b, _world(scene, BC[0]))
    all_ties = ties.ties_of(doc)
    for t in all_ties:
        if t.id == off.id:
            t.active = False
    ties.save_ties(doc, all_ties)
    other = _tie(doc, a, c, _world(scene, BC[1]))
    broken = _tie(doc, a, b, _world(scene, BC[2]))
    b.chunk.remove_markers([broken.b[1]])
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "rigid"})
    reasons = {x["id"]: x["reason"] for x in sol.left_out}
    assert set(reasons) == {no_pos.id, off.id, other.id, broken.id}
    assert "position" in reasons[no_pos.id] and "inactive" in reasons[off.id]
    assert "2026" in reasons[other.id] and "removed" in reasons[broken.id]
    assert len(sol.residuals) == 4
    stored = {d["id"]: d for d in doc.meta["ties"]}
    assert stored[broken.id]["active"]  # a solve stores nothing


def test_chunks_in_a_local_frame_and_utm_are_refused_or_converted(scene):
    # converted: a chunk in a local frame tied to a chunk in a projected system goes into that system, as
    # align_chunks places it (markers with the same labels): same CRS, same transform
    doc, a, b, _c = _three(scene)
    b.chunk.crs = "LOCAL"
    b.chunk.transform = _similarity(deg_z=70.0, shift=(3.0, 4.0, 0.0))  # its own local frame
    for i, xy in enumerate(AB):
        w = _world(scene, xy)[0]
        ties.add_tie(doc, a.chunk, a.internal(w)[0], b.chunk, b.internal(w)[0], name=f"T{i}")
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "similarity"})
    assert sol.crs == CRS
    _close(sol.transforms[b.chunk.key], b.truth)
    ties.apply(doc, sol)
    assert b.chunk.crs == CRS

    twin = ppg.Document()
    ta, tb = twin.add_chunk("2024"), twin.add_chunk("2025")
    ta.transform, ta.crs = a.chunk.transform.copy(), CRS
    tb.transform, tb.crs = _similarity(deg_z=70.0, shift=(3.0, 4.0, 0.0)), "LOCAL"
    for m in a.chunk.markers:
        ta.add_marker(m.label, m.position)
    for m in b.chunk.markers:
        tb.add_marker(m.label, m.position)
    align_chunks(twin, ta, [tb], method="markers")
    assert tb.crs == b.chunk.crs
    _close(tb.transform, b.chunk.transform, atol=1e-6)

    # refused: two fixed chunks, one local and one projected (as merge_chunks refuses them)
    doc, a, b, _c = _three(scene)
    b.chunk.crs = "LOCAL"
    for xy in AB:
        _tie(doc, a, b, _world(scene, xy))
    with pytest.raises(ties.TieError) as err:
        ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "fixed"})
    assert "are not in the same frame" in str(err.value) and "LOCAL" in str(err.value)

    # refused: a known point in a local frame while the solve is in a projected system
    doc, a, b, c = _three(scene)
    c.chunk.crs = "LOCAL"
    for xy in AB:
        _tie(doc, a, c, _world(scene, xy))
    w = _world(scene, BC[0])[0]
    ties.add_known(doc, c.chunk, c.internal(w)[0], [1.0, 2.0, 3.0])
    with pytest.raises(ties.TieError) as err:
        ties.solve(doc, {a.chunk.key: "fixed", c.chunk.key: "similarity"})
    assert "are not in the same frame" in str(err.value)


def test_two_projected_systems_are_converted():
    """A chunk in UTM 32N tied to a fixed chunk in UTM 33N: its points converted, its CRS and references too."""
    from pyarchinit_photogrammetry.core import crs as crs_mod

    doc = ppg.Document()
    a, b = doc.add_chunk("33"), doc.add_chunk("32")
    a.crs, b.crs = "EPSG:32633", "EPSG:32632"
    origin33 = np.array([258000.0, 4642000.0, 50.0])
    a.transform = geometry_translate(origin33)
    rng = np.random.default_rng(3)
    world33 = origin33 + rng.uniform([-20, -20, -2], [20, 20, 2], (5, 3))
    world32 = crs_mod.transform_points(world33, "EPSG:32633", "EPSG:32632")
    origin32 = np.round(world32.mean(0))
    b.transform = geometry_translate(origin32 + [0.5, -0.4, 0.3])  # GPS a little off
    for i, (w33, w32) in enumerate(zip(world33, world32)):
        ties.add_tie(doc, a, w33 - origin33, b, w32 - origin32, name=f"P{i}")
    ref = b.add_marker("R", world32[0] - origin32 + [0, 0, 0.1], reference=world32[0])
    ref.reference.enabled = True
    sol = ties.solve(doc, {a.key: "fixed", b.key: "similarity"}, references=False)
    assert sol.crs == "EPSG:32633"
    internal32 = world32 - origin32
    placed = geometry.apply(sol.transforms[b.key], internal32)
    assert np.max(np.linalg.norm(placed - world33, axis=1)) < 0.005
    ties.apply(doc, sol)
    assert b.crs == "EPSG:32633"
    np.testing.assert_allclose(b.marker_by_key(ref.key).reference.location, world33[0], atol=0.01)


def test_apply_then_restore(scene):
    doc, a, b, c = _three(scene)
    for xy in AB:
        _tie(doc, a, b, _world(scene, xy))
    for xy in BC:
        _tie(doc, b, c, _world(scene, xy))
    saved = {ch.key: (ch.transform.copy(), ch.crs) for ch in doc.chunks}
    stored = [t.to_dict() for t in ties.ties_of(doc)]
    markers = {ch.key: len(ch.markers) for ch in doc.chunks}
    unknowns = {a.chunk.key: "fixed", b.chunk.key: "rigid", c.chunk.key: "similarity"}
    sol = ties.solve(doc, unknowns)
    changed = ties.apply(doc, sol)
    assert set(changed["chunks"]) == {b.chunk.key, c.chunk.key}
    _close(b.chunk.transform, sol.transforms[b.chunk.key])
    assert doc.meta["ties_report"]["redundancy"] == sol.redundancy
    assert b.chunk.meta["chunk_alignment"]["method"] == "ties"
    again = ties.solve(doc, unknowns)  # a second solve on the result: nothing to move, no new markers
    assert {ch.key: len(ch.markers) for ch in doc.chunks} == markers
    assert again.per_chunk[b.chunk.key]["shift"] < 1e-6
    with pytest.raises(ties.TieError):  # the chunks moved since ``sol``
        ties.apply(doc, sol)
    ties.restore(doc, changed)
    for ch in doc.chunks:
        assert ch.transform.tolist() == saved[ch.key][0].tolist() and ch.crs == saved[ch.key][1]
    assert [t.to_dict() for t in ties.ties_of(doc)] == stored
    assert "ties_report" not in doc.meta
    assert "chunk_alignment" not in b.chunk.meta


def test_unknown_kinds_are_refused(scene):
    doc, a, b, _c = _three(scene)
    _tie(doc, a, b, _world(scene, AB[0]))
    with pytest.raises(ties.TieError):
        ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "affine"})


def test_a_local_system_keeps_the_moved_chunks_level_and_check_points_only_report(scene):
    doc, a, b, _c = _three(scene, {"b": _similarity(shift=(0.3, -0.2, 0.1))})
    a.chunk.crs = b.chunk.crs = "LOCAL"
    for xy in AB[:2]:
        _tie(doc, a, b, _world(scene, xy))
    w = _world(scene, AB[2])[0]
    check = b.chunk.add_marker("C1", b.internal(w)[0], reference=w + [0.0, 0.0, 0.02])
    check.checkpoint = True
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "shift"})
    _close(sol.transforms[b.chunk.key], b.truth)
    entry = next(r for r in sol.residuals if r["name"] == "C1")
    assert entry["check"] and not entry["fitted"] and entry["dz"] == pytest.approx(-0.02, abs=1e-9)
    assert sol.redundancy == 6 - 3 and sol.crs == "LOCAL"
    changed = ties.apply(doc, sol)
    assert b.chunk.meta["levelled"] is True
    ties.restore(doc, changed)
    assert "levelled" not in b.chunk.meta


def test_a_known_tie_left_out_does_not_come_back_as_a_reference(scene):
    doc, a, b, _c = _three(scene)
    for xy in AB:
        _tie(doc, a, b, _world(scene, xy))
    w = _world(scene, BC[0])[0]
    known = ties.add_known(doc, b.chunk, b.internal(w)[0], w + [0.0, 0.0, 5.0])  # a wrong height
    chosen = [t for t in ties.ties_of(doc) if t.id != known.id]
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "similarity"}, ties=chosen)
    assert all(r["kind"] == "tie" for r in sol.residuals)
    _close(sol.transforms[b.chunk.key], b.truth)
