//! Triangulated irregular networks (`engine.tin`): the constrained Delaunay triangulation of the
//! vertices (spade), the TIN rasterised on a grid, its contour segments and its volume against a
//! plane. The coordinates are local (the Python side subtracts a round origin), in double precision.

use rayon::prelude::*;
use spade::{ConstrainedDelaunayTriangulation, HasPosition, Point2, Triangulation};

const NEW: usize = usize::MAX;
const SLACK: f64 = 1e-6; // cells: the box of a triangle widened against the rounding of its corners

/// A vertex of the triangulation: its position, the index of the input point (``NEW`` for a vertex
/// made where two constraints cross) and, for a new one, the constraint that made it.
#[derive(Clone, Copy, Debug)]
struct Vertex {
    pos: Point2<f64>,
    index: usize,
    on: [usize; 2],
}

impl HasPosition for Vertex {
    type Scalar = f64;
    fn position(&self) -> Point2<f64> {
        self.pos
    }
}

/// The triangulation of ``xy`` with the constraint edges ``constraints`` (pairs of input indices).
pub struct Triangulated {
    /// counter-clockwise triangles: input indices, then ``n + k`` for the k-th new vertex
    pub triangles: Vec<[i64; 3]>,
    /// the constraint edges as they are in the triangulation (split where constraints cross)
    pub constrained: Vec<[i64; 2]>,
    /// the vertices made where constraints cross, and the constraint (input indices) each one lies on
    pub new_xy: Vec<[f64; 2]>,
    pub new_on: Vec<[i64; 2]>,
}

/// Constrained Delaunay triangulation (spade, robust predicates in f64). Points at the same place
/// are one vertex (the first of them); constraints that cross are split at the crossing, a new
/// vertex there; a constraint through a vertex is split at it.
pub fn triangulate(xy: &[[f64; 2]], constraints: &[[usize; 2]]) -> Result<Triangulated, String> {
    let n = xy.len();
    // first, before any comparison: a NaN is never equal to itself and would never be merged
    for (i, p) in xy.iter().enumerate() {
        if !p[0].is_finite() || !p[1].is_finite() {
            return Err(format!("point {i} is not finite"));
        }
    }
    // the first point of each position: duplicates point to it
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by(|&a, &b| {
        xy[a][0].total_cmp(&xy[b][0]).then(xy[a][1].total_cmp(&xy[b][1])).then(a.cmp(&b))
    });
    let mut first = vec![0usize; n];
    let mut k = 0;
    while k < n {
        let mut j = k;
        while j < n
            && xy[order[j]][0].total_cmp(&xy[order[k]][0]).is_eq()
            && xy[order[j]][1].total_cmp(&xy[order[k]][1]).is_eq()
        {
            first[order[j]] = order[k];
            j += 1;
        }
        k = j;
    }
    let mut vertices = Vec::with_capacity(n);
    for (i, p) in xy.iter().enumerate() {
        if first[i] == i {
            vertices.push(Vertex { pos: Point2::new(p[0], p[1]), index: i, on: [NEW, NEW] });
        }
    }
    let mut cdt: ConstrainedDelaunayTriangulation<Vertex> =
        ConstrainedDelaunayTriangulation::bulk_load(vertices).map_err(|e| format!("{e:?}"))?;
    // handle of every input point (the bulk load orders the vertices its own way)
    let mut handle = vec![None; n];
    for v in cdt.fixed_vertices() {
        let i = cdt.vertex(v).data().index;
        handle[i] = Some(v);
    }
    for (c, &[a, b]) in constraints.iter().enumerate() {
        if a >= n || b >= n {
            return Err(format!("constraint {c}: index out of range"));
        }
        let (fa, fb) = (first[a], first[b]);
        if fa == fb {
            continue;
        }
        let (ha, hb) = match (handle[fa], handle[fb]) {
            (Some(ha), Some(hb)) => (ha, hb),
            _ => return Err(format!("constraint {c}: vertex missing from the triangulation")),
        };
        cdt.add_constraint_and_split(ha, hb, |p| Vertex { pos: p, index: NEW, on: [fa, fb] });
    }
    // output indices: the input index, or n + k for the k-th new vertex
    let mut out_index = vec![0i64; cdt.num_vertices()];
    let mut new_xy = Vec::new();
    let mut new_on = Vec::new();
    for v in cdt.vertices() {
        let d = v.data();
        out_index[v.fix().index()] = if d.index == NEW {
            new_xy.push([d.pos.x, d.pos.y]);
            new_on.push([d.on[0] as i64, d.on[1] as i64]);
            (n + new_xy.len() - 1) as i64
        } else {
            d.index as i64
        };
    }
    let triangles = cdt
        .inner_faces()
        .map(|f| {
            let [a, b, c] = f.vertices();
            [out_index[a.fix().index()], out_index[b.fix().index()], out_index[c.fix().index()]]
        })
        .collect();
    let constrained = cdt
        .undirected_edges()
        .filter(|e| e.is_constraint_edge())
        .map(|e| {
            let [a, b] = e.vertices();
            let (a, b) = (out_index[a.fix().index()], out_index[b.fix().index()]);
            [a.min(b), a.max(b)]
        })
        .collect();
    Ok(Triangulated { triangles, constrained, new_xy, new_on })
}

