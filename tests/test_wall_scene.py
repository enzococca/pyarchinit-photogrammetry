"""The synthetic wall (``testing.synthetic.make_wall_scene``): a wall whose face follows a line of the plan round a
corner, with marks at known lengths along the face, photographed from known poses; its plan (a DEM and an
orthophoto); the documents the tests of the wall elevations are built on (``tests/wall_helpers.py``)."""
from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from pyarchinit_photogrammetry.io.mesh_ply import read_mesh
from pyarchinit_photogrammetry.testing import synthetic
from pyarchinit_photogrammetry.testing.synthetic import (WALL_MARKS, WALL_TRACE, make_wall_scene, render_mesh,
                                                         wall_plan, wall_poses, write_wall_dataset)

from wall_helpers import (CRS, SHIFT, dark_blobs, distance_to, model_doc, share_within, wall_doc, write_plan)

JOG = [[0.0, 0.0], [3.0, 0.0], [3.0, 0.3], [6.0, 0.3]]


def _hit(scene, origin, direction):
    """Where a ray meets the scene, and how far."""
    import open3d as o3d

    rc = o3d.t.geometry.RaycastingScene()
    rc.add_triangles(o3d.core.Tensor(np.asarray(scene.vertices, np.float32)),
                     o3d.core.Tensor(np.asarray(scene.faces, np.uint32)))
    ray = np.hstack([origin, direction]).astype(np.float32)[None]
    t = float(rc.cast_rays(o3d.core.Tensor(ray))["t_hit"].numpy()[0])
    return np.asarray(origin, float) + t * np.asarray(direction, float), t


def _towards(scene, i=0):
    """The horizontal direction from segment ``i`` of the face towards the viewer."""
    t = np.diff(np.asarray(scene.face), axis=0)[i]
    t = t / np.hypot(*t)
    side = 1.0 if scene.viewer == "right" else -1.0
    return np.array([side * t[1], -side * t[0], 0.0])


def test_the_wall_follows_its_trace_and_carries_its_marks():
    scene = make_wall_scene()
    assert np.array_equal(scene.face, np.asarray(WALL_TRACE)) and scene.viewer == "right"
    assert scene.height == 2.0 and scene.thickness == 0.4
    assert len(scene.marks) == 5 == len(scene.targets)
    lengths = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(scene.face, axis=0).T))])
    for i, ((length, z), (got_length, got_z, xyz)) in enumerate(zip(WALL_MARKS, scene.marks)):
        assert got_length == length and got_z == z and xyz[2] == z and scene.targets[i] == xyz
        assert distance_to(scene.face, np.array([xyz[:2]]))[0] < 1e-9
        k = min(int(np.searchsorted(lengths, length, side="right")) - 1, len(scene.face) - 2)
        assert abs(lengths[k] + np.hypot(*(np.asarray(xyz[:2]) - scene.face[k])) - length) < 1e-9
    # two of the marks flank the corner at 6 m, one on each face
    assert scene.marks[2][2][:2] == pytest.approx((5.85, 0.0)) and scene.marks[3][2][:2] == pytest.approx((6.0, 0.15))
    # a ray from 2 m in front of the first mark meets the wall at the mark
    mark = np.asarray(scene.marks[0][2])
    n = _towards(scene)
    assert np.allclose(n, [0.0, -1.0, 0.0])
    point, t = _hit(scene, mark + 2.0 * n, -n)
    assert abs(t - 2.0) < 1e-4 and np.linalg.norm(point - mark) < 1e-4
    # the mark is black on the face, the face around it is light, the body behind the face is plain
    colour = scene.surface_color(np.array([mark, mark + [0.2, 0.0, 0.0], mark + [0.0, 0.2, 2.0 - mark[2]]]))
    assert colour[0].max() < 15 and 150 <= colour[1].min() and colour[1].max() <= 230
    assert np.allclose(colour[2], colour[2][0], atol=10)


def test_the_face_is_meshed_in_columns_that_can_be_left_out():
    scene = make_wall_scene()
    columns = scene.face_columns
    assert columns.shape == (len(scene.faces),) and np.isnan(columns[:scene.mat_faces]).all()
    face = np.isfinite(columns)
    assert face.sum() == 2 * 50 and np.allclose(np.unique(columns[face]), 0.1 + 0.2 * np.arange(50))
    # every face triangle lies on the face, at the length its column says, and spans the whole height
    tri = np.asarray(scene.vertices)[np.asarray(scene.faces)[face]]
    assert distance_to(scene.face, tri.reshape(-1, 3)[:, :2]).max() < 1e-9
    assert tri[..., 2].min() == 0.0 and tri[..., 2].max() == 2.0
    # behind a column left out there is the back of the wall, 0.4 m further
    kept = ~((columns >= 2.0) & (columns < 2.6))
    assert (~kept).sum() == 6
    holed = synthetic.MeshScene(scene.vertices, np.asarray(scene.faces)[kept], scene.mat_faces, scene.mat_size,
                                scene.mat_texel, scene.mat_texture, scene.surface_color)
    for x, depth in ((1.9, 2.0), (2.3, 2.4), (2.7, 2.0)):
        assert abs(_hit(holed, [x, -2.0, 1.0], [0.0, 1.0, 0.0])[1] - depth) < 1e-4


