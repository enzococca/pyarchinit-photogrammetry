"""Gradual selection of the tie points (as in Metashape): metrics, selection, removal, CLI."""
from __future__ import annotations

import shutil

import numpy as np
import pytest


def _copy(processed, tmp_path, name="copia"):
    """The processed trench in a folder of its own (the session fixture stays untouched)."""
    import pyarchinit_photogrammetry as ppg

    doc, _, _ = processed
    d = tmp_path / name
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name, ignore=shutil.ignore_patterns("dense", "versions"))
    return ppg.Document.open(d / doc.path.name)


def _uncertainty(rec, pid) -> float:
    """Reconstruction uncertainty of one point, computed directly (no vectorisation)."""
    p = rec.points3D[pid]
    h = np.zeros((3, 3))
    for el in p.track.elements:
        image = rec.images[el.image_id]
        cam = rec.cameras[image.camera_id]
        pose = np.asarray(image.cam_from_world().matrix())
        x = pose[:, :3] @ p.xyz + pose[:, 3]
        f = cam.focal_length_idxs()
        fx, fy = cam.params[f[0]], cam.params[f[-1]]
        jc = np.array([[fx / x[2], 0, -fx * x[0] / x[2] ** 2], [0, fy / x[2], -fy * x[1] / x[2] ** 2]])
        j = jc @ pose[:, :3]
        h += j.T @ j
    ev = np.linalg.eigvalsh(np.linalg.inv(h))
    return float(np.sqrt(ev[-1] / ev[0]))


def test_metrics_of_an_alignment(processed, tmp_path):
    from pyarchinit_photogrammetry.engine.sfm import load_reconstruction
    from pyarchinit_photogrammetry.engine.tiepoints import tie_point_metrics

    ch = _copy(processed, tmp_path).chunk
    m = tie_point_metrics(ch)
    n = len(m["ids"])
    assert n > 500 and len(np.unique(m["ids"])) == n and m["images"] == len(ch.aligned_cameras)
    assert not m["synthetic"]
    for key in ("reprojection_error", "image_count", "reconstruction_uncertainty", "projection_accuracy"):
        assert m[key] is not None and len(m[key]) == n, key
    assert m["image_count"].min() >= 2
    assert np.all(m["reconstruction_uncertainty"] >= 1.0)
    assert np.median(m["reprojection_error"]) < 0.5 and 0 < m["mean_reprojection_error"] < 1.0
    assert np.all(m["projection_accuracy"] > 0.5) and np.median(m["projection_accuracy"]) < 20  # keypoint size, px
    rec = load_reconstruction(ch)
    rec.update_point_3d_errors()  # COLMAP's own mean reprojection error of every point
    ref = np.array([rec.points3D[int(i)].error for i in m["ids"]])
    assert np.allclose(ref, m["reprojection_error"], atol=1e-6)
    # photos, not measurements: a COLMAP track can hold two measurements in the same photo
    photos = np.array([len({el.image_id for el in rec.points3D[int(i)].track.elements}) for i in m["ids"]])
    assert np.array_equal(photos, m["image_count"])
    for k in np.linspace(0, n - 1, 7).astype(int):
        assert m["reconstruction_uncertainty"][k] == pytest.approx(_uncertainty(rec, int(m["ids"][k])), rel=1e-6)


def test_gradual_selection_finds_bad_points(processed, tmp_path):
    from pyarchinit_photogrammetry.engine.sfm import load_reconstruction
    from pyarchinit_photogrammetry.engine.tiepoints import select_tie_points, tie_point_metrics

    ch = _copy(processed, tmp_path).chunk
    rec = load_reconstruction(ch)
    m0 = tie_point_metrics(ch, rec)
    rng = np.random.default_rng(3)
    span = float(np.ptp(m0["xyz"], axis=0).max())
    bad = rng.choice(m0["ids"], 12, replace=False)
    for pid in bad:  # mismatched points: 3 % of the trench away from where the rays meet
        p = rec.points3D[int(pid)]
        p.xyz = p.xyz + rng.normal(size=3) / np.sqrt(3) * 0.03 * span
    rec.write(str(ch.product_path("reconstruction")))
    m = tie_point_metrics(ch)
    sel = select_tie_points(ch, max_reprojection_error=2.0, metrics=m)
    assert set(bad.tolist()) <= set(sel["ids"].tolist())
    assert sel["selected"] < len(bad) + 0.02 * sel["total"]  # the good points stay
    # every criterion selects exactly what its threshold says, and they add up
    sel = select_tie_points(ch, max_reprojection_error=1.0, min_image_count=3, max_reconstruction_uncertainty=6.0,
                            max_projection_accuracy=8.0, metrics=m)
    by = sel["by_criterion"]
    assert by["reprojection_error"] == int(np.sum(m["reprojection_error"] > 1.0))
    assert by["image_count"] == int(np.sum(m["image_count"] < 3))
    assert by["reconstruction_uncertainty"] == int(np.sum(m["reconstruction_uncertainty"] > 6.0))
    assert by["projection_accuracy"] == int(np.sum(m["projection_accuracy"] > 8.0))
    expect = ((m["reprojection_error"] > 1.0) | (m["image_count"] < 3) | (m["reconstruction_uncertainty"] > 6.0)
              | (m["projection_accuracy"] > 8.0))
    assert np.array_equal(sel["mask"], expect) and sel["selected"] == int(expect.sum())
    assert max(v for v in by.values()) <= sel["selected"] <= sum(by.values())
    assert sel["remaining"] == sel["total"] - sel["selected"] and sel["images_below_minimum"] == 0
    nothing = select_tie_points(ch, metrics=m)
    assert nothing["selected"] == 0 and nothing["by_criterion"] == {}


