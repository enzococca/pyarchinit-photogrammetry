//! PatchMatch multi-view stereo on the GPU (wgpu, WGSL): the kernel of `patchmatch.rs` with the
//! same cost - the normalised cross correlation of a window of the reference photo with its image
//! through the plane of the pixel in up to six neighbouring photos, the mean of the best views -
//! the same red-black propagation (every pixel of one colour tries the planes of its eight
//! neighbours of the other colour, 1 and 5 pixels away, then a random plane and three smaller
//! changes of its best) and the same start (the planes of a depth map given, random elsewhere).
//! One thread per pixel; the planes and the costs stay on the device through all the passes,
//! the parameters of every pass sit in one buffer at dynamic offsets, the whole run is one
//! submission. Geometry in f32 (the CPU uses f64: depths agree to the sixth figure), the random
//! numbers of a 32-bit hash (not the CPU's xorshift64, so a map differs from the CPU's in the
//! noise of its texture-less pixels, not in the surface).
use wgpu::util::DeviceExt;

pub const BAD: f32 = 2.0;
const MAX_SOURCES: usize = 6;
const UNIFORM_STRIDE: u64 = 256; // the alignment of dynamic uniform offsets every backend accepts

#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
struct Params {
    kinv0: [f32; 4],
    kinv1: [f32; 4],
    kinv2: [f32; 4],
    w: u32,
    h: u32,
    n_src: u32,
    half: u32,
    step: u32,
    top_k: u32,
    min_views: u32,
    seed: u32,
    depth_min: f32,
    depth_max: f32,
    scale: f32,
    max_cost: f32,
    pass_id: u32,
    colour: u32,
    iteration: u32,
    _pad: u32,
}

#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
struct Src {
    k: [f32; 4],
    r0: [f32; 4],
    r1: [f32; 4],
    r2: [f32; 4],
    t: [f32; 4],
    dims: [u32; 4], // w, h, offset (floats), 0
}

/// A neighbouring photo: its grey image and where it is seen from (reference camera -> this one).
pub struct Source<'a> {
    pub img: &'a [f32],
    pub w: usize,
    pub h: usize,
    pub k: [f64; 4],
    pub r: [[f64; 3]; 3],
    pub t: [f64; 3],
}

#[derive(Clone, Copy)]
pub struct Options {
    pub depth_min: f64,
    pub depth_max: f64,
    pub iterations: usize,
    pub half_window: usize,
    pub step: usize,
    pub top_k: usize,
    pub min_views: usize,
    pub max_cost: f32,
    pub seed: u64,
}

pub struct Output {
    pub depth: Vec<f32>,
    pub normal: Vec<f32>,
    pub cost: Vec<f32>,
}

const SHADER: &str = r#"
struct Params {
    kinv0: vec4<f32>, kinv1: vec4<f32>, kinv2: vec4<f32>,
    w: u32, h: u32, n_src: u32, half: u32,
    step: u32, top_k: u32, min_views: u32, seed: u32,
    depth_min: f32, depth_max: f32, scale: f32, max_cost: f32,
    pass_id: u32, colour: u32, iteration: u32, pad: u32,
};
struct Src { k: vec4<f32>, r0: vec4<f32>, r1: vec4<f32>, r2: vec4<f32>, t: vec4<f32>, dims: vec4<u32> };

@group(0) @binding(0) var<uniform> p: Params;
@group(0) @binding(1) var<storage, read> reference: array<f32>;
@group(0) @binding(2) var<storage, read> images: array<f32>;
@group(0) @binding(3) var<storage, read> srcs: array<Src>;
@group(0) @binding(4) var<storage, read_write> planes: array<vec4<f32>>;
@group(0) @binding(5) var<storage, read_write> costs: array<f32>;
@group(0) @binding(6) var<storage, read_write> stats: array<vec2<f32>>;
@group(0) @binding(7) var<storage, read> init: array<vec4<f32>>;

const BAD: f32 = 2.0;
const NONE: f32 = -1e30;

fn ray(x: f32, y: f32) -> vec3<f32> {
    let v = vec3<f32>(x, y, 1.0);
    return vec3<f32>(dot(p.kinv0.xyz, v), dot(p.kinv1.xyz, v), 1.0);
}

