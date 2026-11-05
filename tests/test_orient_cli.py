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

    from orient_helpers import drawn

    doc, ch, _ = local_copy(processed, tmp_path)
    drawn(ch)  # a proxy among them: its PLY is copied for an undo
    placed = [m.label for m in ch.markers if m.position is not None][:2]
    r = _run(doc.path, "--markers", ",".join(placed), "--quota", "100")
    assert r.exit_code == 0, r.output
    again = ppg.Document.open(doc.path).chunk
    marks = {m.label: m for m in again.markers if m.orientation}
    assert set(marks) == set(orient.MARKER_LABELS)
    assert abs(marks[orient.MARKER_LABELS[0]].reference.location[2] - 100.0) < 1e-9
    assert again.meta["orientation"]["method"] == "markers"
    # nothing undoes a command: the copies kept for an undo are not left in the chunk's folder
    assert not list(again.data_dir.glob("undo/orient-*"))


def test_dry_run_does_not_call_listening_plugins_unaware(processed, tmp_path):
    from orient_helpers import local_copy

    doc, ch, _ = local_copy(processed, tmp_path)
    ch.meta["myplugin"] = {"x": 1}
    heard = []
    off = orient.on_transform_changed(lambda c, m: heard.append(m), keys=("myplugin",))
    try:
        a = np.asarray(ch.to_world(np.zeros((1, 3))))[0]
        result = orient.orient(ch, points=[a, a + [1.0, 0.2, 0.1], a + [0.1, 1.0, 0.0]], dry_run=True)
    finally:
        off()
    assert "myplugin" not in result["plugins_unaware"]
    assert not heard  # a dry run tells nobody


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


def test_open_with_an_unknown_chunk_lists_the_chunks(tmp_path):
    """``--chunk`` naming no chunk of the project: a usage error with the chunks there are, not a traceback."""
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    doc.add_chunk("Scavo")
    doc.add_chunk("Sezione")
    doc.save(tmp_path / "p.ppg")
    r = _run(tmp_path / "p.ppg", "--points", "0,0,0;1,0,0;0,1,0", "--chunk", "Nessuno", "--dry-run")
    assert r.exit_code == 2, r.output
    assert "Nessuno" in r.output and "Scavo" in r.output and "Sezione" in r.output
    assert not isinstance(r.exception, StopIteration)


def test_orient_lines_name_the_products_in_english():
    """The lines of ``ppg orient`` are English: the products to build again by their English names."""
    from pyarchinit_photogrammetry.cli import _orient_lines
    from pyarchinit_photogrammetry.core import stamps

    result = {"describe": {"tilt_deg": 1.0, "tilt_direction_deg": 2.0, "heading_deg": 3.0, "translation": [0, 0, 0],
                           "scale": 1.0},
              "stale": [{"kind": "product", "product": p, "name": stamps.NAMES[p], "id": None}
                        for p in ("dem", "orthomosaic", "dem_precision", "coverage", "slope", "aspect", "tin")]
              + [{"kind": "elevation", "product": "elevation", "name": "Parete nord", "id": "e1"}]}
    line = next(x for x in _orient_lines(result, False) if x.startswith("stale"))
    assert line == ("stale (to build again): DEM, orthomosaic, DEM precision, coverage, slope, aspect, TIN, "
                    "Parete nord")
    assert set(stamps.NAMES_EN) == set(stamps.NAMES)


def _square(ch, half=0.8):
    from orient_helpers import centre

    x, y, _z = centre(ch)
    return (f"POLYGON(({x - half} {y - half}, {x + half} {y - half}, {x + half} {y + half}, {x - half} {y + half}, "
            f"{x - half} {y - half}))")


def test_cli_plane_from_with_x_kept(processed, tmp_path):
    """``--plane-from`` with A, B and ``--x-kept``: the plane horizontal, A its origin, x the current x projected on
    the plane (not along A->B as without the option)."""
    import pyarchinit_photogrammetry as ppg
    from orient_helpers import centre, local_copy

    for kept in (False, True):
        doc, ch, _ = local_copy(processed, tmp_path, name=f"locale_{kept}")
        a = centre(ch)
        b = a + [0.2, 1.0, 0.0]  # A->B mostly along y
        args = [doc.path, "--plane-from", _square(ch), "--points", _text([a, b])] + (["--x-kept"] if kept else [])
        r = _run(*args)
        assert r.exit_code == 0, r.output
        m = np.asarray(ppg.Document.open(doc.path).chunk.meta["orientation"]["M"], float)
        rot = m[:3, :3] / np.linalg.norm(m[:3, 0])
        n = rot[2]  # the normal of the plane, in the coordinates before
        want = np.array([1.0, 0.0, 0.0]) if kept else b - a
        want = want - (want @ n) * n
        assert np.allclose(rot[0], want / np.linalg.norm(want), atol=1e-9), (kept, rot[0])


def test_cli_markers_errors(processed, tmp_path, monkeypatch):
    """A marker that is not in the chunk is a usage error naming it; any other KeyError of the orientation is not
    passed off as a wrong marker."""
    from orient_helpers import local_copy

    doc, ch, _ = local_copy(processed, tmp_path)
    placed = [m.label for m in ch.markers if m.position is not None][:2]
    r = _run(doc.path, "--markers", f"{placed[0]},Fantasma", "--dry-run")
    assert r.exit_code == 2 and "Fantasma" in r.output, r.output

    def broken(*a, **k):
        raise KeyError("internal")

    monkeypatch.setattr(orient, "orient", broken)
    r = _run(doc.path, "--markers", ",".join(placed), "--dry-run")
    assert isinstance(r.exception, KeyError) and r.exit_code == 1, r.output


def test_two_points_with_x_kept_move_only_the_origin(processed, tmp_path):
    """Two points without C and x not along A->B: the plane stays the current horizontal and x the current x - the
    engine's frame, the command line (``--x-kept``) and the dialog (the box unticked) all honour it."""
    import pyarchinit_photogrammetry as ppg
    from orient_helpers import centre, local_copy

    a = np.array([10.0, 20.0, 3.0])
    b = a + [0.3, 1.0, 0.2]
    f = orient.frame_from_points(a, b, x_along_ab=False)
    assert np.allclose(f.axes, np.eye(3)) and np.allclose(f.origin, a)
    f = orient.frame_from_points(a, a + [0.0, 0.0, 1.0], x_along_ab=False)  # A-B vertical: no matter
    assert np.allclose(f.axes, np.eye(3))
    assert np.allclose(orient.frame_from_points(a, b, x_along_ab=False, flip=True).axes[2], [0.0, 0.0, -1.0])
    along = orient.frame_from_points(a, b).axes[0]
    assert abs(along[1]) > 0.9  # along A->B as before when asked
    for kept in (False, True):
        doc, ch, _ = local_copy(processed, tmp_path, name=f"due_{kept}")
        p = centre(ch)
        r = _run(doc.path, "--points", _text([p, p + [0.3, 1.0, 0.2]]), "--quota", "5", *(["--x-kept"] if kept else []))
        assert r.exit_code == 0, r.output
        m = np.asarray(ppg.Document.open(doc.path).chunk.meta["orientation"]["M"], float)
        if kept:
            assert np.allclose(m[:3, :3], np.eye(3), atol=1e-9)
            assert np.allclose(m[:3, :3] @ p + m[:3, 3], [0.0, 0.0, 5.0], atol=1e-6)
        else:
            assert abs(m[0, 1]) > 0.9
