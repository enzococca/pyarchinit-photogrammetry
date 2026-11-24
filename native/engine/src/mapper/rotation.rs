//! The rotation averaging of the mapper (step 2 of the design, as GLOMAP's
//! `estimators/global_rotation_averaging.cc` and `controllers/global_mapper.cc`): the rotations of
//! the photos from the relative rotations of the edges of the view graph.
//!
//! 1. The largest connected part of the graph is taken; its maximum spanning tree on the weights
//!    of the edges (Kruskal, ties broken by the order of the edges) gives the first rotations,
//!    chained along the tree from its root, the photo of most edges (ties: the first photo). The
//!    edges that close a loop of three photos are taken into the tree before those that close none
//!    (`confirmed`): the weights alone (inlier counts) can put a wrong edge in the tree, and a whole
//!    branch then starts wrong by up to 180 degrees.
//! 2. The rotations are refined on the Lie algebra (Chatterjee and Govindu 2013): the error of an
//!    edge `a-b` is `phi = log(R_b^T R_ab R_a)` (`R` the `cam_from_world` rotation of a photo, `R_ab`
//!    the edge's `cam_b_from_cam_a`), the rotations move by `R <- R exp(-w)` and each step solves
//!    the errors linearised in the updates `w`, the root held at zero. Near agreement the linear
//!    relation is Chatterjee and Govindu's `w_b - w_a = -phi`; here it carries the inverse
//!    Jacobians of the rotations (`jacobians`), so that a spanning tree through wrong edges, which
//!    starts some photos tens of degrees away, is corrected along a direction that lowers the cost,
//!    and each step is halved until it does. First rounds of L1 regression (least absolute
//!    deviations, by the alternating direction method of multipliers of Boyd et al. 2011, section
//!    6.1), then iteratively reweighted least squares with the Geman-McClure weights of Chatterjee
//!    and Govindu of scale 5 degrees, until the mean update of a photo is below a thousandth of a
//!    radian. A photo that most of its edges place elsewhere than the averaging left it is moved
//!    there and the reweighted least squares run again (`reseat`).
//! 3. The edges whose rotation disagrees with the averaged rotations by more than
//!    `Options::max_rotation_error_deg` are dropped, the largest connected part of what is left is
//!    averaged once more from its own spanning tree and the disagreeing edges are dropped again.
//! 4. The photos of the largest connected part of what is left are placed; the others are `None`.
//!
//! Every edge is used for its rotation, also one without parallax (`Edge::parallax == false`: a
//! pair taken from one place has a meaningful rotation and a translation of the noise alone). The
//! weights of the edges choose the spanning tree only; the averaging weighs every edge alike, as
//! GLOMAP does by default (`use_weight = false`).
//!
//! Each linear step is a solve of the 3n x 3n normal equations (three unknowns per photo but the
//! root) by faer's sparse Cholesky: its symbolic factorisation once per averaging, the numeric one
//! once per round of L1 regression and once per iteration of the reweighted least squares.
//!
//! Everything runs on one thread in a fixed order, so that two runs give the same bits.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};

use faer::dyn_stack::{MemBuffer, MemStack};
use faer::linalg::cholesky::llt::factor::LltRegularization;
use faer::sparse::linalg::cholesky::{
    factorize_symbolic_cholesky, CholeskySymbolicParams, SymbolicCholesky, SymmetricOrdering,
};
use faer::sparse::{SparseColMatRef, SymbolicSparseColMat};
use faer::{Conj, MatMut, Par, Side};
use nalgebra::{Matrix3, Rotation3, UnitQuaternion, Vector3};

use super::graph::{Edge, ViewGraph};
use super::Options;

/// A link of a part: its photos `a` and `b` (indices in the part), `cam_b_from_cam_a`, its weight.
type Link = (usize, usize, UnitQuaternion<f64>, f64);