fn rng_seed(i: u32, pass_no: u32) -> u32 {
    var s = p.seed ^ (i * 0x9E3779B9u) ^ (pass_no * 0x85EBCA6Bu);
    s = (s ^ (s >> 16u)) * 0x7feb352du;
    s = (s ^ (s >> 15u)) * 0x846ca68bu;
    s = s ^ (s >> 16u);
    return select(s, 0x2545F491u, s == 0u);
}

fn uniform(state: ptr<function, u32>) -> f32 {
    var s = *state;
    s = s ^ (s << 13u);
    s = s ^ (s >> 17u);
    s = s ^ (s << 5u);
    *state = s;
    return f32(s >> 8u) / 16777216.0;
}

fn random_depth(state: ptr<function, u32>) -> f32 {
    let a = 1.0 / p.depth_max;
    let b = 1.0 / p.depth_min;
    return 1.0 / (a + (b - a) * uniform(state));
}

fn random_normal(state: ptr<function, u32>, r: vec3<f32>) -> vec3<f32> {
    let rn = normalize(r);
    for (var tries = 0u; tries < 64u; tries++) {
        let v = vec3<f32>(uniform(state) * 2.0 - 1.0, uniform(state) * 2.0 - 1.0, uniform(state) * 2.0 - 1.0);
        let l = dot(v, v);
        if (l > 1e-6 && l <= 1.0) {
            var n = normalize(v);
            if (dot(n, r) > 0.0) { n = -n; }
            if (dot(n, rn) < -0.2) { return n; }
        }
    }
    return -rn;
}

fn perturb(state: ptr<function, u32>, pl: vec4<f32>, r: vec3<f32>, scale: f32) -> vec4<f32> {
    let inv = 1.0 / pl.x;
    let span = (1.0 / p.depth_min - 1.0 / p.depth_max) * scale;
    var d = 1.0 / (inv + (uniform(state) * 2.0 - 1.0) * span);
    if (!(d >= p.depth_min && d <= p.depth_max)) { d = pl.x; }
    var n = normalize(pl.yzw + vec3<f32>(uniform(state) * 2.0 - 1.0, uniform(state) * 2.0 - 1.0, uniform(state) * 2.0 - 1.0) * scale);
    if (dot(n, r) >= -1e-3) { n = pl.yzw; }
    return vec4<f32>(d, n);
}

// the plane found at `from` along the ray of `at`: x < 0 when the ray misses it in front of the camera
fn transfer(pl: vec4<f32>, fx: f32, fy: f32, ax: f32, ay: f32) -> vec4<f32> {
    let rf = ray(fx, fy);
    let ra = ray(ax, ay);
    let rho = pl.x * dot(pl.yzw, rf);
    let na = dot(pl.yzw, ra);
    if (na >= -1e-9) { return vec4<f32>(-1.0, 0.0, 0.0, 0.0); }
    let d = rho / na;
    if (d < p.depth_min || d > p.depth_max) { return vec4<f32>(-1.0, 0.0, 0.0, 0.0); }
    return vec4<f32>(d, pl.yzw);
}

