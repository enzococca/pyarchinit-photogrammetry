//! The rotation averaging of the native mapper (`mapper::rotation`) on view graphs built here with
//! known rotations: a ring of photos with noisy relative rotations, the same with a tenth of the
//! pairs replaced by random rotations (on one ring and on twenty), two rings without a pair between
//! them, the edges that cannot be used, and two runs that give the same bits.

mod common;

use common::{rotation_error_deg, Random};
use nalgebra::{Matrix3, Rotation3, UnitQuaternion, Vector3};
use ppg_engine::mapper::graph::{Edge, ViewGraph};
use ppg_engine::mapper::rotation::{average, Rotations};
use ppg_engine::mapper::Options;

fn array(r: &Matrix3<f64>) -> [[f64; 3]; 3] {
    std::array::from_fn(|i| std::array::from_fn(|j| r[(i, j)]))
}

fn matrix(r: &[[f64; 3]; 3]) -> Matrix3<f64> {
    Matrix3::from_fn(|i, j| r[i][j])
}

/// A rotation drawn uniformly (a unit quaternion of four normal values).
fn random_rotation(random: &mut Random) -> Matrix3<f64> {
    let q = nalgebra::Quaternion::new(random.normal(), random.normal(), random.normal(), random.normal());
    UnitQuaternion::from_quaternion(q).to_rotation_matrix().into_inner()
}

/// A rotation of `deg` degrees about an axis drawn uniformly.
fn turn(random: &mut Random, deg: f64) -> Matrix3<f64> {
    let axis = Vector3::new(random.normal(), random.normal(), random.normal()).normalize();
    Rotation3::from_scaled_axis(axis * deg.to_radians()).into_inner()
}

/// A graph of known rotations: `truth[i]` the `cam_from_world` rotation of photo `i`.
struct Truth {
    truth: Vec<Matrix3<f64>>,
    graph: ViewGraph,
    /// The edges replaced by random rotations.
    outliers: Vec<usize>,
}

impl Truth {
    fn relative(&self, a: usize, b: usize) -> Matrix3<f64> {
        self.truth[b] * self.truth[a].transpose()
    }
}

/// An edge `a`-`b` whose rotation is the true `cam_b_from_cam_a` turned by `noise_deg` about a
/// random axis; one edge in four marked without parallax (its rotation is used all the same).
fn edge(truth: &[Matrix3<f64>], a: usize, b: usize, noise_deg: f64, random: &mut Random, k: usize) -> Edge {
    let r = turn(random, noise_deg) * truth[b] * truth[a].transpose();
    Edge {
        pair: k,
        a,
        b,
        rotation: array(&r),
        translation: [1.0, 0.0, 0.0],
        inliers: 100,
        inlier_matches: Vec::new(),
        parallax_deg: if k % 4 == 3 { 0.2 } else { 5.0 },
        parallax: k % 4 != 3,
        weight: random.uniform_in(50.0, 500.0).round(),
    }
}

/// `n` photos (from index `first` among `truth`) on a ring, each joined to its `neighbours`
/// nearest on each side.
fn ring_edges(truth: &[Matrix3<f64>], first: usize, n: usize, neighbours: usize, noise_deg: f64,
              random: &mut Random, edges: &mut Vec<Edge>) {
    for i in 0..n {
        for d in 1..=neighbours {
            let j = (i + d) % n;
            let (a, b) = (first + i.min(j), first + i.max(j));
            let k = edges.len();
            edges.push(edge(truth, a, b, noise_deg, random, k));
        }
    }
}

fn graph_of(edges: Vec<Edge>) -> ViewGraph {
    ViewGraph { edges, left_out: 0, too_few: 0, refused: None }
}

/// 100 photos on a ring, each joined to 3 neighbours on each side, 5 % more edges between random
/// photos, 0.5 degree of noise on every relative rotation; `outlier_share` of the edges then
/// replaced by random rotations.
fn noisy_ring(seed: u64, outlier_share: f64) -> Truth {
    let mut random = Random::new(seed);
    let n = 100;
    let truth: Vec<Matrix3<f64>> = (0..n).map(|_| random_rotation(&mut random)).collect();
    let mut edges = Vec::new();
    ring_edges(&truth, 0, n, 3, 0.5, &mut random, &mut edges);
    let long = (edges.len() as f64 * 0.05).round() as usize;
    let mut added = 0;
    while added < long {
        let (i, j) = (random.below(n), random.below(n));
        let (a, b) = (i.min(j), i.max(j));
        if a == b || edges.iter().any(|e| e.a == a && e.b == b) {
            continue;
        }
        let k = edges.len();
        edges.push(edge(&truth, a, b, 0.5, &mut random, k));
        added += 1;
    }
    let mut outliers = Vec::new();
    let count = (edges.len() as f64 * outlier_share).round() as usize;
    while outliers.len() < count {
        let k = random.below(edges.len());
        if outliers.contains(&k) {
            continue;
        }
        edges[k].rotation = array(&random_rotation(&mut random));
        outliers.push(k);
    }
    outliers.sort_unstable();
    Truth { truth, graph: graph_of(edges), outliers }
}