/// The rounds of L1 regression (GLOMAP has 5: from a spanning tree through several wrong edges the
/// rounds need more, and they stop when a round moves the photos by less than `STEP_CONVERGENCE`)
/// and the iterations of the alternating direction method of the first round, doubled at every
/// round up to the most (the iterations GLOMAP gives its least absolute deviations solver).
const L1_ROUNDS: usize = 50;
const ADMM_FIRST_ITERATIONS: usize = 10;
const ADMM_MAX_ITERATIONS: usize = 100;
/// The penalty, the over-relaxation and the absolute and relative tolerances of the alternating
/// direction method (the figures of Boyd et al. 2011 for least absolute deviations).
const ADMM_RHO: f64 = 1.0;
const ADMM_ALPHA: f64 = 1.0;
const ADMM_ABS_TOL: f64 = 1e-4;
const ADMM_REL_TOL: f64 = 1e-2;
/// A step of the L1 rounds or of the reweighted least squares is halved at most this many times
/// until it lowers the cost.
const LINE_SEARCH_HALVINGS: usize = 10;
/// The iterations of the reweighted least squares and the scale of the Geman-McClure weights, in
/// degrees (GLOMAP's `max_num_irls_iterations`, `irls_loss_parameter_sigma`).
const IRLS_ITERATIONS: usize = 100;
const IRLS_SIGMA_DEG: f64 = 5.0;
/// The L1 rounds and the reweighted least squares stop when the mean norm of the update of a photo
/// is below this, in radians (GLOMAP's `l1_step_convergence_threshold`,
/// `irls_step_convergence_threshold`).
const STEP_CONVERGENCE: f64 = 1e-3;
/// The largest departure from a rotation of the matrix of an edge that is used.
const ROTATION_TOLERANCE: f64 = 1e-6;

/// The rotations of the photos: `world_from_cam` of each photo of the part placed, `None` for the
/// others; `placed` the number of those photos; the edges dropped (their indices in
/// `ViewGraph::edges`, ascending): those that disagree with the averaged rotations and those that
/// cannot be used (a photo out of range, both ends on one photo, a matrix that is not a rotation);
/// the sizes of the connected parts of the photos joined by the edges left, largest first (a photo
/// in no edge is in none); `refused` the reason when nothing was averaged because the view graph
/// was refused (its own reason) or `Options::max_rotation_error_deg` is not a positive finite
/// number, everything else then empty.
///
/// The part placed is the largest of the part averaged. It is the first of `components` but in a
/// rare case: when the edges dropped by the second filter split the part averaged below the size
/// of a part that was never averaged, the first of `components` is that part and has no rotations.
/// The check of the photos placed (90 % of them) and the report of the parts read `placed` and
/// `world_from_cam`, never `components[0]`.
#[derive(Clone, Debug, PartialEq)]
pub struct Rotations {
    pub world_from_cam: Vec<Option<[[f64; 3]; 3]>>,
    pub placed: usize,
    pub dropped: Vec<usize>,
    pub components: Vec<usize>,
    pub refused: Option<String>,
}

/// The rotations of the `n_images` photos from the edges of `graph` (see the module): a graph that
/// was refused, or a `max_rotation_error_deg` that is not a positive finite number, is refused
/// with the reason; a graph without edges places nothing.
pub fn average(graph: &ViewGraph, n_images: usize, o: &Options) -> Rotations {
    average_until(graph, n_images, o, &AtomicBool::new(false))
}

/// `average`, ended early when `stop` is raised (by another thread, checked at every iteration of
/// the solves): the rotations are then refused with the reason `"cancelled"`.
pub fn average_until(graph: &ViewGraph, n_images: usize, o: &Options, stop: &AtomicBool) -> Rotations {
    let mut out = Rotations {
        world_from_cam: vec![None; n_images],
        placed: 0,
        dropped: Vec::new(),
        components: Vec::new(),
        refused: None,
    };
    if let Some(reason) = &graph.refused {
        out.refused = Some(reason.clone());
        return out;
    }
    let limit_deg = o.max_rotation_error_deg;
    if !(limit_deg > 0.0 && limit_deg.is_finite()) {
        let reason = format!("max_rotation_error_deg must be a positive finite number, not {limit_deg}");
        out.refused = Some(reason);
        return out;
    }
    if graph.edges.is_empty() {
        return out;
    }
    let edges = &graph.edges;
    let relative: Vec<Option<UnitQuaternion<f64>>> = edges.iter().map(|e| usable(e, n_images)).collect();
    let mut active: Vec<bool> = relative.iter().map(|r| r.is_some()).collect();
    out.dropped.extend((0..edges.len()).filter(|&k| !active[k]));
    // the part averaged: the largest of the whole graph, then the largest of it once its
    // disagreeing edges are dropped (GLOMAP's two rounds of averaging, filter and largest part)
    let mut part: Option<Vec<usize>> = None;
    let mut rotations: Vec<Option<UnitQuaternion<f64>>> = vec![None; n_images];
    for _ in 0..2 {
        let Some(nodes) = largest_within(&components(n_images, edges, &active), part.as_deref()) else {
            break;
        };
        let inside: Vec<usize> = (0..edges.len()).filter(|&k| active[k] && contains(&nodes, edges[k].a)).collect();
        let limit = o.max_rotation_error_deg.to_radians();
        let estimate = average_part(&nodes, edges, &inside, &relative, limit, stop);
        if stop.load(Ordering::SeqCst) {
            out.refused = Some("cancelled".into());
            return out;
        }
        rotations = vec![None; n_images];
        for (&i, q) in nodes.iter().zip(estimate) {
            rotations[i] = Some(q);
        }
        for &k in &inside {
            let (a, b) = (edges[k].a, edges[k].b);
            let (qa, qb) = (rotations[a].unwrap(), rotations[b].unwrap());
            let rab = relative[k].unwrap();
            // not `>=`: an edge at the limit is kept
            if (rab.inverse() * qb * qa.inverse()).angle() > limit {
                active[k] = false;
                out.dropped.push(k);
            }
        }
        part = Some(nodes);
    }
    let parts = components(n_images, edges, &active);
    if let Some(placed) = largest_within(&parts, part.as_deref()) {
        for i in placed {
            let w = rotations[i].expect("a photo of the part averaged").inverse().to_rotation_matrix();
            out.world_from_cam[i] = Some(std::array::from_fn(|r| std::array::from_fn(|c| w[(r, c)])));
            out.placed += 1;
        }
    }
    out.components = parts.iter().map(|p| p.len()).collect();
    out.dropped.sort_unstable();
    out
}

