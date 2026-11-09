"""``ppg ties``: add, known, from-markers, import, list, remove, check and solve on a synthetic project.

The chunks are the surveys of the synthetic trench of ``test_ties_solve`` (no real coordinates): A right, B moved by
a known similarity; the points are given in the world coordinates of each chunk, as a user reads them in its views."""
from __future__ import annotations

import json
import re

import numpy as np
import pytest
from test_ties_solve import AB, _close, _three, _world
from typer.testing import CliRunner

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.cli import app
from pyarchinit_photogrammetry.engine import ties

FIVE = AB + [(3.0, 1.0)]


@pytest.fixture(scope="module")
def scene():
    from pyarchinit_photogrammetry.testing.synthetic import make_scene

    return make_scene(texel=0.01)


@pytest.fixture
def runner():
    return CliRunner()


def _said(r) -> str:
    """The output of a command as one line: an error in rich's box is wrapped at the terminal's width."""
    return re.sub(r"[\u2500-\u257f\s]+", " ", r.output)


def _xyz(p) -> str:
    return ",".join(f"{v:.6f}" for v in p)


def _project(scene, tmp_path):
    doc, a, b, c = _three(scene)
    path = tmp_path / "ties.ppg"
    doc.save(path)
    return path, a, b, c


def _seen(survey, world) -> np.ndarray:
    """Where ``survey``'s chunk shows a true world point now (its transform is a little wrong)."""
    return survey.chunk.to_world(survey.internal(world))[0]


def _add(runner, path, a, b, world, *extra, chunk_names=("2024", "2025")):
    return runner.invoke(app, ["ties", "add", str(path), chunk_names[0], _xyz(_seen(a, world)), chunk_names[1],
                               _xyz(_seen(b, world)), *extra])


def test_add_and_list(scene, tmp_path, runner):
    path, a, b, _c = _project(scene, tmp_path)
    w = _world(scene, AB[0])[0]
    r = _add(runner, path, a, b, w, "--precision", "0.02", "--name", "picchetto 1")
    assert r.exit_code == 0, r.output
    assert "picchetto 1" in r.output and "2024 / picchetto 1 <-> 2025 / picchetto 1" in r.output
    doc = ppg.Document.open(path)
    (t,) = ties.ties_of(doc)
    assert t.precision == pytest.approx(0.02) and t.name == "picchetto 1"
    ca, cb = doc.chunks[0], doc.chunks[1]
    _close(ca.marker(t.a[1]).position, a.internal(w)[0], atol=1e-5)
    _close(cb.marker(t.b[1]).position, b.internal(w)[0], atol=1e-5)

    r = runner.invoke(app, ["ties", "list", str(path)])
    assert r.exit_code == 0 and t.id in r.output and "active" in r.output
    r = runner.invoke(app, ["ties", "list", str(path), "--json"])
    (row,) = json.loads(r.output)
    assert row["id"] == t.id and row["a_labels"] == ["2024", "picchetto 1"] and row["broken"] is None
    _close(row["b_world"], _seen(b, w), atol=1e-5)


def test_add_without_a_height_needs_a_dem(scene, tmp_path, runner):
    path, a, b, _c = _project(scene, tmp_path)
    w = _world(scene, AB[0])[0]
    xy = ",".join(f"{v:.6f}" for v in _seen(a, w)[:2])
    r = runner.invoke(app, ["ties", "add", str(path), "2024", xy, "2025", _xyz(_seen(b, w))])
    assert r.exit_code == 2 and "has no DEM" in _said(r)
    r = runner.invoke(app, ["ties", "add", str(path), "2024", "1,2,3,4", "2025", _xyz(_seen(b, w))])
    assert r.exit_code == 2 and "X,Y[,Z]" in _said(r)
    r = runner.invoke(app, ["ties", "add", str(path), "nessuno", _xyz(w), "2025", _xyz(w)])
    assert r.exit_code == 2 and "'2024' (key" in _said(r)
    r = runner.invoke(app, ["ties", "add", str(path), "2024", _xyz(w), "2024", _xyz(w)])
    assert r.exit_code == 1 and "two different chunks" in r.output
    assert ties.ties_of(ppg.Document.open(path)) == []