/// The errors in degrees of the placed photos after the best global rotation (the rotation of the
/// world that takes the estimates closest to the truth: the projection onto the rotations of the
/// sum of `world_from_cam_true * cam_from_world_estimated`).
fn errors_deg(t: &Truth, r: &Rotations) -> Vec<f64> {
    let placed: Vec<(usize, Matrix3<f64>)> =
        r.world_from_cam.iter().enumerate().filter_map(|(i, w)| w.map(|w| (i, matrix(&w).transpose()))).collect();
    let mut sum = Matrix3::zeros();
    for (i, est) in &placed {
        sum += t.truth[*i].transpose() * est;
    }
    let svd = sum.svd(true, true);
    let (u, v_t) = (svd.u.unwrap(), svd.v_t.unwrap());
    let mut d = Matrix3::identity();
    if (u * v_t).determinant() < 0.0 {
        d[(2, 2)] = -1.0;
    }
    let gauge = u * d * v_t;
    let mut errors: Vec<f64> = placed.iter().map(|(i, est)| rotation_error_deg(est, &(t.truth[*i] * gauge))).collect();
    errors.sort_by(|a, b| a.total_cmp(b));
    errors
}

fn median(sorted: &[f64]) -> f64 {
    sorted[sorted.len() / 2]
}

fn options() -> Options {
    Options { max_rotation_error_deg: 10.0, ..Options::default() }
}

#[test]
fn noisy_rotations_are_averaged() {
    let t = noisy_ring(5, 0.0);
    let r = average(&t.graph, 100, &options());
    assert_eq!(r.components, vec![100]);
    assert!(r.world_from_cam.iter().all(|w| w.is_some()));
    let e = errors_deg(&t, &r);
    let (med, max) = (median(&e), *e.last().unwrap());
    println!("median {med:.4} deg, largest {max:.4} deg, {} dropped", r.dropped.len());
    assert!(med < 0.3, "median {med} deg");
    assert!(max < 1.0, "largest {max} deg");
    for w in r.world_from_cam.iter().flatten() {
        let m = matrix(w);
        assert!((m * m.transpose() - Matrix3::identity()).amax() < 1e-9);
        assert!((m.determinant() - 1.0).abs() < 1e-9);
    }
}

#[test]
fn outliers_are_dropped() {
    let t = noisy_ring(17, 0.1);
    let r = average(&t.graph, 100, &options());
    assert_eq!(r.components, vec![100]);
    let mut wrong = 0;
    for &k in &t.outliers {
        let e = &t.graph.edges[k];
        if rotation_error_deg(&matrix(&e.rotation), &t.relative(e.a, e.b)) > 10.0 {
            wrong += 1;
            assert!(r.dropped.contains(&k), "edge {k} ({}-{}) kept", e.a, e.b);
        }
    }
    assert!(wrong > 20, "{wrong} wrong edges");
    let e = errors_deg(&t, &r);
    let (med, max) = (median(&e), *e.last().unwrap());
    println!("median {med:.4} deg, largest {max:.4} deg, {} dropped of {} wrong", r.dropped.len(), wrong);
    assert!(med < 0.3, "median {med} deg");
    assert!(max < 1.0, "largest {max} deg");
}

