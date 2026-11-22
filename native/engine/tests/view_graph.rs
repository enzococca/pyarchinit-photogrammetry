//! The input of the native mapper and its view graph (`mapper::input`, `mapper::graph`) on the
//! synthetic scenes of `common`: the relative poses re-estimated from the inliers of each pair,
//! the pairs without parallax, the focal length of a camera found from the fundamental matrices,
//! the checks of the input, and two runs that give the same bits.

mod common;

use common::{angle_deg, rotation_error_deg, scene, InputOptions, Kind, Scene};
use nalgebra::{Matrix3, Vector3};
use ppg_engine::camera::Camera;
use ppg_engine::mapper::graph::{calibrate_focals, ViewGraph};
use ppg_engine::mapper::input::Input;
use ppg_engine::mapper::Options;

fn cameras_of(input: &Input) -> Vec<Camera> {
    input.cameras.iter().map(|c| Camera::new(&c.model, &c.params, c.width, c.height).unwrap()).collect()
}

fn matrix(r: &[[f64; 3]; 3]) -> Matrix3<f64> {
    Matrix3::from_fn(|i, j| r[i][j])
}

#[test]
fn relative_poses_are_recovered() {
    let s = scene(12, Kind::Strip, 7);
    let input = s.input(&InputOptions { noise_px: 0.3, outlier_share: 0.2, seed: 11, ..Default::default() });
    input.check().unwrap();
    let cams = cameras_of(&input);
    let graph = ViewGraph::build(&input, &cams, &Options::default());
    assert_eq!(graph.left_out, 0);
    assert_eq!(graph.edges.len(), input.pairs.len());
    assert!(graph.edges.len() >= 30, "{} pairs", graph.edges.len());
    let (mut worst_r, mut worst_t) = (0.0f64, 0.0f64);
    for e in &graph.edges {
        let pair = &input.pairs[e.pair];
        assert_eq!((e.a, e.b), (pair.a, pair.b));
        let (r, t) = s.relative(e.a, e.b);
        let dr = rotation_error_deg(&matrix(&e.rotation), &r);
        let dt = angle_deg(&Vector3::from(e.translation), &t);
        worst_r = worst_r.max(dr);
        worst_t = worst_t.max(dt);
        // the wrong matches (20 % of the matches) are not among the inliers, but for the few that
        // fell by chance within the threshold of their epipolar line, which no two-view model
        // can tell (about one in a few hundred), and the right ones are
        let n = pair.matches.len();
        let right = n - (0.2 * n as f64).round() as usize;
        assert!(e.inliers <= right + (n / 100).max(2), "{} inliers of {n}, {right} right", e.inliers);
        assert!(e.inliers as f64 >= 0.98 * right as f64, "{} inliers of {n}, {right} right", e.inliers);
        assert_eq!(e.inlier_matches.len(), e.inliers);
        assert_eq!(e.weight, e.inliers as f64);
        assert!(e.parallax, "pair {} {}: median angle {}", e.a, e.b, e.parallax_deg);
    }
    eprintln!("worst rotation error {worst_r:.4} deg, worst translation direction {worst_t:.4} deg");
    assert!(worst_r < 0.1, "rotation error {worst_r} deg");
    assert!(worst_t < 1.0, "translation direction error {worst_t} deg");
}

/// Two `ViewGraph`s with the same bits in every number.
fn same_bits(g: &ViewGraph, h: &ViewGraph) -> bool {
    let bits = |v: &[f64]| v.iter().map(|x| x.to_bits()).collect::<Vec<_>>();
    g.left_out == h.left_out
        && g.edges.len() == h.edges.len()
        && g.edges.iter().zip(&h.edges).all(|(a, b)| {
            (a.pair, a.a, a.b, a.inliers, a.parallax, &a.inlier_matches)
                == (b.pair, b.a, b.b, b.inliers, b.parallax, &b.inlier_matches)
                && bits(a.rotation.as_flattened()) == bits(b.rotation.as_flattened())
                && bits(&a.translation) == bits(&b.translation)
                && a.parallax_deg.to_bits() == b.parallax_deg.to_bits()
                && a.weight.to_bits() == b.weight.to_bits()
        })
}

