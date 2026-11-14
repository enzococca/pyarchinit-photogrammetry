"""The models of several chunks joined into one textured model (*Strumenti > Unisci i modelli*, ``ppg merge-models``).

:func:`merge_models` takes the model of every chosen chunk, brings it into the frame of the first one (each chunk's
transform: the chunks must share the world frame, aligned or georeferenced, as for :func:`.chunks.merge_chunks`)
and writes the meshes as ONE model of a new chunk (no photos: a "model chunk"), with:

* one set of texture pages: the pages of every source kept as they are (``pages="keep"``, pages larger than
  ``max_page`` reduced: texture coordinates are fractions of a page and do not change) or packed together
  (``pages="repack"``) on pages of at most ``page_size`` pixels, a page of a source reduced when it does not fit;
  a source without a texture gets a block of its mean colour on a last page (its vertex colours stay);
* the source chunk of every face kept as the face group (:class:`..io.mesh_ply.MeshData` ``groups``, the chunk
  labels as their names): an OBJ export can write one group per source (:func:`..io.exports.export_mesh`);
* optional colour equalisation (``colour``): every source matched to the colours of the reference (the first
  chunk, or ``reference``) where their surfaces overlap - the vertices of a source within ``match_distance`` of a
  vertex of the reference, or of a source already matched, so a chain of overlaps is followed - by a gain and
  offset per RGB channel (``gain``) or by the mean and spread of each channel in CIE Lab (``lab``, a colour
  transfer); a source that overlaps none is left as it is and said in the warnings;
* optional removal of the faces of a source that lie on the surface of a source before it (``remove_overlaps``):
  sources in ``priority`` order (default the order given), a face goes when its centre is within
  ``overlap_distance`` of a face of a kept source facing the same way. Nothing is closed or bridged: the gaps
  between the sources and the edges left by the removal stay open.

The new chunk is a chunk like the others: DEM (``source="model"``), orthophoto from the texture
(:mod:`.texture_ortho`), elevations, measures and exports work on it. No Qt here.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

from ..core import geometry
from ..core.progress import Progress, as_progress

if TYPE_CHECKING:  # pragma: no cover
    from ..core.chunk import Chunk
    from ..core.document import Document

log = logging.getLogger("pyarchinit_photogrammetry")

COLOURS = ("none", "gain", "lab")
PAGES = ("keep", "repack")
MIN_PAIRS = 200  # vertex pairs in the overlap needed to match the colours of a source
SAMPLE = 200_000  # vertices of a source compared with the reference
PAD = 4  # pixels of border around a page packed with others (filtering does not bleed into the next one)
DOUBLE_BEYOND = 1e4
TEXTURE_QUALITY = 92


class _Source:
    """The model of one chunk in the internal frame of the first one."""

    def __init__(self, index: int, chunk: "Chunk", S: np.ndarray):
        from ..io.mesh_ply import read_mesh
        from .texturing import texture_pages

        path = chunk.product_path("model")
        if path is None or not path.exists():
            raise ValueError(f"the chunk {chunk.label} has no model")
        m = read_mesh(path)
        self.index, self.chunk, self.label = index, chunk, chunk.label
        self.xyz = geometry.apply(S, m.xyz)
        self.faces = np.asarray(m.faces, np.int64)
        self.scale = float(np.cbrt(abs(np.linalg.det(np.asarray(S, float)[:3, :3]))))
        self.pages: list = []  # Path or BGR array per page
        self.texcoords = self.texnumber = None
        if m.texcoords is not None:
            pages = texture_pages(chunk)
            if pages and all(p.exists() for p in pages) and pages[0].suffix.lower() != ".obj":
                self.pages = list(pages)
                self.texcoords = np.asarray(m.texcoords, np.float32).reshape(-1, 6)
                self.texnumber = (np.asarray(m.texnumber, np.int32) if m.texnumber is not None and len(pages) > 1
                                  else np.zeros(len(self.faces), np.int32))
        # the colours of a textured model are read from its texture (what is seen), else its vertex colours
        self.colors = _colours_from_pages(self) if self.pages else m.colors
        if self.colors is None:
            self.colors = np.full((len(self.xyz), 3), 200, np.uint8)
        self.keep = np.ones(len(self.faces), bool)
        self.colour_fn = None  # the colour change found for it

    def edge(self) -> float:
        """Median edge length (internal units of the first chunk)."""
        f = self.faces[np.random.default_rng(0).choice(len(self.faces), min(len(self.faces), 20000), replace=False)]
        return float(np.median(np.linalg.norm(self.xyz[f[:, 0]] - self.xyz[f[:, 1]], axis=1))) if len(f) else 0.0


def _sample_page(img: np.ndarray, uv: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    x = np.clip((np.mod(uv[:, 0], 1.0) * w).astype(np.int64), 0, w - 1)
    y = np.clip(((1.0 - np.mod(uv[:, 1], 1.0)) * h).astype(np.int64), 0, h - 1)
    return img[y, x, ::-1].astype(np.float64)  # RGB


def _read_page(page, reduced: bool = False) -> np.ndarray:
    if isinstance(page, np.ndarray):
        return page
    img = cv2.imread(str(page), cv2.IMREAD_REDUCED_COLOR_4 if reduced else cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"cannot read the texture page {page}")
    return img


def _colours_from_pages(src: _Source) -> np.ndarray:
    """Vertex colours read from the texture at the corners of the faces (reduced pages)."""
    acc = np.zeros((len(src.xyz), 3))
    cnt = np.zeros(len(src.xyz))
    tex = src.texcoords.reshape(-1, 3, 2).astype(np.float64)
    for k, page in enumerate(src.pages):
        sel = src.texnumber == k
        if not sel.any():
            continue
        img = _read_page(page, reduced=True)
        for c in range(3):
            np.add.at(acc, src.faces[sel, c], _sample_page(img, tex[sel, c]))
            np.add.at(cnt, src.faces[sel, c], 1)
    out = np.clip(acc / np.maximum(cnt, 1)[:, None], 0, 255).astype(np.uint8)
    out[cnt == 0] = 200
    return out


# ------------------------------------------------------------------ colours
def _lab(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(np.clip(rgb, 0, 255).astype(np.uint8).reshape(-1, 1, 3), cv2.COLOR_RGB2LAB).reshape(-1, 3) \
        .astype(np.float64)


def fit_colour(src_rgb: np.ndarray, ref_rgb: np.ndarray, method: str) -> dict:
    """The colour change taking ``src_rgb`` to ``ref_rgb`` (pairs of colours of the same places, (n, 3) RGB):
    ``gain`` - per RGB channel ``a x + b`` (least squares, the pairs beyond 2.5 sigma left out once); ``lab`` - per
    CIE Lab channel the mean and spread of the source taken to those of the reference. Returns ``{"method",
    "a", "b"}`` (the channel coefficients: RGB for gain, Lab for lab) and ``"before"``/``"after"``: the mean
    difference of the pairs (Euclidean, in Lab units) before and after."""
    src = np.asarray(src_rgb, np.float64)
    ref = np.asarray(ref_rgb, np.float64)
    if method == "gain":
        a, b = np.ones(3), np.zeros(3)
        for c in range(3):
            x, y = src[:, c], ref[:, c]
            keep = np.ones(len(x), bool)
            for _ in range(2):
                A = np.column_stack([x[keep], np.ones(keep.sum())])
                (a[c], b[c]), *_ = np.linalg.lstsq(A, y[keep], rcond=None)
                r = y - (a[c] * x + b[c])
                keep = np.abs(r) <= 2.5 * max(float(np.std(r[keep])), 1e-6)
        a = np.clip(a, 0.4, 2.5)
    elif method == "lab":
        ls, lr = _lab(src), _lab(ref)
        ms, mr = ls.mean(0), lr.mean(0)
        ss, sr = ls.std(0), lr.std(0)
        a = np.clip(sr / np.maximum(ss, 1e-6), 0.5, 2.0)
        b = mr - a * ms
    else:
        raise ValueError(f"colour: one of {', '.join(COLOURS)}")
    fit = {"method": method, "a": [float(v) for v in a], "b": [float(v) for v in b]}
    before = float(np.mean(np.linalg.norm(_lab(src) - _lab(ref), axis=1)))
    after = float(np.mean(np.linalg.norm(_lab(apply_colour(src, fit)) - _lab(ref), axis=1)))
    fit.update(before=before, after=after)
    return fit


def apply_colour(rgb: np.ndarray, fit: dict) -> np.ndarray:
    """``rgb`` ((n, 3) or an image (h, w, 3), RGB) changed by ``fit`` (:func:`fit_colour`), as uint8."""
    rgb = np.asarray(rgb)
    shape = rgb.shape
    flat = rgb.reshape(-1, 3).astype(np.float64)
    a, b = np.asarray(fit["a"]), np.asarray(fit["b"])
    if fit["method"] == "gain":
        out = flat * a + b
    else:
        lab = _lab(flat) * a + b
        out = cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8).reshape(-1, 1, 3), cv2.COLOR_LAB2RGB).reshape(-1, 3)
    return np.clip(np.rint(out), 0, 255).astype(np.uint8).reshape(shape)


def _apply_to_page(img_bgr: np.ndarray, fit: dict, rows: int = 1024) -> np.ndarray:
    """A page (BGR) changed by ``fit``, a band of rows at a time (pages of 8192 px)."""
    out = np.empty_like(img_bgr)
    for r in range(0, img_bgr.shape[0], rows):
        band = img_bgr[r:r + rows, :, ::-1]
        out[r:r + rows] = apply_colour(band, fit)[:, :, ::-1]
    return out


def _equalise(sources: list[_Source], reference: int, method: str, distance: float, warnings: list[str],
              progress: Progress) -> list[dict]:
    """The colour change of every source, matched to the reference through the overlaps (see the module)."""
    from scipy.spatial import cKDTree

    done = [reference]
    todo = [i for i in range(len(sources)) if i != reference]
    report = []
    rng = np.random.default_rng(0)
    while todo:
        progress.check()
        ref_xyz = np.vstack([sources[i].xyz for i in done])
        ref_rgb = np.vstack([sources[i].colors.astype(np.float64) for i in done])
        tree = cKDTree(ref_xyz)
        best = None
        for i in todo:
            s = sources[i]
            pick = rng.choice(len(s.xyz), min(len(s.xyz), SAMPLE), replace=False)
            d, j = tree.query(s.xyz[pick], distance_upper_bound=distance)
            ok = np.isfinite(d)
            if best is None or ok.sum() > best[1].sum():
                best = (i, ok, pick, j)
        i, ok, pick, j = best
        s = sources[i]
        if ok.sum() < MIN_PAIRS:  # none of the rest overlaps what is matched
            for k in todo:
                warnings.append(f"{sources[k].label}: no overlap with the colours already matched, colours left")
                report.append({"chunk": sources[k].label, "pairs": 0, "fit": None})
            break
        fit = fit_colour(s.colors[pick[ok]], ref_rgb[j[ok]], method)
        s.colour_fn = fit
        s.colors = apply_colour(s.colors, fit)
        report.append({"chunk": s.label, "pairs": int(ok.sum()), "fit": fit})
        log.info("Colori di %s accordati (%s, %d coppie): differenza media %.1f -> %.1f", s.label, method,
                 int(ok.sum()), fit["before"], fit["after"])
        done.append(i)
        todo.remove(i)
        progress(len(done) / len(sources), f"Colours of {s.label}")
    return report


# ------------------------------------------------------------------ overlaps
def _face_normals(xyz: np.ndarray, faces: np.ndarray) -> np.ndarray:
    n = np.cross(xyz[faces[:, 1]] - xyz[faces[:, 0]], xyz[faces[:, 2]] - xyz[faces[:, 0]])
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-30)


def _remove_overlaps(sources: list[_Source], order: list[int], distance: float, progress: Progress,
                     min_cos: float = 0.5) -> dict:
    """Faces of a source on the surface of a source before it in ``order`` left out (see the module)."""
    removed = {}
    for pos, i in enumerate(order[1:], start=1):
        progress.check()
        s = sources[i]
        before = [sources[k] for k in order[:pos]]
        vx = np.vstack([b.xyz for b in before])
        offs = np.cumsum([0] + [len(b.xyz) for b in before])
        fx = np.vstack([b.faces[b.keep] + o for b, o in zip(before, offs[:-1])])
        if not len(fx):
            continue
        nrm_b = _face_normals(vx, fx)
        centres = s.xyz[s.faces].mean(axis=1)
        nrm_s = _face_normals(s.xyz, s.faces)
        dist, face = _closest(vx, fx, centres)
        same_side = np.einsum("ij,ij->i", nrm_s, nrm_b[np.clip(face, 0, len(fx) - 1)]) >= min_cos
        gone = (dist <= distance) & same_side & (face >= 0)
        s.keep &= ~gone
        removed[s.label] = int(gone.sum())
        progress(pos / max(len(order) - 1, 1), f"Overlaps of {s.label}: {int(gone.sum()):,} faces")
    return removed


def _closest(vertices: np.ndarray, faces: np.ndarray, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Distance from ``points`` to the nearest face of the mesh and that face (Open3D, else the nearest vertex)."""
    try:
        import open3d as o3d

        scene = o3d.t.geometry.RaycastingScene()
        centre = vertices.mean(axis=0)
        scene.add_triangles(o3d.core.Tensor((vertices - centre).astype(np.float32)),
                            o3d.core.Tensor(faces.astype(np.uint32)))
        out_d, out_f = np.empty(len(points)), np.empty(len(points), np.int64)
        step = 2_000_000
        for a in range(0, len(points), step):
            q = o3d.core.Tensor((points[a:a + step] - centre).astype(np.float32))
            ans = scene.compute_closest_points(q)
            p = ans["points"].numpy().astype(np.float64)
            out_d[a:a + step] = np.linalg.norm(p - (points[a:a + step] - centre), axis=1)
            out_f[a:a + step] = ans["primitive_ids"].numpy().astype(np.int64)
        out_f[out_f >= len(faces)] = -1
        return out_d, out_f
    except ImportError:  # pragma: no cover - Open3D is a dependency
        from scipy.spatial import cKDTree

        d, v = cKDTree(vertices).query(points)
        owner = np.full(len(vertices), -1, np.int64)
        owner[faces.ravel()] = np.repeat(np.arange(len(faces)), 3)
        return d, owner[v]


