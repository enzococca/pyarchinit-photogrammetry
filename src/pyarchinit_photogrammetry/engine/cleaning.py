"""Cleaning of the dense cloud and of the model, as Metashape does it.

Dense cloud (:func:`point_filters`, :func:`clean_point_cloud`): points with a low confidence
(few depth maps agree on them, see :mod:`.confidence`), isolated points (statistical outlier
removal: the mean distance to the nearest neighbours is far above the average), points with too
few neighbours within a radius, small groups detached from the surface (birds, dust, the sky),
chosen classes, colours close to a chosen one (sky, black borders).

Model (:func:`clean_model`): faces whose vertices have a low confidence (0 = no depth map saw
the surface: a hole the mesh closed), small pieces detached from the main surface (Metashape's
"connected component size"), stretched triangles bridging gaps, the topology (degenerate and
duplicate faces, unused vertices), holes closed up to a size, Taubin smoothing (keeps the
volume) and decimation.

Every filter only reports what it would remove until the result is written, so the dialogs
can show the counts first.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from ..core.chunk import Chunk
from ..core.pointcloud import PointCloud
from ..core.progress import Progress


# ---------------------------------------------------------------- dense cloud
def point_spacing(xyz: np.ndarray, sample: int = 20000) -> float:
    """Median distance between a point and its nearest neighbour (on a sample)."""
    from scipy.spatial import cKDTree

    xyz = np.asarray(xyz, np.float64)
    if len(xyz) < 2:
        return 0.0
    tree = cKDTree(xyz)
    pick = np.random.default_rng(0).choice(len(xyz), min(sample, len(xyz)), replace=False)
    d, _ = tree.query(xyz[pick], k=2, workers=-1)
    return float(np.median(d[:, 1]))


def statistical_outliers(xyz: np.ndarray, neighbors: int = 16, std_ratio: float = 2.0, tree=None) -> np.ndarray:
    """True for the points whose mean distance to their ``neighbors`` nearest points exceeds the
    average of all points by more than ``std_ratio`` standard deviations."""
    from scipy.spatial import cKDTree

    if len(xyz) <= neighbors:
        return np.zeros(len(xyz), bool)
    tree = tree if tree is not None else cKDTree(xyz)
    d, _ = tree.query(xyz, k=neighbors + 1, workers=-1)
    mean = d[:, 1:].mean(axis=1)
    return mean > mean.mean() + std_ratio * mean.std()


def sparse_points(xyz: np.ndarray, radius: float, min_neighbors: int = 4, tree=None) -> np.ndarray:
    """True for the points with fewer than ``min_neighbors`` other points within ``radius``."""
    from scipy.spatial import cKDTree

    if not len(xyz):
        return np.zeros(0, bool)
    tree = tree if tree is not None else cKDTree(xyz)
    counts = tree.query_ball_point(xyz, r=radius, workers=-1, return_length=True)
    return np.asarray(counts) - 1 < min_neighbors


def connected_groups(xyz: np.ndarray, distance: float) -> np.ndarray:
    """Label of the group each point belongs to: points closer than about ``distance`` (cells of
    that size touching on a face, an edge or a corner) are in the same group."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if not len(xyz):
        return np.zeros(0, np.int64)
    q = np.floor((xyz - xyz.min(axis=0)) / distance).astype(np.int64) + 1  # one empty cell around
    dims = q.max(axis=0) + 2
    key = q[:, 0] + dims[0] * (q[:, 1] + dims[1] * q[:, 2])
    cells, inv = np.unique(key, return_inverse=True)
    inv = inv.ravel()
    rows, cols = [], []
    offsets = [(dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
               if (dx, dy, dz) > (0, 0, 0)]  # half of the 26 neighbours: each pair once
    for dx, dy, dz in offsets:
        other = cells + dx + dims[0] * (dy + dims[1] * dz)
        pos = np.searchsorted(cells, other)
        pos = np.minimum(pos, len(cells) - 1)
        hit = cells[pos] == other
        rows.append(np.nonzero(hit)[0])
        cols.append(pos[hit])
    r, c = np.concatenate(rows), np.concatenate(cols)
    graph = coo_matrix((np.ones(len(r), np.int8), (r, c)), shape=(len(cells), len(cells)))
    _, label = connected_components(graph, directed=False)
    return label[inv]


def small_groups(xyz: np.ndarray, distance: float, min_points: int = 0, min_fraction: float = 0.0) -> np.ndarray:
    """True for the points of the groups (see :func:`connected_groups`) with fewer than
    ``min_points`` points or less than ``min_fraction`` of the largest group."""
    if not len(xyz):
        return np.zeros(0, bool)
    label = connected_groups(xyz, distance)
    size = np.bincount(label)
    limit = max(float(min_points), float(min_fraction) * float(size.max()))
    return size[label] < limit


def color_mask(rgb: np.ndarray | None, color, tolerance: float = 30.0) -> np.ndarray | None:
    """True for the points whose colour is within ``tolerance`` (0-255, Euclidean in RGB) of
    ``color``; ``None`` when the cloud has no colours."""
    if rgb is None:
        return None
    d = np.linalg.norm(np.asarray(rgb, np.float64) - np.asarray(color, np.float64)[None, :3], axis=1)
    return d <= tolerance


def point_filters(chunk: Chunk, pc: PointCloud | None = None, min_confidence: int | None = None,
                  remove_classes=None, statistical: tuple[int, float] | None = None,
                  sparse: tuple[float, int] | None = None, groups: tuple[float, int] | None = None,
                  color: tuple | None = None, progress: Progress | None = None) -> dict[str, np.ndarray]:
    """What each filter would remove from the dense cloud (world units for the distances):

    ``min_confidence``: points on which fewer depth maps agree; ``remove_classes``: points of
    these classes; ``statistical=(neighbours, std_ratio)``; ``sparse=(radius, min_neighbours)``
    (radius 0 = 3 x the point spacing); ``groups=(distance, min_points)``: groups smaller than
    ``min_points`` (distance 0 = 4 x the point spacing); ``color=((r, g, b), tolerance)``.

    Returns ``{filter name: boolean mask over the cloud}``."""
    progress = progress or Progress()
    pc = pc if pc is not None else chunk.point_cloud
    if pc is None:
        raise RuntimeError("build the dense cloud first")
    out: dict[str, np.ndarray] = {}
    if min_confidence:
        if pc.confidence is None:
            raise RuntimeError("the dense cloud has no confidence: compute it first")
        out["confidence"] = pc.confidence < int(min_confidence)
    if remove_classes:
        out["classes"] = pc.class_mask(remove_classes)
    if color is not None:
        m = color_mask(pc.rgb, color[0], color[1] if len(color) > 1 else 30.0)
        if m is not None:
            out["color"] = m
    if statistical or sparse or groups:
        from scipy.spatial import cKDTree

        xyz = chunk.to_world(pc.xyz)
        spacing = None
        tree = None
        if statistical or sparse:
            progress(0.1, "Indice spaziale dei punti")
            tree = cKDTree(xyz)
        if statistical:
            progress(0.3, "Punti isolati (filtro statistico)")
            out["statistical"] = statistical_outliers(xyz, int(statistical[0]), float(statistical[1]), tree=tree)
        if sparse:
            radius = float(sparse[0])
            if radius <= 0:
                spacing = spacing or point_spacing(xyz)
                radius = 3.0 * spacing
            progress(0.55, f"Punti senza vicini entro {radius:.3g}")
            out["sparse"] = sparse_points(xyz, radius, int(sparse[1]), tree=tree)
        if groups:
            distance = float(groups[0])
            if distance <= 0:
                spacing = spacing or point_spacing(xyz)
                distance = 4.0 * spacing
            progress(0.8, f"Gruppi staccati (distanza {distance:.3g})")
            out["groups"] = small_groups(xyz, distance, min_points=int(groups[1]))
    progress(1.0, "Filtri calcolati")
    return out


def summarize(masks: dict[str, np.ndarray], total: int) -> dict:
    """Points removed by each filter and by all together."""
    union = np.zeros(total, bool)
    for m in masks.values():
        union |= m
    return {"total": int(total), "removed": int(union.sum()), "kept": int(total - union.sum()),
            "by_filter": {k: int(np.count_nonzero(m)) for k, m in masks.items()}}


def clean_point_cloud(chunk: Chunk, progress: Progress | None = None, **filters) -> dict:
    """Remove from the dense cloud what :func:`point_filters` selects (same keywords) and store
    the result (classes, confidence and the classes given by hand are kept). Returns the counts."""
    progress = progress or Progress()
    pc = chunk.point_cloud
    if pc is None:
        raise RuntimeError("build the dense cloud first")
    masks = point_filters(chunk, pc, progress=progress.sub(0.0, 0.85), **filters)
    info = summarize(masks, len(pc))
    if info["kept"] < 100:
        raise RuntimeError(f"only {info['kept']} points would remain: the filters are too strict")
    if info["removed"]:
        remove = np.zeros(len(pc), bool)
        for m in masks.values():
            remove |= m
        progress(0.9, f"Salvataggio di {info['kept']:,} punti")
        chunk.point_cloud = pc.subset(~remove)
        source = chunk.meta.get("point_cloud_source") or ""
        if "pulita" not in source:
            chunk.meta["point_cloud_source"] = (source + ", pulita").lstrip(", ")
    chunk.meta["point_cloud_cleaning"] = {k: v for k, v in info.items()}
    progress(1.0, f"Nuvola pulita: tolti {info['removed']:,} punti su {info['total']:,}")
    return info


def delete_points(chunk: Chunk, mask: np.ndarray, keep_selected: bool = False) -> dict:
    """Delete the points of ``mask`` from the dense cloud (or keep only them: ``keep_selected``,
    Metashape's crop selection)."""
    pc = chunk.point_cloud
    if pc is None:
        raise RuntimeError("build the dense cloud first")
    mask = np.asarray(mask, bool)
    if len(mask) != len(pc):
        raise ValueError("the selection does not match the dense cloud")
    keep = mask if keep_selected else ~mask
    if keep.sum() < 10:
        raise RuntimeError("too few points would remain")
    chunk.point_cloud = pc.subset(keep)
    return {"total": len(pc), "removed": int(len(pc) - keep.sum()), "kept": int(keep.sum())}


# ---------------------------------------------------------------- model
def weld(vertices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vertices at the same position counted once: ``(first index of each position, index of
    every vertex among them)``. Textured meshes (Metashape) repeat the vertices along the seams
    of the texture: without welding, every texture chart looks like a detached piece."""
    v = np.ascontiguousarray(np.asarray(vertices, np.float64))
    _, first, inverse = np.unique(v.view(np.dtype((np.void, v.dtype.itemsize * 3))).ravel(),
                                  return_index=True, return_inverse=True)
    return first, inverse.ravel()


def _vertex_components(n_vertices: int, faces: np.ndarray) -> np.ndarray:
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if not len(faces):
        return np.arange(n_vertices)
    r = np.concatenate([faces[:, 0], faces[:, 1]])
    c = np.concatenate([faces[:, 1], faces[:, 2]])
    graph = coo_matrix((np.ones(len(r), np.int8), (r, c)), shape=(n_vertices, n_vertices))
    return connected_components(graph, directed=False)[1]


def face_filters(vertices: np.ndarray, faces: np.ndarray, confidence: np.ndarray | None = None,
                 min_confidence: int | None = None, min_component: float | None = None,
                 max_edge: float | None = None, repair: bool = True) -> dict[str, np.ndarray]:
    """What each filter would remove from a mesh (boolean masks over the faces):

    ``min_confidence``: faces with a vertex below it; ``min_component``: pieces with fewer
    faces than this fraction of the largest piece (e.g. 0.01) - or than this number of faces
    when >= 1 (vertices at the same position join the pieces: the seams of a textured mesh);
    ``max_edge``: faces with an edge longer than this many times the median edge; ``repair``:
    degenerate faces (repeated vertices or no area) and duplicates."""
    faces = np.asarray(faces, np.int64)
    vertices = np.asarray(vertices, np.float64)
    out: dict[str, np.ndarray] = {}
    if min_confidence:
        if confidence is None:
            raise RuntimeError("the model has no vertex confidence: compute it first")
        out["confidence"] = (confidence[faces] < int(min_confidence)).any(axis=1)
    if max_edge:
        tri = vertices[faces]
        edges = np.linalg.norm(tri - np.roll(tri, -1, axis=1), axis=2)
        longest = edges.max(axis=1)
        out["long_edges"] = longest > float(max_edge) * float(np.median(edges))
    if repair:
        f = np.sort(faces, axis=1)
        bad = (f[:, 0] == f[:, 1]) | (f[:, 1] == f[:, 2])
        tri = vertices[faces]
        area = np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
        bad |= area <= 1e-12 * max(1.0, float(np.ptp(vertices, axis=0).max()) ** 2)
        _, first = np.unique(f, axis=0, return_index=True)
        dup = np.ones(len(faces), bool)
        dup[first] = False
        out["topology"] = bad | dup
    if min_component:
        alive = np.ones(len(faces), bool)
        for m in out.values():
            alive &= ~m
        first, inverse = weld(vertices)  # pieces joined only through repeated vertices are one piece
        welded = inverse[faces]
        label = _vertex_components(len(first), welded[alive])
        comp = label[welded[:, 0]]
        size = np.bincount(comp[alive], minlength=int(label.max()) + 1)
        limit = float(min_component) if min_component >= 1 else float(min_component) * float(size.max())
        out["components"] = alive & (size[comp] < limit)
    return out


def _taubin(vertices: np.ndarray, faces: np.ndarray, iterations: int, lam: float = 0.5, mu: float = -0.53,
            fixed: np.ndarray | None = None) -> np.ndarray:
    """Taubin smoothing (uniform weights): shrink-free, the volume is kept."""
    from scipy.sparse import coo_matrix, diags

    n = len(vertices)
    r = np.concatenate([faces[:, 0], faces[:, 1], faces[:, 2], faces[:, 1], faces[:, 2], faces[:, 0]])
    c = np.concatenate([faces[:, 1], faces[:, 2], faces[:, 0], faces[:, 0], faces[:, 1], faces[:, 2]])
    adj = coo_matrix((np.ones(len(r)), (r, c)), shape=(n, n)).tocsr()
    adj.data[:] = 1.0
    deg = np.asarray(adj.sum(axis=1)).ravel()
    w = diags(1.0 / np.maximum(deg, 1)) @ adj
    v = vertices.copy()
    move = np.ones(n, bool) if fixed is None else ~fixed
    move &= deg > 0
    for _ in range(int(iterations)):
        for f in (lam, mu):
            delta = w @ v - v
            v[move] += f * delta[move]
    return v


def boundary_vertices(faces: np.ndarray, n_vertices: int) -> np.ndarray:
    """True for the vertices on an open border of the mesh (edges used by one face only)."""
    e = np.sort(np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1)
    uniq, count = np.unique(e, axis=0, return_counts=True)
    out = np.zeros(n_vertices, bool)
    out[uniq[count == 1].ravel()] = True
    return out


def store_model(chunk: Chunk, mesh, texture_kept: bool = True) -> Path:
    """Write a changed model in the chunk folder (every vertex property kept). The texture stays
    when the faces kept their texture coordinates (faces removed only); otherwise, or when the
    texture is a separate OBJ made from the old faces, it is left out: build it again."""
    from ..io.mesh_ply import write_mesh

    textured = chunk.product_path("model_textured")
    legacy = textured is not None and textured.suffix.lower() == ".obj"
    if not texture_kept or mesh.texcoords is None or legacy:
        mesh.texcoords = None
        mesh.set_texture_file(None)
        chunk.products.pop("model_textured", None)
    path = write_mesh(chunk.own_product_path("model"), mesh)
    chunk._set_product("model", path)
    preview = chunk.data_dir / "model_preview.ply"
    if preview.exists():
        preview.unlink()
    return path


def _model(chunk: Chunk):
    from ..io.mesh_ply import read_mesh

    path = chunk.product_path("model")
    if path is None or not path.exists():
        raise RuntimeError("build the model first")
    return read_mesh(path)


def _nearest_fields(mesh, old_xyz: np.ndarray, new_xyz: np.ndarray):
    """Vertex properties for new vertices from the nearest old vertex (positions replaced)."""
    from scipy.spatial import cKDTree

    idx = cKDTree(old_xyz).query(new_xyz, workers=-1)[1]
    out = mesh.vertices[idx].copy()
    for k, n in enumerate("xyz"):
        out[n] = new_xyz[:, k]
    return out


def clean_model(chunk: Chunk, min_confidence: int | None = None, min_component: float | None = None,
                max_edge: float | None = None, repair: bool = True, fill_holes: float | None = None,
                smooth: int = 0, target_faces: int | None = None, progress: Progress | None = None) -> dict:
    """Clean the model and store it: :func:`face_filters` (same keywords), then holes closed up
    to ``fill_holes`` (radius, chunk units, e.g. metres), ``smooth`` Taubin iterations (the
    borders stay where they are) and decimation to ``target_faces``. ``repair`` also welds the
    vertices repeated at the same position, so that smoothing does not open cracks and seams are
    not taken for holes. Every vertex property (colours, confidence...) is kept; new vertices
    take those of the nearest old one. The texture stays when only faces were removed (and
    smoothing moved the vertices a little); closing holes or decimating leaves it out."""
    progress = progress or Progress()
    progress(0.02, "Lettura del modello")
    mesh = _model(chunk)
    v, f = mesh.xyz, mesh.faces
    conf = mesh.field("confidence")
    n_faces0, n_vertices0 = len(f), len(v)
    scale = chunk.scale or 1.0
    progress(0.1, "Filtri sulle facce")
    masks = face_filters(v, f, conf, min_confidence=min_confidence, min_component=min_component, max_edge=max_edge,
                         repair=repair)
    remove = np.zeros(len(f), bool)
    for m in masks.values():
        remove |= m
    if remove.all():
        raise RuntimeError("every face would be removed: the filters are too strict")
    mesh = mesh.subset_faces(~remove)
    info = {"faces_before": n_faces0, "vertices_before": n_vertices0,
            "by_filter": {k: int(np.count_nonzero(m)) for k, m in masks.items()}, "removed_faces": int(remove.sum())}
    texture_kept = True
    if repair:
        first, inverse = weld(mesh.xyz)
        if len(first) < len(mesh.vertices):
            info["welded_vertices"] = int(len(mesh.vertices) - len(first))
            f = inverse[mesh.faces]
            ok = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
            from ..io.mesh_ply import MeshData

            mesh = MeshData(mesh.vertices[first], f[ok], None if mesh.texcoords is None else mesh.texcoords[ok],
                            mesh.comments)
    if fill_holes:
        progress(0.35, "Chiusura dei buchi")
        import open3d as o3d

        v, f = mesh.xyz, mesh.faces
        t = o3d.t.geometry.TriangleMesh(o3d.core.Tensor(v.astype(np.float32)), o3d.core.Tensor(f.astype(np.int32)))
        filled = t.fill_holes(hole_size=float(fill_holes) / scale)
        nv = filled.vertex.positions.numpy().astype(np.float64)
        nf = filled.triangle.indices.numpy().astype(np.int64)
        info["filled_faces"] = int(len(nf) - len(f))
        if info["filled_faces"]:
            texture_kept = False  # the new faces have no texture coordinates
            mesh.texcoords = None
            if len(nv) != len(v):  # pragma: no cover - other versions of Open3D add vertices
                mesh.vertices = _nearest_fields(mesh, v, nv)
            mesh.faces = nf
    if smooth:
        progress(0.55, f"Levigatura ({smooth} passi)")
        mesh.set_xyz(_taubin(mesh.xyz, mesh.faces, int(smooth), fixed=boundary_vertices(mesh.faces, len(mesh.vertices))))
        info["smooth"] = int(smooth)
    if target_faces and len(mesh.faces) > int(target_faces):
        progress(0.7, f"Semplificazione a {int(target_faces):,} facce")
        import open3d as o3d

        v, f = mesh.xyz, mesh.faces
        colors = mesh.colors
        m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(v), o3d.utility.Vector3iVector(f.astype(np.int32)))
        if colors is not None:
            m.vertex_colors = o3d.utility.Vector3dVector(colors.astype(np.float64) / 255.0)
        m = m.simplify_quadric_decimation(int(target_faces))
        nv, nf = np.asarray(m.vertices), np.asarray(m.triangles, np.int64)
        mesh.vertices = _nearest_fields(mesh, v, nv)
        if colors is not None and m.has_vertex_colors():
            mesh.set_colors(np.clip(np.asarray(m.vertex_colors) * 255.0 + 0.5, 0, 255).astype(np.uint8))
        mesh.faces, mesh.texcoords = nf, None
        texture_kept = False
    progress(0.85, "Salvataggio del modello")
    mesh = mesh.subset_faces(np.ones(len(mesh.faces), bool))  # unused vertices out
    store_model(chunk, mesh, texture_kept)
    info.update({"faces": int(len(mesh.faces)), "vertices": int(len(mesh.vertices)),
                 "texture_kept": bool(texture_kept and mesh.texcoords is not None)})
    meta = chunk.meta.setdefault("model", {})
    meta["cleaned"] = {k: val for k, val in info.items() if k != "by_filter"}
    progress(1.0, f"Modello pulito: {info['faces']:,} facce (prima {n_faces0:,})")
    return info


def delete_faces(chunk: Chunk, face_mask: np.ndarray, keep_selected: bool = False) -> dict:
    """Delete the faces of ``face_mask`` from the model (or keep only them), Metashape's
    delete / crop selection; every vertex property and the texture are kept."""
    mesh = _model(chunk)
    mask = np.asarray(face_mask, bool)
    if len(mask) != len(mesh.faces):
        raise ValueError("the selection does not match the model")
    keep = mask if keep_selected else ~mask
    if not keep.any():
        raise RuntimeError("no face would remain")
    out = mesh.subset_faces(keep)
    store_model(chunk, out, texture_kept=True)
    return {"faces_before": int(len(mask)), "faces": int(len(out.faces)), "removed_faces": int(len(mask) - len(out.faces))}


__all__ = ["clean_model", "clean_point_cloud", "color_mask", "connected_groups", "delete_faces", "delete_points",
           "face_filters", "point_filters", "point_spacing", "small_groups", "sparse_points", "statistical_outliers",
           "store_model", "summarize", "weld"]
