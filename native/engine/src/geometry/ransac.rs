//! Robust estimation of a model on correspondences: LO-RANSAC (Chum, Matas and Kittler 2003) with
//! PROSAC sampling (Chum and Matas 2005), arranged as COLMAP's `LORANSAC` (optim/loransac.h,
//! optim/ransac.h, optim/support_measurement.h; BSD 3-clause, see native/NOTICE) so that the
//! trials, the support and the stopping rule are those of the classic engine. The sampler is
//! written from the paper: the correspondences come ordered by quality, best first, and the
//! samples are drawn from a set of the best that grows with the trials, so that a good model is
//! found in a few trials on matches whose ratio test ranks them well.
//!
//! The generator is a 64-bit linear congruential one written here (no `rand` crate), seeded by
//! the caller: the same seed on the same input gives the same model, the same inliers and the
//! same number of trials on every computer and in every thread.

/// The options of a robust estimation: the fields of COLMAP's `RANSACOptions` the estimation
/// reads, with the defaults of `pycolmap.RANSACOptions()` of the installed pycolmap (4.2.1).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Options {
    /// The largest residual (not squared) of an inlier, in the units of the correspondences
    /// (pixels, or normalised coordinates for an essential matrix).
    pub max_error: f64,
    /// The probability that at least one sample is free of outliers, which sets the number of
    /// trials once an inlier ratio is known.
    pub confidence: f64,
    pub min_num_trials: usize,
    pub max_num_trials: usize,
    /// The inlier ratio assumed before any model is found: it bounds `max_num_trials` as COLMAP's
    /// constructor bounds it.
    pub min_inlier_ratio: f64,
    /// The number of trials the confidence asks for is multiplied by this (COLMAP's
    /// `dyn_num_trials_multiplier`).
    pub dyn_num_trials_multiplier: f64,
}

impl Default for Options {
    fn default() -> Options {
        Options {
            max_error: 4.0,
            confidence: 0.999,
            min_num_trials: 100,
            max_num_trials: 10000,
            min_inlier_ratio: 0.25,
            dyn_num_trials_multiplier: 3.0,
        }
    }
}

/// A model that a minimal sample of correspondences gives and that all the inliers refine: the
/// two estimators of COLMAP's `LORANSAC<Estimator, LocalEstimator>`.
pub trait Model: Clone {
    /// The size of a minimal sample.
    const MIN_SAMPLES: usize;
    /// The fewest inliers the local estimator takes.
    const MIN_LOCAL: usize;
    /// The models of a minimal sample (`MIN_SAMPLES` correspondences): none when it is degenerate.
    fn from_sample(x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Vec<Self>;
    /// The models of the inliers of a model (at least `MIN_LOCAL` correspondences).
    fn from_inliers(x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Vec<Self>;
    /// The squared residual of a correspondence under the model (infinite when it is degenerate).
    fn residual_sq(&self, a: [f64; 2], b: [f64; 2]) -> f64;
}

/// The support of a model (COLMAP's `InlierSupportMeasurer`): the inliers and the sum of their
/// squared residuals. More inliers is better; as many with a smaller sum is better.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Support {
    pub num_inliers: usize,
    pub residual_sum: f64,
}

impl Support {
    fn none() -> Support {
        Support { num_inliers: 0, residual_sum: f64::MAX }
    }

    pub(crate) fn evaluate(residuals: &[f64], max_residual: f64) -> Support {
        let mut support = Support { num_inliers: 0, residual_sum: 0.0 };
        for &r in residuals {
            if r <= max_residual {
                support.num_inliers += 1;
                support.residual_sum += r;
            }
        }
        support
    }