/// The relative rotation of an edge as a unit quaternion, `None` when the edge cannot be used.
fn usable(e: &Edge, n_images: usize) -> Option<UnitQuaternion<f64>> {
    if e.a >= n_images || e.b >= n_images || e.a == e.b {
        return None;
    }
    let m = Matrix3::from_fn(|i, j| e.rotation[i][j]);
    let orthogonal = (m * m.transpose() - Matrix3::identity()).amax() <= ROTATION_TOLERANCE;
    if !m.iter().all(|x| x.is_finite()) || !orthogonal || m.determinant() <= 0.0 {
        return None;
    }
    Some(UnitQuaternion::from_rotation_matrix(&Rotation3::from_matrix_unchecked(m)))
}

fn contains(sorted: &[usize], i: usize) -> bool {
    sorted.binary_search(&i).is_ok()
}

/// The connected parts of the photos joined by the `active` edges, each sorted, largest first
/// (ties: the part of the first photo first); a photo in no active edge is in none.
fn components(n: usize, edges: &[Edge], active: &[bool]) -> Vec<Vec<usize>> {
    let mut parent: Vec<usize> = (0..n).collect();
    let mut joined = vec![false; n];
    for (e, _) in edges.iter().zip(active).filter(|(_, &on)| on) {
        let (ra, rb) = (root(&mut parent, e.a), root(&mut parent, e.b));
        parent[ra.max(rb)] = ra.min(rb);
        joined[e.a] = true;
        joined[e.b] = true;
    }
    let mut index = vec![usize::MAX; n];
    let mut parts: Vec<Vec<usize>> = Vec::new();
    for i in (0..n).filter(|&i| joined[i]) {
        let r = root(&mut parent, i);
        if index[r] == usize::MAX {
            index[r] = parts.len();
            parts.push(Vec::new());
        }
        parts[index[r]].push(i);
    }
    // a stable sort: parts of one size stay in the order of their first photo
    parts.sort_by(|p, q| q.len().cmp(&p.len()));
    parts
}

fn root(parent: &mut [usize], mut i: usize) -> usize {
    while parent[i] != i {
        parent[i] = parent[parent[i]];
        i = parent[i];
    }
    i
}

/// The largest of `parts` inside `within` (every part when `None`); parts are inside or outside of
/// it as a whole, the edges dropped being inside it.
fn largest_within(parts: &[Vec<usize>], within: Option<&[usize]>) -> Option<Vec<usize>> {
    parts.iter().find(|p| within.is_none_or(|w| contains(w, p[0]))).cloned()
}

/// The averaged `cam_from_world` rotations of the photos `nodes` (sorted, connected by the edges
/// `inside`), in their order.
fn average_part(nodes: &[usize], edges: &[Edge], inside: &[usize], relative: &[Option<UnitQuaternion<f64>>],
                limit: f64, stop: &AtomicBool) -> Vec<UnitQuaternion<f64>> {
    let local = |i: usize| nodes.binary_search(&i).expect("a photo of the part");
    // (a, b, cam_b_from_cam_a, weight) of each edge, in local indices
    let links: Vec<Link> = inside
        .iter()
        .map(|&k| (local(edges[k].a), local(edges[k].b), relative[k].unwrap(), edges[k].weight))
        .collect();
    let mut rotations = spanning_tree(nodes.len(), &links, &confirmed(nodes.len(), &links, limit));
    let root = root_of(nodes.len(), &links);
    let mut system = match Normal::new(nodes.len(), root, &links) {
        Ok(s) => s,
        Err(_) => return rotations,
    };
    l1_rounds(&mut system, &links, &mut rotations, stop);
    irls(&mut system, &links, &mut rotations, stop);
    if !stop.load(Ordering::Relaxed) && reseat(&links, &mut rotations, limit) > 0 {
        irls(&mut system, &links, &mut rotations, stop);
    }
    rotations
}

