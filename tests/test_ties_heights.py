"""Height ties (engine/stable_height) in the joint solve of engine/ties: z-only observations, counted per axis, with
their own section of the report, carried by *Aggiungi progetti*.

The chunks are the surveys of the synthetic trench of ``test_ties_solve``; a height tie is made by hand here (its
anchors are the same true world point seen from each chunk), the measure on DEMs is tested in ``test_stable_height``
and in ``test_cli_ties``."""
from __future__ import annotations

import numpy as np
import pytest
from test_ties_solve import AB, _close, _similarity, _three, _tie, _world

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.engine import stable_height, ties


@pytest.fixture(scope="module")
def scene():
    from pyarchinit_photogrammetry.testing.synthetic import make_scene

    return make_scene(texel=0.01)


def _height(doc, sa, sb, worlds, precision=0.01, name=""):
    anchors = [{"a": sa.internal(w)[0].tolist(), "b": sb.internal(w)[0].tolist(), "precision": precision}
               for w in np.atleast_2d(worlds)]
    t = stable_height.HeightTie(id="", name=name, a=sa.chunk.key, b=sb.chunk.key, anchors=anchors, dz=0.0, nmad=0.01,
                                cells=100, plane=len(anchors) == 3,
                                placed={"a": sa.chunk.transform.tolist(), "b": sb.chunk.transform.tolist()})
    return stable_height.add(doc, t)


def test_height_tie_alone_fixes_a_height_chunk(scene):
    doc, a, b, _c = _three(scene, {"b": _similarity(shift=(0.0, 0.0, 0.5))})
    h = _height(doc, a, b, _world(scene, AB[0]))
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "height"})
    _close(sol.transforms[b.chunk.key], b.truth)
    (r,) = sol.residuals
    assert r["kind"] == "height" and r["id"] == h.id and r["fitted"]
    assert abs(r["dz"]) < 1e-9 and r["d"] == pytest.approx(abs(r["dz"])) and r["dx"] == 0.0 and r["dy"] == 0.0
    assert r["precision"] == pytest.approx(0.01)
    assert sol.redundancy == 0 and sol.per_chunk[b.chunk.key]["observations"] == 1
    assert not [m for m in sol.messages if m["code"] == "remeasure_height"]


def test_plan_with_one_point_tie_and_a_height_tie(scene):
    """3 + 1 equations pass the count of a "plan" chunk (4 unknowns); the rotation about the vertical is still free
    (a height says nothing about it): the rank test names the chunk. "shift" (3) takes the same observations."""
    doc, a, b, _c = _three(scene, {"b": _similarity(shift=(0.3, -0.2, 0.4))})
    _tie(doc, a, b, _world(scene, AB[0]), precision=0.02)
    _height(doc, a, b, _world(scene, AB[2]), precision=0.005)
    with pytest.raises(ties.TieError) as err:
        ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "plan"})
    assert "do not determine" in str(err.value) and "equations" not in str(err.value)
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "shift"})
    assert sol.redundancy == 1 and all(r["d"] < 1e-3 for r in sol.residuals)
    _close(sol.transforms[b.chunk.key], b.truth, atol=1e-6)
    assert {r["kind"] for r in sol.residuals} == {"tie", "height"}


def test_plan_with_two_point_ties_and_a_plane_takes_the_heights(scene):
    """The z of a "plan" chunk from points and heights together, from its first position."""
    doc, a, b, _c = _three(scene, {"b": _similarity(deg_z=8.0, shift=(0.6, -0.4, 0.9))})
    _tie(doc, a, b, _world(scene, AB[0]), precision=0.05)
    _tie(doc, a, b, _world(scene, AB[2]), precision=0.05)
    _height(doc, a, b, _world(scene, [AB[1], AB[3], (3.0, 1.0)]), precision=0.005)
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "plan"})
    assert sol.redundancy == 6 + 3 - 4
    _close(sol.transforms[b.chunk.key], b.truth, atol=1e-6)


def test_height_only_axes_count_once(scene):
    doc, a, b, _c = _three(scene, {"b": _similarity(shift=(0.0, 0.0, 0.5))})
    _height(doc, a, b, _world(scene, AB[0]))
    with pytest.raises(ties.TieError) as err:
        ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "shift"})
    assert "1 equazioni per 3" in err.value.italian and "1 equations for 3" in str(err.value)


def test_remeasure_warning_after_a_horizontal_move(scene):
    doc, a, b, _c = _three(scene, {"b": _similarity(shift=(0.0, 0.0, 0.5))})
    h = _height(doc, a, b, _world(scene, AB[0]), name="sabbia")
    moved = b.chunk.transform.copy()
    moved[0, 3] += 0.5  # B moved in plan after the measure
    b.chunk.transform = moved
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "height"})
    (m,) = [m for m in sol.messages if m["code"] == "remeasure_height"]
    assert m["args"][0] == "sabbia" and m["chunk"] == b.chunk.key and m["args"][3] == pytest.approx(0.5, abs=1e-6)
    assert sol.residuals[0]["horizontal"] == pytest.approx(0.5, abs=1e-6)
    text = ties.report_text(sol, [], {}, [], language="it")
    assert "Quote stabili" in text and "da rimisurare" in text and h.name in text
    # a small move (under 0.10 m without a DEM) says nothing
    moved[0, 3] -= 0.45
    b.chunk.transform = moved
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "height"})
    assert not [m for m in sol.messages if m["code"] == "remeasure_height"]


