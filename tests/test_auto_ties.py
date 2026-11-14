"""Automatic ties between chunks (engine/auto_ties.py) on the synthetic trench.

The trench (``testing.synthetic.make_scene``) is cut into two overlapping surveys A (x 0-2.6 m) and B (x 1.4-4 m),
each with an orthophoto and a DEM built where its chunk is placed now. A is placed right; B is placed by a wrong
similarity (1.5° about the vertical, +2000 ppm, 0.15 / -0.10 / +0.03 m: a drone's GPS), its photos taken in another
light (gain, gamma) with noise in the colours and the heights. A third chunk C shows another trench (another seed)
where B lies: a wrong pair, which must be rejected. The pair A-B is matched, accepted as ties, solved; B must come
back onto its true transform."""
from __future__ import annotations

import numpy as np
import pytest
from typer.testing import CliRunner

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.cli import app
from pyarchinit_photogrammetry.core import geometry, groups, stamps
from pyarchinit_photogrammetry.engine import auto_ties, ties
from pyarchinit_photogrammetry.engine.chunks import affine_inverse
from pyarchinit_photogrammetry.engine.dem import NODATA, write_geotiff
from pyarchinit_photogrammetry.testing.synthetic import _noise_texture, _sample, make_scene

CRS = "EPSG:3004"
CELL = 0.008


def _translate(t):
    m = np.eye(4)
    m[:3, 3] = t
    return m


def _rot_z(deg, scale=1.0, shift=(0, 0, 0)):
    a = np.radians(deg)
    m = np.eye(4)
    m[:3, :3] = scale * np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    m[:3, 3] = shift
    return m


@pytest.fixture(scope="module")
def scenes():
    other = make_scene(texel=0.004, seed=99)
    h, w = other.texture.shape[:2]  # another ground: no targets (the trench's are the same codes in the same places)
    other.texture = _noise_texture(h, w, np.random.default_rng(99))
    return make_scene(texel=0.004), other


def _survey(doc, scene, label, x_range, truth, error=np.eye(4), light=(1.0, 1.0), seed=0, noise=(3.0, 0.001)):
    """A chunk seeing ``x_range`` (local metres) of ``scene``: its rasters made in the world where ``error @ truth``
    puts it (plan rotation, scale and shift only, so the inverse in plan does not need the height)."""
    rng = np.random.default_rng(seed)
    ch = doc.add_chunk(label)
    ch.crs = CRS
    ch.transform = error @ truth
    D = error  # true world -> world of the chunk now
    s = float(np.cbrt(np.linalg.det(D[:3, :3])))
    R = D[:3, :3] / s
    x0, y0, z0 = scene.origin
    corners = np.array([[x0 + x, y0 + y, z0] for x in x_range for y in (0.0, scene.size[1])])
    placed = geometry.apply(D, corners)
    left, right = placed[:, 0].min(), placed[:, 0].max()
    bottom, top = placed[:, 1].min(), placed[:, 1].max()
    w, h = int(np.ceil((right - left) / CELL)), int(np.ceil((top - bottom) / CELL))
    cols, rows = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    X, Y = left + cols * CELL, top - rows * CELL
    plan = np.stack([X.ravel() - D[0, 3], Y.ravel() - D[1, 3]])
    true_xy = (R[:2, :2].T @ plan) / s
    lx, ly = true_xy[0] - x0, true_xy[1] - y0
    inside = (lx >= x_range[0]) & (lx <= x_range[1]) & (ly >= 0) & (ly <= scene.size[1])
    col, row = lx / scene.texel - 0.5, (scene.size[1] - ly) / scene.texel - 0.5
    bgr = _sample(scene.texture, col, row).astype(float)
    gain, gamma = light
    bgr = 255.0 * (np.clip(bgr * gain, 0, 255) / 255.0) ** gamma + rng.normal(0, noise[0], bgr.shape)
    rgb = np.clip(bgr[:, ::-1], 0, 255).astype(np.uint8).reshape(h, w, 3)
    z_true = scene.height_at(lx, ly) + z0
    z = (s * z_true + D[2, 3] + rng.normal(0, noise[1], z_true.shape)).reshape(h, w).astype(np.float32)
    inside = inside.reshape(h, w)
    ortho = np.zeros((4, h, w), np.uint8)
    ortho[:3] = np.moveaxis(rgb, -1, 0)
    ortho[3] = np.where(inside, 255, 0)
    z[~inside] = NODATA
    ch._set_product("orthomosaic", write_geotiff(ch.data_dir / "ortho.tif", ortho, (left, top), CELL, CRS))
    ch._set_product("dem", write_geotiff(ch.data_dir / "dem.tif", z[None], (left, top), CELL, CRS, nodata=NODATA))
    stamps.stamp(ch, "dem")
    stamps.stamp(ch, "orthomosaic")
    return ch