/// Moves each photo, in order, to the rotation that most of its links agree on when that is more
/// links than agree with its own: every link predicts the rotation of the photo from the other
/// photo's, and a prediction is supported by the predictions within `limit` radians of it. A photo
/// that the spanning tree reached through a wrong link, and whose right links close no loop (half
/// of its links wrong), can be held there by the averaging, its right links all disagreeing with
/// it; this puts it back where they agree before the disagreeing links are dropped. The number of
/// photos moved.
///
/// The root of the part may be moved too. That changes only the gauge (the rotation of the whole
/// part about the world, which nothing fixes): the next reweighted least squares hold the root
/// where it now is, and the agreement of the links, the only thing read afterwards, does not depend
/// on the gauge.
fn reseat(links: &[Link], rotations: &mut [UnitQuaternion<f64>], limit: f64) -> usize {
    let mut around: Vec<Vec<usize>> = vec![Vec::new(); rotations.len()];
    for (k, &(a, b, _, _)) in links.iter().enumerate() {
        around[a].push(k);
        around[b].push(k);
    }
    let mut moved = 0;
    for i in 0..rotations.len() {
        let predictions: Vec<UnitQuaternion<f64>> = around[i]
            .iter()
            .map(|&k| {
                let (a, b, rab, _) = links[k];
                if a == i {
                    rab.inverse() * rotations[b]
                } else {
                    rab * rotations[a]
                }
            })
            .collect();
        let support = |q: &UnitQuaternion<f64>| predictions.iter().filter(|p| p.angle_to(q) <= limit).count();
        let own = support(&rotations[i]);
        let mut best: Option<(usize, UnitQuaternion<f64>)> = None;
        for p in &predictions {
            let s = support(p);
            if s > own && best.is_none_or(|(b, _)| s > b) {
                best = Some((s, *p));
            }
        }
        if let Some((_, q)) = best {
            rotations[i] = q;
            moved += 1;
        }
    }
    moved
}

/// The photo of most edges, the first of them on a tie.
fn root_of(n: usize, links: &[Link]) -> usize {
    let mut degree = vec![0usize; n];
    for &(a, b, _, _) in links {
        degree[a] += 1;
        degree[b] += 1;
    }
    let mut best = 0;
    for i in 1..n {
        if degree[i] > degree[best] {
            best = i;
        }
    }
    best
}

/// Whether each link closes at least one loop of three photos within `limit` radians: `a-b`, `b-c`
/// and `a-c` (in either direction) whose rotations compose to within `limit` of the identity. A
/// random wrong rotation closes a loop with two right ones only by chance (the share of the
/// rotations within `limit` of a given one, about one in 3600 at 10 degrees), so a confirmed link
/// is right but for a coincidence; a link in no loop, or in none that closes, is not confirmed.
fn confirmed(n: usize, links: &[Link], limit: f64) -> Vec<bool> {
    // the links of each photo, sorted by the other photo: `(other, link)`
    let mut around: Vec<Vec<(usize, usize)>> = vec![Vec::new(); n];
    for (k, &(a, b, _, _)) in links.iter().enumerate() {
        around[a].push((b, k));
        around[b].push((a, k));
    }
    for list in around.iter_mut() {
        list.sort_unstable();
    }
    // `cam_to_from_cam_from` of a link read from photo `from`
    let towards = |k: usize, from: usize| if links[k].0 == from { links[k].2 } else { links[k].2.inverse() };
    links
        .iter()
        .map(|&(a, b, rab, _)| {
            let (mut i, mut j) = (0, 0);
            let (la, lb) = (&around[a], &around[b]);
            while i < la.len() && j < lb.len() {
                let (ca, ka) = la[i];
                let (cb, kb) = lb[j];
                if ca < cb {
                    i += 1;
                } else if cb < ca {
                    j += 1;
                } else {
                    // a -> b -> c against a -> c
                    let through_b = towards(kb, b) * rab;
                    if (towards(ka, a).inverse() * through_b).angle() <= limit {
                        return true;
                    }
                    i += 1;
                    j += 1;
                }
            }
            false
        })
        .collect()
}