    /// COLMAP's `Compare`: `self` is a better support than `other`.
    pub fn better_than(&self, other: &Support) -> bool {
        self.num_inliers > other.num_inliers
            || (self.num_inliers == other.num_inliers && self.residual_sum < other.residual_sum)
    }
}

/// The outcome of an estimation: the best model, its support, which correspondences are its
/// inliers (one flag per correspondence, in the order given) and the trials made.
#[derive(Clone, Debug)]
pub struct Report<M> {
    pub model: M,
    pub support: Support,
    pub inlier_mask: Vec<bool>,
    pub num_trials: usize,
}

/// Knuth's MMIX linear congruential generator, seeded through the finaliser of SplitMix64 so
/// that near seeds (the same pair under another model, consecutive pair ids) start unrelated
/// streams.
#[derive(Clone, Debug)]
pub struct Lcg(u64);

impl Lcg {
    pub fn new(seed: u64) -> Lcg {
        let mut z = seed.wrapping_add(0x9E3779B97F4A7C15);
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
        Lcg(z ^ (z >> 31))
    }

    /// The next state, 64 bits (the low bits of a congruential generator are weak: `below` takes
    /// the high ones).
    pub fn bits(&mut self) -> u64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        self.0
    }

    /// A value in `[0, n)`, uniform to a part in 2^53: the upper 53 bits scaled by `n`.
    pub fn below(&mut self, n: usize) -> usize {
        (((self.bits() >> 11) as u128 * n as u128) >> 53) as usize
    }
}

/// The number of trials after which a sample free of outliers has been drawn with probability
/// `confidence`, given `num_inliers` of `num_samples` (COLMAP's `ComputeNumTrials`), times the
/// multiplier; the largest count when the confidence is 1 or no inlier is known, 1 when every
/// correspondence is an inlier.
fn num_trials(num_inliers: usize, num_samples: usize, min_samples: usize, confidence: f64, multiplier: f64) -> usize {
    let inlier_ratio = num_inliers as f64 / num_samples as f64;
    let nom = 1.0 - confidence;
    if nom <= 0.0 {
        return usize::MAX;
    }
    let denom = 1.0 - inlier_ratio.powi(min_samples as i32);
    if denom <= 0.0 {
        return 1;
    }
    let trials = (nom.ln() / denom.ln() * multiplier).ceil();
    if trials.is_finite() && trials < usize::MAX as f64 {
        trials as usize
    } else {
        usize::MAX
    }
}

/// PROSAC's choice of the hypothesis generation set (Chum and Matas 2005, section 2.2): the
/// `t`-th sample is drawn from the `n` best correspondences, where `n` grows from the size of a
/// sample to all of them as `t` passes `T'_n`, the number of samples that contain only points of
/// the `n` best among `T_N` samples drawn at random from all (`T_N` = 200,000, the paper's
/// figure). Of the samples drawn from the `n` best and not from the `n - 1` best, every one holds
/// the `n`-th correspondence and `m - 1` of the `n - 1` before it. Past `T'_N` the samples are
/// drawn at random from all, as RANSAC's.
struct Prosac {
    /// The size of a sample.
    m: usize,
    /// All the correspondences.
    total: usize,
    /// The best `n` the samples come from.
    n: usize,
    /// The samples drawn so far.
    t: usize,
    /// `T_n`: the expected number of samples from the best `n` among `T_N` random ones.
    t_n: f64,
    /// `T'_n`: the sample at which the set grows past `n`.
    t_n_prime: usize,
}

const T_N: f64 = 200000.0;

impl Prosac {
    fn new(m: usize, total: usize) -> Prosac {
        let mut t_n = T_N;
        for i in 0..m {
            t_n *= (m - i) as f64 / (total - i) as f64;
        }
        Prosac { m, total, n: m, t: 0, t_n, t_n_prime: 1 }
    }

    /// The indices of the next sample, distinct, in `out`.
    fn sample(&mut self, random: &mut Lcg, out: &mut Vec<usize>) {
        self.t += 1;
        while self.n < self.total && self.t > self.t_n_prime {
            // T_{n+1} by the recurrence of the paper (equation 3), T'_{n+1} by its equation 4
            let t_next = self.t_n * (self.n + 1) as f64 / (self.n + 1 - self.m) as f64;
            self.t_n_prime = self.t_n_prime.saturating_add((t_next - self.t_n).ceil() as usize);
            self.t_n = t_next;
            self.n += 1;
        }
        out.clear();
        if self.n < self.total || self.t <= self.t_n_prime {
            out.push(self.n - 1);
            distinct(random, self.m - 1, self.n - 1, out);
        } else {
            distinct(random, self.m, self.total, out);
        }
    }
}