/// The corners of triangle ``t``: positions and heights.
#[inline]
fn corners(xy: &[[f64; 2]], z: &[f64], t: &[usize; 3]) -> ([[f64; 2]; 3], [f64; 3]) {
    ([xy[t[0]], xy[t[1]], xy[t[2]]], [z[t[0]], z[t[1]], z[t[2]]])
}

/// The TIN on a grid of ``width`` x ``height`` cells of side ``cell`` whose top-left corner is
/// (``x0``, ``y0``): each cell centre takes the plane of the triangle it lies in, NaN outside them.
/// Rows in parallel, the triangles bucketed by the rows they span.
pub fn rasterize(xy: &[[f64; 2]], z: &[f64], triangles: &[[usize; 3]], x0: f64, y0: f64, cell: f64,
                 width: usize, height: usize) -> Vec<f32> {
    let mut out = vec![f32::NAN; width * height];
    if width == 0 || height == 0 || triangles.is_empty() {
        return out;
    }
    // the rows (centres) each triangle may cover
    let span = |t: &[usize; 3]| -> Option<(usize, usize)> {
        let ys = [xy[t[0]][1], xy[t[1]][1], xy[t[2]][1]];
        let (lo, hi) = (ys[0].min(ys[1]).min(ys[2]), ys[0].max(ys[1]).max(ys[2]));
        // row r has its centre at y0 - (r + 0.5) cell
        // (a slack of a millionth of a cell: a centre on a vertex's row is not lost to the rounding)
        let r0 = ((y0 - hi) / cell - 0.5 - SLACK).ceil().max(0.0);
        let r1 = ((y0 - lo) / cell - 0.5 + SLACK).floor();
        if !(r0.is_finite() && r1.is_finite()) || r1 < r0 || r0 >= height as f64 || r1 < 0.0 {
            return None;
        }
        Some((r0 as usize, (r1 as usize).min(height - 1)))
    };
    let mut count = vec![0usize; height + 1];
    for t in triangles {
        if let Some((a, b)) = span(t) {
            for c in &mut count[a..=b] {
                *c += 1;
            }
        }
    }
    let mut start = vec![0usize; height + 1];
    for r in 0..height {
        start[r + 1] = start[r] + count[r];
    }
    let mut fill = start.clone();
    let mut bucket = vec![0usize; start[height]];
    for (i, t) in triangles.iter().enumerate() {
        if let Some((a, b)) = span(t) {
            for r in a..=b {
                bucket[fill[r]] = i;
                fill[r] += 1;
            }
        }
    }
    out.par_chunks_mut(width).enumerate().for_each(|(r, row)| {
        let y = y0 - (r as f64 + 0.5) * cell;
        for &i in &bucket[start[r]..start[r + 1]] {
            let (p, h) = corners(xy, z, &triangles[i]);
            let det = (p[1][0] - p[0][0]) * (p[2][1] - p[0][1]) - (p[2][0] - p[0][0]) * (p[1][1] - p[0][1]);
            if det == 0.0 || !det.is_finite() {
                continue;
            }
            let xs = [p[0][0], p[1][0], p[2][0]];
            let (lo, hi) = (xs[0].min(xs[1]).min(xs[2]), xs[0].max(xs[1]).max(xs[2]));
            let c0 = ((lo - x0) / cell - 0.5 - SLACK).ceil().max(0.0);
            let c1 = ((hi - x0) / cell - 0.5 + SLACK).floor().min(width as f64 - 1.0);
            if c1 < c0 {
                continue;
            }
            // a small tolerance keeps the cells on an edge (either side gives the same height) and
            // on the edge of the hull despite the rounding of the coordinates
            let eps = -1e-9;
            for c in c0 as usize..=c1 as usize {
                let x = x0 + (c as f64 + 0.5) * cell;
                let l1 = ((x - p[0][0]) * (p[2][1] - p[0][1]) - (p[2][0] - p[0][0]) * (y - p[0][1])) / det;
                let l2 = ((p[1][0] - p[0][0]) * (y - p[0][1]) - (x - p[0][0]) * (p[1][1] - p[0][1])) / det;
                let l0 = 1.0 - l1 - l2;
                if l0 >= eps && l1 >= eps && l2 >= eps && row[c].is_nan() {
                    row[c] = (l0 * h[0] + l1 * h[1] + l2 * h[2]) as f32;
                }
            }
        }
    });
    out
}