/// The rotations chained from the root along the maximum spanning tree of the weights of `links`
/// (Kruskal; the links `confirmed` by a loop first, heavier first within each group, ties in the
/// order of the links; a weight that is not a number last), the root at the identity, breadth
/// first. A tree through a wrong link would start a whole branch of photos wrong by up to 180
/// degrees, which the averaging does not always bring back; a wrong link is in the tree only where
/// no confirmed link joins its branch.
fn spanning_tree(n: usize, links: &[Link], confirmed: &[bool]) -> Vec<UnitQuaternion<f64>> {
    let mut order: Vec<usize> = (0..links.len()).collect();
    let key = |k: usize| if links[k].3.is_nan() { f64::NEG_INFINITY } else { links[k].3 };
    order.sort_by(|&p, &q| confirmed[q].cmp(&confirmed[p]).then(key(q).total_cmp(&key(p))));
    let mut parent: Vec<usize> = (0..n).collect();
    let mut tree: Vec<Vec<usize>> = vec![Vec::new(); n];
    for k in order {
        let (a, b, _, _) = links[k];
        let (ra, rb) = (root(&mut parent, a), root(&mut parent, b));
        if ra != rb {
            parent[ra.max(rb)] = ra.min(rb);
            tree[a].push(k);
            tree[b].push(k);
        }
    }
    let start = root_of(n, links);
    let mut rotations = vec![UnitQuaternion::identity(); n];
    let mut seen = vec![false; n];
    seen[start] = true;
    let mut queue = VecDeque::from([start]);
    while let Some(i) = queue.pop_front() {
        for &k in &tree[i] {
            let (a, b, rab, _) = links[k];
            let (next, q) = if a == i { (b, rab * rotations[a]) } else { (a, rab.inverse() * rotations[b]) };
            if !seen[next] {
                seen[next] = true;
                rotations[next] = q;
                queue.push_back(next);
            }
        }
    }
    rotations
}

/// The rotation errors `phi = log(R_b^T R_ab R_a)` of the links (zero when the rotations agree
/// with the edge), in the frame of the world.
fn errors(links: &[Link], rotations: &[UnitQuaternion<f64>]) -> Vec<Vector3<f64>> {
    links.iter().map(|&(a, b, rab, _)| (rotations[b].inverse() * rab * rotations[a]).scaled_axis()).collect()
}

/// The L1 cost of the errors: the sum of the absolute values of their components.
fn l1_cost(phi: &[Vector3<f64>]) -> f64 {
    phi.iter().map(|p| p.abs().sum()).sum()
}

fn skew(v: &Vector3<f64>) -> Matrix3<f64> {
    Matrix3::new(0.0, -v.z, v.y, v.z, 0.0, -v.x, -v.y, v.x, 0.0)
}

/// The derivatives of the error `log(exp(w_b) E exp(-w_a))` of a link in `w_a` and `w_b` at zero,
/// `phi = log(E)`: `-J_r^-1(phi)` and `J_l^-1(phi)`, the inverse right and left Jacobians of the
/// rotations, `I -+ [phi]/2 + c [phi]^2` with `c = 1/t^2 - (1 + cos t) / (2 t sin t)`, `t = |phi|`
/// (1/12 near zero, 1/pi^2 at pi). Near the identity both are the identity (with the sign), the
/// first-order relation `w_b - w_a = -phi` of Chatterjee and Govindu; with them a large error of the
/// first rotations (a spanning tree through a wrong edge) is corrected along a descent direction.
fn jacobians(phi: &Vector3<f64>) -> (Matrix3<f64>, Matrix3<f64>) {
    let t = phi.norm();
    let c = if t < 1e-4 {
        1.0 / 12.0
    } else if std::f64::consts::PI - t < 1e-9 {
        1.0 / (t * t)
    } else {
        1.0 / (t * t) - (1.0 + t.cos()) / (2.0 * t * t.sin())
    };
    let (k, k2) = (skew(phi), skew(phi) * skew(phi));
    let id = Matrix3::identity();
    (-(id + 0.5 * k + c * k2), id - 0.5 * k + c * k2)
}

/// `R <- R exp(-w)` for every photo (the root's update is zero) and the mean norm of the updates.
fn update(system: &Normal, x: &[f64], rotations: &mut [UnitQuaternion<f64>]) -> f64 {
    let mut total = 0.0;
    for (i, q) in rotations.iter_mut().enumerate() {
        let w = system.of(x, i);
        total += w.norm();
        let moved = *q * UnitQuaternion::from_scaled_axis(-w);
        *q = UnitQuaternion::new_normalize(moved.into_inner());
    }
    total / rotations.len() as f64
}