fn cost(x: u32, y: u32, pl: vec4<f32>) -> f32 {
    let i = y * p.w + x;
    let st = stats[i];
    if (st.x < -1e29) { return BAD; }
    let r = ray(f32(x), f32(y));
    let n = pl.yzw;
    let nr = dot(n, r);
    if (nr >= -1e-9) { return BAD; }
    let rho = pl.x * nr;
    let half = i32(p.half);
    let step = i32(max(p.step, 1u));
    let count = f32(((2 * half) / step + 1) * ((2 * half) / step + 1));
    var cs = array<f32, 6>(BAD, BAD, BAD, BAD, BAD, BAD);
    var nv = 0u;
    for (var s = 0u; s < p.n_src; s++) {
        let S = srcs[s];
        // H = K_s (R + t n^T / rho) K_ref^-1
        let m0 = S.r0.xyz + S.t.x * n / rho;
        let m1 = S.r1.xyz + S.t.y * n / rho;
        let m2 = S.r2.xyz + S.t.z * n / rho;
        let kc0 = vec3<f32>(p.kinv0.x, p.kinv1.x, p.kinv2.x);   // columns of kinv
        let kc1 = vec3<f32>(p.kinv0.y, p.kinv1.y, p.kinv2.y);
        let kc2 = vec3<f32>(p.kinv0.z, p.kinv1.z, p.kinv2.z);
        let mk0 = vec3<f32>(dot(m0, kc0), dot(m0, kc1), dot(m0, kc2));
        let mk1 = vec3<f32>(dot(m1, kc0), dot(m1, kc1), dot(m1, kc2));
        let mk2 = vec3<f32>(dot(m2, kc0), dot(m2, kc1), dot(m2, kc2));
        let h0 = S.k.x * mk0 + S.k.z * mk2;
        let h1 = S.k.y * mk1 + S.k.w * mk2;
        let h2 = mk2;
        let sw = S.dims.x;
        let sh = S.dims.y;
        let off = S.dims.z;
        var ss = 0.0;
        var sss = 0.0;
        var srs = 0.0;
        var seen = true;
        for (var dy = -half; dy <= half && seen; dy += step) {
            for (var dx = -half; dx <= half && seen; dx += step) {
                let u = f32(i32(x) + dx);
                let v = f32(i32(y) + dy);
                let q = vec3<f32>(u, v, 1.0);
                let pz = dot(h2, q);
                if (pz <= 1e-12) { seen = false; break; }
                let pu = dot(h0, q) / pz;
                let pv = dot(h1, q) / pz;
                if (!(pu >= 0.0 && pv >= 0.0)) { seen = false; break; }
                let x0 = u32(pu);
                let y0 = u32(pv);
                if (x0 + 1u >= sw || y0 + 1u >= sh) { seen = false; break; }
                let fx = pu - f32(x0);
                let fy = pv - f32(y0);
                let b = off + y0 * sw + x0;
                let top = images[b] + (images[b + 1u] - images[b]) * fx;
                let bottom = images[b + sw] + (images[b + sw + 1u] - images[b + sw]) * fx;
                let val = top + (bottom - top) * fy;
                let rv = reference[u32(i32(y) + dy) * p.w + u32(i32(x) + dx)];
                ss += val;
                sss += val * val;
                srs += rv * val;
            }
        }
        if (!seen) { continue; }
        let mean_s = ss / count;
        let var_s = max(sss - count * mean_s * mean_s, 0.0);
        var c = BAD;
        if (!(var_s < 1e-6 * count || st.y < 1e-12)) {
            let ncc = (srs - count * st.x * mean_s) / (st.y * sqrt(var_s));
            c = clamp(1.0 - ncc, 0.0, 2.0);
        }
        // sorted insertion among the views
        var k = nv;
        while (k > 0u && cs[k - 1u] > c) { cs[k] = cs[k - 1u]; k--; }
        cs[k] = c;
        nv++;
    }
    if (nv < max(p.min_views, 1u)) { return BAD; }
    let kk = clamp(p.top_k, 1u, nv);
    var sum = 0.0;
    for (var j = 0u; j < kk; j++) { sum += cs[j]; }
    return sum / f32(kk);
}

@compute @workgroup_size(16, 16)
fn stats_pass(@builtin(global_invocation_id) gid: vec3<u32>) {
    let x = gid.x;
    let y = gid.y;
    if (x >= p.w || y >= p.h) { return; }
    let i = y * p.w + x;
    let half = i32(p.half);
    let step = i32(max(p.step, 1u));
    if (i32(y) < half || i32(y) + half >= i32(p.h) || i32(x) < half || i32(x) + half >= i32(p.w)) {
        stats[i] = vec2<f32>(NONE, 0.0);
        return;
    }
    var s = 0.0;
    var ss = 0.0;
    var n = 0.0;
    for (var dy = -half; dy <= half; dy += step) {
        for (var dx = -half; dx <= half; dx += step) {
            let v = reference[u32(i32(y) + dy) * p.w + u32(i32(x) + dx)];
            s += v;
            ss += v * v;
            n += 1.0;
        }
    }
    let m = s / n;
    stats[i] = vec2<f32>(m, sqrt(max(ss - n * m * m, 0.0)));
}

@compute @workgroup_size(16, 16)
fn init_pass(@builtin(global_invocation_id) gid: vec3<u32>) {
    let x = gid.x;
    let y = gid.y;
    if (x >= p.w || y >= p.h) { return; }
    let i = y * p.w + x;
    let r = ray(f32(x), f32(y));
    var state = rng_seed(i, 0u);
    var pl: vec4<f32>;
    let d0 = init[i];
    if (d0.x >= p.depth_min && d0.x <= p.depth_max) {
        var n = d0.yzw;
        if (dot(n, n) > 0.5 && dot(n, r) < -1e-3) { n = normalize(n); } else { n = normalize(-r); }
        pl = vec4<f32>(d0.x, n);
    } else {
        pl = vec4<f32>(random_depth(&state), random_normal(&state, r));
    }
    planes[i] = pl;
    costs[i] = cost(x, y, pl);
}