def _project(tmp_path, scenes):
    scene, other = scenes
    doc = ppg.Document()
    doc.save(tmp_path / "p.ppg")
    origin = _translate(scene.origin)
    truth = {"A": origin, "B": origin @ _rot_z(25.0, 1.0, (0.4, -0.2, 0.0)), "C": origin}
    centre = np.array(scene.origin) + (2.7, 1.5, 0.0)
    error = _translate(centre) @ _rot_z(1.5, 1.002, (0.15, -0.10, 0.03)) @ _translate(-centre)
    _survey(doc, scene, "A", (0.0, 2.6), truth["A"], seed=1)
    _survey(doc, scene, "B", (1.4, 4.0), truth["B"], error, light=(1.15, 0.8), seed=2)
    _survey(doc, other, "C", (1.4, 4.0), truth["C"], seed=3)
    doc.save()
    return doc, truth


def _errors(doc, chunk, truth, origin):
    """How far the chunk is from its truth over B's area: shift at its middle (m), rotation (deg), scale (ppm), the
    largest displacement of its corners (m)."""
    D = np.asarray(chunk.transform, float) @ affine_inverse(np.asarray(truth, float))  # true world -> placed
    o = np.asarray(origin, float)
    mid = o + (2.7, 1.5, 0.0)
    corners = np.array([o + (x, y, 0.0) for x in (1.4, 4.0) for y in (0.0, 3.0)])
    shift = float(np.linalg.norm(geometry.apply(D, mid[None])[0] - mid))
    worst = float(np.max(np.linalg.norm(geometry.apply(D, corners) - corners, axis=1)))
    scale = float(np.cbrt(np.linalg.det(D[:3, :3])))
    return shift, ties._rotation_deg(D), (scale - 1) * 1e6, worst


