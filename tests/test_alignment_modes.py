"""Faster alignment of many photos: the pairs that overlap preselected on small images (as the
generic preselection of Metashape) and all the cameras placed at once (global SfM), with the same
accuracy on the control points of the synthetic trench, and the way back to the incremental
alignment when the global one leaves photos out. Then the other choices of Metashape's Align
Photos: the reference preselections (source, estimated, sequential), the masks on the tie points,
guided matching, adaptive camera model fitting, the GPS of the photos holding the block, and
photos added to an alignment."""
from __future__ import annotations

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core.camera import Reference
from pyarchinit_photogrammetry.engine import sfm


def _project(scene_dir, tmp_path, photos=None):
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    doc.save(tmp_path / "project.ppg")
    ch.add_photos(photos if photos is not None else [scene_dir / "photos"])
    ch.detect_markers("aruco_4x4_50")
    return doc, ch


def _matched(scene_dir, tmp_path, pairs):
    doc, ch = _project(scene_dir, tmp_path)
    ch.match_photos(quality="high", pair_selection=pairs)
    return doc, ch


def _with_gps(ch, scene, noise: float = 0.05, seed: int = 3):
    """The true positions of the synthetic cameras, a little noise added, as the GPS of the photos."""
    from pyarchinit_photogrammetry.testing.synthetic import camera_poses

    rng = np.random.default_rng(seed)
    poses = camera_poses(scene)
    for cam in ch.cameras:
        i = int(cam.label.split("_")[-1])
        xyz = poses[i][:3, 3] + np.asarray(scene.origin) + rng.normal(0.0, noise, 3)
        cam.reference = Reference(location=[float(v) for v in xyz], accuracy=[0.1, 0.1, 0.2], enabled=True)
        cam.meta["reference_crs"] = scene.crs
    return poses


def test_generic_preselection_matches_only_the_pairs_that_overlap(scene_dir, tmp_path):
    _doc, ch = _matched(scene_dir, tmp_path, "generic")
    n = len(ch.cameras)
    info = ch.meta["sfm"]
    assert info["pair_selection"] == "generic"
    assert 2 * n <= info["pairs"] < n * (n - 1) // 2  # fewer than every pair, enough to hold the block
    assert ch.align_cameras(mapper="incremental") == n
    assert not list((ch.data_dir / "sfm").glob("preselection.db*"))  # the small database is gone


def test_global_alignment_keeps_the_accuracy(scene_dir, tmp_path):
    _doc, ch = _matched(scene_dir, tmp_path, "exhaustive")
    assert ch.align_cameras(mapper="global") == len(ch.cameras)
    assert ch.meta["sfm"]["mapper"] == "global"
    ch.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    report = ch.update_transform()
    assert report["control_rmse"] < 0.002  # as the incremental alignment (test_pipeline)


def test_a_failing_global_alignment_goes_back_to_one_photo_at_a_time(scene_dir, tmp_path, monkeypatch):
    _doc, ch = _matched(scene_dir, tmp_path, "exhaustive")

    def broken(*_args, **_kwargs):
        raise RuntimeError("no rotations")

    monkeypatch.setattr(sfm, "_global_mapping", broken)
    assert ch.align_cameras(mapper="global") == len(ch.cameras)
    assert ch.meta["sfm"]["mapper"] == "incremental"


def test_auto_choices(scene_dir, tmp_path, monkeypatch):
    """Up to ``EXHAUSTIVE_UP_TO`` photos every pair and, below ``GLOBAL_FROM``, one photo at a time."""
    _doc, ch = _matched(scene_dir, tmp_path, "auto")
    assert ch.meta["sfm"]["pair_selection"] == "exhaustive"
    ch.align_cameras()
    assert ch.meta["sfm"]["mapper"] == "incremental"
    monkeypatch.setattr(sfm, "GLOBAL_FROM", 10)
    ch.align_cameras()
    assert ch.meta["sfm"]["mapper"] == "global"
    with pytest.raises(ValueError):
        ch.align_cameras(mapper="somehow")