@pytest.mark.parametrize("viewer", ["right", "left"])
def test_the_body_is_behind_the_face(viewer):
    for trace in (WALL_TRACE, JOG):
        scene = make_wall_scene(trace=trace, viewer=viewer)
        for i in range(len(scene.face) - 1):
            mid = np.append((scene.face[i] + scene.face[i + 1]) / 2, 1.0)
            n = _towards(scene, i)
            assert abs(_hit(scene, mid + 0.1 * n, -n)[1] - 0.1) < 1e-4               # the face, from the viewer
            if np.hypot(*(scene.face[i + 1] - scene.face[i])) > 1.0:                 # the back, from behind
                assert abs(_hit(scene, mid - 1.5 * n, n)[1] - (1.5 - scene.thickness)) < 1e-4
    assert [m[0] for m in make_wall_scene(trace=JOG).marks] == [1.0, 3.0, 5.85, 6.15]   # 8 m is past its end
    with pytest.raises(ValueError, match="viewer"):
        make_wall_scene(viewer="front")


def test_the_first_photo_has_the_wall_in_its_middle():
    scene = make_wall_scene()
    poses = wall_poses(scene)
    size, focal = (800, 600), 700.0
    K = np.array([[focal, 0, size[0] / 2], [0, focal, size[1] / 2], [0, 0, 1]])
    pose = poses[0]
    assert np.allclose(pose[:3, 3], [0.0, -2.5, 0.5]) and np.allclose(pose[:3, 2], [0.0, 1.0, 0.0])   # level
    img = render_mesh(scene, pose, K, size, k1=0.0)
    marks = np.array([m[2] for m in scene.marks])
    nearest = marks[np.argmin(np.linalg.norm(marks - pose[:3, 3], axis=1))]
    cam = np.linalg.inv(pose) @ np.append(nearest, 1.0)
    px = K @ (cam[:3] / cam[2])
    assert 0 <= px[0] < size[0] and 0 <= px[1] < size[1]
    assert img[int(px[1]), int(px[0])].max() < 40                     # the disc of the mark
    assert img[size[1] // 2, size[0] // 2].min() > 60                 # the face of the wall, lit


def test_the_stations_face_the_wall_and_fan_round_a_corner():
    scene = make_wall_scene()
    poses = wall_poses(scene)
    eyes = np.array([p[:3, 3] for p in poses])
    assert len(poses) == 2 * (11 + 3 + 6)                             # two heights: 6 m, the fan, 4 m
    assert sorted(set(np.round(eyes[:, 2], 9))) == [0.5, 1.5] and np.allclose(eyes[:20, 2], 0.5)
    assert np.allclose([p[2, 2] for p in poses], 0.0)                 # level
    assert distance_to(scene.face, eyes[:, :2]).min() == pytest.approx(2.5)
    fan = eyes[11:14, :2] - [6.0, 0.0]
    assert np.allclose(np.hypot(*fan.T), 2.5)
    assert np.allclose(np.degrees(np.arctan2(fan[:, 1], fan[:, 0])), [-67.5, -45.0, -22.5])
    for p in poses[11:14]:                                            # each looks at the corner
        to_corner = np.array([6.0, 0.0, p[2, 3]]) - p[:3, 3]
        assert np.allclose(p[:3, 2], to_corner / np.linalg.norm(to_corner))
    assert len(wall_poses(scene, spacing=1.2, heights=(1.0,))) == 6 + 3 + 3
    # seen from inside, the stations that would stand by the other face are left out, the corner is seen from
    # the middle of the room
    inside = make_wall_scene(viewer="left")
    eyes = np.array([p[:3, 3] for p in wall_poses(inside)])
    assert len(eyes) == 2 * (8 + 1 + 4)
    assert distance_to(inside.face, eyes[:, :2]).min() >= 0.6 * 2.5 - 1e-9
    assert np.allclose(eyes[8, :2], [6.0 - 2.5 * np.sqrt(0.5), 2.5 * np.sqrt(0.5)])


def test_the_plan_of_the_wall():
    scene = make_wall_scene()
    dem, rgb, top_left = wall_plan(scene, 0.02)
    assert dem.dtype == np.float32 and dem.shape == (1500, 1500) and rgb.shape == (3, 1500, 1500)
    assert rgb.dtype == np.uint8 and top_left == (-15.0, 15.0)

    def cell(x, y):
        return int((top_left[1] - y) / 0.02), int((x - top_left[0]) / 0.02)

    for x, y in ((3.0, 0.2), (0.01, 0.01), (5.99, 0.39), (5.8, 3.99), (5.61, 0.5)):   # on the footprint
        assert dem[cell(x, y)] == 2.0, (x, y)
    for x, y in ((3.0, -0.05), (3.0, 0.45), (-0.05, 0.2), (6.05, 2.0), (5.55, 2.0), (5.8, 4.05)):   # two cells away
        assert dem[cell(x, y)] == 0.0, (x, y)
    assert set(np.unique(dem)) == {0.0, 2.0}
    assert abs((dem == 2.0).sum() * 0.02 ** 2 - (6.0 * 0.4 + 3.6 * 0.4)) < 0.05      # the area of the footprint
    top = rgb[:, dem == 2.0]
    assert top.std(axis=1).max() < 1.0 and rgb[:, dem == 0.0].std(axis=1).min() > 10  # a plain top on a textured ground
    # the plan of the wall seen from inside lies on the other side of the same face
    inside, _rgb, _tl = wall_plan(make_wall_scene(viewer="left"), 0.02)
    assert inside[cell(3.0, -0.2)] == 2.0 and inside[cell(3.0, 0.2)] == 0.0 and inside[cell(6.2, 2.0)] == 2.0


def test_the_dataset_has_its_photos_its_model_and_its_poses(tmp_path):
    scene = write_wall_dataset(tmp_path, size=(400, 300), focal=350.0)
    info = json.loads((tmp_path / "poses.json").read_text())
    photos = sorted((tmp_path / "photos").glob("WALL_*.jpg"))
    assert len(photos) == len(info["poses"]) == len(wall_poses(scene)) == len(scene.cameras) == 40
    assert photos[0].name == "WALL_000.jpg" and cv2.imread(str(photos[0])).shape == (300, 400, 3)
    assert info["size"] == [400, 300] and info["focal"] == 350.0 and info["viewer"] == "right"
    assert info["face"] == [list(p) for p in WALL_TRACE] and info["height"] == 2.0 and info["thickness"] == 0.4
    assert [m[:2] for m in info["marks"]] == [list(m) for m in WALL_MARKS]
    assert np.allclose(info["poses"][0], wall_poses(scene)[0])
    mesh = read_mesh(tmp_path / "model.ply")
    assert len(mesh.faces) == len(scene.faces) and np.allclose(mesh.xyz, scene.vertices, atol=1e-6)
    from pyarchinit_photogrammetry.io.exif import read_exif

    assert read_exif(photos[0])["focal_length_35mm"] == pytest.approx(36.0 * 350.0 / 400.0, abs=0.51)


def test_the_documents_of_the_wall(wall_dataset, wall_dataset_inside, tmp_path):
    import rasterio

    from pyarchinit_photogrammetry.archaeo.timeline import phase_date

    doc, chunks, info = wall_doc(tmp_path / "a", wall_dataset, chunks=2, plan=True,
                                 dates=("2017:11:05 10:00:00", "2022:11:03 09:00:00"))
    assert len(chunks) == 2 == len(doc.chunks) and doc.path.exists()
    local = json.loads((wall_dataset / "poses.json").read_text())
    assert np.allclose(np.asarray(info["face"]) - SHIFT[:2], local["face"]) and info["viewer"] == "right"
    assert np.allclose([np.asarray(m[2]) - SHIFT for m in info["marks"]], [m[2] for m in local["marks"]])
    assert [list(m[:2]) for m in info["marks"]] == [m[:2] for m in local["marks"]]
    for ch, year in zip(chunks, (2017, 2022)):
        assert ch.crs == CRS and ch.meta["metric"] is True and np.allclose(ch.transform[:3, 3], SHIFT)
        assert len(ch.cameras) == len(local["poses"]) == len(ch.aligned_cameras)
        cam = ch.camera("WALL_003")
        sensor = ch.sensor(cam)
        assert sensor.model == "PINHOLE" and list(sensor.params) == [700.0, 700.0, 400.0, 300.0]
        assert np.allclose(cam.transform, local["poses"][3]) and phase_date(ch).year == year
        # a mark is seen where the pose says: the photo is dark at its projection
        mark = ch.to_internal(np.array([info["marks"][0][2]]))
        px, depth = ch.camera("WALL_000").project(ch.sensor(ch.camera("WALL_000")), mark)
        img = cv2.imread(str(ch.camera("WALL_000").path))
        assert depth[0] > 0 and img[int(px[0, 1]), int(px[0, 0])].max() < 40
        assert len(read_mesh(ch.product_path("model")).faces) == len(read_mesh(wall_dataset / "model.ply").faces)
        with rasterio.open(ch.dem_path) as src:
            assert next(src.sample([(SHIFT[0] + 3.0, SHIFT[1] + 0.2)]))[0] == pytest.approx(102.0)
            assert next(src.sample([(SHIFT[0] + 3.0, SHIFT[1] - 1.0)]))[0] == pytest.approx(100.0)
            assert src.crs.to_epsg() == 32633
        with rasterio.open(ch.orthomosaic_path) as src:
            assert src.count == 4 and src.res == (0.02, 0.02)
    _doc, (ch,), inside = wall_doc(tmp_path / "b", wall_dataset_inside)
    assert inside["viewer"] == "left" and ch.dem_path is None and len(ch.cameras) == 26


def test_the_model_alone_and_a_plan_written_by_hand(tmp_path):
    import rasterio

    scene = make_wall_scene()
    doc, ch, info = model_doc(tmp_path / "a", scene)
    assert doc.path.exists() and ch.cameras == [] and ch.crs == CRS and ch.meta["metric"] is True
    assert np.allclose(np.asarray(info["face"]) - SHIFT[:2], WALL_TRACE) and info["thickness"] == 0.4
    assert np.allclose(ch.to_world(read_mesh(ch.product_path("model")).xyz), np.asarray(scene.vertices) + SHIFT,
                       atol=1e-5)
    _doc, holed, _info = model_doc(tmp_path / "b", scene, crs="LOCAL", shift=(0.0, 0.0, 0.0), drop=(2.0, 2.6))
    assert len(read_mesh(holed.product_path("model")).faces) == len(scene.faces) - 6 and holed.crs == "LOCAL"
    dem, rgb, top_left = wall_plan(scene, 0.05)
    dem[100:110, 100:110] = 0.1                                       # a bump added by a test
    write_plan(ch, dem, rgb, top_left, 0.05)
    with rasterio.open(ch.dem_path) as src:
        z = src.read(1)
        assert src.res == (0.05, 0.05) and (src.bounds.left, src.bounds.top) == (SHIFT[0] - 15.0, SHIFT[1] + 15.0)
    assert z[105, 105] == pytest.approx(100.1) and z.max() == pytest.approx(102.0) and z.min() == pytest.approx(100.0)
    with rasterio.open(ch.orthomosaic_path) as src:
        assert np.array_equal(src.read()[:3], rgb) and (src.read(4) == 255).all()


def test_blobs_and_distances():
    rgba = np.full((4, 60, 80), 200, np.uint8)
    yy, xx = np.mgrid[0:60, 0:80]
    for r, c, radius in ((20.25, 30.5, 4.0), (40.0, 60.0, 5.0), (10.0, 10.0, 1.5)):   # the third is too small
        rgba[:3, np.hypot(yy - r, xx - c) <= radius] = 10
    rgba[:3, 50:58, 5:13] = 10
    rgba[3, 50:58, 5:13] = 0                                          # dark, but without alpha
    found = dark_blobs(rgba)
    assert len(found) == 2 and np.allclose(sorted(found), [(20.25, 30.5), (40.0, 60.0)], atol=0.3)
    assert len(dark_blobs(rgba, min_cells=5)) == 3 and dark_blobs(rgba, level=5) == []
    line = np.array([[0.0, 0.0], [4.0, 0.0], [4.0, 3.0]])
    assert np.allclose(distance_to(line, np.array([[2.0, 1.0], [5.0, 4.0], [-3.0, 4.0], [4.0, 1.0]])),
                       [1.0, np.sqrt(2.0), 5.0, 0.0])
    assert share_within(line, line, 0.01) == 1.0
    assert share_within(line + [0.0, 0.05], line, 0.1) == pytest.approx(1.0)
    # the first leg 0.3 m off, the second on the line: of 7.3 m, the 3.2 m of the second leg from 0.2 m below the
    # corner on are within 0.2 m
    off = np.array([[0.0, -0.3], [4.0, -0.3], [4.0, 3.0]])
    assert share_within(off, line, 0.2) == pytest.approx(3.2 / 7.3, abs=0.01)