def test_pairs_matched_accepted_and_solved_back_to_the_truth(tmp_path, scenes):
    doc, truth = _project(tmp_path, scenes)
    a, b, c = doc.chunks
    pairs = {(p["a_label"], p["b_label"]): p for p in auto_ties.find_pairs(doc)}
    assert pairs[("A", "B")]["candidate"] and 0.3 < pairs[("A", "B")]["share"] < 0.7
    assert pairs[("A", "C")]["candidate"]  # placed over each other: a candidate, the matching must say no
    res = auto_ties.match(doc, a, b)
    assert res.ok, auto_ties.report_text(res)
    assert res.refined >= 100 and res.rms < 2 * CELL
    # the similarity found is the inverse of the error B was given
    assert abs(res.rotation_deg - 1.5) < 0.02 and abs(res.scale_ppm + 2000 / 1.002) < 300
    wrong = auto_ties.match(doc, a, c)
    assert not wrong.ok and wrong.messages, auto_ties.report_text(wrong)
    with pytest.raises(auto_ties.AutoTieError, match="rejected"):
        auto_ties.accept(doc, wrong)
    made = auto_ties.accept(doc, res)
    assert len(made) == auto_ties.MAX_TIES and all(t.name.startswith("auto ") for t in made)
    assert auto_ties.records_of(doc)[0]["ties"] == [t.id for t in made]
    sol = ties.solve(doc, {"A": "fixed", "B": "similarity"})
    assert sol.redundancy > 0 and sol.rms < 2 * CELL
    ties.apply(doc, sol)
    shift, rot, ppm, worst = _errors(doc, b, truth["B"], scenes[0].origin)
    print(f"auto ties A-B: shift {shift * 1000:.2f} mm, rotation {rot:.4f} deg, scale {ppm:+.0f} ppm, "
          f"worst corner {worst * 1000:.2f} mm, refined {res.refined}, RMS {res.rms * 1000:.2f} mm")
    assert shift < 0.002 and rot < 0.03 and abs(ppm) < 300 and worst < 0.003
    # matched again after the solve: the pair agrees, and accepting it replaces the ties it made
    again = auto_ties.match(doc, a, b)
    # B's rasters were built before the solve tilted it a little: placed in plan only (as the 2D view places them),
    # so they are some centimetres off at 100 m of height - said, to be built again
    assert again.ok and again.rotation_deg < 0.03 and abs(again.scale_ppm) < 300
    assert [m["code"] for m in again.messages] == ["stale_raster"], auto_ties.report_text(again)
    before = {t.id for t in ties.ties_of(doc)}
    remade = auto_ties.accept(doc, again, max_ties=6)
    now = {t.id for t in ties.ties_of(doc)}
    assert len(now) == 6 and not (now & before) and {t.id for t in remade} == now
    assert len(auto_ties.records_of(doc)) == 1


def test_a_chunk_moved_after_the_matching_is_refused(tmp_path, scenes):
    doc, _truth = _project(tmp_path, scenes)
    a, b, _c = doc.chunks
    res = auto_ties.match(doc, a, b)
    b.transform = _translate((0.01, 0, 0)) @ b.transform
    with pytest.raises(auto_ties.AutoTieError, match="moved after the matching"):
        auto_ties.accept(doc, res)


def test_chunks_of_one_group_are_not_paired(tmp_path, scenes):
    doc, _truth = _project(tmp_path, scenes)
    groups.add_group(doc, "2025", chunks=["A", "B"])
    pairs = {(p["a_label"], p["b_label"]): p for p in auto_ties.find_pairs(doc)}
    assert not pairs[("A", "B")]["candidate"] and pairs[("A", "B")]["reason"]["code"] == "same_group"
    assert auto_ties.find_pairs(doc, same_group=True)[0]["candidate"]


def test_cli_finds_matches_and_accepts(tmp_path, scenes):
    doc, _truth = _project(tmp_path, scenes)
    runner = CliRunner()
    out = runner.invoke(app, ["ties", "auto", str(tmp_path / "p.ppg"), "--json", str(tmp_path / "auto.json")])
    assert out.exit_code == 0, out.output
    assert "B -> A (ortho): proposed" in out.output and "C -> A (ortho): rejected" in out.output
    assert (tmp_path / "auto.json").exists()
    assert not ties.ties_of(ppg.Document.open(tmp_path / "p.ppg"))  # nothing stored without --accept
    out = runner.invoke(app, ["ties", "auto", str(tmp_path / "p.ppg"), "--pairs", "A:B", "--accept", "--max-ties", "8"])
    assert out.exit_code == 0, out.output
    saved = ppg.Document.open(tmp_path / "p.ppg")
    assert len(ties.ties_of(saved)) == 8
    out = runner.invoke(app, ["ties", "solve", str(tmp_path / "p.ppg"), "-u", "A=fixed,B=similarity", "--yes"])
    assert out.exit_code == 0, out.output


