"""A drawing classified by orientation along an axis (engine/orient_lines): synthetic drawings made here, in metres -
the frames and strakes of a hull, the beds and joints of a wall, polygons and curves; the plate, the layers, the
command line. The chunk of ``test_colour_mask`` (a synthetic origin, no real place) for the layers."""
from __future__ import annotations

import json

import numpy as np
import pytest

from pyarchinit_photogrammetry.engine import orient_lines
from pyarchinit_photogrammetry.engine.orient_lines import OrientLinesError


def _noisy(rng, pts, sigma=0.003, steps=6):
    """A line through ``pts`` with ``steps`` vertices per piece, each moved by ``sigma`` (drawn by hand)."""
    pts = np.asarray(pts, float)
    out = []
    for a, b in zip(pts[:-1], pts[1:]):
        for k in range(steps):
            out.append(a + (b - a) * k / steps)
    out.append(pts[-1])
    return np.asarray(out) + rng.normal(0, sigma, (len(out), 2))


def hull_lines(seed=1):
    """12 frames (both sides of each: two lines 0.08 m apart) every 0.4 m on both sides of the keel, 4 strake lines
    along the axis each broken in 3 pieces with gaps of 0.05 m; 3 mm of noise."""
    rng = np.random.default_rng(seed)
    lines = []
    for k in range(12):
        s = 0.3 + 0.4 * k
        for side in (1, -1):
            for ds in (0.0, 0.08):
                lines.append(_noisy(rng, [(s + ds, side * 0.15), (s + ds, side * 1.25)]))
    for t in (0.35, 0.95, -0.35, -0.95):
        x = 0.1
        for length in (1.6, 1.5, 1.6):
            lines.append(_noisy(rng, [(x, t), (x + length, t)], steps=12))
            x += length + 0.05
    return lines


def masonry_lines(seed=2):
    """6 courses 0.25 m high: 7 bed lines, 5 joints in each course every ~0.4 m staggered by half a stone."""
    rng = np.random.default_rng(seed)
    lines = [_noisy(rng, [(0.0, 0.25 * i), (2.4, 0.25 * i)], steps=20) for i in range(7)]
    for c in range(6):
        start = 0.4 if c % 2 == 0 else 0.2
        for j in range(5):
            u = start + 0.4 * j + rng.uniform(-0.03, 0.03)
            lines.append(_noisy(rng, [(u, 0.25 * c), (u, 0.25 * (c + 1))], steps=3))
    return lines


def test_hull_drawing_frames_and_strakes():
    lines = hull_lines()
    r = orient_lines.classify(lines, axis=((0, 0), (5, 0)))
    counts = r.counts()
    assert counts["pair_across"] == 24 and counts["along"] == 4, counts
    assert counts["across"] == 0 and counts["pair_along"] == 0 and counts["other"] == 0, counts
    assert all(abs(e.width - 0.08) < 0.01 for e in r.elements if e.group == "pair_across")
    frames = [e for e in r.elements if e.group == "pair_across"]
    assert sorted({e.side for e in frames}) == [-1, 1]
    for side in (-1, 1):  # 12 per side, every 0.4 m, at the middle of their two lines
        pos = sorted(e.position for e in frames if e.side == side)
        assert np.allclose(pos, 0.34 + 0.4 * np.arange(12), atol=0.01), pos
        assert all(e.geometry.geom_type == "Polygon" for e in frames)
    strakes = [e for e in r.elements if e.group == "along"]
    assert all(e.geometry.geom_type == "LineString" and e.length > 4.7 for e in strakes)  # the 3 pieces joined
    assert sorted(round(e.offset, 2) for e in strakes) == [-0.95, -0.35, 0.35, 0.95]
    assert [e.id for e in r.elements] == [str(k) for k in range(1, 29)]
    # the same drawing turned and moved: the same groups along an axis turned with it
    a = np.radians(37.0)
    rot = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
    shift = np.array([1000.0, 2000.0])
    moved = [ln @ rot.T + shift for ln in lines]
    axis = (tuple(shift), tuple(shift + rot @ np.array([5.0, 0.0])))
    r2 = orient_lines.classify(moved, axis=axis)
    assert r2.counts() == counts