/// The rotations moved by the update `x` scaled by 1, 1/2, 1/4, ... until their errors cost less
/// than `before` (`cost` of the errors), at most `LINE_SEARCH_HALVINGS` times; the mean norm of the
/// update taken, `None` when none lowers the cost (the rotations are then left as they were).
fn descend(system: &Normal, links: &[Link], x: &[f64], rotations: &mut [UnitQuaternion<f64>], before: f64,
           cost: impl Fn(&[Vector3<f64>]) -> f64) -> Option<f64> {
    let mut scale = 1.0;
    for _ in 0..=LINE_SEARCH_HALVINGS {
        let mut trial = rotations.to_vec();
        let scaled: Vec<f64> = x.iter().map(|v| v * scale).collect();
        let step = update(system, &scaled, &mut trial);
        if cost(&errors(links, &trial)) < before {
            rotations.copy_from_slice(&trial);
            return Some(step);
        }
        scale *= 0.5;
    }
    None
}

/// The rounds of L1 regression: in each the errors are linearised (`jacobians`) and
/// `min |J w + phi|_1` is solved by the alternating direction method of multipliers (Boyd et al.
/// 2011, 6.1: its `x` step the normal equations of `J`, factorised once per round), then the
/// rotations moved along it as far as it lowers the L1 cost of the errors (`descend`).
fn l1_rounds(system: &mut Normal, links: &[Link], rotations: &mut [UnitQuaternion<f64>], stop: &AtomicBool) {
    let m = 3 * links.len();
    let ones = vec![1.0; links.len()];
    let mut iterations = ADMM_FIRST_ITERATIONS;
    for _ in 0..L1_ROUNDS {
        if stop.load(Ordering::Relaxed) {
            return;
        }
        let phi = errors(links, rotations);
        system.linearise(&phi);
        if system.factorise(&ones).is_err() {
            return;
        }
        let b: Vec<f64> = phi.iter().flat_map(|p| [-p.x, -p.y, -p.z]).collect();
        let (mut z, mut u) = (vec![0.0; m], vec![0.0; m]);
        let mut x = vec![0.0; system.unknowns()];
        for _ in 0..iterations {
            if stop.load(Ordering::Relaxed) {
                return;
            }
            let target: Vec<f64> = (0..m).map(|i| b[i] + z[i] - u[i]).collect();
            x = system.solve(&system.transpose_times(&ones, &target));
            let ax = system.times(&x);
            let z_old = z.clone();
            for i in 0..m {
                let hat = ADMM_ALPHA * ax[i] + (1.0 - ADMM_ALPHA) * (z_old[i] + b[i]);
                z[i] = shrink(hat - b[i] + u[i], 1.0 / ADMM_RHO);
                u[i] += hat - z[i] - b[i];
            }
            let r_norm = norm((0..m).map(|i| ax[i] - z[i] - b[i]));
            let dz: Vec<f64> = (0..m).map(|i| ADMM_RHO * (z[i] - z_old[i])).collect();
            let s_norm = norm(system.transpose_times(&ones, &dz).into_iter());
            let rho_u: Vec<f64> = u.iter().map(|v| ADMM_RHO * v).collect();
            let largest = norm(ax.iter().copied()).max(norm(z.iter().copied())).max(norm(b.iter().copied()));
            let eps_pri = (m as f64).sqrt() * ADMM_ABS_TOL + ADMM_REL_TOL * largest;
            let eps_dual = (system.unknowns() as f64).sqrt() * ADMM_ABS_TOL
                + ADMM_REL_TOL * norm(system.transpose_times(&ones, &rho_u).into_iter());
            if r_norm < eps_pri && s_norm < eps_dual {
                break;
            }
        }
        if !x.iter().all(|v| v.is_finite()) {
            return;
        }
        match descend(system, links, &x, rotations, l1_cost(&phi), l1_cost) {
            Some(step) if step >= STEP_CONVERGENCE => {}
            _ => return,
        }
        iterations = (iterations * 2).min(ADMM_MAX_ITERATIONS);
    }
}

/// The Geman-McClure weight `s^2 / (e^2 + s^2)^2` of an error of norm `e` and its cost
/// `e^2 / (e^2 + s^2)` (up to a constant factor), `s2` the square of the scale.
fn geman_mcclure(e2: f64, s2: f64) -> (f64, f64) {
    (s2 / ((e2 + s2) * (e2 + s2)), e2 / (e2 + s2))
}