# ------------------------------------------------------------------ pages
def _reduced(img: np.ndarray, side: int | None) -> np.ndarray:
    if not side or max(img.shape[:2]) <= side:
        return img
    f = side / max(img.shape[:2])
    return cv2.resize(img, (max(1, int(img.shape[1] * f + 1e-9)), max(1, int(img.shape[0] * f + 1e-9))),
                      interpolation=cv2.INTER_AREA)


def _shelf_pack(sizes: list[tuple[int, int]], page: int) -> list[tuple[int, int, int]]:
    """Places ``(page, x, y)`` for rectangles ``(w, h)`` (each at most ``page``) on pages ``page`` x ``page``,
    in shelves, the tallest first."""
    order = sorted(range(len(sizes)), key=lambda k: (-sizes[k][1], -sizes[k][0]))
    out: list = [None] * len(sizes)
    pages: list[list] = []  # per page: [y of the next shelf, current shelf (y, height, x)]
    for k in order:
        w, h = sizes[k]
        placed = False
        for p, st in enumerate(pages):
            y, sh, x = st[1]
            if x + w <= page and h <= sh:
                out[k] = (p, x, y)
                st[1] = (y, sh, x + w)
                placed = True
                break
            if st[0] + h <= page:
                st[1] = (st[0], h, w)
                out[k] = (p, 0, st[0])
                st[0] += h
                placed = True
                break
        if not placed:
            pages.append([h, (0, h, w)])
            out[k] = (len(pages) - 1, 0, 0)
    return out