def test_masonry_drawing_beds_and_joints():
    r = orient_lines.classify(masonry_lines(), axis=((0, 0), (2.4, 0)))
    counts = r.counts()
    assert counts["along"] == 7 and counts["across"] == 30, counts
    assert counts["pair_along"] == 0 and counts["pair_across"] == 0, counts
    beds = sorted(e.offset for e in r.elements if e.group == "along")
    assert np.allclose(beds, 0.25 * np.arange(7), atol=0.01)
    joints = [e for e in r.elements if e.group == "across"]
    assert all(abs(e.length - 0.25) < 0.03 for e in joints)


def test_polygons_and_curves_are_split_into_straight_segments():
    # a closed rectangle 2 x 1 m: its 4 edges are lines (2 along, 2 across), also the closing one
    rect = np.array([(0, 0), (2, 0), (2, 1), (0, 1), (0, 0)], float)
    r = orient_lines.classify([rect], axis=((0, 0), (1, 0)))
    assert r.counts()["along"] == 2 and r.counts()["across"] == 2, r.counts()
    assert len(r.segments) == 4
    # a narrow closed polygon (a plank 0.2 m wide): its two long edges are a pair along the axis
    plank = np.array([(0, 2), (3, 2), (3, 2.2), (0, 2.2), (0, 2)], float)
    r = orient_lines.classify([plank], axis=((0, 0), (1, 0)))
    assert r.counts()["pair_along"] == 1 and r.counts()["across"] == 2, r.counts()
    pair = next(e for e in r.elements if e.group == "pair_along")
    assert pair.width == pytest.approx(0.2, abs=1e-6) and pair.geometry.area == pytest.approx(0.6, abs=1e-6)
    # a half circle of 200 vertices: straight pieces within the simplification, sorted by their direction
    th = np.linspace(0, np.pi, 200)
    arc = np.column_stack([np.cos(th), np.sin(th)])
    r = orient_lines.classify([arc], axis=((0, 0), (1, 0)))
    assert 6 <= len(r.segments) <= 40, len(r.segments)
    for s in r.segments:  # each piece straight: its chord within the tolerance of the arc
        mid = (s.p0 + s.p1) / 2
        assert 1 - np.hypot(*mid) <= orient_lines.DEFAULTS["simplify"] + 0.002
        assert s.length < 0.6
    counts = r.counts()
    assert counts["along"] >= 1 and counts["across"] >= 1 and counts["other"] >= 2, counts
    assert sum(len(e.segments) for e in r.elements) == len(r.segments)  # every piece in one element
    # pieces shorter than min_length left out
    r = orient_lines.classify([arc, np.array([(5, 5), (5.02, 5)])], axis=((0, 0), (1, 0)))
    assert all(s.source == 0 for s in r.segments)


def test_zero_length_axis_refused():
    with pytest.raises(OrientLinesError) as exc:
        orient_lines.classify(hull_lines(), axis=((1, 1), (1, 1)))
    assert "no length" in str(exc.value) and "lunghezza" in exc.value.italian
    with pytest.raises(ValueError):
        orient_lines.classify(hull_lines(), axis=((1, 1), (1 + 1e-9, 1)))
    with pytest.raises(OrientLinesError):
        orient_lines.classify([], axis=((0, 0), (1, 0)))
    with pytest.raises(OrientLinesError):  # nothing long enough
        orient_lines.classify([np.array([(0, 0), (0.01, 0)])], axis=((0, 0), (1, 0)))
    with pytest.raises(OrientLinesError):
        orient_lines.classify(hull_lines(), axis=((0, 0), (1, 0)), params={"nope": 1})
    with pytest.raises(OrientLinesError):
        orient_lines.classify(hull_lines(), axis=((0, 0), (1, 0)), params={"pair_min": 0.6})


