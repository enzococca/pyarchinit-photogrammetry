//! The `Mapper`: the stages of the native mapper run in their order on one input, each with its
//! `observe` (called between its steps and inside its solves; `false` stops it with
//! `Err("cancelled")`), the block kept between them, the report of every stage and the model
//! written in COLMAP's binary format.

use std::io;
use std::path::Path;

use super::graph::{calibrate_focals, FocalEstimate, ViewGraph};
use super::input::Input;
use super::model_io::{quaternion, write_colmap_binary, ModelCamera, ModelImage, ModelPoint, NO_POINT};
use super::positioning::{self, Frame, Gnss};
use super::rotation::{self, Rotations};
use super::rounds::{self, CameraState, RoundReport, State};
use super::tracks::{self, Tracks};
use super::Options;
use crate::camera::{Camera, Model};

/// The stages in their order.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Stage {
    New,
    ViewGraph,
    Rotations,
    Tracks,
    Position,
    Refine,
}

/// The names of the stages after `New`, as the errors give them.
fn name(stage: Stage) -> &'static str {
    match stage {
        Stage::New => "new",
        Stage::ViewGraph => "view_graph",
        Stage::Rotations => "rotations",
        Stage::Tracks => "tracks",
        Stage::Position => "position",
        Stage::Refine => "refine",
    }
}

/// What the stages did: the photos of the input; the edges of the view graph and the pairs left out
/// or with too few matches, the focal lengths calibrated; the photos the rotations placed, the
/// edges they dropped and the sizes of the connected parts; the tracks, those selected and those
/// dropped for a conflict; the frame of the positioning, the rotation that takes the averaged
/// rotations into it (`Positioned::frame_rotation`), its residual, the photos whose position is
/// an outlier; after the rounds the photos placed, the points and observations of the model, the
/// share of the observations of the triangulated tracks the last filter kept, the median
/// reprojection error in pixels, the accuracy of the positions estimated again, the bias of each
/// flight and the posterior standard deviation of each axis of the biases that were adjusted (the
/// reference flight has none) with the variance factors of the groups it used (tie points,
/// horizontal and vertical positions), the reports of the rounds and their notes.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct MapperReport {
    pub photos: usize,
    pub edges: usize,
    pub pairs_left_out: usize,
    pub pairs_too_few: usize,
    pub focals: Vec<FocalEstimate>,
    pub rotations_placed: usize,
    pub edges_dropped: usize,
    pub components: Vec<usize>,
    pub tracks: usize,
    pub selected: usize,
    pub dropped_conflicts: usize,
    pub frame: Option<Frame>,
    pub frame_rotation: Option<[[f64; 3]; 3]>,
    pub frame_residual_deg: Option<f64>,
    pub gnss_outliers: Vec<usize>,
    pub placed: usize,
    pub points: usize,
    pub observations: usize,
    pub tracks_kept_share: f64,
    pub median_reprojection_px: f64,
    pub sigma_h: Option<f64>,
    pub sigma_v: Option<f64>,
    pub bias: Vec<(i64, [f64; 3])>,
    pub bias_sd: Vec<(i64, [f64; 3])>,
    pub variance_factors: Option<[f64; 3]>,
    pub rounds: Vec<RoundReport>,
    pub notes: Vec<String>,
}

/// The native mapper on one input (see the module of `mapper`): `view_graph`, `rotations`,
/// `tracks`, `position`, `refine` in this order, each once, then `write_model`. A stage called out
/// of its order is an `Err` naming the stage it needs; a stage that fails or is cancelled can be
/// called again but for `refine`, after which the mapper only reports.
///
/// `view_graph`, `rotations` and `tracks` call `observe` only between their steps (the relative
/// poses of all the pairs, the averaging and the union-find run whole); `position` and `refine`
/// also call it after every iteration of their solves. The bindings (Task 8), which must give
/// progress at most every 0.5 s, need it inside those steps too.
pub struct Mapper {
    input: Input,
    o: Options,
    cams: Vec<Camera>,
    stage: Stage,
    stopped: Option<String>,
    focals: Vec<FocalEstimate>,
    graph: Option<ViewGraph>,
    rotations: Option<Rotations>,
    tracks: Option<(Tracks, Vec<usize>)>,
    state: Option<State>,
    report: MapperReport,
}

fn cancelled() -> String {
    "cancelled".into()
}