def _plain_block(colors: list[np.ndarray]) -> tuple[np.ndarray, list[tuple[float, float]]]:
    side = 8
    cols = int(np.ceil(np.sqrt(len(colors))))
    rows = int(np.ceil(len(colors) / cols))
    img = np.full((rows * side, cols * side, 3), 200, np.uint8)
    uv = []
    for j, rgb in enumerate(colors):
        r, c = divmod(j, cols)
        img[r * side:(r + 1) * side, c * side:(c + 1) * side] = np.asarray(rgb[::-1], np.uint8)
        uv.append(((c * side + side / 2) / img.shape[1], 1.0 - (r * side + side / 2) / img.shape[0]))
    return img, uv


def _texture(sources: list[_Source], pages: str, page_size: int, max_page: int | None, progress: Progress):
    """The pages of the merged model (BGR arrays) and, per source, its texture coordinates and page numbers."""
    textured = [s for s in sources if s.pages]
    if not textured:
        return [], [None] * len(sources), [None] * len(sources)
    plain = [s for s in sources if not s.pages]
    images, owners = [], []  # every source page: the image (colours already changed) and (source, page)
    for s in textured:
        for k, page in enumerate(s.pages):
            progress.check()
            img = _read_page(page)
            if s.colour_fn is not None:
                img = _apply_to_page(img, s.colour_fn)
            cap = max_page if pages == "keep" else min(max_page or page_size, page_size - 2 * PAD)
            images.append(_reduced(img, cap))
            owners.append((s.index, k))
    uvs = [None] * len(sources)
    nums = [None] * len(sources)
    if pages == "keep":
        out = images
        first = {}
        for n, (i, k) in enumerate(owners):
            first.setdefault(i, n)
        for s in textured:
            uvs[s.index] = s.texcoords.copy()
            nums[s.index] = (s.texnumber + first[s.index]).astype(np.int32)
    else:
        sizes = [(im.shape[1] + 2 * PAD, im.shape[0] + 2 * PAD) for im in images]
        place = _shelf_pack(sizes, page_size)
        n_pages = max(p for p, _, _ in place) + 1
        widths = [0] * n_pages
        heights = [0] * n_pages
        for (p, x, y), (w, h) in zip(place, sizes):
            widths[p] = max(widths[p], x + w)
            heights[p] = max(heights[p], y + h)
        out = [np.full((heights[p], widths[p], 3), 127, np.uint8) for p in range(n_pages)]
        where = {}
        for im, (p, x, y), owner in zip(images, place, owners):
            padded = cv2.copyMakeBorder(im, PAD, PAD, PAD, PAD, cv2.BORDER_REPLICATE)
            out[p][y:y + padded.shape[0], x:x + padded.shape[1]] = padded
            where[owner] = (p, x + PAD, y + PAD, im.shape[1], im.shape[0])
        for s in textured:
            uv = s.texcoords.reshape(-1, 3, 2).astype(np.float64).copy()
            num = np.zeros(len(s.faces), np.int32)
            for k in range(len(s.pages)):
                sel = s.texnumber == k
                p, x, y, w, h = where[(s.index, k)]
                W, H = out[p].shape[1], out[p].shape[0]
                u = np.clip(uv[sel, :, 0], 0.0, 1.0)  # a packed page does not repeat
                v = np.clip(uv[sel, :, 1], 0.0, 1.0)
                uv[sel, :, 0] = (x + u * w) / W
                uv[sel, :, 1] = 1.0 - (y + (1.0 - v) * h) / H
                num[sel] = p
            uvs[s.index] = uv.reshape(-1, 6).astype(np.float32)
            nums[s.index] = num
    if plain:  # a block of the mean colour of each source without a texture, on a last page
        img, centres = _plain_block([s.colors.mean(axis=0) for s in plain])
        last = len(out)
        out = list(out) + [img]
        for s, (u, v) in zip(plain, centres):
            uvs[s.index] = np.tile(np.array([u, v], np.float32), (len(s.faces), 3))
            nums[s.index] = np.full(len(s.faces), last, np.int32)
    return out, uvs, nums


