"""Confidence, cleaning of the dense cloud and of the model, cropping and cropped exports."""
from __future__ import annotations

import shutil

import numpy as np
import pytest
import rasterio

from pyarchinit_photogrammetry.core import geometry
from pyarchinit_photogrammetry.core.pointcloud import PointCloud


def _copy(processed, tmp_path, name="copia"):
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path / name
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")  # the photos are stored relative to the project
    out = ppg.Document.open(d / doc.path.name)
    return out, out.chunk


def _trench_box(ch, shrink=0.25):
    """A polygon inside the trench (world XY): the middle of the dense cloud."""
    w = ch.to_world(ch.point_cloud.xyz)
    lo, hi = np.percentile(w[:, :2], 10, axis=0), np.percentile(w[:, :2], 90, axis=0)
    c, h = (lo + hi) / 2, (hi - lo) / 2 * (1 - shrink)
    return np.array([[c[0] - h[0], c[1] - h[1]], [c[0] + h[0], c[1] - h[1]], [c[0], c[1] + h[1]]])  # a triangle


# ---------------------------------------------------------------- confidence
def test_dense_cloud_confidence(processed, tmp_path):
    """Every point of the fused cloud gets the number of depth maps agreeing on it; points
    added off the surface get none and are cleaned away by a minimum confidence."""
    doc, ch = _copy(processed, tmp_path)
    pc = ch.point_cloud
    assert pc.confidence is not None and len(pc.confidence) == len(pc)  # computed by the fusion
    assert np.median(pc.confidence) >= 3 and (pc.confidence >= 2).mean() > 0.95
    # floating noise 20 cm above the trench
    rng = np.random.default_rng(1)
    w = ch.to_world(pc.xyz)
    noise_w = w[rng.choice(len(w), 300, replace=False)] + [0, 0, 0.20]
    noisy = PointCloud(np.vstack([pc.xyz, ch.to_internal(noise_w)]),
                       np.vstack([pc.rgb, np.full((300, 3), 250, np.uint8)]))
    ch.point_cloud = noisy
    info = ch.compute_confidence("point_cloud")
    assert info["points"] == len(noisy) and info["source"] == "mappe di profondita'"
    stored = PointCloud.load(ch.product_path("point_cloud")).confidence
    assert stored is not None
    assert np.median(stored[:len(pc)]) >= 3 and (stored[len(pc):] <= 1).mean() > 0.95
    report = ch.clean_point_cloud(min_confidence=2)
    assert report["by_filter"]["confidence"] >= 290 and report["kept"] >= 0.9 * len(pc)
    cleaned = ch.point_cloud
    assert cleaned.confidence.min() >= 2
    # the LAS keeps the confidence (extra bytes, as Metashape writes it)
    las = PointCloud.load(ch.export_point_cloud(tmp_path / "nuvola.las"))
    assert np.array_equal(las.confidence, cleaned.confidence)