impl Mapper {
    /// `Err` when the input cannot be used (`Input::check`), a camera is of a model the mapper does
    /// not know, `o.opencv_sensor` is neither empty nor one flag per camera, or `o.rounds` is 0.
    pub fn new(input: Input, o: Options) -> Result<Mapper, String> {
        input.check()?;
        let cams = input
            .cameras
            .iter()
            .map(|c| Camera::new(&c.model, &c.params, c.width, c.height))
            .collect::<Result<Vec<_>, _>>()?;
        if !o.opencv_sensor.is_empty() && o.opencv_sensor.len() != input.cameras.len() {
            return Err(format!("{} OPENCV flags for {} cameras", o.opencv_sensor.len(), input.cameras.len()));
        }
        if o.rounds == 0 {
            return Err("rounds must be 1 or more".into());
        }
        let report = MapperReport { photos: input.images.len(), ..MapperReport::default() };
        Ok(Mapper {
            input,
            o,
            cams,
            stage: Stage::New,
            stopped: None,
            focals: Vec::new(),
            graph: None,
            rotations: None,
            tracks: None,
            state: None,
            report,
        })
    }

    /// `Ok` when `stage` can run now: the stage before it has run and it has not.
    fn ready(&self, stage: Stage) -> Result<(), String> {
        if let Some(why) = &self.stopped {
            return Err(format!("the mapper stopped in refine ({why}): {} cannot run", name(stage)));
        }
        if self.stage >= stage {
            return Err(format!("{} has run already", name(stage)));
        }
        let needed = match stage {
            Stage::New | Stage::ViewGraph => Stage::New,
            Stage::Rotations => Stage::ViewGraph,
            Stage::Tracks => Stage::Rotations,
            Stage::Position => Stage::Tracks,
            Stage::Refine => Stage::Position,
        };
        if self.stage != needed {
            let next = match self.stage {
                Stage::New => Stage::ViewGraph,
                Stage::ViewGraph => Stage::Rotations,
                Stage::Rotations => Stage::Tracks,
                Stage::Tracks => Stage::Position,
                Stage::Position | Stage::Refine => Stage::Refine,
            };
            return Err(format!("{} needs {} to have run first (next: {})", name(stage), name(needed), name(next)));
        }
        Ok(())
    }

    pub fn input(&self) -> &Input {
        &self.input
    }

    /// The focal lengths that were guessed calibrated from the fundamental matrices, then the
    /// relative pose of every pair (`ViewGraph::build`).
    pub fn view_graph(&mut self, observe: &mut dyn FnMut() -> bool) -> Result<&ViewGraph, String> {
        self.ready(Stage::ViewGraph)?;
        if !observe() {
            return Err(cancelled());
        }
        let mut cams = self.cams.clone();
        let focals = calibrate_focals(&self.input, &mut cams);
        if !observe() {
            return Err(cancelled());
        }
        let graph = ViewGraph::build(&self.input, &cams, &self.o);
        if let Some(reason) = &graph.refused {
            return Err(reason.clone());
        }
        if !observe() {
            return Err(cancelled());
        }
        self.cams = cams;
        self.report.edges = graph.edges.len();
        self.report.pairs_left_out = graph.left_out;
        self.report.pairs_too_few = graph.too_few;
        self.report.focals.clone_from(&focals);
        self.focals = focals;
        self.stage = Stage::ViewGraph;
        Ok(self.graph.insert(graph))
    }

    /// The view graph, once built.
    pub fn graph(&self) -> Option<&ViewGraph> {
        self.graph.as_ref()
    }

    /// The view graph replaced by `graph` (the tests turn its rotations), between `view_graph` and
    /// `rotations` only.
    pub fn set_view_graph(&mut self, graph: ViewGraph) -> Result<(), String> {
        if self.stage != Stage::ViewGraph || self.stopped.is_some() {
            return Err("the view graph is replaced after view_graph and before rotations only".into());
        }
        self.report.edges = graph.edges.len();
        self.graph = Some(graph);
        Ok(())
    }

    /// The rotations averaged from the view graph (`rotation::average`).
    pub fn rotations(&mut self, observe: &mut dyn FnMut() -> bool) -> Result<&Rotations, String> {
        self.ready(Stage::Rotations)?;
        if !observe() {
            return Err(cancelled());
        }
        let graph = self.graph.as_ref().expect("a view graph");
        let rotations = rotation::average(graph, self.input.images.len(), &self.o);
        if let Some(reason) = &rotations.refused {
            return Err(reason.clone());
        }
        if !observe() {
            return Err(cancelled());
        }
        self.report.rotations_placed = rotations.placed;
        self.report.edges_dropped = rotations.dropped.len();
        self.report.components.clone_from(&rotations.components);
        self.stage = Stage::Rotations;
        Ok(self.rotations.insert(rotations))
    }