# ------------------------------------------------------------------ the merge
def merge_models(doc: "Document", chunks, label: str | None = None, reference=None, colour: str = "none",
                 match_distance: float | None = None, remove_overlaps: bool = False, priority=None,
                 overlap_distance: float | None = None, pages: str = "keep", page_size: int = 8192,
                 max_page: int | None = None, progress: Progress | None = None) -> tuple["Chunk", dict]:
    """The models of ``chunks`` (at least two, chunks or their labels/keys) as one model of a new chunk (see the
    module). ``label``: its name (default "Modello unito"). ``colour``: "none", "gain" or "lab";
    ``reference``: the chunk whose colours the others take (default the first); ``match_distance``: metres
    between two vertices taken for the same place (default twice the median edge of the models).
    ``remove_overlaps``: faces of a source on a source before it in ``priority`` (default the order of
    ``chunks``) left out, within ``overlap_distance`` metres (default twice the median edge). ``pages``: "keep" or
    "repack" (pages of at most ``page_size`` px); ``max_page``: pages reduced to this side. Returns ``(chunk,
    report)``; the report is also ``chunk.meta["merge_models"]``. The source chunks are not changed."""
    from ..core import versions
    from ..io.mesh_ply import MeshData, write_mesh
    from .chunks import _frame_to, _resolve_many, resolve_chunk

    progress = as_progress(progress)
    if colour not in COLOURS:
        raise ValueError(f"colour: one of {', '.join(COLOURS)}")
    if pages not in PAGES:
        raise ValueError(f"pages: one of {', '.join(PAGES)}")
    page_size = int(page_size)
    if page_size < 256:
        raise ValueError("page_size: at least 256 pixels")
    chunks = _resolve_many(doc, chunks)
    if len(chunks) < 2:
        raise ValueError("merge_models needs the models of at least two chunks")
    first = chunks[0]
    warnings: list[str] = []
    progress(0.0, f"Reading {len(chunks)} models")
    sources = []
    for i, ch in enumerate(chunks):
        progress.check()
        sources.append(_Source(i, ch, _frame_to(ch, first, warnings)))
        progress(0.25 * (i + 1) / len(chunks), f"Model of {ch.label}: {len(sources[-1].faces):,} faces")
    scale = float(first.scale) or 1.0  # world metres per internal unit of the first chunk
    edge = float(np.median([s.edge() for s in sources])) or 1e-3
    ref_index = 0 if reference is None else next(
        (s.index for s in sources if s.chunk is resolve_chunk(doc, reference)), None)
    if ref_index is None:
        raise ValueError("the reference chunk is not among the chunks merged")
    colour_report = []
    if colour != "none":
        d = (match_distance / scale) if match_distance else 2.0 * edge
        colour_report = _equalise(sources, ref_index, colour, d, warnings, progress.sub(0.25, 0.15))
    removed = {}
    order = list(range(len(sources)))
    if priority is not None:
        wanted = [resolve_chunk(doc, c) for c in priority]
        order = [next(s.index for s in sources if s.chunk is c) for c in wanted if any(s.chunk is c for s in sources)]
        order += [i for i in range(len(sources)) if i not in order]
    if remove_overlaps:
        d = (overlap_distance / scale) if overlap_distance else 2.0 * edge
        removed = _remove_overlaps(sources, order, d, progress.sub(0.4, 0.2))
    progress(0.6, "Texture pages")
    images, uvs, nums = _texture(sources, pages, page_size, max_page, progress.sub(0.6, 0.25))
    # one mesh: every source's kept faces and the vertices they use
    xyz, rgb, faces, tex, num, groups = [], [], [], [], [], []
    base = 0
    for s in sources:
        keep = s.keep
        f = s.faces[keep]
        used = np.zeros(len(s.xyz), bool)
        used[f.ravel()] = True
        remap = np.cumsum(used) - 1
        xyz.append(s.xyz[used])
        rgb.append(s.colors[used])
        faces.append(remap[f] + base)
        groups.append(np.full(len(f), s.index, np.int32))
        if images:
            tex.append(uvs[s.index][keep])
            num.append(nums[s.index][keep])
        base += int(used.sum())
    xyz = np.vstack(xyz)
    if not len(xyz):
        raise ValueError("no face left in the merged model")
    double = bool(np.abs(xyz).max() > DOUBLE_BEYOND)
    ft = "<f8" if double else "<f4"
    vertices = np.empty(len(xyz), [("x", ft), ("y", ft), ("z", ft), ("red", "u1"), ("green", "u1"), ("blue", "u1")])
    vertices["x"], vertices["y"], vertices["z"] = xyz.T
    vertices["red"], vertices["green"], vertices["blue"] = np.vstack(rgb).T
    mesh = MeshData(vertices, np.vstack(faces).astype(np.int64),
                    np.vstack(tex).astype(np.float32) if images else None, [],
                    np.concatenate(num).astype(np.int32) if len(images) > 1 else None)
    mesh.set_groups(np.concatenate(groups), {s.index: s.label for s in sources})
    merged = doc.add_chunk(label or "Modello unito")
    try:
        merged.crs = first.crs
        merged.transform = np.asarray(first.transform, float).copy()
        for key in ("metric", "levelled"):
            if first.meta.get(key):
                merged.meta[key] = first.meta[key]
        data = merged.data_dir
        names = ["model_texture.jpg" if k == 0 else f"model_texture_{k}.jpg" for k in range(len(images))]
        for k, (img, name) in enumerate(zip(images, names)):
            progress.check()
            if not cv2.imwrite(str(data / name), img, [cv2.IMWRITE_JPEG_QUALITY, TEXTURE_QUALITY]):
                raise OSError(f"cannot write {data / name}")
            progress(0.85 + 0.1 * (k + 1) / len(images), f"Texture page {k + 1}/{len(images)}")
        if len(names) > 1:
            mesh.set_texture_files(names)
        elif names:
            mesh.set_texture_file(names[0])
        merged._set_product("model", write_mesh(merged.own_product_path("model"), mesh))
        if names:
            merged._set_product("model_textured", data / names[0])
            merged.meta["texture"] = {"mapping": "merge", "size": [int(images[0].shape[1]), int(images[0].shape[0])],
                                      "pages": len(names)}
        report = {"sources": [{"chunk": s.label, "key": s.chunk.key, "faces": int(len(s.faces)),
                               "kept": int(s.keep.sum()), "pages": len(s.pages)} for s in sources],
                  "faces": int(len(mesh.faces)), "vertices": int(len(xyz)), "pages": len(names),
                  "page_layout": pages, "colour": colour, "colours": colour_report,
                  "reference": sources[ref_index].label, "removed": removed,
                  "priority": [sources[i].label for i in order] if remove_overlaps else None,
                  "warnings": warnings, "when": datetime.now().isoformat(timespec="seconds")}
        merged.meta["model"] = {"source": "merge", "chunks": [s.label for s in sources]}
        merged.meta["merge_models"] = report
        versions.set_active_name(merged, "model", versions.label("Modello unito da {0}",
                                                                  ", ".join(s.label for s in sources))[:80])
    except BaseException:
        doc.remove_chunk(merged)
        raise
    progress(1.0, f"Merged model: {report['faces']:,} faces, {len(names)} texture pages")
    return merged, report


__all__ = ["COLOURS", "PAGES", "merge_models", "fit_colour", "apply_colour"]