def test_known_point(scene, tmp_path, runner):
    path, a, _b, _c = _project(scene, tmp_path)
    w = _world(scene, AB[1])[0]
    known = w + [0.0, 0.0, 0.0]
    r = runner.invoke(app, ["ties", "known", str(path), "2024", _xyz(_seen(a, w)), _xyz(known), "--precision", "0.03"])
    assert r.exit_code == 0, r.output
    (t,) = ties.ties_of(ppg.Document.open(path))
    assert t.b is None and t.precision == pytest.approx(0.03)
    _close(t.known, known, atol=1e-5)
    r = runner.invoke(app, ["ties", "known", str(path), "2024", _xyz(w), "1,2"])
    assert r.exit_code == 2 and "X,Y,Z" in _said(r)


def test_from_markers(scene, tmp_path, runner):
    doc, a, b, _c = _three(scene)
    for i, xy in enumerate(AB[:3]):
        w = _world(scene, xy)[0]
        a.chunk.add_marker(f"T{i}", a.internal(w)[0])
        b.chunk.add_marker(f"T{i}", b.internal(w)[0])
    a.chunk.add_marker("solo A", a.internal(_world(scene, AB[3]))[0])
    path = tmp_path / "m.ppg"
    doc.save(path)
    r = runner.invoke(app, ["ties", "from-markers", str(path), "2024", "2025"])
    assert r.exit_code == 0, r.output
    assert "3 ties from the markers of 2024 and 2025" in r.output
    doc = ppg.Document.open(path)
    assert sorted(t.name for t in ties.ties_of(doc)) == ["T0", "T1", "T2"]
    assert len(doc.chunks[0].markers) == 4  # no new markers
    r = runner.invoke(app, ["ties", "from-markers", str(path), "2024", "2025"])
    assert r.exit_code == 0 and "0 ties" in r.output


def test_import_table(scene, tmp_path, runner):
    path, a, b, _c = _project(scene, tmp_path)
    rows = ["chunk_a,x_a,y_a,z_a,chunk_b,x_b,y_b,z_b,precision,name"]
    for i, xy in enumerate(AB):
        w = _world(scene, xy)[0]
        rows.append(f"2024,{_xyz(_seen(a, w))},2025,{_xyz(_seen(b, w))},0.01,riga {i}")
    w = _world(scene, FIVE[4])[0]
    rows.append(f"2024,{_xyz(_seen(a, w))},,{_xyz(w)},,caposaldo")
    table = tmp_path / "legami.csv"
    table.write_text("\n".join(rows) + "\n", encoding="utf-8")
    r = runner.invoke(app, ["ties", "import", str(path), str(table)])
    assert r.exit_code == 0, r.output
    assert "5 ties imported" in r.output
    got = ties.ties_of(ppg.Document.open(path))
    assert [t.name for t in got] == ["riga 0", "riga 1", "riga 2", "riga 3", "caposaldo"]
    assert got[-1].b is None and got[-1].precision == pytest.approx(ties.TABLE_PRECISION)

    bad = tmp_path / "bad.csv"
    bad.write_text("2024,1,2,3,nessuno,1,2,3\n", encoding="utf-8")
    r = runner.invoke(app, ["ties", "import", str(path), str(bad)])
    assert r.exit_code == 1 and "row 1" in r.output
    assert len(ties.ties_of(ppg.Document.open(path))) == 5


def test_remove(scene, tmp_path, runner):
    path, a, b, _c = _project(scene, tmp_path)
    for xy in AB[:2]:
        assert _add(runner, path, a, b, _world(scene, xy)[0]).exit_code == 0
    first, second = ties.ties_of(ppg.Document.open(path))
    r = runner.invoke(app, ["ties", "remove", str(path), first.id, "--keep-markers"])
    assert r.exit_code == 0 and "markers kept" in r.output
    doc = ppg.Document.open(path)
    assert [t.id for t in ties.ties_of(doc)] == [second.id] and len(doc.chunks[0].markers) == 2
    r = runner.invoke(app, ["ties", "remove", str(path), second.name])  # by its name, markers removed
    assert r.exit_code == 0, r.output
    doc = ppg.Document.open(path)
    assert ties.ties_of(doc) == [] and len(doc.chunks[0].markers) == 1
    r = runner.invoke(app, ["ties", "remove", str(path), "t00000000"])
    assert r.exit_code == 2 and "no tie" in _said(r)