/// `count` indices below `bound`, distinct from each other and from those in `out`, appended to
/// it (`bound` is at least `count` plus what `out` holds).
fn distinct(random: &mut Lcg, count: usize, bound: usize, out: &mut Vec<usize>) {
    let wanted = out.len() + count;
    while out.len() < wanted {
        let k = random.below(bound);
        if !out.contains(&k) {
            out.push(k);
        }
    }
}

/// How the minimal samples are drawn.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Sampling {
    /// PROSAC, on correspondences ordered best first.
    Prosac,
    /// Every sample drawn at random from all the correspondences (COLMAP's `RandomSampler`): for
    /// correspondences that carry no order, such as the inliers a watermark is looked for in.
    Uniform,
}

/// LO-RANSAC on the correspondences `(x1[i], x2[i])`: minimal samples drawn by `sampling` (PROSAC
/// wants them ordered best first), each model scored by its support over all the correspondences
/// (an inlier has a squared residual at most `max_error` squared); a model better than all before
/// it is refined from its inliers by the local estimator, again while the inliers grow, at most
/// ten times. The trials stop when they reach the count the confidence asks for from the best
/// support so far, never before `min_num_trials` nor after `max_num_trials` (bounded as COLMAP
/// bounds it by `min_inlier_ratio`). `None` when there are fewer correspondences than a sample
/// or no model gained as many inliers as a sample holds. The generator is seeded by `seed`.
pub fn lo_ransac<M: Model>(x1: &[[f64; 2]], x2: &[[f64; 2]], o: &Options, sampling: Sampling, seed: u64)
                           -> Option<Report<M>> {
    assert_eq!(x1.len(), x2.len(), "as many points in both photos");
    let n = x1.len();
    if n < M::MIN_SAMPLES {
        return None;
    }
    let max_residual = o.max_error * o.max_error;
    let assumed = (o.min_inlier_ratio * 100000.0) as usize;
    let max_trials = o.max_num_trials.min(num_trials(assumed, 100000, M::MIN_SAMPLES, o.confidence,
                                                     o.dyn_num_trials_multiplier));
    let mut dyn_max_trials = max_trials;
    let mut random = Lcg::new(seed);
    let mut sampler = Prosac::new(M::MIN_SAMPLES, n);
    let mut sample = Vec::with_capacity(M::MIN_SAMPLES);
    let mut s1 = vec![[0.0; 2]; M::MIN_SAMPLES];
    let mut s2 = vec![[0.0; 2]; M::MIN_SAMPLES];
    let mut best: Option<(M, Support, Vec<f64>)> = None;
    let mut best_support = Support::none();
    let mut residuals = vec![0.0; n];
    let (mut i1, mut i2) = (Vec::with_capacity(n), Vec::with_capacity(n));
    let mut trials = 0;
    let residuals_of = |m: &M, out: &mut Vec<f64>| {
        for ((r, &a), &b) in out.iter_mut().zip(x1).zip(x2) {
            *r = m.residual_sq(a, b);
        }
    };
    'trials: for trial in 0..max_trials {
        trials = trial + 1;
        match sampling {
            Sampling::Prosac => sampler.sample(&mut random, &mut sample),
            Sampling::Uniform => {
                sample.clear();
                distinct(&mut random, M::MIN_SAMPLES, n, &mut sample);
            }
        }
        for (k, &i) in sample.iter().enumerate() {
            s1[k] = x1[i];
            s2[k] = x2[i];
        }
        for model in M::from_sample(&s1, &s2) {
            residuals_of(&model, &mut residuals);
            let support = Support::evaluate(&residuals, max_residual);
            if support.better_than(&best_support) {
                best_support = support;
                best = Some((model, support, residuals.clone()));
                if support.num_inliers > M::MIN_SAMPLES && support.num_inliers >= M::MIN_LOCAL {
                    for _ in 0..10 {
                        let before = best_support.num_inliers;
                        i1.clear();
                        i2.clear();
                        let current = &best.as_ref().expect("a best model").2;
                        for (i, &r) in current.iter().enumerate() {
                            if r <= max_residual {
                                i1.push(x1[i]);
                                i2.push(x2[i]);
                            }
                        }
                        for local in M::from_inliers(&i1, &i2) {
                            residuals_of(&local, &mut residuals);
                            let local_support = Support::evaluate(&residuals, max_residual);
                            if local_support.better_than(&best_support) {
                                best_support = local_support;
                                best = Some((local, local_support, residuals.clone()));
                            }
                        }
                        if best_support.num_inliers <= before {
                            break;
                        }
                    }
                }
                dyn_max_trials = num_trials(best_support.num_inliers, n, M::MIN_SAMPLES, o.confidence,
                                            o.dyn_num_trials_multiplier);
            }
            if trial >= dyn_max_trials && trial >= o.min_num_trials {
                break 'trials;
            }
        }
    }
    let (model, support, residuals) = best?;
    if support.num_inliers < M::MIN_SAMPLES {
        return None;
    }
    let inlier_mask = residuals.iter().map(|&r| r <= max_residual).collect();
    Some(Report { model, support, inlier_mask, num_trials: trials })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_trial_count_is_colmaps() {
        // 50 % inliers, samples of five, confidence 0.999, multiplier 3:
        // ceil(ln 0.001 / ln(1 - 0.5^5) * 3) = ceil(217.58 * 3) = 653
        assert_eq!(num_trials(50, 100, 5, 0.999, 3.0), 653);
        assert_eq!(num_trials(100, 100, 5, 0.999, 3.0), 1);
        assert_eq!(num_trials(50, 100, 5, 1.0, 3.0), usize::MAX);
        assert_eq!(num_trials(0, 100, 5, 0.999, 3.0), usize::MAX);
        // the bound of the constructor on the default options: 25 % and samples of four
        assert_eq!(num_trials(25000, 100000, 4, 0.999, 3.0), 5295);
    }

    #[test]
    fn prosac_grows_the_set_from_the_best_and_holds_the_newest_point() {
        let mut random = Lcg::new(1);
        let mut sampler = Prosac::new(4, 50);
        let mut out = Vec::new();
        // the first sample is the four best
        sampler.sample(&mut random, &mut out);
        let mut first = out.clone();
        first.sort_unstable();
        assert_eq!(first, vec![0, 1, 2, 3]);
        let mut largest_seen = 3;
        for _ in 0..300 {
            sampler.sample(&mut random, &mut out);
            assert_eq!(out.len(), 4);
            let mut sorted = out.clone();
            sorted.sort_unstable();
            sorted.dedup();
            assert_eq!(sorted.len(), 4, "distinct indices");
            assert!(sorted[3] < 50);
            // the set grows by at most one point per sample and the newest is in the sample
            assert!(sorted[3] <= largest_seen + 1, "{sorted:?} after {largest_seen}");
            largest_seen = largest_seen.max(sorted[3]);
            assert_eq!(out[0], sampler.n - 1, "the newest point of the set is in the sample");
        }
        // T'_n of the paper with T_N = 200,000, for 50 points and samples of four: T'_11 = 290
        // and T'_12 = 434, so the 301st sample comes from the 12 best
        assert_eq!(sampler.n, 12, "the set after 301 samples of 50");
        // a set of one: every sample is the newest point until the set grows
        let mut single = Prosac::new(1, 200);
        single.sample(&mut random, &mut out);
        assert_eq!(out, vec![0]);
        // all the points: once past T'_N the samples are uniform over all
        let mut all = Prosac::new(2, 2);
        for _ in 0..5 {
            all.sample(&mut random, &mut out);
            out.sort_unstable();
            assert_eq!(out, vec![0, 1]);
        }
    }

    #[test]
    fn distinct_draws_fill_a_set_as_large_as_the_bound() {
        let mut random = Lcg::new(3);
        let mut out = vec![2];
        distinct(&mut random, 4, 5, &mut out);
        out.sort_unstable();
        assert_eq!(out, vec![0, 1, 2, 3, 4]);
    }
}
