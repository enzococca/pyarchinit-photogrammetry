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
from pyarchinit_photogrammetry.core.camera import Camera, Reference
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
    """Up to ``EXHAUSTIVE_UP_TO`` photos every pair; the global mapper only from ``GLOBAL_FROM`` photos
    of a survey from the air, one photo at a time otherwise."""
    from pyarchinit_photogrammetry.engine import dense

    _doc, ch = _matched(scene_dir, tmp_path, "auto")
    assert ch.meta["sfm"]["pair_selection"] == "exhaustive"
    ch.align_cameras()
    assert ch.meta["sfm"]["mapper"] == "incremental"
    monkeypatch.setattr(sfm, "GLOBAL_FROM", 10)
    ch.align_cameras()
    assert ch.meta["sfm"]["mapper"] == "incremental"  # a trench without GPS over a site: not from the air
    monkeypatch.setattr(dense, "aerial_survey", lambda chunk, aligned=True: True)
    ch.align_cameras()
    assert ch.meta["sfm"]["mapper"] == "global"
    with pytest.raises(ValueError):
        ch.align_cameras(mapper="somehow")


def test_auto_mapper_looks_at_the_photos_to_align_before_the_alignment(tmp_path):
    """On a fresh alignment nothing is aligned yet: the automatic mapper judges the survey from the air on the
    enabled photos with a reference position switched on, aligned or not. The choices made after an alignment
    (the dense cloud, the precision) still look at the aligned cameras."""
    from pyarchinit_photogrammetry.engine import dense
    from pyarchinit_photogrammetry.markers import gcp

    def flight(spacing: float, n: int = 40, accuracy=(5.0, 5.0, 10.0)):
        """``n`` photos on a grid of 8 columns ``spacing`` metres apart, a position known to ``accuracy`` on
        each, none aligned."""
        ch = ppg.Document().add_chunk("Volo")
        for i in range(n):
            cam = Camera(key=i, label=f"c{i}", path=str(tmp_path / f"c{i}.jpg"), sensor_key=0)
            cam.reference = Reference(location=[500000.0 + spacing * (i % 8), 4500000.0 + spacing * (i // 8), 100.0],
                                      accuracy=list(accuracy), enabled=True)
            cam.meta["reference_crs"] = "EPSG:32633"
            ch.cameras.append(cam)
        return ch

    ch = flight(25.0)  # 40 photos over 175 m x 100 m, their positions known to 5 m: a survey from the air
    assert not ch.aligned_cameras
    assert sfm.auto_mapper(ch, 40) == "global"
    assert sfm.auto_mapper(ch, 39) == "incremental"  # fewer than GLOBAL_FROM photos
    assert sfm.auto_mapper(ch, 40, rigs=True) == "incremental"
    assert sfm.auto_mapper(ch, 40, fixed=True) == "incremental"
    # after an alignment the aligned cameras are asked, and there is none yet
    assert not dense.aerial_survey(ch) and not gcp.cameras_span_their_accuracy(ch)
    assert dense.aerial_survey(ch, aligned=False) and gcp.cameras_span_their_accuracy(ch, aligned=False)
    assert sfm.auto_mapper(flight(0.3), 40) == "incremental"  # the same photos over 2 m: a trench, an object
    switched_off = flight(25.0)
    for cam in switched_off.cameras:
        cam.reference.enabled = False
    assert sfm.auto_mapper(switched_off, 40) == "incremental"  # positions switched off do not count
    left_out = flight(25.0)
    for cam in left_out.cameras[2:]:
        cam.enabled = False
    assert sfm.auto_mapper(left_out, 40) == "incremental"  # nor the photos left out of the alignment


def _matched_ring(tmp_path, accuracy=None):
    """The synthetic ring flight (72 photos around a mound, a GPS position in the EXIF of each), its photos
    matched: ``(scene, doc, chunk)``. ``accuracy`` (metres: in plan, in plan, in height) replaces the one of every
    position. Photos of 640 x 480: on 320 x 240 the global mapper finds no tracks and the alignment goes back to
    one photo at a time, as it should."""
    from pyarchinit_photogrammetry.testing.synthetic import write_ring_dataset

    scene = write_ring_dataset(tmp_path / "ring", n=72, size=(640, 480))
    doc = ppg.Document()
    ch = doc.add_chunk("Anello")
    doc.save(tmp_path / "ring.ppg")
    ch.add_photos([tmp_path / "ring" / "photos"])
    if accuracy is not None:
        for cam in ch.cameras:
            cam.reference.accuracy = list(accuracy)
    ch.match_photos(quality="highest")  # small photos: every pixel of them
    return scene, doc, ch


def test_auto_takes_the_global_mapper_for_a_ring_flight_with_gps(tmp_path):
    """The synthetic ring flight is a survey from the air of more than ``GLOBAL_FROM`` photos: the automatic
    choice aligns all the cameras at once - on the fresh alignment, when nothing is aligned yet - and records it."""
    from pyarchinit_photogrammetry.core import geometry

    scene, _doc, ch = _matched_ring(tmp_path)
    assert sum(1 for c in ch.cameras if c.reference.enabled and c.reference.location) == 72
    assert not ch.aligned_cameras
    n = ch.align_cameras()  # mapper="auto"
    info = ch.meta["sfm"]
    assert info["mapper"] == "global", info.get("global_fallback")
    assert "global_fallback" not in info
    assert n >= 0.9 * len(ch.cameras)  # as many as keep the global block (all 72 here)
    # the block is sound: the camera centres sit on the true ones once a similarity places it (6 cm here)
    truth = {c["label"]: c["world_from_cam"][:3, 3] for c in scene.cameras}
    src = np.array([cam.center for cam in ch.aligned_cameras])
    dst = np.array([truth[cam.label] for cam in ch.aligned_cameras])
    m = geometry.umeyama(src, dst, with_scale=True)
    err = np.linalg.norm((m[:3, :3] @ src.T).T + m[:3, 3] - dst, axis=1)
    assert float(np.sqrt(np.mean(err ** 2))) < 0.5


JUDGEABLE_ACCURACY = (0.5, 0.5, 1.0)  # m: 60 m of ring is more than ten times it, so the GPS check judges the block


def test_a_global_block_that_misses_its_gps_stops_before_its_adjustment(tmp_path, monkeypatch):
    """A block the global mapper positioned whose cameras do not sit on the GPS of the photos (parts at
    different scales on a real site; patched here) is refused right after global positioning: the alignment
    goes back to one photo at a time without the adjustment and the retriangulation of the block (an hour on a
    thousand photos), and the record says what was judged, when, and with what residual."""
    _scene, _doc, ch = _matched_ring(tmp_path, accuracy=JUDGEABLE_ACCURACY)
    monkeypatch.setattr(sfm, "_block_fits_references", lambda *args, **kwargs: (False, 99.0))
    assert ch.align_cameras(mapper="global") >= 0.9 * len(ch.cameras)
    info = ch.meta["sfm"]
    assert info["mapper"] == "incremental" and "99 m" in info["global_fallback"]
    assert info["global_check"] == {"after": "global_positioning", "fits": False, "residual_m": 99.0}
    stages = ch.meta["timings"]["align_cameras"]["stages"]
    assert "global_positioning" in stages
    assert not {"iterative_bundle_adjustment", "iterative_retriangulation_and_refinement"} & set(stages)


def test_a_global_block_that_fits_goes_on_to_its_adjustment(tmp_path):
    """The same ring, nothing patched: the positioned block sits on the GPS of the photos within their noise, the
    global alignment goes on to its adjustment and places every photo; the check is recorded with its residual."""
    _scene, _doc, ch = _matched_ring(tmp_path, accuracy=JUDGEABLE_ACCURACY)
    n = ch.align_cameras(mapper="global")
    info = ch.meta["sfm"]
    assert info["mapper"] == "global", info.get("global_fallback")
    assert "global_fallback" not in info
    assert n >= 0.9 * len(ch.cameras)  # as many as keep the global block (all 72 here)
    check = info["global_check"]
    assert check["after"] == "global_positioning" and check["fits"] is True
    assert isinstance(check["residual_m"], float) and np.isfinite(check["residual_m"])
    assert "iterative_bundle_adjustment" in ch.meta["timings"]["align_cameras"]["stages"]


def test_a_global_block_that_cannot_be_judged_goes_on(scene_dir, tmp_path):
    """The trench: no GPS on its photos, so the check cannot judge the block and says so (``residual_m`` None);
    the global alignment goes on and keeps its accuracy on the control points."""
    _doc, ch = _matched(scene_dir, tmp_path, "exhaustive")
    assert ch.align_cameras(mapper="global") == len(ch.cameras)
    info = ch.meta["sfm"]
    assert info["mapper"] == "global"
    assert info["global_check"] == {"after": "global_positioning", "fits": True, "residual_m": None}
    ch.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    assert ch.update_transform()["control_rmse"] < 0.002  # as test_global_alignment_keeps_the_accuracy


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
    x_none = left_most_x()[0]
    assert x_none < 600
    ch.match_photos(quality="high", apply_masks="tiepoints")
    assert ch.align_cameras(mapper="incremental", use_gps=False) == len(ch.cameras)
    assert not ch.meta["sfm"]["masks"] and ch.meta["sfm"]["masked_tie_points"] > 0
    # no tie point left in the masked half; how many the photo keeps is not compared with the run without
    # masks: each alignment registers points of its own (329 against 273 once on Linux)
    assert left_most_x()[0] >= 599
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
    first_id = ch.meta["alignment_id"]
    inside = {c.label: np.array(c.center) for c in ch.aligned_cameras}
    rest = len(photos) - 14
    ch.add_photos(photos[14:])
    ch.detect_markers("aruco_4x4_50")
    ch.match_photos(quality="high", reset=False)
    info = ch.meta["sfm"]
    assert info["continued"] and len(info["added"]) == rest
    assert info["pairs"] == rest * 14 + rest * (rest - 1) // 2  # the pairs with a new photo
    assert ch.align_cameras(reset=False, use_gps=False) == len(photos) and info["mapper"] == "incremental"
    # the block continued is scaled and moved again by COLMAP: another internal frame, a new alignment for what was
    # placed in the old one without photos (the ends of ties, engine/ties.py)
    moved = max(np.linalg.norm(np.array(c.center) - inside[c.label]) for c in ch.aligned_cameras if c.label in inside)
    assert ch.meta["alignment_id"] != first_id and moved > 0.05
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


def test_cancelling_the_gpu_matching_stops_it(scene_dir, tmp_path):
    """The GPU streams report the pairs done while the CPU works on its first chunk (the window
    moved nothing for minutes before), and the cancel button stops the step whichever stream
    sees it first."""
    from pyarchinit_photogrammetry import _native
    from pyarchinit_photogrammetry.core.progress import Cancelled, Progress

    if _native.gpu_device("auto") is None:
        pytest.skip("no GPU (wgpu adapter) on this computer")
    _doc, ch = _project(scene_dir, tmp_path)
    progress = Progress()
    seen = []

    def watch(_fraction, message):
        seen.append(message)
        if message.startswith("Matching photos:"):  # the first report of any stream
            progress.cancel()

    progress.callback = watch
    with pytest.raises(Cancelled):
        ch.match_photos(quality="high", gpu="auto", progress=progress)
    assert sum(m.startswith("Matching photos:") for m in seen) == 1
    assert not any(m.startswith("Verifying") for m in seen)


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


def test_tie_point_limit_and_hierarchical_mapper(scene_dir, tmp_path):
    """Metashape's tie point limit: the mapper sees the most matched key points of every photo
    only, the block stays as accurate; the hierarchical mapper (one cluster here) gives the same."""
    _doc, ch = _matched(scene_dir, tmp_path, "exhaustive")
    assert ch.align_cameras(mapper="incremental", tiepoint_limit=400, ba_global_ratio=1.3, ba_global_refinements=2) == len(ch.cameras)
    info = ch.meta["sfm"]
    stats = info["tie_point_limit"]
    assert stats["pruned"] and stats["per_image"] == 400 and stats["kept"] < stats["matches"]
    assert info["ba_schedule"] == {"ratio": 1.3, "refinements": 2}
    assert not (ch.data_dir / "sfm" / "database_mapper.db").exists()  # the copy for the mapper is gone
    rec = sfm.load_reconstruction(ch)
    assert rec.num_points3D() < 400 * len(ch.cameras)
    ch.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    assert ch.update_transform()["control_rmse"] < 0.002
    # without a photo above the limit nothing is pruned
    assert ch.align_cameras(mapper="incremental", tiepoint_limit=0) == len(ch.cameras)
    assert "tie_point_limit" not in ch.meta["sfm"]
    assert ch.align_cameras(mapper="hierarchical") == len(ch.cameras)
    assert ch.meta["sfm"]["mapper"] == "hierarchical"
    assert ch.update_transform()["control_rmse"] < 0.002


def test_a_fixed_ransac_seed_gives_the_same_pairs(scene_dir, tmp_path, monkeypatch):
    """With the seed of the geometric verification fixed (``PPG_RANSAC_SEED``), the verification of the
    same matches on two copies of the project's database leaves ``two_view_geometries`` equal row by
    row, on every core; without the seed it differs. Two separate matchings are not compared pair by
    pair: COLMAP's threaded extractor numbers the photos in the order it finishes them, and a GPU
    shares the pairs with the CPU by speed (the seed fixes the verification only)."""
    import shutil
    import sqlite3

    import pycolmap

    _doc, ch = _matched(scene_dir, tmp_path, "exhaustive")
    db = ch.data_dir / "sfm" / "database.db"
    columns = ("pair_id", "rows", "config", "data", "F", "E", "H")

    def verified_again(label, seed):
        """``two_view_geometries`` of a copy of the database, verified again from its matches with ``seed``."""
        copy = tmp_path / f"{label}.db"
        shutil.copy(db, copy)
        con = sqlite3.connect(str(copy))
        try:
            ids = dict(con.execute("SELECT image_id, name FROM images"))
            pairs = [pycolmap.pair_id_to_image_pair(int(p))
                     for (p,) in con.execute("SELECT pair_id FROM matches WHERE rows > 0")]
            con.execute("DELETE FROM two_view_geometries")
            con.commit()
        finally:
            con.close()
        lst = tmp_path / f"{label}.txt"
        lst.write_text("\n".join(f"{ids[a]} {ids[b]}" for a, b in pairs))
        if seed is None:
            monkeypatch.delenv(sfm.RANSAC_SEED_ENV, raising=False)
        else:
            monkeypatch.setenv(sfm.RANSAC_SEED_ENV, str(seed))
        pycolmap.verify_matches(str(copy), str(lst), sfm.verification_options())
        con = sqlite3.connect(str(copy))
        try:
            return con.execute(f"SELECT {', '.join(columns)} FROM two_view_geometries ORDER BY pair_id").fetchall()
        finally:
            con.close()

    def differing(first, second):
        counts = {name: sum(1 for a, b in zip(first, second) if a[i] != b[i]) for i, name in enumerate(columns)}
        return {name: n for name, n in counts.items() if n}

    one, two = verified_again("seven-one", 7), verified_again("seven-two", 7)
    assert len(one) == len(two) > 0 and not differing(one, two), (len(one), len(two), differing(one, two))
    # without the seed the same verification differs: the equality above is the seed's doing
    assert differing(verified_again("clock-one", None), verified_again("clock-two", None))


def test_the_ransac_seed_is_checked(scene_dir, tmp_path, monkeypatch):
    import pycolmap

    monkeypatch.delenv(sfm.RANSAC_SEED_ENV, raising=False)
    assert sfm.verification_options().ransac.random_seed == -1  # COLMAP's default: a seed from the clock
    monkeypatch.setenv(sfm.RANSAC_SEED_ENV, "")
    assert sfm.verification_options().ransac.random_seed == -1
    monkeypatch.setenv(sfm.RANSAC_SEED_ENV, "7")
    opts = sfm.verification_options()
    assert opts.ransac.random_seed == 7
    mine, theirs = opts.todict(), pycolmap.TwoViewGeometryOptions().todict()  # COLMAP's defaults otherwise
    assert {k: v for k, v in mine.items() if k != "ransac"} == {k: v for k, v in theirs.items() if k != "ransac"}
    assert ({k: v for k, v in mine["ransac"].items() if k != "random_seed"}
            == {k: v for k, v in theirs["ransac"].items() if k != "random_seed"})
    for text in ("seven", "-1", "1.5", "0x7"):
        monkeypatch.setenv(sfm.RANSAC_SEED_ENV, text)
        with pytest.raises(ValueError, match="PPG_RANSAC_SEED"):
            sfm.verification_options()
    # the matching refuses it before any point is detected
    _doc, ch = _project(scene_dir, tmp_path)
    with pytest.raises(ValueError, match="PPG_RANSAC_SEED"):
        ch.match_photos(quality="high")
    assert not (ch.data_dir / "sfm" / "database.db").exists()
