//! The preselection on a device whose backend refuses the kernels of a block: the test hook
//! `gpu::BLOCK_REFUSED_VAR` refuses them as a shader compiler would (in a process of its own,
//! this file, since every matcher of the process reads the hook), the pair kernels of the device
//! still work, the device is logged and left out of the matching, and the CPU gives the same
//! pairs. With no adapter at all there is nothing to refuse and the test says so.

use std::sync::atomic::AtomicBool;

use ppg_engine::gpu::{self, Matcher, DIM};
use ppg_engine::matching;
use ppg_engine::preselect::{match_blocks, MatchOptions};

/// Knuth's MMIX linear congruential generator, as in the other tests of the workspace.
struct Lcg(u64);

impl Lcg {
    fn unit(&mut self) -> f64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }
}

/// `n` descriptors as SIFT's bytes: the squares of uniform values, a norm of 512, rounded.
fn descriptors(n: usize, random: &mut Lcg) -> Vec<u8> {
    let mut out = Vec::with_capacity(n * DIM);
    for _ in 0..n {
        let values: Vec<f64> = (0..DIM).map(|_| random.unit().powi(2)).collect();
        let norm = values.iter().map(|v| v * v).sum::<f64>().sqrt();
        out.extend(values.iter().map(|v| (v / norm * 512.0).round().clamp(0.0, 255.0) as u8));
    }
    out
}

#[test]
fn a_device_whose_backend_refuses_the_block_kernels_is_left_to_the_cpu() {
    let adapters = gpu::adapters();
    // the adapter ppg-bench would take: the first that is no software rasteriser, else the first
    let Some(adapter) = adapters.iter().find(|a| a.device_type != "Cpu").or(adapters.first()) else {
        eprintln!("no GPU adapter: nothing to refuse");
        return;
    };
    let mut random = Lcg(3);
    let photos: Vec<Vec<u8>> = (0..10).map(|_| descriptors(128, &mut random)).collect();
    let refs: Vec<&[u8]> = photos.iter().map(Vec::as_slice).collect();
    let o = MatchOptions { max_ratio: 0.8, max_distance: 0.7, cross_check: true, workgroup_bytes: None };
    let never = AtomicBool::new(false);
    let on_cpu = match_blocks(&refs, &o, &[], &never, &|_, _| {}).unwrap();
    assert_eq!(on_cpu.pairs.len(), 45);

    std::env::set_var(gpu::BLOCK_REFUSED_VAR, "1");
    // the device opens and its pair kernels answer; the block kernels are refused, naming the hook
    let mut matcher = Matcher::new(Some(adapter.index))
        .unwrap_or_else(|p| panic!("adapter {} ({}) opens no matcher: {p}", adapter.index, adapter.name));
    let refused = matcher.prepare_block().err();
    assert!(refused.as_ref().is_some_and(|p| p.contains(gpu::BLOCK_REFUSED_VAR)), "{refused:?}");
    let refused = matcher.bests_block(&[refs[0]], &[refs[1]]).err();
    assert!(refused.as_ref().is_some_and(|p| p.contains("block kernels")), "{refused:?}");
    let pair = matcher.bests(0, refs[0], 0, refs[1]).unwrap();
    assert_eq!(pair, matching::bests_cpu(refs[0], refs[1]), "the pair kernels are touched by the refusal");
    drop(matcher);
    // the preselection: the device logged and left out, the CPU doing the matching, the same pairs
    let fallen_back = match_blocks(&refs, &o, &[adapter.index], &never, &|_, _| {}).unwrap();
    std::env::remove_var(gpu::BLOCK_REFUSED_VAR);
    assert_eq!(fallen_back.pairs, on_cpu.pairs);
    assert_eq!(fallen_back.by_device, (0, 1));
    assert!(fallen_back.gpus.is_empty(), "{:?}", fallen_back.gpus);
    let named = format!("GPU {}", adapter.index);
    assert!(fallen_back.log.iter().any(|l| l.starts_with(&named) && l.contains("block kernels are refused")),
            "{:?}", fallen_back.log);
}