def test_removal_keeps_the_cameras_and_the_accuracy(processed, tmp_path):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.engine.sfm import load_reconstruction
    from pyarchinit_photogrammetry.engine.tiepoints import remove_tie_points, select_tie_points

    doc = _copy(processed, tmp_path)
    ch = doc.chunk
    aligned = {c.key for c in ch.aligned_cameras}
    poses = {c.key: np.array(c.transform) for c in ch.aligned_cameras}
    before = len(ch.tie_points)
    sel = select_tie_points(ch, max_reprojection_error=0.8, min_image_count=3)
    assert 0 < sel["selected"] < 0.5 * sel["total"]
    out = remove_tie_points(ch, sel["ids"])
    assert out["removed"] == sel["selected"] and out["remaining"] == before - sel["selected"]
    assert out["reprojection_error_after"] < out["reprojection_error_before"]
    assert {c.key for c in ch.aligned_cameras} == aligned
    assert all(np.allclose(c.transform, poses[c.key]) for c in ch.aligned_cameras)  # no optimization asked
    rec = load_reconstruction(ch)
    assert rec.num_points3D() == len(ch.tie_points) == out["remaining"]
    assert not any(rec.exists_point3D(int(i)) for i in sel["ids"])
    assert ch.update_transform()["control_rmse"] < 0.002  # the georeference is as good as before
    doc.save()
    again = ppg.Document.open(doc.path).chunk
    assert len(again.tie_points) == out["remaining"]
    assert again.meta["sfm"]["tie_point_cleaning"][-1]["removed"] == out["removed"]
    assert remove_tie_points(ch, sel["ids"])["removed"] == 0  # already gone: nothing to do


def test_removal_refuses_to_empty_the_model_or_a_photo(processed, tmp_path):
    from pyarchinit_photogrammetry.engine.sfm import load_reconstruction
    from pyarchinit_photogrammetry.engine.tiepoints import MIN_IMAGE_POINTS, remove_tie_points, select_tie_points

    ch = _copy(processed, tmp_path).chunk
    everything = select_tie_points(ch, max_reprojection_error=0.0)
    assert everything["selected"] == everything["total"] and everything["images_below_minimum"] > 0
    with pytest.raises(ValueError, match="tie points would remain"):
        remove_tie_points(ch, everything["ids"])
    rec = load_reconstruction(ch)
    image = max(rec.images.values(), key=lambda im: im.num_points3D)
    own = [q.point3D_id for q in image.get_observation_points2D()]
    keep = MIN_IMAGE_POINTS - 1  # one point fewer than a photo needs
    with pytest.raises(ValueError, match="photos would keep fewer"):
        remove_tie_points(ch, own[keep:])
    assert load_reconstruction(ch).num_points3D() == rec.num_points3D()  # nothing was removed


def test_clean_with_optimization(processed, tmp_path):
    ch = _copy(processed, tmp_path).chunk
    n_cams = len(ch.aligned_cameras)
    out = ch.clean_tie_points(max_reprojection_error=1.0, max_reconstruction_uncertainty=8.0, optimize=True)
    assert out["removed"] == out["selected"] > 0 and set(out["by_criterion"]) == {"reprojection_error",
                                                                                   "reconstruction_uncertainty"}
    assert len(ch.aligned_cameras) == n_cams
    assert out["optimize_error"] < 0.5
    assert out["georeference"]["control_rmse"] < 0.002
    assert ch.meta["sfm"]["optimize"]["reprojection_error"] == pytest.approx(out["optimize_error"])


def test_model_from_imported_poses_is_flagged(processed, tmp_path):
    from pyarchinit_photogrammetry.engine.sfm import write_reconstruction_from_chunk
    from pyarchinit_photogrammetry.engine.tiepoints import select_tie_points, tie_point_metrics

    ch = _copy(processed, tmp_path).chunk
    (ch.data_dir / "sfm" / "database.db").unlink()  # as after an import: no keypoints
    write_reconstruction_from_chunk(ch)
    m = tie_point_metrics(ch)
    assert m["synthetic"] and m["projection_accuracy"] is None
    assert float(np.max(m["reprojection_error"])) < 1e-3
    sel = select_tie_points(ch, max_projection_accuracy=3.0, max_reconstruction_uncertainty=50.0, metrics=m)
    assert sel["by_criterion"]["projection_accuracy"] is None
    assert sel["by_criterion"]["reconstruction_uncertainty"] == int(np.sum(m["reconstruction_uncertainty"] > 50.0))


def test_cli_clean_tie_points(processed, tmp_path):
    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app

    doc = _copy(processed, tmp_path)
    n = len(doc.chunk.tie_points)
    runner = CliRunner()
    r = runner.invoke(app, ["clean-tie-points", str(doc.path), "--max-reprojection-error", "1.0",
                            "--min-image-count", "3", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "selected by reprojection_error" in r.output and "selected " in r.output
    assert len(ppg.Document.open(doc.path).chunk.tie_points) == n  # a dry run changes nothing
    r = runner.invoke(app, ["clean-tie-points", str(doc.path)])
    assert r.exit_code != 0  # no criterion
    r = runner.invoke(app, ["clean-tie-points", str(doc.path), "--max-reprojection-error", "1.0"])
    assert r.exit_code == 0, r.output
    assert "removed" in r.output
    assert len(ppg.Document.open(doc.path).chunk.tie_points) < n