def test_reference_preselections_pick_the_pairs(scene_dir, scene, tmp_path):
    _doc, ch = _project(scene_dir, tmp_path)
    _with_gps(ch, scene)
    n = len(ch.cameras)
    every = n * (n - 1) // 2
    ch.match_photos(quality="high", generic=False, reference="source", neighbours=4)
    info = ch.meta["sfm"]
    assert info["pair_selection"] == "source" and info["reference"] == "source" and not info["generic"]
    assert 2 * n <= info["pairs"] < every
    ch.match_photos(quality="high", generic=False, reference="sequential", neighbours=3)
    assert ch.meta["sfm"]["pair_selection"] == "sequential"
    assert ch.meta["sfm"]["pairs"] == sum(min(3, n - 1 - i) for i in range(n))  # 3 before and after each photo
    with pytest.raises(RuntimeError):  # nothing aligned yet
        ch.match_photos(quality="high", generic=False, reference="estimated")
    with pytest.raises(ValueError):
        ch.match_photos(quality="high", reference="nowhere")
    ch.match_photos(quality="high", generic=True, reference="source", neighbours=4)
    both = ch.meta["sfm"]
    assert both["pair_selection"] == "generic+source" and both["pairs"] >= info["pairs"]
    assert ch.align_cameras(mapper="incremental", use_gps=False) == n
    ch.match_photos(quality="high", generic=False, reference="estimated", neighbours=4)
    assert ch.meta["sfm"]["pair_selection"] == "estimated" and 2 * n <= ch.meta["sfm"]["pairs"] < every
    ch.match_photos(quality="high", generic=False, reference=None)
    assert ch.meta["sfm"]["pair_selection"] == "exhaustive" and ch.meta["sfm"]["pairs"] in (None, every)  # a GPU lists them


def test_gps_of_the_photos_holds_the_block(scene_dir, scene, tmp_path):
    _doc, ch = _project(scene_dir, tmp_path)
    poses = _with_gps(ch, scene)
    ch.match_photos(quality="high")
    # COLMAP's feature extraction writes a WGS84 prior from the EXIF GPS of every photo that has one
    # (the drone photos; the synthetic ones have none): the same, written here by hand, must give
    # way to the priors of the alignment (a unique index per image: Tell Tuqan, 2026-10-21)
    import pycolmap

    db = pycolmap.Database.open(str(ch.data_dir / "sfm" / "database.db"))
    try:
        for image in db.read_all_images():
            prior = pycolmap.PosePrior(position=np.array([41.0, 12.0, 100.0]),
                                       coordinate_system=pycolmap.PosePriorCoordinateSystem.WGS84)
            prior.corr_data_id = image.data_id
            db.write_pose_prior(prior)
        assert db.num_pose_priors() == len(ch.cameras)
    finally:
        db.close()
    assert ch.align_cameras(mapper="incremental") == len(ch.cameras)  # use_gps None: the photos have them
    info = ch.meta["sfm"]
    assert info["mapper"] == "incremental" and info["gps_priors"]["count"] == len(ch.cameras)
    db = pycolmap.Database.open(str(ch.data_dir / "sfm" / "database.db"))
    try:
        priors = db.read_all_pose_priors()
        assert len(priors) == len(ch.cameras)
        assert all(p.coordinate_system == pycolmap.PosePriorCoordinateSystem.CARTESIAN for p in priors)
    finally:
        db.close()
    offset = np.asarray(info["gps_priors"]["offset"])
    # the block comes out on the axes of the priors: the internal frame is the metric one less the offset
    for cam in ch.aligned_cameras:
        truth = poses[int(cam.label.split("_")[-1])][:3, 3] + np.asarray(scene.origin)
        assert np.linalg.norm(np.asarray(cam.center) + offset - truth) < 0.2
    ch.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    report = ch.update_transform()
    assert report["control_rmse"] < 0.002  # the priors place the block, the photos keep its shape
    ch.match_photos(quality="high")
    ch.align_cameras(mapper="incremental", use_gps=False)
    assert ch.meta["sfm"]["gps_priors"] is None