def test_no_height_ties_changes_nothing(scene):
    doc, a, b, c = _three(scene)
    for xy in AB:
        _tie(doc, a, b, _world(scene, xy))
    _tie(doc, b, c, _world(scene, (3.0, 1.0)))
    unknowns = {a.chunk.key: "fixed", b.chunk.key: "similarity", c.chunk.key: "shift"}
    before = ties.solve(doc, unknowns, robust=True)
    loo_before = ties.leave_one_out(doc, unknowns, solution=before)
    h = _height(doc, a, b, _world(scene, (3.0, 2.0)))
    for heights in ([], None):
        if heights is None:  # the height tie switched off: listed as left out, nothing else
            stored = stable_height.heights_of(doc)
            stored[0].active = False
            stable_height.save_heights(doc, stored)
        after = ties.solve(doc, unknowns, robust=True, heights=heights)
        for k in before.transforms:
            np.testing.assert_allclose(after.transforms[k], before.transforms[k], rtol=0, atol=1e-12)
        assert after.residuals == before.residuals and after.redundancy == before.redundancy
        assert after.messages == before.messages and after.sigma0 == before.sigma0
        extra = [x for x in after.left_out if x not in before.left_out]
        assert extra == ([] if heights == [] else [ties._out(h.id, h.name, ("out_inactive",), reason="inactive")])
    assert ties.leave_one_out(doc, unknowns, heights=[]) == loo_before
    rep = ties.report(before, [], {}, [])
    assert rep["heights"] == [] and "Stable heights" not in ties.report_text(before, [], {}, [])


def test_height_ties_never_left_out_one_at_a_time(scene):
    doc, a, b, _c = _three(scene, {"b": _similarity(shift=(0.1, 0.2, 0.5))})
    made = [_tie(doc, a, b, _world(scene, xy)) for xy in AB]
    h = _height(doc, a, b, _world(scene, (3.0, 1.0)))
    unknowns = {a.chunk.key: "fixed", b.chunk.key: "shift"}
    sol = ties.solve(doc, unknowns)
    assert sol.redundancy == 12 + 1 - 3
    loo = ties.leave_one_out(doc, unknowns, solution=sol)
    assert set(loo) == {t.id for t in made} and h.id not in loo
    rep = ties.report(sol, [], loo, [])
    assert [r["id"] for r in rep["heights"]] == [h.id] and h.id not in {r["id"] for r in rep["ties"]}


def test_append_carries_height_ties(tmp_path):
    from test_append import _target, make_tied_source

    from pyarchinit_photogrammetry.core.append import append_projects

    src = make_tied_source(tmp_path / "src")
    source = ppg.Document.open(src)
    ca, cb, cc = source.chunks
    for x, y, name in ((ca, cb, "sabbia"), (cb, cc, "strada")):
        stable_height.add(source, stable_height.HeightTie(
            id="", name=name, a=x.key, b=y.key, anchors=[{"a": [1.0, 2.0, 3.0], "b": [1.5, 2.5, 3.5],
                                                          "precision": 0.01}], dz=0.2, nmad=0.01, cells=50))
    source.save()
    doc = _target(tmp_path, "X")
    mine = stable_height.add(doc, stable_height.HeightTie(id="", name="sabbia", a=doc.chunks[0].key,
                                                          b=doc.add_chunk("Y").key, anchors=[], dz=0.0, nmad=0.0,
                                                          cells=0))
    from pyarchinit_photogrammetry.core.append import _restore_ties, ties_state

    before = ties_state(doc)
    (res,) = append_projects(doc, [src], chunks=["A", "B"])
    a, b = res.chunks
    assert res.heights == 1 and res.heights_dropped == ["strada"]
    got = {t.name: t for t in stable_height.heights_of(doc)}
    assert set(got) == {"sabbia", "sabbia (legami)"} and got["sabbia"].id == mine.id
    new = got["sabbia (legami)"]
    assert (new.a, new.b) == (a.key, b.key) and new.anchors[0]["b"] == [1.5, 2.5, 3.5] and new.id != mine.id
    # undone with the ties: both lists back as they were
    _restore_ties(doc, before)
    assert [t.name for t in stable_height.heights_of(doc)] == ["sabbia"]
    assert len(ties.ties_of(doc)) == len(ties.ties_of(ppg.Document.open(doc.path)))
    # B alone: the height tie A-B is said, B-C too (one end on B)
    (res2,) = append_projects(doc, [src], chunks=["B"])
    assert res2.heights == 0 and sorted(res2.heights_dropped) == ["sabbia", "strada"]