def test_a_chunk_far_off_is_found_over_the_whole_rasters(tmp_path, scenes):
    """B put 15 m away and turned by 40° (a flight with a wrong GPS, or a local frame): no candidate where it lies;
    the search over the whole rasters (margin None) still finds it, and the solve brings it back."""
    scene, _other = scenes
    doc = ppg.Document()
    doc.save(tmp_path / "p.ppg")
    origin = _translate(scene.origin)
    truth_b = origin @ _rot_z(25.0, 1.0, (0.4, -0.2, 0.0))
    centre = np.array(scene.origin) + (2.7, 1.5, 0.0)
    error = _translate(centre) @ _rot_z(40.0, 0.995, (15.0, 6.0, -2.0)) @ _translate(-centre)
    a = _survey(doc, scene, "A", (0.0, 2.6), origin, seed=1)
    b = _survey(doc, scene, "B", (1.4, 4.0), truth_b, error, light=(0.9, 1.2), seed=2)
    assert not auto_ties.find_pairs(doc)[0]["candidate"]
    near = auto_ties.match(doc, a, b)
    assert not near.ok and near.messages[0]["code"] == "no_overlap"
    res = auto_ties.match(doc, a, b, margin=None)
    assert res.ok, auto_ties.report_text(res)
    assert abs(res.rotation_deg - 40.0) < 0.05
    auto_ties.accept(doc, res)
    ties.apply(doc, ties.solve(doc, {"A": "fixed", "B": "similarity"}))
    shift, rot, ppm, worst = _errors(doc, b, truth_b, scene.origin)
    print(f"auto ties far off: shift {shift * 1000:.2f} mm, rotation {rot:.4f} deg, scale {ppm:+.0f} ppm, "
          f"worst corner {worst * 1000:.2f} mm")
    assert worst < 0.004 and abs(ppm) < 500


def test_the_relief_of_the_dems_ties_chunks_whose_colours_changed(tmp_path, scenes):
    """B's orthophoto in other colours (another ground cover, another light); the shaded DEMs still agree. The trench
    is given a micro-relief (stones of a centimetre), as real ground has: the smooth synthetic surface has none."""
    import copy

    import cv2

    scene = copy.copy(scenes[0])
    rough = cv2.GaussianBlur(np.random.default_rng(5).normal(0, 1, scene.heights.shape).astype(np.float32), (0, 0), 4)
    scene.heights = scene.heights + 0.01 * rough / rough.std()
    other = scenes[1]
    doc = ppg.Document()
    doc.save(tmp_path / "p.ppg")
    origin = _translate(scene.origin)
    truth_b = origin @ _rot_z(25.0, 1.0, (0.4, -0.2, 0.0))
    centre = np.array(scene.origin) + (2.7, 1.5, 0.0)
    error = _translate(centre) @ _rot_z(1.5, 1.002, (0.15, -0.10, 0.03)) @ _translate(-centre)
    a = _survey(doc, scene, "A", (0.0, 2.6), origin, seed=1, noise=(3.0, 0.0005))
    recoloured = copy.copy(scene)
    recoloured.texture = other.texture  # B's colours from another ground: the orthophotos have nothing in common
    b2 = _survey(doc, recoloured, "B2", (1.4, 4.0), truth_b, error, seed=2, noise=(3.0, 0.0005))
    assert not auto_ties.match(doc, a, b2).ok
    res = auto_ties.match(doc, a, b2, source="relief")
    assert res.ok, auto_ties.report_text(res)
    auto_ties.accept(doc, res)
    ties.apply(doc, ties.solve(doc, {"A": "fixed", "B2": "similarity"}))
    shift, rot, ppm, worst = _errors(doc, b2, truth_b, scene.origin)
    print(f"auto ties relief: shift {shift * 1000:.2f} mm, rotation {rot:.4f} deg, scale {ppm:+.0f} ppm, "
          f"worst corner {worst * 1000:.2f} mm, refined {res.refined}, RMS {res.rms * 1000:.2f} mm")
    assert worst < 0.010