def _five_with_a_wrong_one(scene, tmp_path, runner):
    path, a, b, c = _project(scene, tmp_path)
    for i, xy in enumerate(FIVE):
        w = _world(scene, xy)[0]
        rb = _seen(b, w) + ([1.0, 0.0, 0.0] if i == 3 else 0.0)
        r = runner.invoke(app, ["ties", "add", str(path), "2024", _xyz(_seen(a, w)), "2025", _xyz(rb),
                                "--precision", "0.01", "--name", f"L{i}"])
        assert r.exit_code == 0, r.output
    return path, a, b, c


def test_check_names_the_wrong_tie_and_changes_nothing(scene, tmp_path, runner):
    path, *_ = _five_with_a_wrong_one(scene, tmp_path, runner)
    before = path.read_bytes()
    r = runner.invoke(app, ["ties", "check", str(path), "--unknowns", "2024=fixed,2025=similarity"])
    assert r.exit_code == 0, r.output
    assert "Redundancy: 8" in r.output and "Tie «L3» probably wrong" in r.output
    assert "Tie «L3» left out: the others put its ends" in r.output
    assert path.read_bytes() == before
    r = runner.invoke(app, ["ties", "check", str(path), "-u", "2024=fixed,2025=similarity", "--json"])
    rep = json.loads(r.output)
    assert rep["redundancy"] == 8 and [row["name"] for row in rep["ties"] if row["suspect"]] == ["L3"]
    assert path.read_bytes() == before


def test_unknowns_are_checked(scene, tmp_path, runner):
    path, *_ = _project(scene, tmp_path)
    for text, words in (("2024=fixed,2025=tilt", "the unknowns are one of"), ("2024", "write CHUNK=KIND"),
                        ("2024=fixed,2024=plan", "given twice"), ("2024=fixed,nessuno=plan", "no chunk")):
        r = runner.invoke(app, ["ties", "check", str(path), "--unknowns", text])
        assert r.exit_code == 2 and words in _said(r), (text, r.output)


def test_similarity_with_two_ties_is_refused(scene, tmp_path, runner):
    path, a, b, _c = _project(scene, tmp_path)
    for xy in AB[:2]:
        assert _add(runner, path, a, b, _world(scene, xy)[0]).exit_code == 0
    r = runner.invoke(app, ["ties", "solve", str(path), "-u", "2024=fixed,2025=similarity", "--yes"])
    assert r.exit_code == 1 and r.output.startswith("error:")


def test_solve_asks_before_applying(scene, tmp_path, runner):
    path, a, b, _c = _project(scene, tmp_path)
    for xy in FIVE:
        assert _add(runner, path, a, b, _world(scene, xy)[0], "--precision", "0.01").exit_code == 0
    before = path.read_bytes()
    args = ["ties", "solve", str(path), "--unknowns", "2024=fixed,2025=similarity"]
    r = runner.invoke(app, args, input="n\n")
    assert r.exit_code == 0 and "Apply the solve to 2025?" in r.output and "Nothing applied." in r.output
    assert path.read_bytes() == before
    r = runner.invoke(app, args, input="y\n")
    assert r.exit_code == 0, r.output
    assert "applied to 2025 (system EPSG:3004); project saved" in r.output
    doc = ppg.Document.open(path)
    _close(doc.chunks[1].transform, b.truth)
    _close(doc.chunks[0].transform, a.truth)
    assert doc.meta["ties_report"]["redundancy"] == 8
    assert doc.chunks[1].meta["chunk_alignment"]["method"] == "ties"
    # applied again with --yes: nothing left to move, no new markers
    markers = [len(c.markers) for c in doc.chunks]
    r = runner.invoke(app, args + ["--yes"])
    assert r.exit_code == 0 and "applied to 2025" in r.output
    doc = ppg.Document.open(path)
    assert [len(c.markers) for c in doc.chunks] == markers
    _close(doc.chunks[1].transform, b.truth)


