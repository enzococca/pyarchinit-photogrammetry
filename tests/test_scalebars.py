"""Scale bars, as in Metashape: two markers and the distance between them scale the chunk - in
local coordinates for an object or a trench without control points - with the error of every
bar; check bars only measure, several control bars are averaged."""
from __future__ import annotations

import numpy as np
import pytest

from pyarchinit_photogrammetry.core import crs as crs_mod
from test_chunks import _gcp, _true_centres, doc, two_chunks  # noqa: F401 - chunks aligned on their own


def _true(scene, a, b) -> float:
    return float(np.linalg.norm(_gcp(scene, a.label) - _gcp(scene, b.label)))


def _pairs(chunk, scene):
    """Pairs of markers seen in the chunk, the farthest first."""
    chunk.triangulate_markers()
    ms = [m for m in chunk.markers if m.position is not None]
    assert len(ms) >= 4
    return sorted(((a, b) for i, a in enumerate(ms) for b in ms[i + 1:]), key=lambda p: -_true(scene, *p))


def test_scale_bars_in_local_coordinates(doc, scene):
    ch = doc.chunks[0]
    pairs = _pairs(ch, scene)
    (a, b), checks = pairs[0], pairs[1:5]
    bar = ch.add_scalebar(a.label, str(b.code), _true(scene, a, b))  # by label or by target code
    assert bar.label == f"{a.label}_{b.label}" and not bar.checkpoint
    for x, y in checks:
        ch.add_scalebar(x, y, _true(scene, x, y), checkpoint=True)
    rep = ch.update_transform()
    assert rep["method"] == "scalebars" and crs_mod.is_local(ch.crs)
    errors = {e["label"]: e for e in rep["scalebars"]}
    assert abs(errors[bar.label]["error"]) < 1e-9  # one control bar: its length exactly
    checked = [e for e in rep["scalebars"] if e["type"] == "check"]
    assert len(checked) == 4 and max(abs(e["error"]) for e in checked) < 0.004
    assert rep["scalebar_check_rmse"] < 0.003
    # a check bar never moves the scale, even a wrong one
    s = ch.scale
    x, y = checks[0]
    ch.add_scalebar(x, y, 10.0, checkpoint=True)  # the same pair: the bar changes, no new one
    assert len(ch.scalebars) == 5
    ch.update_transform()
    assert ch.scale == pytest.approx(s)
    # two control bars: their weighted mean, within the error of the photogrammetry
    ch.add_scalebar(x, y, _true(scene, x, y))
    rep = ch.update_transform()
    assert abs(ch.scale / s - 1) < 0.002 and rep["scalebar_control_rmse"] < 0.003
    ch.remove_scalebar(bar.label)
    assert bar.label not in {sb.label for sb in ch.scalebars}


def test_the_gps_of_the_photos_does_not_move_an_object_or_a_trench(doc, scene):
    """Photos with the position of a phone or a drone (5 m accuracy) over a few metres: the
    scale bars scale the chunk in local coordinates instead of placing it with that noise."""
    ch = doc.chunks[0]
    (a, b) = _pairs(ch, scene)[0]
    truth = _true_centres(scene)
    rng = np.random.default_rng(0)
    for cam in ch.aligned_cameras:
        cam.reference.location = list(truth[cam.label] + rng.normal(0, 3.0, 3))
        cam.reference.accuracy, cam.reference.enabled = [5.0, 5.0, 10.0], True
        cam.meta["reference_crs"] = "EPSG:3004"
    ch.add_scalebar(a, b, _true(scene, a, b))
    rep = ch.update_transform()
    assert rep["method"] == "scalebars" and crs_mod.is_local(ch.crs)
    assert abs(rep["scalebars"][0]["error"]) < 1e-9
    rep = ch.update_transform(use_cameras=True)  # asked for: the photos place it, the bar scales it
    assert rep["method"] == "cameras+scalebars" and ch.crs == "EPSG:3004"
    rep = ch.update_transform()  # and back: local coordinates are not labelled with the CRS of the photos
    assert rep["method"] == "scalebars" and crs_mod.is_local(ch.crs)
    assert abs(rep["scalebars"][0]["error"]) < 1e-9