#[test]
fn two_runs_give_the_same_graph() {
    let s = scene(12, Kind::Strip, 3);
    let input = s.input(&InputOptions { noise_px: 0.5, outlier_share: 0.3, seed: 5, ..Default::default() });
    let cams = cameras_of(&input);
    let first = ViewGraph::build(&input, &cams, &Options::default());
    let second = ViewGraph::build(&input, &cams, &Options::default());
    assert!(!first.edges.is_empty());
    assert!(same_bits(&first, &second));
}

#[test]
fn a_rotation_only_pair_has_no_parallax() {
    let s = scene(2, Kind::Rotation, 9);
    let input = s.input(&InputOptions { noise_px: 0.3, seed: 2, ..Default::default() });
    assert_eq!(input.pairs.len(), 1);
    let cams = cameras_of(&input);
    let graph = ViewGraph::build(&input, &cams, &Options::default());
    assert_eq!(graph.edges.len(), 1, "left out: {}", graph.left_out);
    let e = &graph.edges[0];
    assert!(!e.parallax, "median angle {} deg", e.parallax_deg);
    assert!(e.parallax_deg < 0.1, "median angle {} deg", e.parallax_deg);
    // the rotation of a pair without baseline is still right
    let (r, _) = s.relative(0, 1);
    assert!(rotation_error_deg(&matrix(&e.rotation), &r) < 0.1);
}

#[test]
fn a_guessed_focal_is_found_from_the_fundamental_matrices() {
    let s = scene(12, Kind::Strip, 21);
    let guessed =
        InputOptions { start_focal: 1400.0, prior_focal: false, with_f: true, seed: 4, ..Default::default() };
    let input = s.input(&guessed);
    input.check().unwrap();
    assert!(input.pairs.len() >= 10);
    let mut cams = cameras_of(&input);
    let estimates = calibrate_focals(&input, &mut cams);
    assert_eq!(estimates.len(), 1);
    let e = &estimates[0];
    assert_eq!((e.camera, e.before, e.pairs), (0, 1400.0, input.pairs.len()));
    eprintln!("focal {} -> {}", e.before, e.after);
    assert!((e.after - 1000.0).abs() < 10.0, "focal {}", e.after);
    assert_eq!(cams[0].focal(), e.after);
    assert_eq!((cams[0].fx, cams[0].fy), (e.after, e.after));

    // with a prior focal length nothing moves
    let input = s.input(&InputOptions { prior_focal: true, ..guessed.clone() });
    let mut cams = cameras_of(&input);
    let estimates = calibrate_focals(&input, &mut cams);
    assert!(estimates.is_empty());
    assert_eq!(cams[0].focal(), 1400.0);

    // too few pairs: unchanged
    let few = scene(4, Kind::Strip, 21);
    let input = few.input(&guessed);
    assert!(input.pairs.len() < 10);
    let mut cams = cameras_of(&input);
    let estimates = calibrate_focals(&input, &mut cams);
    assert_eq!(estimates.len(), 1);
    assert_eq!((estimates[0].before, estimates[0].after), (1400.0, 1400.0));
    assert_eq!(cams[0].focal(), 1400.0);
}

#[test]
fn a_bad_input_is_refused() {
    let s = scene(4, Kind::Strip, 1);
    let good = s.input(&InputOptions::default());
    good.check().unwrap();

    let mut bad = good.clone();
    bad.pairs[0].b = 9;
    let err = bad.check().unwrap_err();
    assert!(err.contains("pair 0") && err.contains("image 9"), "{err}");

    let mut bad = good.clone();
    let first = bad.pairs[0].matches.start;
    let a = bad.pairs[0].a;
    let n = bad.images[a].keypoints.len() as u32;
    bad.matches[first][0] = n;
    let err = bad.check().unwrap_err();
    assert!(err.contains("pair 0") && err.contains(&format!("key point {n}")), "{err}");

    let mut bad = good.clone();
    let k = bad.images[1].keypoints.start + 3;
    bad.keypoints[k][1] = f32::NAN;
    let err = bad.check().unwrap_err();
    assert!(err.contains(&format!("key point {k}")) && err.contains(&Scene::name(1)), "{err}");

    let mut bad = good.clone();
    bad.cameras[0].model = "FISHEYE".to_string();
    assert!(bad.check().unwrap_err().contains("FISHEYE"));

    let mut bad = good.clone();
    bad.pairs[0].matches = 0..bad.matches.len() + 1;
    assert!(bad.check().unwrap_err().contains("pair 0"));

    let mut bad = good;
    bad.images[0].camera = 3;
    assert!(bad.check().unwrap_err().contains("camera 3"));
}