def test_parameters_change_the_groups():
    rng = np.random.default_rng(5)
    a = np.radians(10.0)
    lines = [np.array([(0, y), (2 * np.cos(a), y + 2 * np.sin(a))]) for y in rng.uniform(0, 10, 4)]
    lines = [ln + np.array([3.0 * k, 0]) for k, ln in enumerate(lines)]  # far apart: no pairs
    r = orient_lines.classify(lines, axis=((0, 0), (1, 0)))
    assert r.counts()["along"] == 4
    assert all(abs(s.angle - 10.0) < 1e-6 for s in r.segments)
    r = orient_lines.classify(lines, axis=((0, 0), (1, 0)), params={"along_deg": 5})
    assert r.counts()["other"] == 4 and r.counts()["along"] == 0
    assert r.params["along_deg"] == 5.0 and r.params["across_deg"] == orient_lines.DEFAULTS["across_deg"]
    # frames 0.08 m apart are no pairs when the pairs start from 0.1 m
    r = orient_lines.classify(hull_lines(), axis=((0, 0), (5, 0)), params={"pair_min": 0.1})
    assert r.counts()["pair_across"] == 0 and r.counts()["across"] == 48, r.counts()
    # strake pieces 0.05 m apart are not joined with a gap of 0.02 m
    r = orient_lines.classify(hull_lines(), axis=((0, 0), (5, 0)), params={"join_gap": 0.02})
    assert r.counts()["along"] == 12


def test_plate_and_layers_written(tmp_path):
    from test_colour_mask import X0, Y1, dem_z, project

    from pyarchinit_photogrammetry.core import free_drawings

    doc, a = project(tmp_path)
    shift = np.array([X0 + 0.5, Y1 - 1.5])
    lines = [ln * 0.6 + shift for ln in hull_lines()]  # within the DEM (4 x 3 m)
    for ln in lines[:4]:  # two frames drawn as free drawings, the rest given as arrays
        free_drawings.add(a, "line", [[x, y, 0.0] for x, y in ln], layer="Rilievo")
    free_drawings.add(a, "polygon", [[X0 + 3.3, Y1 - 0.5, 0], [X0 + 3.8, Y1 - 0.5, 0], [X0 + 3.8, Y1 - 0.2, 0],
                                     [X0 + 3.3, Y1 - 0.2, 0]], layer="Rilievo")
    from_layer = orient_lines.lines_from_layer(a, "Rilievo")
    assert len(from_layer) == 5 and np.array_equal(from_layer[4][0], from_layer[4][-1])  # the ring closed
    with pytest.raises(OrientLinesError):
        orient_lines.lines_from_layer(a, "Nessuno")
    axis = (tuple(shift), tuple(shift + np.array([3.0, 0.0])))
    r = orient_lines.classify(lines, axis=axis)
    png = r.plate(tmp_path / "tavola.png")
    assert png.exists() and png.read_bytes()[:4] == b"\x89PNG"
    from PIL import Image

    assert Image.open(png).size == (2000, 1400)
    svg = r.plate(tmp_path / "tavola.svg", size=(800, 600), language="en", names={"1": "S1"})
    text = svg.read_text()
    assert "<svg" in text and "S1" in text and "pairs across the axis" in text
    with pytest.raises(OrientLinesError):
        r.plate(tmp_path / "tavola.jpg")
    json.dumps(r.to_dict())  # JSON-ready
    assert r.to_dict()["counts"] == r.counts()
    written = orient_lines.to_layers(a, r)
    assert written == {"Classificato - coppie di traverso": 24, "Classificato - lungo l'asse": 4}
    feats = free_drawings.features(a, "Classificato - coppie di traverso")
    assert len(feats) == 24 and all(f["kind"] == "polygon" for f in feats)
    f = feats[0]
    assert f["attributes"]["gruppo"] == "pair_across" and abs(f["attributes"]["larghezza"] - 0.048) < 0.01
    xyz = np.asarray(f["xyz"])
    assert np.allclose(xyz[:, 2], dem_z(xyz[:, 0], xyz[:, 1]), atol=0.01)  # heights from the DEM
    # written again: the earlier classification replaced, a drawing by hand in the layer kept
    free_drawings.add(a, "point", [[X0 + 1, Y1 - 1, 50]], layer="Classificato - lungo l'asse")
    orient_lines.to_layers(a, r)
    assert len(free_drawings.features(a, "Classificato - lungo l'asse")) == 5
    assert len(free_drawings.features(a, "Classificato - coppie di traverso")) == 24
    # an elevation's classification is not written on the plan
    r_el = orient_lines.classify(lines, axis=axis, frame="e1")
    with pytest.raises(OrientLinesError):
        orient_lines.to_layers(a, r_el)