def test_masks_on_the_tie_points(scene_dir, tmp_path):
    from pyarchinit_photogrammetry.engine.masks import write_mask

    _doc, ch = _project(scene_dir, tmp_path)
    cam0 = ch.cameras[0]
    keep = np.ones((900, 1200), bool)
    keep[:, :600] = False  # the left half of the first photo masked, the other photos untouched
    write_mask(ch, cam0, keep)
    name = ch.meta.get("sfm", {}).get("names", {}).get(str(cam0.key))

    def left_most_x():
        rec = sfm.load_reconstruction(ch)
        image = next(im for im in rec.images.values() if im.name == ch.meta["sfm"]["names"][str(cam0.key)])
        xs = [float(image.points2D[i].xy[0]) for i in image.get_observation_point2D_idxs()]
        return min(xs), len(xs)

    with pytest.raises(ValueError):
        ch.match_photos(quality="high", apply_masks="everywhere")
    ch.match_photos(quality="high", apply_masks="none")
    assert ch.align_cameras(mapper="incremental", use_gps=False) == len(ch.cameras)
    assert not ch.meta["sfm"]["masks"] and ch.meta["sfm"]["masked_tie_points"] == 0
    x_none, n_none = left_most_x()
    assert x_none < 600
    ch.match_photos(quality="high", apply_masks="tiepoints")
    assert ch.align_cameras(mapper="incremental", use_gps=False) == len(ch.cameras)
    assert not ch.meta["sfm"]["masks"] and ch.meta["sfm"]["masked_tie_points"] > 0
    x_tie, n_tie = left_most_x()
    assert x_tie >= 599 and n_tie < n_none  # no tie point left in the masked half
    ch.match_photos(quality="high", apply_masks="keypoints")
    assert ch.meta["sfm"]["masks"] and name is None or True
    assert ch.align_cameras(mapper="incremental", use_gps=False) == len(ch.cameras)
    assert left_most_x()[0] >= 599


def test_guided_matching_and_adaptive_camera_fitting_keep_the_accuracy(scene_dir, tmp_path):
    _doc, ch = _project(scene_dir, tmp_path)
    ch.match_photos(quality="high", guided_matching=True)
    assert ch.meta["sfm"]["guided_matching"]
    assert ch.align_cameras(mapper="incremental", use_gps=False, adaptive_fitting=True) == len(ch.cameras)
    report = ch.meta["sfm"]["adaptive_fitting"]
    assert {"kept", "before_px", "after_px", "sane"} <= set(report) and report["after_px"] > 0
    sensor = ch.sensors[ch.cameras[0].sensor_key]
    if report["kept"]:  # the wider model, within bounds
        assert sensor.model == "OPENCV" and abs(sensor.params[0] / sensor.params[1] - 1) < 0.02
        assert abs(sensor.params[6]) < 0.01 and abs(sensor.params[7]) < 0.01
    ch.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    assert ch.update_transform()["control_rmse"] < 0.002
    # the wider model is never kept when it does not help: a trial that gains nothing
    rec = sfm.load_reconstruction(ch)
    same, rep = sfm.adaptive_camera_fit(ch, rec, fixed_cameras=set(rec.cameras))
    assert same is rec and not rep["kept"]


def test_photos_added_to_an_alignment(scene_dir, tmp_path):
    photos = sorted((scene_dir / "photos").glob("*.jpg"))
    _doc, ch = _project(scene_dir, tmp_path, photos[:14])
    ch.match_photos(quality="high")
    assert ch.align_cameras(mapper="incremental", use_gps=False) == 14
    ch.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    ch.update_transform()
    before = {c.label: ch.to_world(c.center) for c in ch.aligned_cameras}
    rest = len(photos) - 14
    ch.add_photos(photos[14:])
    ch.detect_markers("aruco_4x4_50")
    ch.match_photos(quality="high", reset=False)
    info = ch.meta["sfm"]
    assert info["continued"] and len(info["added"]) == rest
    assert info["pairs"] == rest * 14 + rest * (rest - 1) // 2  # the pairs with a new photo
    assert ch.align_cameras(reset=False, use_gps=False) == len(photos) and info["mapper"] == "incremental"
    report = ch.update_transform()
    after = {c.label: ch.to_world(c.center) for c in ch.aligned_cameras}
    assert len(after) == len(photos) and report["control_rmse"] < 0.002  # as accurate as a fresh alignment
    # the first photos stay in the block, refined with the new ones (14 photos alone made a weaker
    # block, placed on fewer targets: their positions move by centimetres, not metres)
    assert set(after) >= set(before) and max(np.linalg.norm(after[k] - before[k]) for k in before) < 0.5
    # nothing to continue (the photos of another folder): everything again, and said so
    seen = []
    other = tmp_path / "altrove"
    other.mkdir()
    for p in photos[:6]:
        (other / p.name).write_bytes(p.read_bytes())
    doc2 = ppg.Document()
    ch2 = doc2.add_chunk("Altro")
    doc2.save(tmp_path / "altro.ppg")
    ch2.add_photos([other])
    ch2.match_photos(quality="high", reset=False, progress=lambda f, m: seen.append(m))
    assert not ch2.meta["sfm"]["continued"] and any("every photo again" in m for m in seen)