    /// The tracks of the photos the rotations placed and, per photo, the `per_photo` longest
    /// spread over it (`tracks::build`, `tracks::select`).
    pub fn tracks(&mut self, per_photo: usize, observe: &mut dyn FnMut() -> bool) -> Result<&Tracks, String> {
        self.ready(Stage::Tracks)?;
        if !observe() {
            return Err(cancelled());
        }
        let rotations = self.rotations.as_ref().expect("rotations");
        let registered: Vec<bool> = rotations.world_from_cam.iter().map(Option::is_some).collect();
        let t = tracks::build(&self.input, &registered);
        if let Some(reason) = &t.refused {
            return Err(reason.clone());
        }
        let selected = tracks::select(&t, &self.input, per_photo)?;
        if !observe() {
            return Err(cancelled());
        }
        self.report.tracks = t.len();
        self.report.selected = selected.len();
        self.report.dropped_conflicts = t.dropped_conflicts;
        self.stage = Stage::Tracks;
        Ok(&self.tracks.insert((t, selected)).0)
    }

    /// The global positioning with the positions `gnss` (`positioning::position`), and the block the
    /// rounds start from.
    pub fn position(&mut self, gnss: Option<&Gnss>, observe: &mut dyn FnMut() -> bool) -> Result<(), String> {
        self.ready(Stage::Position)?;
        let (graph, rotations) = (self.graph.as_ref().expect("graph"), self.rotations.as_ref().expect("rotations"));
        let (t, selected) = self.tracks.as_ref().expect("tracks");
        let p = positioning::position(&self.input, &self.cams, graph, rotations, t, selected, gnss, &self.o,
                                      observe)?;
        let n = self.input.images.len();
        let q = nalgebra::Matrix3::from_fn(|i, j| p.frame_rotation[i][j]);
        let mut world_from_cam = vec![None; n];
        let mut centres = vec![None; n];
        for i in 0..n {
            if let (Some(w), Some(c)) = (rotations.world_from_cam[i], p.centres[i]) {
                let w = q * nalgebra::Matrix3::from_fn(|a, b| w[a][b]);
                world_from_cam[i] = Some(std::array::from_fn(|a| std::array::from_fn(|b| w[(a, b)])));
                centres[i] = Some(c);
            }
        }
        let mut points = vec![None; t.len()];
        for (slot, &track) in selected.iter().enumerate() {
            points[track] = p.points[slot];
        }
        let cameras = self
            .input
            .cameras
            .iter()
            .zip(&self.cams)
            .map(|(c, cam)| {
                let (model, _) = Model::from_name(&c.model).expect("a checked model");
                let mut params = c.params.clone();
                // the calibration of the focal lengths changes them alone
                params[0] = cam.fx;
                if !matches!(model, Model::SimplePinhole | Model::SimpleRadial | Model::Radial) {
                    params[1] = cam.fy;
                }
                CameraState { model, params, width: c.width, height: c.height }
            })
            .collect();
        let state = State {
            world_from_cam,
            centres,
            cameras,
            opencv_sensor: self.o.opencv_sensor.clone(),
            tracks: t.clone(),
            selected: selected.clone(),
            points,
            keep: vec![false; t.observations.len()],
            frame: p.frame,
            gnss: gnss.cloned(),
            gnss_outliers: p.gnss_outliers.clone(),
            gnss_sigma: gnss.map(|g| g.sigma.clone()).unwrap_or_default(),
            sigma_estimate: None,
            bias: p.bias.clone(),
            bias_covariance: Vec::new(),
            variance_factors: None,
            last_filter: (0, 0),
            notes: Vec::new(),
        };
        self.report.frame = Some(p.frame);
        self.report.frame_rotation = Some(p.frame_rotation);
        self.report.frame_residual_deg = p.frame_residual_deg;
        self.report.gnss_outliers.clone_from(&p.gnss_outliers);
        self.report.bias.clone_from(&p.bias);
        self.report.placed = (0..n).filter(|&i| state.placed(i)).count();
        self.state = Some(state);
        self.stage = Stage::Position;
        Ok(())
    }

    /// The rounds of adjustment (`rounds::refine`). A failure or a cancel leaves the mapper stopped:
    /// it reports, it writes nothing.
    pub fn refine(&mut self, observe: &mut dyn FnMut() -> bool) -> Result<&[RoundReport], String> {
        self.ready(Stage::Refine)?;
        let mut state = self.state.take().expect("a positioned block");
        match rounds::refine(&mut state, &self.input, &self.o, observe) {
            Ok(reports) => {
                let r = &mut self.report;
                let n = self.input.images.len();
                r.placed = (0..n).filter(|&i| state.placed(i)).count();
                r.points = (0..state.tracks.len()).filter(|&t| written(&state, t)).count();
                r.observations = state.keep.iter().filter(|&&k| k).count();
                let (seen, kept) = state.last_filter;
                r.tracks_kept_share = if seen > 0 { kept as f64 / seen as f64 } else { 0.0 };
                let mut errors = state.reprojection_px(&self.input);
                r.median_reprojection_px = median(&mut errors);
                r.sigma_h = state.sigma_estimate.as_ref().map(|e| e.sigma_h);
                r.sigma_v = state.sigma_estimate.as_ref().map(|e| e.sigma_v);
                r.bias.clone_from(&state.bias);
                let sd = |c: &[[f64; 3]; 3]| std::array::from_fn(|a| c[a][a].sqrt());
                r.bias_sd = state.bias_covariance.iter().map(|(f, c)| (*f, sd(c))).collect();
                r.variance_factors = state.variance_factors;
                r.rounds = reports;
                r.notes.clone_from(&state.notes);
                self.state = Some(state);
                self.stage = Stage::Refine;
                Ok(&self.report.rounds)
            }
            Err(why) => {
                self.stopped = Some(why.clone());
                Err(why)
            }
        }
    }

