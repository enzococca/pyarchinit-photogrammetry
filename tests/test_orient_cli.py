"""``ppg orient`` and the plugin API to orient a local chunk and to follow its transform."""
from __future__ import annotations

import numpy as np
from typer.testing import CliRunner

from pyarchinit_photogrammetry.engine import orient


def _plane_normal(tilt, direction):
    t, d = np.radians(tilt), np.radians(direction)
    return np.array([np.sin(t) * np.sin(d), np.sin(t) * np.cos(d), np.cos(t)])


def _in_plane(n, plan_deg):
    h = np.radians(plan_deg)
    hx, hy = np.cos(h), np.sin(h)
    return np.array([hx, hy, -(n[0] * hx + n[1] * hy) / n[2]])


def _text(points) -> str:
    return ";".join(",".join(f"{v:.9f}" for v in p) for p in points)


def _run(*args):
    from pyarchinit_photogrammetry.cli import app

    return CliRunner().invoke(app, ["orient", *[str(a) for a in args]])


def test_cli_dry_run_prints_the_angles(processed, tmp_path):
    from orient_helpers import centre, local_copy

    doc, ch, _ = local_copy(processed, tmp_path)
    n = _plane_normal(12.0, 40.0)
    a = centre(ch)
    b = a + 1.5 * _in_plane(n, 90.0 - 35.0)  # azimuth 35 from north
    c = a + 1.0 * _in_plane(n, 160.0)
    before = doc.path.read_bytes()
    r = _run(doc.path, "--points", _text([a, b, c]), "--dry-run")
    assert r.exit_code == 0, r.output
    assert "12.0" in r.output and "35.0" in r.output and "40.0" in r.output
    assert "dry run" in r.output
    assert doc.path.read_bytes() == before


def test_cli_markers_and_quota(processed, tmp_path):
    import pyarchinit_photogrammetry as ppg
    from orient_helpers import local_copy

    doc, ch, _ = local_copy(processed, tmp_path)
    placed = [m.label for m in ch.markers if m.position is not None][:2]
    r = _run(doc.path, "--markers", ",".join(placed), "--quota", "100")
    assert r.exit_code == 0, r.output
    again = ppg.Document.open(doc.path).chunk
    marks = {m.label: m for m in again.markers if m.orientation}
    assert set(marks) == set(orient.MARKER_LABELS)
    assert abs(marks[orient.MARKER_LABELS[0]].reference.location[2] - 100.0) < 1e-9
    assert again.meta["orientation"]["method"] == "markers"


def test_cli_refuses_a_projected_chunk(processed):
    doc, ch, _ = processed
    before = doc.path.read_bytes()
    r = _run(doc.path, "--points", "0,0,0;1,0,0;0,1,0")
    assert r.exit_code == 1
    assert "crs" in r.output and "the world gives the orientation" in r.output
    assert doc.path.read_bytes() == before


def test_cli_points_are_parsed_strictly(processed, tmp_path):
    from orient_helpers import local_copy

    doc, ch, _ = local_copy(processed, tmp_path)
    for bad in ("1,2,3", "1,2,3;4,5", "1,2,3;4,5,6;7,8,9;1,1,1", "a,b,c;1,2,3"):
        r = _run(doc.path, "--points", bad, "--dry-run")
        assert r.exit_code == 2, (bad, r.output)


def test_cli_plane_from_wkt(processed, tmp_path):
    import pyarchinit_photogrammetry as ppg
    from orient_helpers import centre, local_copy

    doc, ch, _ = local_copy(processed, tmp_path)
    x, y, _z = centre(ch)
    square = f"POLYGON(({x - 0.8} {y - 0.8}, {x + 0.8} {y - 0.8}, {x + 0.8} {y + 0.8}, {x - 0.8} {y + 0.8}, " \
             f"{x - 0.8} {y - 0.8}))"
    import shapely

    polygon = shapely.from_wkt(square)
    inside = orient.points_in_plan(ch, polygon)
    assert len(inside) >= 10
    assert shapely.contains_xy(polygon.buffer(1e-9), inside[:, 0], inside[:, 1]).all()
    wkt = tmp_path / "piano.wkt"
    wkt.write_text(square)
    r = _run(doc.path, "--plane-from", wkt)
    assert r.exit_code == 0, r.output
    again = ppg.Document.open(doc.path).chunk
    assert again.meta["orientation"]["method"] == "plane"
    moved = orient.points_in_plan(again, shapely.box(-1e9, -1e9, 1e9, 1e9))
    m = np.asarray(again.meta["orientation"]["M"], float)
    carried = inside @ m[:3, :3].T + m[:3, 3]
    _c, normal, _ = orient.fit_plane(carried)
    assert abs(normal[2]) > np.cos(np.radians(0.5))  # the plane of those points is horizontal now
    assert len(moved) >= len(inside)


def test_cli_plane_from_too_few_points(processed, tmp_path):
    from orient_helpers import local_copy

    doc, ch, _ = local_copy(processed, tmp_path)
    r = _run(doc.path, "--plane-from", "POLYGON((1e6 1e6, 1e6 1.001e6, 1.001e6 1.001e6, 1e6 1e6))", "--dry-run")
    assert r.exit_code == 1 and "points" in r.output


def test_context_orient_and_listener(processed, tmp_path):
    from orient_helpers import centre, local_copy

    from pyarchinit_photogrammetry.plugins.api import Context

    doc, ch, _ = local_copy(processed, tmp_path)
    context = Context("prova", tmp_path / "ctx")
    seen = []
    context.on_transform_changed(lambda c, m: seen.append((c, np.array(m))), keys=("prova_dati",))
    ch.meta["prova_dati"] = {"x": 1}
    a = centre(ch)
    result = context.orient(ch, points=[a, a + [1.0, 0.2, 0.1], a + [0.1, 1.0, 0.0]])
    assert len(seen) == 1 and seen[0][0] is ch
    assert np.allclose(seen[0][1], result["M"])
    assert "prova_dati" not in result["plugins_unaware"]
    context.release()
    context.orient(ch, points=[a, a + [1.0, 0.0, 0.0]])
    assert len(seen) == 1  # released: not told any more