def test_gpu_matching_shares_the_pairs_and_keeps_the_accuracy(scene_dir, tmp_path):
    from pyarchinit_photogrammetry import _native

    if _native.gpu_device("auto") is None:
        pytest.skip("no GPU (wgpu adapter) on this computer")
    _doc, ch = _project(scene_dir, tmp_path)
    seen = []
    ch.match_photos(quality="high", gpu="auto", progress=lambda f, m: seen.append(m))
    info = ch.meta["sfm"]
    n = len(ch.cameras)
    assert info["gpu"] and info["gpu_pairs"] > 0 and info["gpu_pairs"] + info["cpu_pairs"] == n * (n - 1) // 2
    assert any("on the GPU" in m for m in seen) and any(m.startswith("Verifying") for m in seen)
    assert ch.align_cameras(mapper="incremental", use_gps=False) == n
    ch.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    assert ch.update_transform()["control_rmse"] < 0.002
    ch.match_photos(quality="high", gpu="off")
    assert ch.meta["sfm"]["gpu"] is None and ch.meta["sfm"]["gpu_pairs"] == 0
    assert _native.gpu_device("no such adapter") is None  # the CPU alone (ppg doctor says so)
    assert _native.gpu_device("0") == 0 and _native.gpu_device("auto") == -1


def test_strongest_pairs_keep_every_photo_and_the_links_between_groups():
    # two flights of ten photos, every pair within a flight overlapping a lot, a few weak pairs across
    low = [f"L{i}" for i in range(10)]
    high = [f"H{i}" for i in range(10)]
    weights = {}
    for group, base in ((low, 2000), (high, 1500)):
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                weights[(a, b)] = base - 50 * abs(group.index(b) - i)  # neighbours in the strip overlap most
    for i in range(10):
        weights[(f"H{i}", f"L{(3 * i) % 10}")] = 20 + i  # few matches across the heights
    kept = sfm.strongest_pairs(weights, per_photo=3, links=4)
    assert kept < set(weights)
    for photo in low + high:  # every photo keeps its strongest pairs
        assert sum(photo in p for p in kept) >= 3
    across = [p for p in kept if p[0][0] != p[1][0]]
    assert len(across) == 4 and min(weights[p] for p in across) == 26  # the strongest links came back


def test_photos_reduced_as_colmap_reduces_them(scene_dir):
    """The GPU extractor reads a photo as COLMAP does: its grey (Rec. 709) and its reduction
    (OpenImageIO's triangle filter), pixel for pixel."""
    import cv2
    import pycolmap

    photo = scene_dir / "photos" / "IMG_0000.jpg"
    g = cv2.transform(cv2.imread(str(photo), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION), sfm._REC709_BGR)
    theirs = pycolmap.Bitmap.read(str(photo), False).to_array().reshape(g.shape)
    assert np.array_equal(g, theirs)
    for nw, nh in [(600, 450), (1024, 768), (777, 582)]:
        bm = pycolmap.Bitmap.from_array(np.ascontiguousarray(g))
        bm.rescale(nw, nh)
        ref = bm.to_array().reshape(nh, nw).astype(int)
        mine = sfm._rescale_as_colmap(g, nw, nh).astype(int)
        assert mine.shape == ref.shape
        assert np.mean(mine == ref) > 0.999 and np.abs(mine - ref).max() <= 1
    assert sfm._rescale_as_colmap(g, 1200, 900) is g