def test_solve_with_every_chunk_fixed_applies_nothing(scene, tmp_path, runner):
    path, a, b, _c = _project(scene, tmp_path)
    assert _add(runner, path, a, b, _world(scene, AB[0])[0]).exit_code == 0
    r = runner.invoke(app, ["ties", "solve", str(path), "-u", "2024=fixed,2025=fixed"])
    assert r.exit_code == 0 and "Nothing to apply" in r.output


def test_a_negative_coordinate_is_a_point_not_an_option(scene, tmp_path, runner):
    path, *_ = _project(scene, tmp_path)
    r = runner.invoke(app, ["ties", "add", str(path), "2024", "-1.5,2,3", "2025", "-1.4,2,3", "--precision", "0.1"])
    assert r.exit_code == 0, r.output
    doc = ppg.Document.open(path)
    (t,) = ties.ties_of(doc)
    _close(doc.chunks[0].to_world(doc.chunks[0].marker(t.a[1]).position[None])[0], [-1.5, 2.0, 3.0], atol=1e-6)


def test_a_known_point_with_negative_coordinates(scene, tmp_path, runner):
    path, *_ = _project(scene, tmp_path)
    r = runner.invoke(app, ["ties", "known", str(path), "2024", "-1.5,2,3", "-10.5,-20,3"])
    assert r.exit_code == 0, r.output
    (t,) = ties.ties_of(ppg.Document.open(path))
    _close(t.known, [-10.5, -20.0, 3.0])


def test_a_mistyped_option_writes_nothing(scene, tmp_path, runner):
    """``add``/``known`` take unknown options as arguments (negative points): a typo must still fail, untouched."""
    path, *_ = _project(scene, tmp_path)
    before = path.read_bytes()
    for args in (["add", str(path), "2024", "1,2,3", "2025", "1,2,3", "--precison", "0.02"],
                 ["add", str(path), "2024", "1,2,3", "--nme", "x"],
                 ["known", str(path), "2024", "1,2,3", "1,2,3", "--precison", "0.02"]):
        r = runner.invoke(app, ["ties", *args])
        assert r.exit_code == 2, (args, r.output)
    assert path.read_bytes() == before


def test_an_empty_table_leaves_the_project_as_it_is(scene, tmp_path, runner):
    path, *_ = _project(scene, tmp_path)
    before = path.read_bytes()
    table = tmp_path / "vuota.csv"
    table.write_text("chunk_a,x_a,y_a,z_a,chunk_b,x_b,y_b,z_b\n", encoding="utf-8")
    r = runner.invoke(app, ["ties", "import", str(path), str(table)])
    assert r.exit_code == 0 and "0 ties imported" in r.output
    assert path.read_bytes() == before


def test_no_check_at_all_needs_its_own_confirmation(scene, tmp_path, runner):
    """One tie and ``shift``: redundancy 0. ``--yes`` alone refuses; ``--accept-no-check`` applies; asked, the
    question says so."""
    path, a, b, _c = _project(scene, tmp_path)
    assert _add(runner, path, a, b, _world(scene, AB[0])[0]).exit_code == 0
    before = path.read_bytes()
    args = ["ties", "solve", str(path), "-u", "2024=fixed,2025=shift"]
    r = runner.invoke(app, args + ["--yes"])
    assert r.exit_code == 1 and "--accept-no-check" in _said(r) and "redundancy 0" in _said(r)
    assert path.read_bytes() == before
    r = runner.invoke(app, args, input="n\n")
    assert r.exit_code == 0 and "No check at all (redundancy 0)" in _said(r) and "Nothing applied." in r.output
    assert path.read_bytes() == before
    r = runner.invoke(app, args + ["--yes", "--accept-no-check"])
    assert r.exit_code == 0 and "applied to 2025" in r.output
    assert ppg.Document.open(path).chunks[1].meta["chunk_alignment"]["redundancy"] == 0