def test_lines_from_elevation_and_file(tmp_path):
    from test_colour_mask import project

    from pyarchinit_photogrammetry.core import free_drawings
    from pyarchinit_photogrammetry.io import free_drawings as io_free

    doc, a = project(tmp_path)
    a.meta["elevations"] = [{"id": "e1", "name": "Muro", "kind": "elevation",
                             "frame": {"kind": "plane", "origin": [10.0, 20.0, 0.0], "u_axis": [0.6, 0.8, 0.0],
                                       "v_axis": [0.0, 0.0, 1.0]}}]
    a.meta.setdefault("drawings", {})["frames"] = {"e1": {
        "lines": [{"id": "l1", "kind": "breakline", "coords": [[0, 0, None], [2, 0, None]], "attributes": {}}],
        "units": [{"wkt": "POLYGON ((0 0.5, 1 0.5, 1 1, 0 1, 0 0.5))"}],
        "stones": {"e1": ["POLYGON ((5 5, 6 5, 6 6, 5 5))"]}}}
    lines = orient_lines.lines_from_elevation(a, "e1")
    assert len(lines) == 2 and lines[1].shape == (5, 2)  # the stones left out
    r = orient_lines.classify(lines, axis=((0, 0), (1, 0)), frame="e1")
    assert r.counts()["along"] == 3 and r.counts()["across"] == 2, r.counts()
    assert orient_lines.elevation_axis(a, "e1") == ((10.0, 20.0), (10.6, 20.8))
    with pytest.raises(OrientLinesError):
        orient_lines.lines_from_elevation(a, "nope")
    # a file of free drawings
    for ln in hull_lines()[:2]:
        free_drawings.add(a, "line", [[x, y, 0.0] for x, y in ln], layer="F")
    path = io_free.export(a, tmp_path / "linee.geojson", layer="F")
    got = orient_lines.lines_from_file(path)
    assert len(got) == 2 and got[0].shape[1] == 2
    with pytest.raises(OrientLinesError):
        orient_lines.lines_from_file(tmp_path / "manca.geojson")


def test_cli_classify_lines(tmp_path):
    from test_colour_mask import X0, Y1, project
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app
    from pyarchinit_photogrammetry.core import free_drawings

    doc, a = project(tmp_path)
    shift = np.array([X0 + 0.5, Y1 - 1.5])
    for ln in hull_lines():
        free_drawings.add(a, "line", [[x, y, 0.0] for x, y in ln * 0.6 + shift], layer="Rilievo")
    doc.save()
    runner = CliRunner()
    axis = f"{shift[0]},{shift[1]},{shift[0] + 3},{shift[1]}"
    res = runner.invoke(app, ["classify-lines", str(doc.path), "A", "--layer", "Rilievo", "--axis", axis,
                              "--plate", str(tmp_path / "t.png"), "--json", str(tmp_path / "t.json"), "--layers"])
    assert res.exit_code == 0, res.output
    assert "pairs across the axis: 24" in res.output and "along the axis: 4" in res.output
    assert (tmp_path / "t.png").exists()
    data = json.loads((tmp_path / "t.json").read_text())
    assert data["counts"]["pair_across"] == 24
    import pyarchinit_photogrammetry as ppg

    doc2 = ppg.Document.open(doc.path)
    assert len(free_drawings.features(doc2.chunks[0], "Classificato - coppie di traverso")) == 24
    # a parameter: frames 0.08 m apart (0.048 here) are no pairs from 0.1 m
    res = runner.invoke(app, ["classify-lines", str(doc.path), "A", "--layer", "Rilievo", "--axis", axis,
                              "--param", "pair_min=0.1"])
    assert res.exit_code == 0, res.output
    assert "pairs across the axis: 0" in res.output
    # errors: no source, a zero-length axis, an unknown parameter
    res = runner.invoke(app, ["classify-lines", str(doc.path), "A", "--axis", axis])
    assert res.exit_code != 0
    res = runner.invoke(app, ["classify-lines", str(doc.path), "A", "--layer", "Rilievo", "--axis", "1,1,1,1"])
    assert res.exit_code == 1 and "no length" in res.output
    res = runner.invoke(app, ["classify-lines", str(doc.path), "A", "--layer", "Rilievo", "--axis", axis,
                              "--param", "nope=1"])
    assert res.exit_code == 1 and "unknown parameter" in res.output
