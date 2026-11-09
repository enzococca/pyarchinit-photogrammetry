"""The checks of a solve before it is applied (engine/ties.py): distances between pairs of ties, leave one out,
plausibility of the moves, a straight axis, the report (structured, Italian and English).

Synthetic chunks of the trench as in ``test_ties_solve``."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from test_ties_solve import AB, _similarity, _three, _tie, _world

from pyarchinit_photogrammetry.engine import ties

EN_JSON = Path(ties.__file__).parents[1] / "gui" / "translations" / "en.json"
FIVE = AB + [(3.0, 1.0)]


@pytest.fixture(scope="module")
def scene():
    from pyarchinit_photogrammetry.testing.synthetic import make_scene

    return make_scene(texel=0.01)


def _five_with_a_wrong_one(scene, off=(1.0, 0.0, 0.0), precision=0.01):
    """A fixed, B moved (3 degrees, x1.01): five ties, the fourth with its end in B moved by ``off``."""
    doc, a, b, c = _three(scene)
    made = []
    for i, xy in enumerate(FIVE):
        w = _world(scene, xy)[0]
        wb = w + np.asarray(off, float) if i == 3 else w
        made.append(ties.add_tie(doc, a.chunk, a.internal(w)[0], b.chunk, b.internal(wb)[0], precision=precision))
    return doc, a, b, c, made


def test_distance_check_names_the_wrong_tie(scene):
    doc, a, b, _c, made = _five_with_a_wrong_one(scene)
    checks = ties.distance_checks(doc)
    assert len(checks) == 10  # every pair of the five ties between the same two chunks
    wrong = made[3].id
    for ch in checks:
        assert {"ties", "chunks", "distance_a", "distance_b", "difference", "tolerance", "ok", "scale"} <= set(ch)
        assert ch["tolerance"] == pytest.approx(3 * np.hypot(0.01, 0.01))
        assert ch["ok"] == (wrong not in ch["ties"])
    # B's scale is 1% off: the ratio of the distances is taken out (five ties: it can be measured)
    assert checks[0]["scale"] == pytest.approx(1.01, rel=2e-3)
    assert ties.suspects(checks) == [wrong]
    json.dumps(checks)


def test_two_ties_cannot_say_which_one_is_wrong(scene):
    doc, a, b, _c, made = _five_with_a_wrong_one(scene)
    checks = ties.distance_checks(doc, ties=[made[0].id, made[3].id])
    assert len(checks) == 1 and not checks[0]["ok"] and checks[0]["scale"] == 1.0
    assert sorted(ties.suspects(checks)) == sorted([made[0].id, made[3].id])


def test_distance_check_of_known_points(scene):
    """Stakes with known coordinates on one chunk: the distances between them against those of their coordinates."""
    doc, a, b, _c = _three(scene)
    made = []
    for i, xy in enumerate(FIVE):
        w = _world(scene, xy)[0]
        known = w + ([0.0, 0.5, 0.0] if i == 1 else 0.0)
        made.append(ties.add_known(doc, a.chunk, a.internal(w)[0], known, precision=0.01))
    checks = ties.distance_checks(doc)
    assert len(checks) == 10 and all(ch["chunks"][1] is None for ch in checks)
    assert ties.suspects(checks) == [made[1].id]


def test_leave_one_out_moves_the_wrong_tie_most(scene):
    doc, a, b, _c, made = _five_with_a_wrong_one(scene)
    unknowns = {a.chunk.key: "fixed", b.chunk.key: "similarity"}
    loo = ties.leave_one_out(doc, unknowns)
    assert set(loo) == {t.id for t in made}
    worst = max(loo, key=loo.get)
    assert worst == made[3].id and loo[worst] == pytest.approx(1.0, abs=0.05)
    assert all(v < 0.6 for k, v in loo.items() if k != worst)
    # the solve with every tie is not changed by the checks
    assert len(ties.ties_of(doc)) == 5 and all(t.active for t in ties.ties_of(doc))


def test_zero_redundancy_says_no_check(scene):
    doc, a, b, _c = _three(scene)
    t = _tie(doc, a, b, _world(scene, AB[0]))
    unknowns = {a.chunk.key: "fixed", b.chunk.key: "shift"}
    sol = ties.solve(doc, unknowns)
    assert sol.redundancy == 0
    assert ties.leave_one_out(doc, unknowns) == {}
    checks = ties.distance_checks(doc)
    assert checks == []
    rep = ties.report(sol, checks, {}, [])
    assert rep["redundancy"] == 0
    assert any(m["code"] == "no_check" for m in rep["messages"])
    text = ties.report_text(sol, checks, {}, [])
    assert "no check" in text.lower()
    assert "nessun controllo" in ties.report_text(sol, checks, {}, [], language="it").lower()
    assert t.id in [r["id"] for r in rep["residuals"]]


def test_leave_one_out_without_redundancy_left_is_skipped(scene):
    """Three ties for a similarity: leaving one out leaves 6 equations for 7 unknowns - no value for it."""
    doc, a, b, _c = _three(scene)
    for xy in AB[:3]:
        _tie(doc, a, b, _world(scene, xy))
    assert ties.leave_one_out(doc, {a.chunk.key: "fixed", b.chunk.key: "similarity"}) == {}
    # four ties for a rigid chunk: three left after one is out, redundancy 3 - a value each
    doc, a, b, _c = _three(scene, {"b": _similarity(deg_z=3.0)})
    made = [_tie(doc, a, b, _world(scene, xy)) for xy in AB]
    loo = ties.leave_one_out(doc, {a.chunk.key: "fixed", b.chunk.key: "rigid"})
    assert set(loo) == {t.id for t in made} and max(loo.values()) < 1e-4


def test_plausibility_warns_on_rotation_and_scale(scene):
    doc, a, b, _c = _three(scene, {"b": _similarity(deg_z=8.0, scale=1.03, shift=(0.4, -0.3, 0.6))})
    for xy in FIVE:
        _tie(doc, a, b, _world(scene, xy))
    unknowns = {a.chunk.key: "fixed", b.chunk.key: "similarity"}
    sol = ties.solve(doc, unknowns)
    warns = ties.plausibility(doc, sol)
    codes = sorted(w["code"] for w in warns)
    assert codes == ["implausible_rotation", "implausible_scale"]
    assert all(w["chunk"] == b.chunk.key for w in warns)
    assert "8.0" in ties.render(warns[0]) or "8.0" in ties.render(warns[1])
    assert "8,0" in " ".join(ties.render(w, "it") for w in warns)
    # small moves: nothing
    doc, a, b, _c = _three(scene)
    for xy in FIVE:
        _tie(doc, a, b, _world(scene, xy))
    assert ties.plausibility(doc, ties.solve(doc, unknowns)) == []
    # a chunk in a local frame: no limits at all
    doc, a, b, _c = _three(scene, {"b": _similarity(deg_z=40.0, scale=2.0)})
    b.chunk.crs = "LOCAL"
    for xy in FIVE:
        _tie(doc, a, b, _world(scene, xy))
    assert ties.plausibility(doc, ties.solve(doc, unknowns)) == []


def test_axis_check_angle(scene):
    doc, a, b, _c = _three(scene)
    for xy in FIVE:
        _tie(doc, a, b, _world(scene, xy))
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "similarity"})
    p, q = _world(scene, [(0.5, 1.0), (3.5, 1.0)])
    axes = {a.chunk.key: (a.internal(p)[0], a.internal(q)[0]), b.chunk.key: (b.internal(p)[0], b.internal(q)[0])}
    res = ties.axis_check(doc, sol, axes)
    pair = res["pairs"][0]
    assert pair["chunks"] == [a.chunk.key, b.chunk.key]
    assert pair["angle_deg"] < 1e-4 and pair["ok"] and res["ok"]
    assert pair["angle_before_deg"] == pytest.approx(3.0, abs=0.5)  # B was turned 3 degrees before the solve
    # the axis of B drawn 16 degrees off (a chunk that bends the hull): it says so, in plan
    th = np.radians(16.0)
    d = q - p
    q16 = p + np.array([d[0] * np.cos(th) - d[1] * np.sin(th), d[0] * np.sin(th) + d[1] * np.cos(th), d[2]])
    axes[b.chunk.key] = (b.internal(p)[0], b.internal(q16)[0])
    res = ties.axis_check(doc, sol, axes)
    pair = res["pairs"][0]
    assert pair["angle_deg"] == pytest.approx(16.0, abs=0.5) and pair["plan_deg"] == pytest.approx(16.0, abs=0.5)
    assert not pair["ok"] and not res["ok"]
    # an axis given the other way round is the same axis
    axes[b.chunk.key] = (b.internal(q)[0], b.internal(p)[0])
    assert ties.axis_check(doc, sol, axes)["pairs"][0]["angle_deg"] < 1e-4
    with pytest.raises(ties.TieError):
        ties.axis_check(doc, sol, {a.chunk.key: axes[a.chunk.key]})


SPREAD = [(0.5, 0.5), (3.5, 0.6), (3.4, 2.5), (0.6, 2.4)]


def test_a_precision_inherited_from_the_linked_chunk_names_it(scene):
    """N1: A fixed; B tied to A by three well spread ties, C tied only to B by three well spread ties (5 cm each).
    C's own ties place it well; its precision comes through B: the warning says so and does not ask for ties far
    apart (they are)."""
    doc, a, b, c = _three(scene)
    for xy in SPREAD[:3]:
        _tie(doc, a, b, _world(scene, xy), precision=0.05)
    for xy in SPREAD[1:]:
        _tie(doc, b, c, _world(scene, xy), precision=0.05)
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "rigid", c.chunk.key: "rigid"})
    pb, pc = sol.per_chunk[b.chunk.key], sol.per_chunk[c.chunk.key]
    assert pb["precision_m"] < pb["precision_limit"] and pb["precision_cause"] is None
    assert pc["precision_own_m"] < pc["precision_limit"] < pc["precision_m"]
    assert pc["precision_cause"] == "inherited" and pc["precision_through"] == [b.chunk.key]
    msgs = [m for m in sol.messages if m["code"].startswith("precision_")]
    assert [(m["code"], m["chunk"]) for m in msgs] == [("precision_inherited", c.chunk.key)]
    text = ties.render(msgs[0])
    assert "2026" in text and "2025" in text and "fixed chunk" in text and "far from" not in text
    assert "punti noti" in ties.render(msgs[0], "it")
    # the English warnings are the messages rendered
    assert [ties.render(m) for m in sol.messages] == sol.warnings


def test_a_chunk_whose_own_ties_are_on_a_line_is_told_to_spread_them(scene):
    doc, a, b, _c = _three(scene)
    for i, x in enumerate((0.5, 1.5, 2.5, 3.5)):
        w = _world(scene, (x, 1.0))[0] * [1, 1, 0] + [0, 0, 100.0] + ([0.0, 0.01, 0.0] if i == 2 else 0.0)
        ties.add_tie(doc, a.chunk, a.internal(w)[0], b.chunk, b.internal(w)[0], precision=0.005)
    sol = ties.solve(doc, {a.chunk.key: "fixed", b.chunk.key: "rigid"})
    pc = sol.per_chunk[b.chunk.key]
    assert pc["precision_cause"] == "spread" and pc["precision_own_m"] > pc["precision_limit"]
    m = next(m for m in sol.messages if m["chunk"] == b.chunk.key)
    assert m["code"] == "precision_spread" and "far from the others" in ties.render(m)


def test_report_structured_and_in_two_languages(scene):
    doc, a, b, _c, made = _five_with_a_wrong_one(scene)
    unknowns = {a.chunk.key: "fixed", b.chunk.key: "similarity"}
    sol = ties.solve(doc, unknowns)
    checks = ties.distance_checks(doc)
    loo = ties.leave_one_out(doc, unknowns)
    warns = ties.plausibility(doc, sol)
    rep = ties.report(sol, checks, loo, warns)
    json.dumps(rep)
    assert rep["suspects"] == [made[3].id]
    codes = [m["code"] for m in rep["messages"]]
    assert "distance_suspect" in codes and "loo_large" in codes
    # the chunk's precision is poor because the ties disagree (sigma0), not because of their spread: it says so
    assert sol.per_chunk[b.chunk.key]["precision_cause"] == "residuals" and "precision_residuals" in codes
    assert "precision_spread" not in codes
    row = next(r for r in rep["ties"] if r["id"] == made[3].id)
    assert row["loo"] == pytest.approx(loo[made[3].id]) and row["suspect"]
    en = ties.report_text(sol, checks, loo, warns)
    it = ties.report_text(sol, checks, loo, warns, language="it")
    assert made[3].name in en and "2025" in en and "Redundancy" in en
    assert "Gradi di libertà in avanzo" in it and made[3].name in it
    for m in rep["messages"]:
        assert ties.render(m, "it") != ties.render(m, "en") or not m["args"]


def test_every_message_has_its_translation_in_the_catalog():
    """The GUI shows a message with ``tr(message["it"], *args)``: every Italian template of the engine must be a format
    of en.json with the same English text."""
    formats = json.loads(EN_JSON.read_text(encoding="utf-8"))["formats"]
    texts = json.loads(EN_JSON.read_text(encoding="utf-8"))["texts"]
    for code, (it, en) in ties.MESSAGES.items():
        assert formats.get(it, texts.get(it)) == en, code