/// The reweighted least squares with the Geman-McClure weights of the norm of the error of each
/// link, each step taken as far as it lowers the Geman-McClure cost.
fn irls(system: &mut Normal, links: &[Link], rotations: &mut [UnitQuaternion<f64>], stop: &AtomicBool) {
    let s2 = IRLS_SIGMA_DEG.to_radians().powi(2);
    let cost = |phi: &[Vector3<f64>]| phi.iter().map(|p| geman_mcclure(p.norm_squared(), s2).1).sum::<f64>();
    for _ in 0..IRLS_ITERATIONS {
        if stop.load(Ordering::Relaxed) {
            return;
        }
        let phi = errors(links, rotations);
        let weights: Vec<f64> = phi.iter().map(|p| geman_mcclure(p.norm_squared(), s2).0).collect();
        system.linearise(&phi);
        if system.factorise(&weights).is_err() {
            return;
        }
        let b: Vec<f64> = phi.iter().flat_map(|p| [-p.x, -p.y, -p.z]).collect();
        let x = system.solve(&system.transpose_times(&weights, &b));
        if !x.iter().all(|v| v.is_finite()) {
            return;
        }
        match descend(system, links, &x, rotations, cost(&phi), cost) {
            Some(step) if step >= STEP_CONVERGENCE => {}
            _ => return,
        }
    }
}

fn shrink(v: f64, kappa: f64) -> f64 {
    (v - kappa).max(0.0) - (-v - kappa).max(0.0)
}

fn norm(values: impl Iterator<Item = f64>) -> f64 {
    values.map(|v| v * v).sum::<f64>().sqrt()
}

/// The 3n x 3n normal equations `J^T W J` of the linearised errors of the links, the photos of a
/// part without its root (three columns per photo): the pattern (the lower triangle in compressed
/// columns, whole 3 x 3 blocks) and its symbolic factorisation built once, the derivatives of the
/// links at every `linearise`, the values and the numeric factorisation at every `factorise`.
struct Normal {
    /// The photos of each link and the first column of each photo, `None` for the root.
    ends: Vec<(usize, usize)>,
    column: Vec<Option<usize>>,
    /// The derivatives of the error of each link in the update of `a` and of `b` (`jacobians`).
    jac: Vec<(Matrix3<f64>, Matrix3<f64>)>,
    pattern: SymbolicSparseColMat<usize>,
    symbolic: SymbolicCholesky<usize>,
    col_ptr: Vec<usize>,
    row_idx: Vec<usize>,
    values: Vec<f64>,
    factor: Vec<f64>,
    factor_scratch: MemBuffer,
    solve_scratch: MemBuffer,
}

impl Normal {
    fn new(n: usize, root: usize, links: &[Link]) -> Result<Normal, String> {
        let mut column = vec![None; n];
        let mut blocks = 0;
        for (i, c) in column.iter_mut().enumerate() {
            if i != root {
                *c = Some(3 * blocks);
                blocks += 1;
            }
        }
        // the block rows of each block column: its own and those of the photos after it it is linked to
        let mut rows: Vec<Vec<usize>> = (0..blocks).map(|c| vec![c]).collect();
        for &(a, b, _, _) in links {
            if let (Some(ca), Some(cb)) = (column[a], column[b]) {
                let (lo, hi) = ((ca.min(cb)) / 3, (ca.max(cb)) / 3);
                rows[lo].push(hi);
            }
        }
        let mut col_ptr = vec![0usize];
        let mut row_idx = Vec::new();
        for r in rows.iter_mut() {
            r.sort_unstable();
            r.dedup();
            for j in 0..3 {
                // the lower triangle of the diagonal block, then the whole blocks below it
                row_idx.extend((j..3).map(|i| 3 * r[0] + i));
                row_idx.extend(r[1..].iter().flat_map(|&h| (0..3).map(move |i| 3 * h + i)));
                col_ptr.push(row_idx.len());
            }
        }
        let cols = 3 * blocks;
        let pattern = SymbolicSparseColMat::new_checked(cols, cols, col_ptr.clone(), None, row_idx.clone());
        let symbolic = factorize_symbolic_cholesky(
            pattern.as_ref(),
            Side::Lower,
            SymmetricOrdering::Amd,
            CholeskySymbolicParams::default(),
        )
        .map_err(|e| format!("the symbolic factorisation of the rotation system failed: {e:?}"))?;
        let factor_scratch =
            MemBuffer::new(symbolic.factorize_numeric_llt_scratch::<f64>(Par::Seq, Default::default()));
        let solve_scratch = MemBuffer::new(symbolic.solve_in_place_scratch::<f64>(1, Par::Seq));
        let factor = vec![0.0; symbolic.len_val()];
        let entries = row_idx.len();
        Ok(Normal {
            ends: links.iter().map(|&(a, b, _, _)| (a, b)).collect(),
            column,
            jac: vec![(-Matrix3::identity(), Matrix3::identity()); links.len()],
            pattern,
            symbolic,
            col_ptr,
            row_idx,
            values: vec![0.0; entries],
            factor,
            factor_scratch,
            solve_scratch,
        })
    }