/// Twenty rings with a tenth of their edges wrong and the weights of the edges drawn without regard
/// to it, so that the spanning tree of the weights alone would go through several wrong edges:
/// among them rings where a branch of photos was started wrong by a hundred degrees and more, and
/// photos with half of their edges wrong (seeds 7, 10, 16).
#[test]
fn wrong_edges_in_the_spanning_tree_are_overcome() {
    let mut medians = Vec::new();
    for seed in 1..=20 {
        let t = noisy_ring(seed, 0.1);
        let r = average(&t.graph, 100, &options());
        assert_eq!(r.components, vec![100], "seed {seed}");
        for &k in &t.outliers {
            let e = &t.graph.edges[k];
            if rotation_error_deg(&matrix(&e.rotation), &t.relative(e.a, e.b)) > 10.0 {
                assert!(r.dropped.contains(&k), "seed {seed}: edge {k} ({}-{}) kept", e.a, e.b);
            }
        }
        let e = errors_deg(&t, &r);
        let (med, max) = (median(&e), *e.last().unwrap());
        assert!(med < 0.4 && max < 1.0, "seed {seed}: median {med} deg, largest {max} deg");
        medians.push(med);
    }
    medians.sort_by(|a, b| a.total_cmp(b));
    println!("medians from {:.4} to {:.4} deg", medians[0], medians[19]);
}

#[test]
fn two_parts_give_two_components() {
    let mut random = Random::new(29);
    let truth: Vec<Matrix3<f64>> = (0..60).map(|_| random_rotation(&mut random)).collect();
    let mut edges = Vec::new();
    ring_edges(&truth, 0, 30, 3, 0.5, &mut random, &mut edges);
    ring_edges(&truth, 30, 30, 3, 0.5, &mut random, &mut edges);
    let t = Truth { truth: truth.clone(), graph: graph_of(edges.clone()), outliers: Vec::new() };
    let r = average(&t.graph, 60, &options());
    assert_eq!(r.components, vec![30, 30]);
    // two parts of one size: the part of the first photo is placed, the other is not
    assert!(r.world_from_cam[..30].iter().all(|w| w.is_some()));
    assert!(r.world_from_cam[30..].iter().all(|w| w.is_none()));
    // two rings of 30 without long edges hold their rotations less tightly than the ring of 100
    let med = median(&errors_deg(&t, &r));
    println!("two parts: median {med:.4} deg");
    assert!(med < 0.5, "median {med} deg");

    let k = edges.len();
    edges.push(edge(&truth, 7, 41, 0.5, &mut random, k));
    let t = Truth { truth, graph: graph_of(edges), outliers: Vec::new() };
    let r = average(&t.graph, 60, &options());
    assert_eq!(r.components, vec![60]);
    assert!(r.world_from_cam.iter().all(|w| w.is_some()));
    let med = median(&errors_deg(&t, &r));
    println!("joined: median {med:.4} deg");
    assert!(med < 0.5, "median {med} deg");
}

#[test]
fn nothing_is_placed_from_a_refused_or_empty_graph() {
    let empty = graph_of(Vec::new());
    let r = average(&empty, 5, &options());
    assert_eq!(r.world_from_cam, vec![None; 5]);
    assert!(r.components.is_empty() && r.dropped.is_empty());
    let mut refused = noisy_ring(3, 0.0).graph;
    refused.refused = Some("2 cameras given, 1 in the input".into());
    let r = average(&refused, 100, &options());
    assert_eq!(r.world_from_cam, vec![None; 100]);
    assert!(r.components.is_empty());
}

#[test]
fn edges_that_cannot_be_used_are_dropped() {
    let mut t = noisy_ring(3, 0.0);
    let n = t.graph.edges.len();
    let mut bad = t.graph.edges[0].clone();
    bad.b = 100;
    t.graph.edges.push(bad);
    let mut bad = t.graph.edges[1].clone();
    bad.b = bad.a;
    t.graph.edges.push(bad);
    let mut bad = t.graph.edges[2].clone();
    bad.rotation[0][0] = f64::NAN;
    t.graph.edges.push(bad);
    let mut bad = t.graph.edges[3].clone();
    bad.rotation = [[2.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]];
    t.graph.edges.push(bad);
    let r = average(&t.graph, 100, &options());
    assert_eq!(r.dropped, vec![n, n + 1, n + 2, n + 3]);
    assert_eq!(r.components, vec![100]);
    assert!(median(&errors_deg(&t, &r)) < 0.3);
}

#[test]
fn the_same_bits_twice() {
    let t = noisy_ring(41, 0.1);
    let a = average(&t.graph, 100, &options());
    let b = average(&t.graph, 100, &options());
    let bits = |r: &Rotations| -> Vec<u64> {
        r.world_from_cam.iter().flatten().flat_map(|w| w.iter().flatten().map(|x| x.to_bits())).collect()
    };
    assert_eq!(bits(&a), bits(&b));
    assert_eq!(a.dropped, b.dropped);
    assert_eq!(a.components, b.components);
}