    pub fn report(&self) -> MapperReport {
        self.report.clone()
    }

    /// The centre of each photo in the frame of the positions (`None` for a photo not placed, and
    /// for all of them before the positioning or after a refine that stopped).
    pub fn centres(&self) -> Vec<Option<[f64; 3]>> {
        match &self.state {
            Some(s) => s.centres.clone(),
            None => vec![None; self.input.images.len()],
        }
    }

    /// The block after the positioning or the rounds.
    pub fn state(&self) -> Option<&State> {
        self.state.as_ref()
    }

    /// The model written into the folder `dir` (which exists) in COLMAP's binary format
    /// (`model_io::write_colmap_binary`): every camera of the input with its adjusted parameters,
    /// every placed photo with all its key points, every track with a point and two kept
    /// observations or more as a point 3D (ids from 1 in the order of the tracks, the mean
    /// reprojection error in pixels, no colour). The ids of cameras and photos are those of the
    /// input. The number of photos written; `Err` before `refine` has run.
    pub fn write_model(&self, dir: &Path) -> io::Result<usize> {
        let state = match (&self.stopped, self.stage, &self.state) {
            (None, Stage::Refine, Some(state)) => state,
            _ => return Err(io::Error::other("write_model needs refine to have run first")),
        };
        let input = &self.input;
        let cameras: Vec<ModelCamera> = input
            .cameras
            .iter()
            .zip(&state.cameras)
            .map(|(c, s)| ModelCamera {
                camera_id: c.camera_id,
                model: s.model,
                width: c.width.round() as u64,
                height: c.height.round() as u64,
                params: s.params.clone(),
            })
            .collect();
        let mut point_of: Vec<Vec<u64>> = input.images.iter().map(|im| vec![NO_POINT; im.keypoints.len()]).collect();
        let poses = state.poses();
        let mut points = Vec::new();
        for t in 0..state.tracks.len() {
            if !written(state, t) {
                continue;
            }
            let id = points.len() as u64 + 1;
            let x = state.points[t].expect("a written track has a point");
            let (mut track, mut error) = (Vec::new(), 0.0);
            for i in state.tracks.offsets[t]..state.tracks.offsets[t + 1] {
                if !state.keep[i] {
                    continue;
                }
                let (image, k) = state.tracks.observations[i];
                point_of[image as usize][k as usize] = id;
                track.push((input.images[image as usize].image_id, k));
                error += state.error_px(input, &poses, image as usize, k, &x).unwrap_or(0.0);
            }
            error /= track.len() as f64;
            points.push(ModelPoint { point_id: id, xyz: x, color: [0; 3], error, track });
        }
        let mut images = Vec::new();
        for (i, image) in input.images.iter().enumerate() {
            let Some(pose) = &poses[i] else { continue };
            let r: [[f64; 3]; 3] = std::array::from_fn(|a| std::array::from_fn(|b| pose[a][b]));
            let points2d = image
                .keypoints
                .clone()
                .zip(&point_of[i])
                .map(|(k, &id)| ([input.keypoints[k][0] as f64, input.keypoints[k][1] as f64], id))
                .collect();
            images.push(ModelImage {
                image_id: image.image_id,
                camera_id: input.cameras[image.camera].camera_id,
                name: image.name.clone(),
                rotation: quaternion(&r),
                translation: [pose[0][3], pose[1][3], pose[2][3]],
                points2d,
            });
        }
        write_colmap_binary(dir, &cameras, &images, &points)?;
        Ok(images.len())
    }
}

/// Whether the track `t` goes into the model: a point and two kept observations or more.
fn written(state: &State, t: usize) -> bool {
    let range = state.tracks.offsets[t]..state.tracks.offsets[t + 1];
    state.points[t].is_some() && range.filter(|&i| state.keep[i]).count() >= 2
}

fn median(values: &mut [f64]) -> f64 {
    values.sort_by(|a, b| a.total_cmp(b));
    let n = values.len();
    if n == 0 {
        f64::NAN
    } else if n % 2 == 1 {
        values[n / 2]
    } else {
        0.5 * (values[n / 2 - 1] + values[n / 2])
    }
}