@compute @workgroup_size(16, 16)
fn propagate_pass(@builtin(global_invocation_id) gid: vec3<u32>) {
    let y = gid.y;
    if (y >= p.h) { return; }
    let x = 2u * gid.x + (y + p.colour) % 2u;
    if (x >= p.w) { return; }
    let i = y * p.w + x;
    if (stats[i].x < -1e29) { return; }
    var best = planes[i];
    var best_cost = costs[i];
    let r = ray(f32(x), f32(y));
    let dxs = array<i32, 8>(-1, 1, 0, 0, -5, 5, 0, 0);
    let dys = array<i32, 8>(0, 0, -1, 1, 0, 0, -5, 5);
    for (var k = 0u; k < 8u; k++) {
        let nx = i32(x) + dxs[k];
        let ny = i32(y) + dys[k];
        if (nx < 0 || ny < 0 || nx >= i32(p.w) || ny >= i32(p.h)) { continue; }
        let cand = transfer(planes[u32(ny) * p.w + u32(nx)], f32(nx), f32(ny), f32(x), f32(y));
        if (cand.x < 0.0) { continue; }
        let c = cost(x, y, cand);
        if (c < best_cost) { best = cand; best_cost = c; }
    }
    var state = rng_seed(i, p.pass_id);
    let fresh = vec4<f32>(random_depth(&state), random_normal(&state, r));
    let cf = cost(x, y, fresh);
    if (cf < best_cost) { best = fresh; best_cost = cf; }
    var scale = p.scale;
    for (var j = 0u; j < 3u; j++) {
        let cand = perturb(&state, best, r, scale);
        let c = cost(x, y, cand);
        if (c < best_cost) { best = cand; best_cost = c; }
        scale *= 0.25;
    }
    if (best_cost < costs[i]) {
        planes[i] = best;
        costs[i] = best_cost;
    }
}
"#;

/// The PatchMatch kernels on one GPU, with their working buffers grown as needed.
pub struct PatchMatcher {
    #[allow(dead_code)]
    pub name: String,
    device: wgpu::Device,
    queue: wgpu::Queue,
    layout: wgpu::BindGroupLayout,
    stats: wgpu::ComputePipeline,
    init: wgpu::ComputePipeline,
    propagate: wgpu::ComputePipeline,
}

fn buffer_entry(binding: u32, read_only: bool) -> wgpu::BindGroupLayoutEntry {
    wgpu::BindGroupLayoutEntry {
        binding, visibility: wgpu::ShaderStages::COMPUTE, count: None,
        ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only }, has_dynamic_offset: false, min_binding_size: None },
    }
}