    /// The unknowns: three per photo but the root.
    fn unknowns(&self) -> usize {
        self.col_ptr.len() - 1
    }

    /// The update of photo `i` in `x`, zero for the root.
    fn of(&self, x: &[f64], i: usize) -> Vector3<f64> {
        match self.column[i] {
            Some(c) => Vector3::new(x[c], x[c + 1], x[c + 2]),
            None => Vector3::zeros(),
        }
    }

    /// The derivatives of the links at the errors `phi`.
    fn linearise(&mut self, phi: &[Vector3<f64>]) {
        for (j, p) in self.jac.iter_mut().zip(phi) {
            *j = jacobians(p);
        }
    }

    /// Adds `m` to the block of the rows of the photo at column `r` and the columns of the photo at
    /// column `c` (`r >= c`; on the diagonal only its lower triangle).
    fn add(&mut self, r: usize, c: usize, m: &Matrix3<f64>) {
        for j in 0..3 {
            let start = self.col_ptr[c + j];
            let rows = &self.row_idx[start..self.col_ptr[c + j + 1]];
            let low = if r == c { j } else { 0 };
            let first = start + rows.binary_search(&(r + low)).expect("in the pattern");
            for i in low..3 {
                self.values[first + i - low] += m[(i, j)];
            }
        }
    }

    /// The values of `J^T W J` for the link weights `w` (on the three rows of a link alike),
    /// factorised.
    fn factorise(&mut self, w: &[f64]) -> Result<(), String> {
        self.values.fill(0.0);
        for k in 0..self.ends.len() {
            let (a, b) = self.ends[k];
            let (ja, jb) = self.jac[k];
            let (ca, cb) = (self.column[a], self.column[b]);
            if let Some(ca) = ca {
                self.add(ca, ca, &(w[k] * ja.transpose() * ja));
            }
            if let Some(cb) = cb {
                self.add(cb, cb, &(w[k] * jb.transpose() * jb));
            }
            if let (Some(ca), Some(cb)) = (ca, cb) {
                if cb > ca {
                    self.add(cb, ca, &(w[k] * jb.transpose() * ja));
                } else {
                    self.add(ca, cb, &(w[k] * ja.transpose() * jb));
                }
            }
        }
        let matrix = SparseColMatRef::new(self.pattern.as_ref(), &self.values);
        let stack = MemStack::new(&mut self.factor_scratch);
        self.symbolic
            .factorize_numeric_llt(&mut self.factor, matrix, Side::Lower, LltRegularization::default(), Par::Seq,
                                   stack, Default::default())
            .map(|_| ())
            .map_err(|e| format!("the rotation system is not positive definite: {e:?}"))
    }

    /// The solution of the factorised system for `rhs`.
    fn solve(&mut self, rhs: &[f64]) -> Vec<f64> {
        let n = self.unknowns();
        let mut x = rhs.to_vec();
        let llt = faer::sparse::linalg::cholesky::LltRef::new(&self.symbolic, &self.factor);
        let stack = MemStack::new(&mut self.solve_scratch);
        llt.solve_in_place_with_conj(Conj::No, MatMut::from_column_major_slice_mut(&mut x, n, 1), Par::Seq, stack);
        x
    }

    /// `J x`: `J_a x_a + J_b x_b` of every link, three values per link.
    fn times(&self, x: &[f64]) -> Vec<f64> {
        let mut out = Vec::with_capacity(3 * self.ends.len());
        for (&(a, b), (ja, jb)) in self.ends.iter().zip(&self.jac) {
            out.extend_from_slice((ja * self.of(x, a) + jb * self.of(x, b)).as_slice());
        }
        out
    }

    /// `J^T W r` with the weight `w[k]` on the three rows of link `k`.
    fn transpose_times(&self, w: &[f64], r: &[f64]) -> Vec<f64> {
        let mut out = vec![0.0; self.unknowns()];
        for (k, (&(a, b), (ja, jb))) in self.ends.iter().zip(&self.jac).enumerate() {
            let v = w[k] * Vector3::new(r[3 * k], r[3 * k + 1], r[3 * k + 2]);
            for (end, j) in [(a, ja), (b, jb)] {
                if let Some(c) = self.column[end] {
                    let g = j.transpose() * v;
                    for i in 0..3 {
                        out[c + i] += g[i];
                    }
                }
            }
        }
        out
    }
}