/// The contour segments of the TIN at ``levels``: in each triangle the plane meets a level along
/// one segment between two edges (a corner counts as above when its height is >= the level).
/// Returns the segments ((x, y), (x, y)) and the index of their level, triangle by triangle.
pub fn contours(xy: &[[f64; 2]], z: &[f64], triangles: &[[usize; 3]], levels: &[f64])
                -> (Vec<[[f64; 2]; 2]>, Vec<i64>) {
    let parts: Vec<(Vec<[[f64; 2]; 2]>, Vec<i64>)> = triangles
        .par_chunks(4096)
        .map(|block| {
            let mut segs = Vec::new();
            let mut idx = Vec::new();
            for t in block {
                let (p, h) = corners(xy, z, t);
                let (lo, hi) = (h[0].min(h[1]).min(h[2]), h[0].max(h[1]).max(h[2]));
                for (k, &level) in levels.iter().enumerate() {
                    if level < lo || level > hi {
                        continue;
                    }
                    let mut pts = [[0.0; 2]; 2];
                    let mut m = 0;
                    for e in 0..3 {
                        // each edge from its smaller vertex index: the two triangles of an edge give
                        // the same point to the last bit, so the segments join into lines
                        let (a, b) = if t[e] < t[(e + 1) % 3] { (e, (e + 1) % 3) } else { ((e + 1) % 3, e) };
                        if (h[a] >= level) != (h[b] >= level) {
                            let s = (level - h[a]) / (h[b] - h[a]);
                            pts[m] = [p[a][0] + s * (p[b][0] - p[a][0]), p[a][1] + s * (p[b][1] - p[a][1])];
                            m += 1;
                        }
                    }
                    if m == 2 {
                        segs.push(pts);
                        idx.push(k as i64);
                    }
                }
            }
            (segs, idx)
        })
        .collect();
    let mut segs = Vec::new();
    let mut idx = Vec::new();
    for (s, i) in parts {
        segs.extend(s);
        idx.extend(i);
    }
    (segs, idx)
}

/// Exact volumes between the TIN and the plane z = a x + b y + c: what lies above it, what lies
/// below it, and the area (plan) of the triangles. The difference is linear on each triangle, so
/// the prism splits exactly where it crosses zero.
pub fn volume(xy: &[[f64; 2]], z: &[f64], triangles: &[[usize; 3]], plane: [f64; 3]) -> (f64, f64, f64) {
    // sums of fixed blocks added in order: the same result to the last bit whatever the threads do
    let parts: Vec<(f64, f64, f64)> = triangles
        .par_chunks(8192)
        .map(|block| {
            let mut sum = (0.0, 0.0, 0.0);
            for t in block {
                let (p, h) = corners(xy, z, t);
                let area = 0.5
                    * ((p[1][0] - p[0][0]) * (p[2][1] - p[0][1]) - (p[2][0] - p[0][0]) * (p[1][1] - p[0][1])).abs();
                let d = [0, 1, 2].map(|k| h[k] - (plane[0] * p[k][0] + plane[1] * p[k][1] + plane[2]));
                let (above, below) = split_prism(area, d);
                sum = (sum.0 + above, sum.1 + below, sum.2 + area);
            }
            sum
        })
        .collect();
    parts.iter().fold((0.0, 0.0, 0.0), |a, b| (a.0 + b.0, a.1 + b.1, a.2 + b.2))
}

/// The integral of max(d, 0) and of max(-d, 0) over a triangle of ``area`` where d is linear with
/// the values ``d`` at the corners.
pub fn split_prism(area: f64, d: [f64; 3]) -> (f64, f64) {
    let total = area * (d[0] + d[1] + d[2]) / 3.0;
    let pos: Vec<usize> = (0..3).filter(|&k| d[k] > 0.0).collect();
    // the part of one sign on the side of a lone corner: a triangle t1 t2 times as large, height d
    let lone = |k: usize| -> f64 {
        let (a, b) = ((k + 1) % 3, (k + 2) % 3);
        let t1 = d[k] / (d[k] - d[a]);
        let t2 = d[k] / (d[k] - d[b]);
        area * t1 * t2 * d[k] / 3.0
    };
    match pos.len() {
        0 => (0.0, -total),
        3 => (total, 0.0),
        1 => {
            let k = pos[0];
            if d[(k + 1) % 3] == 0.0 && d[(k + 2) % 3] == 0.0 {
                return (total, 0.0);
            }
            let above = lone(k);
            (above, above - total)
        }
        _ => {
            let k = (0..3).find(|&k| d[k] <= 0.0).unwrap();
            if d[k] == 0.0 {
                return (total, 0.0);
            }
            let below = -lone(k);
            (total + below, below)
        }
    }
}