def test_model_vertex_confidence_and_cleaning(processed, tmp_path):
    """Vertex confidence of the model; a detached piece, faces nobody saw and holes are cleaned;
    smoothing keeps the borders; decimation keeps colours and confidence."""
    import trimesh

    from pyarchinit_photogrammetry.engine.confidence import read_model_confidence, write_model

    doc, ch = _copy(processed, tmp_path)
    mesh = ch.model
    n0 = len(mesh.faces)
    # a floating box 1 m above the trench (no depth map sees it) added to the model
    box = trimesh.creation.box(extents=[0.05, 0.05, 0.05])
    top = ch.to_world(np.asarray(mesh.vertices)).max(axis=0) + [0, 0, 1.0]
    box.apply_translation(ch.to_internal(top[None])[0])
    both = trimesh.util.concatenate([mesh, trimesh.Trimesh(box.vertices, box.faces,
                                                           vertex_colors=np.full((8, 4), 255, np.uint8))])
    write_model(ch.product_path("model"), both)
    info = ch.compute_confidence("model")
    conf = read_model_confidence(ch.product_path("model"))
    assert len(conf) == len(both.vertices) and info["points"] == len(conf)
    # vertices on the surface the photos saw (near the dense points) are confirmed by several
    # depth maps; the box, which no photo saw, by none
    from scipy.spatial import cKDTree

    near = cKDTree(ch.point_cloud.xyz).query(np.asarray(mesh.vertices))[0] * ch.scale < 0.01
    assert near.mean() > 0.3 and np.median(conf[: len(mesh.vertices)][near]) >= 3
    assert conf[len(mesh.vertices):].max() == 0
    from pyarchinit_photogrammetry.engine.cleaning import face_filters

    low = face_filters(np.asarray(both.vertices), np.asarray(both.faces), conf, min_confidence=1)["confidence"]
    assert low[n0:].all() and low[:n0].mean() < 0.9
    report = ch.clean_model(min_component=0.01)  # the box and any crumb of the mesh itself
    assert report["by_filter"]["components"] >= 12 and report["faces"] >= n0 - 0.01 * n0
    cleaned = ch.model
    assert cleaned.visual.kind == "vertex"
    assert ch.to_world(np.asarray(cleaned.vertices))[:, 2].max() < top[2] - 0.5  # the box is gone
    stored = read_model_confidence(ch.product_path("model"))
    assert len(stored) == len(cleaned.vertices) and np.median(stored) == np.median(conf[: len(mesh.vertices)])
    # a hole closed again, then smoothing and decimation
    v = np.asarray(cleaned.vertices)
    centre = v[np.argmin(np.linalg.norm(v - v.mean(axis=0), axis=1))]
    tri_c = v[np.asarray(cleaned.faces)].mean(axis=1)
    hole = np.argsort(np.linalg.norm(tri_c - centre, axis=1))[:30]
    from pyarchinit_photogrammetry.engine.cleaning import boundary_vertices, delete_faces

    mask = np.zeros(len(cleaned.faces), bool)
    mask[hole] = True
    delete_faces(ch, mask)
    holed = ch.model
    open_border = boundary_vertices(np.asarray(holed.faces), len(holed.vertices)).sum()
    radius = 2.5 * ch.scale * float(np.linalg.norm(tri_c[hole] - centre, axis=1).max())  # the mesh varies by run
    report = ch.clean_model(fill_holes=radius, smooth=3, target_faces=len(holed.faces) // 2)
    final = ch.model
    assert report["filled_faces"] > 0
    assert boundary_vertices(np.asarray(final.faces), len(final.vertices)).sum() < open_border
    assert len(final.faces) <= len(holed.faces) // 2 + 10 and final.visual.kind == "vertex"
    assert len(read_model_confidence(ch.product_path("model"))) == len(final.vertices)
    assert ch.product_path("model_textured") is None  # the texture belonged to the old faces


def test_point_filters_on_a_synthetic_cloud(tmp_path):
    """Statistical outliers, sparse points, small detached groups, colours and classes."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.engine import cleaning

    rng = np.random.default_rng(0)
    g = np.stack(np.meshgrid(np.arange(0, 4, 0.02), np.arange(0, 3, 0.02)), -1).reshape(-1, 2)
    ground = np.column_stack([g, 0.01 * np.sin(g[:, 0])])
    lonely = rng.uniform([0, 0, 0.5], [4, 3, 2.0], (40, 3))  # isolated points in the air
    clump = rng.normal([2.0, 1.5, 1.2], 0.01, (60, 3))  # a bird
    xyz = np.vstack([ground, lonely, clump])
    rgb = np.vstack([np.full((len(ground), 3), 120), np.full((40, 3), 120), np.full((60, 3), [20, 40, 230])])
    codes = np.r_[np.full(len(ground), 2), np.full(40, 1), np.full(60, 7)]
    doc = ppg.Document()
    ch = doc.add_chunk("Prova")
    ch.transform = geometry.compose_similarity(1.0, np.eye(3), np.zeros(3))
    doc.save(tmp_path / "prova.ppg")
    ch.point_cloud = PointCloud(xyz, rgb.astype(np.uint8), classification=codes)
    masks = cleaning.point_filters(ch, statistical=(16, 2.0), sparse=(0.0, 3), groups=(0.0, 100),
                                   color=((20, 40, 230), 30), remove_classes=[7])
    n = len(ground)
    assert masks["statistical"][n:n + 40].mean() > 0.9 and masks["statistical"][:n].mean() < 0.01
    assert masks["sparse"][n:n + 40].all() and not masks["sparse"][:n].any()
    assert masks["groups"][n:].all() and not masks["groups"][:n].any()  # the bird and the lonely points
    assert masks["color"][n + 40:].all() and not masks["color"][:n + 40].any()
    assert masks["classes"].sum() == 60
    info = ch.clean_point_cloud(groups=(0.0, 100))
    assert info["removed"] == 100 and len(ch.point_cloud) == n


# ---------------------------------------------------------------- crop
def test_cropped_exports_and_crops(processed, tmp_path):
    """DEM, orthophoto, model and cloud cut by a polygon, for export and in the project."""
    from shapely.geometry import Point, Polygon

    from pyarchinit_photogrammetry.io.mesh_ply import PlyMesh

    doc, ch = _copy(processed, tmp_path)
    tri = _trench_box(ch)
    poly = Polygon(tri)
    # DEM: the window of the triangle, no data outside it, the same heights inside
    out = ch.export_dem(tmp_path / "dem_ritaglio.tif", region=tri)
    with rasterio.open(ch.dem_path) as full, rasterio.open(out) as cut:
        assert cut.crs == full.crs and cut.res == full.res
        assert cut.width < full.width and cut.height < full.height
        z = cut.read(1, masked=True)
        b = cut.bounds
        assert poly.bounds[0] - cut.res[0] <= b.left and b.right <= poly.bounds[2] + cut.res[0]
        rows, cols = np.nonzero(~z.mask)
        xs, ys = rasterio.transform.xy(cut.transform, rows, cols)
        assert all(poly.buffer(cut.res[0]).contains(Point(x, y)) for x, y in zip(xs[::50], ys[::50]))
        same = list(full.sample(list(zip(xs[::200], ys[::200]))))
        assert np.allclose([s[0] for s in same], z.compressed()[::200], atol=1e-6)
        assert z.mask[0, -1] and z.mask[-1, 0] or z.mask[0, 0]  # corners of the box are outside the triangle
    # orthophoto: transparent outside
    out = ch.export_orthomosaic(tmp_path / "orto_ritaglio.tif", region=tri)
    with rasterio.open(out) as cut:
        alpha = cut.read(4)
        rows, cols = np.nonzero(alpha)
        xs, ys = rasterio.transform.xy(cut.transform, rows[::97], cols[::97])
        assert all(poly.buffer(2 * cut.res[0]).contains(Point(x, y)) for x, y in zip(xs, ys))
        assert alpha.mean() > 0
    # model: faces inside, world coordinates in double precision, colours kept
    path = ch.export_model(tmp_path / "modello_ritaglio.ply", region=tri)
    head = path.read_bytes()[:400].decode("ascii", "replace")
    assert "property double x" in head and "red" in head
    m = PlyMesh(path)
    v = m.xyz()
    f = np.concatenate(list(m.face_chunks()))
    centres = v[f].mean(axis=1)
    assert all(poly.buffer(1e-6).contains(Point(x, y)) for x, y in centres[::50, :2])
    world = ch.to_world(np.asarray(ch.model.vertices))
    assert np.abs(np.asarray(sorted(v[:, 0]))[[0, -1]] - [world[:, 0].min(), world[:, 0].max()]).max() < 2.0
    # textured OBJ cropped too
    obj = ch.export_model(tmp_path / "modello_ritaglio.obj", region=tri)
    assert obj.exists() and (tmp_path / "modello_ritaglio_texture.jpg").exists()
    # dense cloud: points inside
    las = PointCloud.load(ch.export_point_cloud(tmp_path / "nuvola_ritaglio.las", region=tri))
    assert len(las) > 100 and all(poly.buffer(1e-6).contains(Point(x, y)) for x, y in las.xyz[::100, :2])
    # inside the project
    n = len(ch.point_cloud)
    ch.crop_point_cloud(region=tri)
    assert len(ch.point_cloud) == len(las) < n
    faces_before = len(ch.model.faces)
    rep = ch.crop_model(tri)
    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    kept = read_mesh(ch.product_path("model"))
    assert rep["faces"] < faces_before and len(kept.faces) == rep["faces"]
    assert ch.product_path("model_textured") is not None and kept.texcoords is not None  # the texture stays
    assert kept.colors is not None
    ch.crop_dem(tri)
    with rasterio.open(ch.dem_path) as src:
        assert src.width < 2 * src.height + 10_000  # cropped in place, still readable
    ch.crop_orthomosaic(tri)
    with rasterio.open(ch.orthomosaic_path) as src:
        assert src.count == 4
    with pytest.raises(ValueError):
        ch.export_dem(tmp_path / "fuori.tif", region=[[0, 0], [1, 0], [0, 1]])


def test_world_ply_export_keeps_map_precision(tmp_path):
    """A model exported in UTM coordinates kept only float32 (7 cm off at 4,000 km)."""
    import trimesh

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.io.mesh_ply import PlyMesh

    doc = ppg.Document()
    ch = doc.add_chunk("UTM")
    ch.crs = "EPSG:32637"
    ch.transform = geometry.compose_similarity(1.0, np.eye(3), np.array([351234.567, 4012345.678, 210.123]))
    doc.save(tmp_path / "utm.ppg")
    box = trimesh.creation.box(extents=[1, 1, 1])
    path = ch.data_dir / "model.ply"
    box.export(str(path))
    ch._set_product("model", path)
    out = PlyMesh(ch.export_model(tmp_path / "utm.ply"))
    assert np.abs(out.xyz() - ch.to_world(np.asarray(box.vertices))).max() < 1e-6


def test_changes_never_touch_files_of_other_programs(processed, tmp_path):
    """An imported project points to the DEM and orthophoto exported by Metashape in the
    user's folder: cropping or cleaning writes the changed copy in the project, the original
    file stays as it was."""
    import hashlib
    import os

    doc, ch = _copy(processed, tmp_path)
    outside = tmp_path / "metashape" / "dem"
    outside.mkdir(parents=True)
    external = {}
    for product in ("dem", "orthomosaic"):
        src = ch.product_path(product)
        dst = outside / f"TELL_{product}.tif"
        shutil.copy2(src, dst)
        ch.products[product] = os.path.relpath(dst, ch.data_dir)
        external[product] = (dst, hashlib.sha256(dst.read_bytes()).hexdigest())
    model_out = tmp_path / "metashape" / "modello.ply"
    shutil.copy2(ch.product_path("model"), model_out)
    ch.products["model"] = os.path.relpath(model_out, ch.data_dir)
    model_hash = hashlib.sha256(model_out.read_bytes()).hexdigest()
    tri = _trench_box(ch)
    ch.crop_dem(tri)
    ch.crop_orthomosaic(tri)
    ch.crop_model(tri)
    ch.clean_model(min_component=0.01)
    for product, (path, digest) in external.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        assert ch.product_path(product).parent == ch.data_dir
    assert hashlib.sha256(model_out.read_bytes()).hexdigest() == model_hash
    assert ch.product_path("model") == ch.data_dir / "model.ply"


def test_command_line(processed, tmp_path):
    """ppg confidence, clean-cloud, clean-model, crop and relink."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc, ch = _copy(processed, tmp_path)
    project = str(doc.path)
    run = CliRunner()

    def ok(*args):
        r = run.invoke(app, list(args))
        assert r.exit_code == 0, r.output + repr(r.exception)
        return r.output

    out = ok("confidence", project, "--model")
    assert "point_cloud:" in out and "model:" in out
    out = ok("clean-cloud", project, "--min-confidence", "3", "--statistical", "16,2", "--dry-run")
    assert "would remove" in out
    n = len(PointCloud.load(ch.product_path("point_cloud")))
    out = ok("clean-cloud", project, "--min-confidence", "3", "--groups", "0,50")
    assert "removed" in out and len(PointCloud.load(ch.product_path("point_cloud"))) < n
    out = ok("clean-model", project, "--min-component", "1", "--smooth", "1")
    assert "faces" in out
    tri = _trench_box(ch)
    poly = " ".join(f"{x},{y}" for x, y in tri)
    out = ok("crop", project, "--polygon", poly, "--out", str(tmp_path / "ritagli"), "--name", "prova")
    assert {p.name for p in (tmp_path / "ritagli").iterdir()} >= {"prova_dem.tif", "prova_ortofoto.tif",
                                                                   "prova_modello.ply", "prova_nuvola.las"}
    lo, hi = tri.min(axis=0), tri.max(axis=0)
    out = ok("export", project, "--dem", str(tmp_path / "dem_box.tif"), "--bbox", f"{lo[0]},{lo[1]},{hi[0]},{hi[1]}")
    assert (tmp_path / "dem_box.tif").exists()
    ok("crop", project, "--bbox", f"{lo[0]},{lo[1]},{hi[0]},{hi[1]}", "--in-project", "--products", "dem,point_cloud")
    r = run.invoke(app, ["crop", project, "--bbox", "1,2,3"])
    assert r.exit_code != 0
    # relink to a copy of the photos elsewhere
    other = tmp_path / "copia_foto"
    shutil.copytree(doc.path.parent / "photos", other)
    out = ok("relink", project, str(other))
    assert out.startswith(f"{len(ch.cameras)} photos linked")


def test_seams_of_a_textured_mesh_are_not_detached_pieces(tmp_path):
    """Metashape repeats the vertices along the seams of the texture: every chart looked like a
    detached piece and the 1 % filter removed 17 % of a real tell. Vertices at the same position
    now join the pieces; repair welds them, so smoothing opens no cracks."""
    import trimesh

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.engine.cleaning import boundary_vertices, face_filters, weld
    from pyarchinit_photogrammetry.engine.confidence import write_model

    grid = trimesh.creation.box(extents=[1, 1, 1]).subdivide().subdivide().subdivide()
    v, f = np.asarray(grid.vertices), np.asarray(grid.faces)
    # split the surface in two charts at x = 0.3: they share positions, not vertices
    right = v[f].mean(axis=1)[:, 0] > 0.3
    v2 = np.vstack([v, v])
    f2 = np.where(right[:, None], f + len(v), f)
    used = np.zeros(len(v2), bool)
    used[f2.ravel()] = True
    remap = np.cumsum(used) - 1
    v2, f2 = v2[used], remap[f2]
    first, inverse = weld(v2)
    assert len(first) == len(v) and len(v2) > len(v)
    # a real detached piece far away
    crumb = trimesh.creation.icosphere(subdivisions=0, radius=0.01)
    vv = np.vstack([v2, crumb.vertices + 5.0])
    ff = np.vstack([f2, crumb.faces + len(v2)])
    small_chart = int(min(right.sum(), (~right).sum()))
    assert small_chart > len(crumb.faces)
    masks = face_filters(vv, ff, min_component=small_chart + 1, repair=False)
    assert masks["components"].sum() == len(crumb.faces)  # only the crumb, not the smaller chart
    doc = ppg.Document()
    ch = doc.add_chunk("Seams")
    doc.save(tmp_path / "seams.ppg")
    path = write_model(ch.data_dir / "model.ply", trimesh.Trimesh(vv, ff, process=False),
                       np.full(len(vv), 7, np.uint8))
    ch._set_product("model", path)
    info = ch.clean_model(min_component=small_chart + 1, smooth=2)
    out = ch.model
    assert info["welded_vertices"] == len(v2) - len(v) and info["faces"] == len(f)
    assert boundary_vertices(np.asarray(out.faces), len(out.vertices)).sum() == 0  # closed: no crack opened