impl PatchMatcher {
    pub fn new(index: Option<usize>) -> Result<PatchMatcher, String> {
        let inst = wgpu::Instance::default();
        let mut all = inst.enumerate_adapters(wgpu::Backends::all());
        if all.is_empty() {
            return Err("no GPU adapter (wgpu found no Metal, Vulkan or DirectX 12 device)".into());
        }
        let chosen = match index {
            Some(i) if i < all.len() => i,
            Some(i) => return Err(format!("GPU adapter {i} does not exist: {} found", all.len())),
            None => (0..all.len()).min_by_key(|&i| (super::gpu::rank(all[i].get_info().device_type), i)).unwrap(),
        };
        let adapter = all.swap_remove(chosen);
        let name = adapter.get_info().name;
        let limits = adapter.limits();
        let (device, queue) = pollster::block_on(adapter.request_device(
            &wgpu::DeviceDescriptor { label: Some("ppg_core patchmatch"), required_limits: limits, ..Default::default() }, None))
            .map_err(|e| format!("{name}: {e}"))?;
        let module = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("patchmatch"), source: wgpu::ShaderSource::Wgsl(SHADER.into()) });
        let layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor { label: None, entries: &[
            wgpu::BindGroupLayoutEntry { binding: 0, visibility: wgpu::ShaderStages::COMPUTE, count: None,
                ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Uniform, has_dynamic_offset: true,
                                                min_binding_size: wgpu::BufferSize::new(std::mem::size_of::<Params>() as u64) } },
            buffer_entry(1, true), buffer_entry(2, true), buffer_entry(3, true),
            buffer_entry(4, false), buffer_entry(5, false), buffer_entry(6, false), buffer_entry(7, true),
        ] });
        let pipeline_layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor { label: None, bind_group_layouts: &[&layout], push_constant_ranges: &[] });
        let make = |entry: &str| device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some(entry), layout: Some(&pipeline_layout), module: &module, entry_point: Some(entry),
            compilation_options: Default::default(), cache: None });
        let stats = make("stats_pass");
        let init = make("init_pass");
        let propagate = make("propagate_pass");
        Ok(PatchMatcher { name, device, queue, layout, stats, init, propagate })
    }

    /// Depth, normal and cost of every pixel of `reference` (w x h, grey 0-1) seen with
    /// intrinsics `k` from the `sources`, as `patchmatch::patchmatch` gives them.
    #[allow(clippy::too_many_arguments)]
    pub fn run(&self, reference: &[f32], w: usize, h: usize, k: [f64; 4], sources: &[Source], o: Options,
               init: Option<&[f32]>, init_normal: Option<&[f32]>) -> Result<Output, String> {
        if sources.is_empty() || sources.len() > MAX_SOURCES {
            return Err(format!("1 to {MAX_SOURCES} source photos, {} given", sources.len()));
        }
        let n = w * h;
        let dev = &self.device;
        let storage = |label: &str, data: &[u8], extra: wgpu::BufferUsages| dev.create_buffer_init(
            &wgpu::util::BufferInitDescriptor { label: Some(label), contents: data, usage: wgpu::BufferUsages::STORAGE | extra });
        let ref_buf = storage("reference", bytemuck::cast_slice(reference), wgpu::BufferUsages::empty());
        let mut all_images: Vec<f32> = Vec::with_capacity(sources.iter().map(|s| s.img.len()).sum());
        let mut srcs = Vec::with_capacity(sources.len());
        for s in sources {
            let f = |v: f64| v as f32;
            srcs.push(Src {
                k: [f(s.k[0]), f(s.k[1]), f(s.k[2]), f(s.k[3])],
                r0: [f(s.r[0][0]), f(s.r[0][1]), f(s.r[0][2]), 0.0],
                r1: [f(s.r[1][0]), f(s.r[1][1]), f(s.r[1][2]), 0.0],
                r2: [f(s.r[2][0]), f(s.r[2][1]), f(s.r[2][2]), 0.0],
                t: [f(s.t[0]), f(s.t[1]), f(s.t[2]), 0.0],
                dims: [s.w as u32, s.h as u32, all_images.len() as u32, 0],
            });
            all_images.extend_from_slice(s.img);
        }
        let img_buf = storage("sources", bytemuck::cast_slice(&all_images), wgpu::BufferUsages::empty());
        let src_buf = storage("source params", bytemuck::cast_slice(&srcs), wgpu::BufferUsages::empty());
        // the start: depth and normal per pixel (depth 0 = none)
        let mut start = vec![[0.0f32; 4]; n];
        if let Some(d) = init {
            for i in 0..n {
                start[i][0] = d[i];
                if let Some(nm) = init_normal {
                    start[i][1] = nm[3 * i];
                    start[i][2] = nm[3 * i + 1];
                    start[i][3] = nm[3 * i + 2];
                }
            }
        }
        let init_buf = storage("init", bytemuck::cast_slice(&start), wgpu::BufferUsages::empty());
        let planes = dev.create_buffer(&wgpu::BufferDescriptor { label: Some("planes"), size: (n * 16) as u64, usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC, mapped_at_creation: false });
        let costs = dev.create_buffer(&wgpu::BufferDescriptor { label: Some("costs"), size: (n * 4) as u64, usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC, mapped_at_creation: false });
        let stats = dev.create_buffer(&wgpu::BufferDescriptor { label: Some("stats"), size: (n * 8) as u64, usage: wgpu::BufferUsages::STORAGE, mapped_at_creation: false });
        // the parameters of every pass, at dynamic offsets
        let (fx, fy, cx, cy) = (k[0] as f32, k[1] as f32, k[2] as f32, k[3] as f32);
        let base = Params {
            kinv0: [1.0 / fx, 0.0, -cx / fx, 0.0], kinv1: [0.0, 1.0 / fy, -cy / fy, 0.0], kinv2: [0.0, 0.0, 1.0, 0.0],
            w: w as u32, h: h as u32, n_src: sources.len() as u32, half: o.half_window.max(1) as u32,
            step: o.step.max(1) as u32, top_k: o.top_k.max(1) as u32, min_views: o.min_views as u32,
            seed: (o.seed ^ (o.seed >> 32)) as u32, depth_min: o.depth_min as f32, depth_max: o.depth_max as f32,
            scale: 0.0, max_cost: o.max_cost, pass_id: 0, colour: 0, iteration: 0, _pad: 0,
        };
        let mut passes = vec![base, base]; // stats, init
        for it in 0..o.iterations {
            for colour in 0..2u32 {
                let mut q = base;
                q.pass_id = (it as u32) * 2 + colour + 1;
                q.colour = colour;
                q.iteration = it as u32;
                q.scale = 0.25 / (1.0 + it as f32);
                passes.push(q);
            }
        }
        let mut raw = vec![0u8; passes.len() * UNIFORM_STRIDE as usize];
        for (i, q) in passes.iter().enumerate() {
            let b = bytemuck::bytes_of(q);
            raw[i * UNIFORM_STRIDE as usize..i * UNIFORM_STRIDE as usize + b.len()].copy_from_slice(b);
        }
        let params = dev.create_buffer_init(&wgpu::util::BufferInitDescriptor { label: Some("params"), contents: &raw, usage: wgpu::BufferUsages::UNIFORM });
        let bind = dev.create_bind_group(&wgpu::BindGroupDescriptor {
            label: None, layout: &self.layout,
            entries: &[
                wgpu::BindGroupEntry { binding: 0, resource: wgpu::BindingResource::Buffer(wgpu::BufferBinding {
                    buffer: &params, offset: 0, size: wgpu::BufferSize::new(std::mem::size_of::<Params>() as u64) }) },
                wgpu::BindGroupEntry { binding: 1, resource: ref_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 2, resource: img_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 3, resource: src_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 4, resource: planes.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 5, resource: costs.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 6, resource: stats.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 7, resource: init_buf.as_entire_binding() },
            ],
        });
        let staging = dev.create_buffer(&wgpu::BufferDescriptor { label: Some("staging"), size: (n * 20) as u64, usage: wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST, mapped_at_creation: false });
        let groups = (((w + 15) / 16) as u32, ((h + 15) / 16) as u32);
        let half_groups = (((w / 2 + 1 + 15) / 16) as u32, groups.1);
        let mut enc = dev.create_command_encoder(&Default::default());
        {
            let mut pass = enc.begin_compute_pass(&Default::default());
            pass.set_pipeline(&self.stats);
            pass.set_bind_group(0, &bind, &[0]);
            pass.dispatch_workgroups(groups.0, groups.1, 1);
            pass.set_pipeline(&self.init);
            pass.set_bind_group(0, &bind, &[UNIFORM_STRIDE as u32]);
            pass.dispatch_workgroups(groups.0, groups.1, 1);
            pass.set_pipeline(&self.propagate);
            for i in 2..passes.len() {
                pass.set_bind_group(0, &bind, &[(i as u64 * UNIFORM_STRIDE) as u32]);
                pass.dispatch_workgroups(half_groups.0, half_groups.1, 1);
            }
        }
        enc.copy_buffer_to_buffer(&planes, 0, &staging, 0, (n * 16) as u64);
        enc.copy_buffer_to_buffer(&costs, 0, &staging, (n * 16) as u64, (n * 4) as u64);
        self.queue.submit(Some(enc.finish()));
        let slice = staging.slice(..);
        let (tx, rx) = std::sync::mpsc::channel();
        slice.map_async(wgpu::MapMode::Read, move |r| { let _ = tx.send(r); });
        dev.poll(wgpu::Maintain::Wait);
        rx.recv().map_err(|e| e.to_string())?.map_err(|e| e.to_string())?;
        let words: Vec<f32> = bytemuck::cast_slice(&slice.get_mapped_range()).to_vec();
        staging.unmap();
        let mut depth = vec![0.0f32; n];
        let mut normal = vec![0.0f32; 3 * n];
        let mut cost = vec![BAD; n];
        for i in 0..n {
            let c = words[4 * n + i];
            cost[i] = c;
            if c <= o.max_cost {
                depth[i] = words[4 * i];
                normal[3 * i] = words[4 * i + 1];
                normal[3 * i + 1] = words[4 * i + 2];
                normal[3 * i + 2] = words[4 * i + 3];
            }
        }
        Ok(Output { depth, normal, cost })
    }
}
