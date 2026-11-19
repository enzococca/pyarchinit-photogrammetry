"""``ppg bench adjust`` (``bench/adjust.py``): the native adjustment, COLMAP's and scipy's on the plain problem of the
reconstruction of a kept project, side by side in one record. The runs here are on the session's synthetic trench
copied into the test's folder: the project is opened and never written, the records hold no path and no photo name.
The tests that solve need the native core of API 12 and psutil (the child process); the checks run everywhere."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.bench import adjust, record
from pyarchinit_photogrammetry.engine import bundle
from pyarchinit_photogrammetry.engine.sfm import load_reconstruction

pycolmap = pytest.importorskip("pycolmap")

needs_core = pytest.mark.skipif(not _native.has("bundle") or (_native.api() or 0) < _native.BUNDLE_API,
                                reason=bundle.NATIVE_MISSING)
# the only reasons a record is not valid for (record.judge), and the name of its file
REASONS = (r"slow disk link: \d+ Mb/s", r"slow disk: \d+ MB/s", r"online-only photos: \d+", r"failed: .+")
FILE_NAME = re.compile(r"^t-\d{14}-\d+\.json$")
STEP_KEYS = {"step", "seconds", "peak_memory_mb", "stages", "engine"}
PER_SOLVER = ("cost", "iterations", "gradient_max", "converged")


def a_copy(processed, tmp_path: Path) -> Path:
    """The processed trench copied into ``tmp_path`` (project, files and photos): the project file of the copy."""
    doc, _, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    return d / doc.path.name


def snapshot(project: Path) -> dict:
    """The bytes and the modification time of the project file and of every file of its model."""
    model = ppg.Document.open(project).chunk.product_path("reconstruction")
    files = [project, *sorted(p for p in model.rglob("*") if p.is_file())]
    return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}


def the_record(folder: Path, paths: list[Path]) -> dict:
    assert len(paths) == 1 and paths[0].parent == folder and FILE_NAME.match(paths[0].name)
    return json.loads(paths[0].read_text(encoding="utf-8"))


def strings_of(value, where: str = "record"):
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings_of(key, where + ".<key>")
            yield from strings_of(item, f"{where}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from strings_of(item, f"{where}[{i}]")


@needs_core
def test_colmap_and_native_solve_the_same_plain_problem(processed, tmp_path):
    """The plain problem of the reconstruction has two residual rows per observation and COLMAP's adjuster, set up
    on the same problem, reports the same number of residuals; the two end points evaluate to the same cost by the
    native evaluation, within 1e-4 relative (gate row 1); the two held photos stay where they were."""
    project = a_copy(processed, tmp_path)
    chunk = ppg.Document.open(project).chunk
    rec = load_reconstruction(chunk)
    pb, ids = bundle.problem_from_reconstruction(rec)
    start = bundle.evaluate(pb)
    assert len(start.residuals) == 2 * len(pb.obs_image) and len(pb.fixed_images) == 2
    held = tuple(ids["images"][i] for i in sorted(pb.fixed_images))
    out = adjust.colmap_adjust(rec, held, adjust.MAX_ITERATIONS)
    assert out.residuals == 2 * len(pb.obs_image) and out.seconds > 0 and out.iterations > 0
    assert out.termination == "convergence" and out.linear_solver.endswith("schur")
    after, ids_after = bundle.problem_from_reconstruction(rec)
    assert ids_after == ids
    for i in pb.fixed_images:
        assert np.array_equal(after.centres[i], pb.centres[i]) and np.array_equal(after.rotations[i], pb.rotations[i])
    colmap = bundle.evaluate(after).cost
    nat = bundle.adjust(pb, max_iterations=adjust.MAX_ITERATIONS, solver="native")
    assert nat.solver == "native" and nat.fallback is None
    native = bundle.evaluate(bundle.problem_at(pb, nat)).cost
    assert colmap < start.cost and native < start.cost
    assert abs(native - colmap) <= 1e-4 * colmap


@needs_core
def test_a_bench_adjust_run_writes_one_record(processed, tmp_path):
    pytest.importorskip("psutil")
    project = a_copy(processed, tmp_path)
    before = snapshot(project)
    chunk = ppg.Document.open(project).chunk
    rec = load_reconstruction(chunk)
    paths = adjust.run(project, "t", repeat=1, records=tmp_path)
    r = the_record(tmp_path, paths)
    assert list(r) == list(record.KEYS) and r["schema"] == 1 and r["name"] == "t" and r["error"] is None
    assert [s["step"] for s in r["steps"]] == ["bundle_native", "bundle_colmap", "bundle_scipy"]
    for s in r["steps"]:
        assert set(s) == STEP_KEYS and s["seconds"] > 0 and s["peak_memory_mb"] > 0 and s["stages"] == {}
        solver = s["step"].removeprefix("bundle_")
        assert s["engine"]["requested"] == s["engine"]["used"] == solver and s["engine"]["fallback"] is None
        assert isinstance(s["engine"]["termination"], str)
    res = r["results"]
    assert res["photos"] == len(chunk.aligned_cameras) == 28 and res["points"] == rec.num_points3D()
    assert res["observations"] == sum(p.track.length() for p in rec.points3D.values())
    figures = ["bundle_cost_initial", "bundle_gap_colmap", "bundle_gap_scipy", "bundle_sigma_ratio_scipy"]
    figures += [f"bundle_{what}_{s}" for s in adjust.SOLVERS for what in PER_SOLVER]
    for key in figures:
        assert np.isfinite(res[key]), key
    for s in adjust.SOLVERS:
        assert res[f"bundle_cost_{s}"] < res["bundle_cost_initial"] and res[f"bundle_iterations_{s}"] > 0
        assert res[f"bundle_converged_{s}"] == 1 and res[f"bundle_gradient_max_{s}"] >= 0
    assert res["bundle_gap_colmap"] <= 1e-4  # gate row 1: the native cost against COLMAP's on the same problem
    assert res["bundle_gap_scipy"] <= 1e-6  # and not above scipy's
    assert 0 <= res["bundle_sigma_ratio_scipy"] <= 0.1  # the parameters within a tenth of their sigma
    settings = r["settings"]
    assert settings["solvers"] == list(adjust.SOLVERS) and settings["max_iterations"] == adjust.MAX_ITERATIONS
    assert settings["linear_solver"] == "dense" and settings["linear_solver_colmap"].endswith("schur")
    assert settings["scipy_max_photos"] == adjust.SCIPY_MAX_PHOTOS == 100
    assert settings["pycolmap"] == pycolmap.__version__
    assert r["set"]["photos"] == 28 and r["set"]["bytes"] > 0 and r["set"]["online_only"] == 0
    assert "read_mb_s" in r["disk"] and isinstance(r["machine"]["adapters"], list)
    assert r["valid"] in (True, False) and r["valid"] == (r["invalid_reasons"] == [])
    assert all(any(re.fullmatch(p, reason) for p in REASONS) for reason in r["invalid_reasons"])
    assert not any("scipy" in w for w in r["warnings"])
    photos = [Path(c.path) for c in chunk.cameras]
    for where, text in strings_of(r):
        assert str(tmp_path) not in text, where
        assert not any(p.name in text or p.stem in text for p in photos), where
    assert snapshot(project) == before  # the project is opened and never written
    assert not (tmp_path / "t-1").exists()  # the work folder of the run is removed


@needs_core
def test_scipy_is_left_out_above_its_limit(processed, tmp_path, monkeypatch):
    pytest.importorskip("psutil")
    monkeypatch.setattr(adjust, "SCIPY_MAX_PHOTOS", 5)
    project = a_copy(processed, tmp_path)
    r = the_record(tmp_path, adjust.run(project, "t", repeat=1, records=tmp_path))
    assert [s["step"] for s in r["steps"]] == ["bundle_native", "bundle_colmap"] and r["error"] is None
    assert any("scipy" in w and "28 photos" in w for w in r["warnings"])
    assert r["settings"]["scipy_max_photos"] == 5
    assert "bundle_gap_colmap" in r["results"] and r["results"]["bundle_gap_colmap"] <= 1e-4
    assert not any(key.endswith("_scipy") for key in r["results"])


@needs_core
def test_a_point_seen_once_is_left_out_of_every_solver(processed, tmp_path):
    """A tie point with one observation cannot be adjusted (the native solver would hold it, scipy move it along
    its ray, COLMAP adjust it): it is left out of the reconstruction every solver loads, so that the three get the
    same problem, and the record says so."""
    pytest.importorskip("psutil")
    project = a_copy(processed, tmp_path)
    chunk = ppg.Document.open(project).chunk
    rec = load_reconstruction(chunk)
    points, observations = rec.num_points3D(), sum(p.track.length() for p in rec.points3D.values())
    image = next(im for im in rec.images.values() if im.has_pose)
    idx = next(i for i, p in enumerate(image.points2D) if not p.has_point3D())
    track = pycolmap.Track()
    track.add_element(image.image_id, idx)
    ahead = image.projection_center() + 5.0 * image.cam_from_world().rotation.matrix().T @ np.array([0.0, 0.0, 1.0])
    pid = rec.add_point3D(ahead, track)
    assert rec.images[image.image_id].points2D[idx].point3D_id == pid
    rec.write(str(chunk.product_path("reconstruction")))
    r = the_record(tmp_path, adjust.run(project, "t", repeat=1, solvers=("native", "colmap"), records=tmp_path))
    assert r["error"] is None and [s["step"] for s in r["steps"]] == ["bundle_native", "bundle_colmap"]
    assert r["results"]["points"] == points and r["results"]["observations"] == observations
    assert any("1 tie point" in w and "fewer than two" in w for w in r["warnings"])
    assert r["results"]["bundle_gap_colmap"] <= 1e-4


@needs_core
def test_camera_sigmas_are_those_of_the_dense_inverse():
    """The sigmas of the photo and intrinsic columns by the reduced camera system are those of the dense inverse
    of the whole normal matrix (``bundle_problems.sigmas``), which eliminates nothing; a problem with other
    residuals than tie points is refused."""
    from pyarchinit_photogrammetry.testing import bundle_problems

    pb = bundle_problems.case("ties")
    res = bundle.adjust(pb, max_iterations=50, solver="native")
    n_cam = bundle._Layout(pb).point_col
    dense = bundle_problems.sigmas(pb, res)[:n_cam]
    reduced = adjust.camera_sigmas(pb, res)
    assert reduced.shape == dense.shape == (n_cam,) and np.all(reduced > 0)
    assert np.abs(reduced / dense - 1).max() < 1e-6
    with pytest.raises(ValueError, match="tie points alone"):
        adjust.camera_sigmas(bundle_problems.case("control_points"), res)


def test_the_checks_before_a_run(tmp_path):
    """What :func:`adjust.check` refuses before any child starts: a name a file cannot take, a solver it does not
    know or named twice, no solver, a repeat below 1, a project that is not a file, a work folder that exists."""
    pytest.importorskip("psutil")
    project = tmp_path / "kept" / "project.ppg"
    project.parent.mkdir()
    project.write_text("{}", encoding="utf-8")
    assert adjust.check(project, "t", records=tmp_path / "rec") == (project.resolve(), (tmp_path / "rec").resolve())
    assert adjust.check(project, "t")[1] == Path.cwd()
    with pytest.raises(ValueError, match="letters, digits"):
        adjust.check(project, "../t", records=tmp_path)
    with pytest.raises(ValueError, match="native, colmap or scipy"):
        adjust.check(project, "t", solvers=("native", "ceres"), records=tmp_path)
    with pytest.raises(ValueError, match="twice"):
        adjust.check(project, "t", solvers=("native", "native"), records=tmp_path)
    with pytest.raises(ValueError, match="no solver"):
        adjust.check(project, "t", solvers=(), records=tmp_path)
    with pytest.raises(ValueError, match="repeat"):
        adjust.check(project, "t", repeat=0, records=tmp_path)
    with pytest.raises(ValueError, match="not a project file"):
        adjust.check(project.parent, "t", records=tmp_path)
    (tmp_path / "t-2").mkdir()
    with pytest.raises(FileExistsError, match="t-2"):
        adjust.check(project, "t", repeat=2, records=tmp_path)
    assert adjust.check(project, "t", repeat=1, records=tmp_path) == (project.resolve(), tmp_path.resolve())