def test_bars_give_the_scale_to_gnss_cameras(doc, scene):
    """Precise camera positions (RTK) with a scale 3 % off: the cameras place and turn the
    survey, a control bar fixes its scale, a check bar measures it."""
    ch = doc.chunks[0]
    pairs = _pairs(ch, scene)
    (a, b), (x, y) = pairs[0], pairs[1]
    truth = _true_centres(scene)
    centre = np.mean(list(truth.values()), axis=0)
    rng = np.random.default_rng(1)
    for cam in ch.aligned_cameras:
        t = truth[cam.label]
        cam.reference.location = list(centre + 1.03 * (t - centre) + rng.normal(0, 0.003, 3))
        cam.reference.accuracy, cam.reference.enabled = [0.02, 0.02, 0.03], True
        cam.meta["reference_crs"] = "EPSG:3004"
    check = ch.add_scalebar(x, y, _true(scene, x, y), checkpoint=True)
    rep = ch.update_transform()
    assert rep["method"] == "cameras"
    error = {e["label"]: e["error"] for e in rep["scalebars"]}[check.label]
    assert abs(error) > 0.015  # 3 % of the bar
    ch.add_scalebar(a, b, _true(scene, a, b))
    rep = ch.update_transform()
    assert rep["method"] == "cameras+scalebars" and ch.crs == "EPSG:3004"
    assert abs({e["label"]: e["error"] for e in rep["scalebars"]}[check.label]) < 0.004
    # the bars keep their place: the cameras still fit where they were
    ends = np.array([ch.to_world(m.position) for m in (a, b)])
    assert np.linalg.norm(ends.mean(0) - np.array([_gcp(scene, m.label) for m in (a, b)]).mean(0)) < 0.1


def test_scale_bars_from_a_file(doc, scene, tmp_path):
    ch = doc.chunks[0]
    pairs = _pairs(ch, scene)
    (a, b), (c, d) = pairs[0], pairs[1]
    f = tmp_path / "barre.csv"
    f.write_text("marker_a;marker_b;distanza\n"  # semicolons, Italian decimal commas, codes or labels
                 f"{a.code};{b.code};{_true(scene, a, b):.5f}".replace(".", ",") + "\n"
                 f"{c.label};{d.label};{_true(scene, c, d):.5f};0,002;check\n"
                 "# a comment\n"
                 "98;99;1,0\n", encoding="utf-8")
    out = ch.import_scalebars(f)
    assert len(out["bars"]) == 2 and out["missing"] == ["98-99"]
    first, second = ch.scalebar(out["bars"][0]), ch.scalebar(out["bars"][1])
    assert first.distance == pytest.approx(_true(scene, a, b), abs=1e-5) and not first.checkpoint
    assert second.checkpoint and second.accuracy == pytest.approx(0.002)
    rep = ch.update_transform()
    assert rep["method"] == "scalebars" and rep["scalebar_check_rmse"] < 0.004


def test_printable_scale_bar(tmp_path):
    """Two coded targets at a known distance, a ruler, and the CSV to import."""
    import cv2

    from pyarchinit_photogrammetry.markers import targets

    files, csv = targets.generate_scale_bar(tmp_path / "barra.png", distance_mm=150.0, count=2)
    assert len(files) == 2
    (a, b), (c, d) = targets.scale_bar_codes(count=2)
    assert csv.read_text().splitlines() == ["marker_a,marker_b,distance", f"{a},{b},0.15000", f"{c},{d},0.15000"]
    found = {t["code"]: t["center"] for t in targets.detect(cv2.imread(str(files[0])))}
    assert set(found) == {a, b}
    assert np.linalg.norm(found[a] - found[b]) / (300 / 25.4) == pytest.approx(150.0, abs=0.2)
    with pytest.raises(ValueError):
        targets.generate_scale_bar(tmp_path / "troppo.png", distance_mm=400.0)


def test_command_line(two_chunks, scene, tmp_path):
    """ppg scalebar: a bar by target codes, a check bar, the list with the errors, and away."""
    import shutil

    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app

    d = tmp_path / "copia"
    shutil.copytree(two_chunks.parent, d)
    project = d / two_chunks.name
    ch = ppg.Document.open(project).chunks[0]
    (a, b), (x, y) = _pairs(ch, scene)[:2]
    run = CliRunner()

    def ok(*args):
        r = run.invoke(app, list(args))
        assert r.exit_code == 0, r.output + repr(r.exception)
        return r.output

    out = ok("scalebar", str(project), "--markers", f"{a.code},{b.code}", "--distance", f"{_true(scene, a, b):.5f}",
             "--chunk", ch.label)
    assert "method scalebars, CRS LOCAL" in out
    out = ok("scalebar", str(project), "--markers", f"{x.label},{y.label}", "--distance", f"{_true(scene, x, y):.5f}",
             "--check", "--chunk", ch.label)
    line = next(ln for ln in out.splitlines() if ln.startswith(f"{x.label}_{y.label}"))
    assert ", check, error " in line and abs(float(line.split("error ")[1].split(" mm")[0])) < 4
    ok("scalebar", str(project), "--remove", "all", "--chunk", ch.label)
    assert ppg.Document.open(project).chunks[0].scalebars == []
