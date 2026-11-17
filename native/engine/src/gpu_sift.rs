//! SIFT on the GPU through wgpu: the key points and descriptors of a photo as VLFeat and COLMAP
//! compute them on the CPU - the same scale space (first octave -1 by doubling the image, 3
//! levels per octave, sigma 1.6), the same extrema with their quadratic refinement, peak and
//! edge tests, the same orientation histogram and the same 4 x 4 x 8 descriptor laid out as
//! VLFeat's and quantised as COLMAP's (L1 root, x 512, unsigned bytes) - so that points found
//! here match points found there and the alignment sees no difference.
//!
//! Every octave is one submission: the Gaussian levels (separable blurs), the differences, the
//! extrema (one thread per pixel, the refinement in the thread, the keypoints appended with an
//! atomic counter), the orientations (one thread per candidate) and the descriptors (one thread
//! per oriented key point), the results read back once. The image is kept on the device as one
//! buffer per scale space, bound level by level.
use std::collections::HashMap;
use wgpu::util::DeviceExt;

use crate::gpu::{AdapterInfo, rank};

pub const DESC: usize = 128;
const MAX_CANDIDATES: usize = 1 << 17;
const MAX_ORIENTED: usize = 1 << 17;
const SLOT: u64 = 256; // the uniform parameters of one dispatch (dynamic offsets)

#[derive(Clone, Copy, Debug)]
pub struct Options {
    pub first_octave: i32,
    pub num_octaves: u32,
    pub octave_resolution: u32,
    pub peak_threshold: f32,
    pub edge_threshold: f32,
    pub max_orientations: u32,
    pub upright: bool,
    pub max_features: usize,
}

impl Default for Options {
    fn default() -> Self {
        Options { first_octave: -1, num_octaves: 4, octave_resolution: 3, peak_threshold: 0.02 / 3.0, edge_threshold: 10.0,
                  max_orientations: 2, upright: false, max_features: 8192 }
    }
}

#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
struct Params {
    w: u32,
    h: u32,
    radius: u32,
    level: u32,
    peak: f32,
    edge: f32,
    sigma0: f32,
    sigmak: f32,
    s_scale: f32,   // 1 / octave_resolution
    max_cand: u32,
    max_oriented: u32,
    max_orient: u32,
    upright: u32,
    pad0: u32,
    pad1: u32,
    pad2: u32,
    kernel: [[f32; 4]; 12], // 48 weights: radius up to 23
}

const SHADER: &str = r#"
struct Params {
    w: u32, h: u32, radius: u32, level: u32,
    peak: f32, edge: f32, sigma0: f32, sigmak: f32,
    s_scale: f32, max_cand: u32, max_oriented: u32, max_orient: u32,
    upright: u32, pad0: u32, pad1: u32, pad2: u32,
    kernel: array<vec4<f32>, 12>,
};

fn weight(i: u32) -> f32 {
    return p.kernel[i / 4u][i % 4u];
}

// ---------------------------------------------------------------- blurs (separable, clamped)
@group(0) @binding(0) var<storage, read> src: array<f32>;
@group(0) @binding(1) var<storage, read_write> dst: array<f32>;
@group(0) @binding(2) var<uniform> p: Params;

@compute @workgroup_size(16, 16)
fn blur_h(@builtin(global_invocation_id) id: vec3<u32>) {
    let x = id.x; let y = id.y;
    if (x >= p.w || y >= p.h) { return; }
    let r = i32(p.radius);
    var acc = 0.0;
    for (var k = -r; k <= r; k++) {
        let xx = clamp(i32(x) + k, 0, i32(p.w) - 1);
        acc += src[y * p.w + u32(xx)] * weight(u32(abs(k)));
    }
    dst[y * p.w + x] = acc;
}

@compute @workgroup_size(16, 16)
fn blur_v(@builtin(global_invocation_id) id: vec3<u32>) {
    let x = id.x; let y = id.y;
    if (x >= p.w || y >= p.h) { return; }
    let r = i32(p.radius);
    var acc = 0.0;
    for (var k = -r; k <= r; k++) {
        let yy = clamp(i32(y) + k, 0, i32(p.h) - 1);
        acc += src[u32(yy) * p.w + x] * weight(u32(abs(k)));
    }
    dst[y * p.w + x] = acc;
}

// src = the finer octave's level, dst = half size (every other pixel, as VLFeat)
@compute @workgroup_size(16, 16)
fn decimate(@builtin(global_invocation_id) id: vec3<u32>) {
    let x = id.x; let y = id.y;
    if (x >= p.w || y >= p.h) { return; }  // p.w, p.h = the size of dst; src is 2 x
    dst[y * p.w + x] = src[(2u * y) * (2u * p.w) + 2u * x];
}
"#;

const SHADER_DOG: &str = r#"
struct Params {
    w: u32, h: u32, radius: u32, level: u32,
    peak: f32, edge: f32, sigma0: f32, sigmak: f32,
    s_scale: f32, max_cand: u32, max_oriented: u32, max_orient: u32,
    upright: u32, pad0: u32, pad1: u32, pad2: u32,
    kernel: array<vec4<f32>, 12>,
};
@group(0) @binding(0) var<storage, read> a: array<f32>;
@group(0) @binding(1) var<storage, read> b: array<f32>;
@group(0) @binding(2) var<storage, read_write> d: array<f32>;
@group(0) @binding(3) var<uniform> p: Params;

@compute @workgroup_size(16, 16)
fn dog(@builtin(global_invocation_id) id: vec3<u32>) {
    let x = id.x; let y = id.y;
    if (x >= p.w || y >= p.h) { return; }
    let i = y * p.w + x;
    d[i] = b[i] - a[i];
}
"#;

const SHADER_EXTREMA: &str = r#"
struct Params {
    w: u32, h: u32, radius: u32, level: u32,
    peak: f32, edge: f32, sigma0: f32, sigmak: f32,
    s_scale: f32, max_cand: u32, max_oriented: u32, max_orient: u32,
    upright: u32, pad0: u32, pad1: u32, pad2: u32,
    kernel: array<vec4<f32>, 12>,
};
@group(0) @binding(0) var<storage, read> d0: array<f32>;
@group(0) @binding(1) var<storage, read> d1: array<f32>;
@group(0) @binding(2) var<storage, read> d2: array<f32>;
@group(0) @binding(3) var<storage, read_write> cands: array<vec4<f32>>;
@group(0) @binding(4) var<storage, read_write> counts: array<atomic<u32>>;
@group(0) @binding(5) var<uniform> p: Params;

fn at(level: i32, x: i32, y: i32) -> f32 {
    let i = u32(y) * p.w + u32(x);
    if (level == 0) { return d0[i]; }
    if (level == 1) { return d1[i]; }
    return d2[i];
}

// VLFeat's extremum: a pixel of the middle difference above (or below) its 26 neighbours, then
// up to five quadratic refinements that may move it by a pixel, the peak and edge tests
@compute @workgroup_size(16, 16)
fn extrema(@builtin(global_invocation_id) id: vec3<u32>) {
    let w = i32(p.w); let h = i32(p.h);
    var x = i32(id.x) + 1; var y = i32(id.y) + 1;
    if (x > w - 2 || y > h - 2) { return; }
    let v = at(1, x, y);
    if (abs(v) < 0.8 * p.peak) { return; }
    var is_max = v > 0.0; var is_min = v < 0.0;
    for (var l = 0; l < 3; l++) {
        for (var dy = -1; dy <= 1; dy++) {
            for (var dx = -1; dx <= 1; dx++) {
                if (l == 1 && dx == 0 && dy == 0) { continue; }
                let n = at(l, x + dx, y + dy);
                is_max = is_max && (v > n);
                is_min = is_min && (v < n);
            }
        }
    }
    if (!(is_max || is_min)) { return; }
    var bx = 0.0; var by = 0.0; var bs = 0.0;
    var gx = 0.0; var gy = 0.0; var gs = 0.0;
    var dxx = 0.0; var dyy = 0.0; var dxy = 0.0;
    for (var iter = 0; iter < 5; iter++) {
        let c = at(1, x, y);
        gx = 0.5 * (at(1, x + 1, y) - at(1, x - 1, y));
        gy = 0.5 * (at(1, x, y + 1) - at(1, x, y - 1));
        gs = 0.5 * (at(2, x, y) - at(0, x, y));
        dxx = at(1, x + 1, y) + at(1, x - 1, y) - 2.0 * c;
        dyy = at(1, x, y + 1) + at(1, x, y - 1) - 2.0 * c;
        let dss = at(2, x, y) + at(0, x, y) - 2.0 * c;
        dxy = 0.25 * (at(1, x + 1, y + 1) + at(1, x - 1, y - 1) - at(1, x + 1, y - 1) - at(1, x - 1, y + 1));
        let dxs = 0.25 * (at(2, x + 1, y) + at(0, x - 1, y) - at(2, x - 1, y) - at(0, x + 1, y));
        let dys = 0.25 * (at(2, x, y + 1) + at(0, x, y - 1) - at(2, x, y - 1) - at(0, x, y + 1));
        // H b = -g, H symmetric (Cramer)
        let a11 = dxx; let a12 = dxy; let a13 = dxs; let a22 = dyy; let a23 = dys; let a33 = dss;
        let det = a11 * (a22 * a33 - a23 * a23) - a12 * (a12 * a33 - a23 * a13) + a13 * (a12 * a23 - a22 * a13);
        if (abs(det) < 1e-20) { bx = 0.0; by = 0.0; bs = 0.0; } else {
            let r1 = -gx; let r2 = -gy; let r3 = -gs;
            bx = (r1 * (a22 * a33 - a23 * a23) - a12 * (r2 * a33 - a23 * r3) + a13 * (r2 * a23 - a22 * r3)) / det;
            by = (a11 * (r2 * a33 - a23 * r3) - r1 * (a12 * a33 - a23 * a13) + a13 * (a12 * r3 - r2 * a13)) / det;
            bs = (a11 * (a22 * r3 - r2 * a23) - a12 * (a12 * r3 - r2 * a13) + r1 * (a12 * a23 - a22 * a13)) / det;
        }
        var mx = 0; var my = 0;
        if (bx > 0.6 && x < w - 2) { mx = 1; } else if (bx < -0.6 && x > 1) { mx = -1; }
        if (by > 0.6 && y < h - 2) { my = 1; } else if (by < -0.6 && y > 1) { my = -1; }
        if (mx == 0 && my == 0) { break; }
        x += mx; y += my;
    }
    let val = at(1, x, y) + 0.5 * (gx * bx + gy * by + gs * bs);
    let tr = dxx + dyy;
    let det2 = dxx * dyy - dxy * dxy;
    let score = tr * tr / det2;
    let ok = abs(val) > p.peak && score < (p.edge + 1.0) * (p.edge + 1.0) / p.edge && score >= 0.0
             && abs(bx) < 1.5 && abs(by) < 1.5 && abs(bs) < 1.5;
    if (!ok) { return; }
    let k = atomicAdd(&counts[0], 1u);
    if (k >= p.max_cand) { return; }
    // level: the DoG index p.level (1..3) corresponds to the Gaussian level s = level - 1 of the octave
    let s = f32(p.level) - 1.0;
    cands[k] = vec4<f32>(f32(x) + bx, f32(y) + by, s + bs, val);
}
"#;

const SHADER_KEYS: &str = r#"
struct Params {
    w: u32, h: u32, radius: u32, level: u32,
    peak: f32, edge: f32, sigma0: f32, sigmak: f32,
    s_scale: f32, max_cand: u32, max_oriented: u32, max_orient: u32,
    upright: u32, pad0: u32, pad1: u32, pad2: u32,
    kernel: array<vec4<f32>, 12>,
};
// the Gaussian levels of the octave, one after the other (stride p.pad0 = floats per level)
@group(0) @binding(0) var<storage, read> levels: array<f32>;
@group(0) @binding(1) var<storage, read> cands: array<vec4<f32>>;
@group(0) @binding(2) var<storage, read_write> counts: array<atomic<u32>>;
@group(0) @binding(3) var<storage, read_write> kps: array<vec4<f32>>;
@group(0) @binding(4) var<storage, read_write> descs: array<u32>;
@group(0) @binding(5) var<uniform> p: Params;

const PI: f32 = 3.14159265358979;

fn level_of(s: f32) -> u32 {
    // VLFeat: the integer scale of the extremum, within the levels whose gradient exists
    let si = clamp(i32(floor(s + 0.5)), 0, 2);
    return u32(si + 1); // Gaussian level index (s_min = -1 is level 0)
}

fn pix(level: u32, x: i32, y: i32) -> f32 {
    let xx = clamp(x, 0, i32(p.w) - 1);
    let yy = clamp(y, 0, i32(p.h) - 1);
    return levels[level * p.pad0 + u32(yy) * p.w + u32(xx)];
}

fn gradient(level: u32, x: i32, y: i32) -> vec2<f32> {
    // VLFeat: central differences inside, one-sided at the borders
    var gx: f32; var gy: f32;
    if (x == 0) { gx = pix(level, 1, y) - pix(level, 0, y); }
    else if (x == i32(p.w) - 1) { gx = pix(level, x, y) - pix(level, x - 1, y); }
    else { gx = 0.5 * (pix(level, x + 1, y) - pix(level, x - 1, y)); }
    if (y == 0) { gy = pix(level, x, 1) - pix(level, x, 0); }
    else if (y == i32(p.h) - 1) { gy = pix(level, x, y) - pix(level, x, y - 1); }
    else { gy = 0.5 * (pix(level, x, y + 1) - pix(level, x, y - 1)); }
    return vec2<f32>(gx, gy);
}

fn mod2pi(a: f32) -> f32 {
    var r = a;
    while (r > 2.0 * PI) { r -= 2.0 * PI; }
    while (r < 0.0) { r += 2.0 * PI; }
    return r;
}

@compute @workgroup_size(64)
fn orient(@builtin(global_invocation_id) id: vec3<u32>) {
    let k = id.x;
    if (k >= atomicLoad(&counts[0]) || k >= p.max_cand) { return; }
    let c = cands[k];
    let x = c.x; let y = c.y; let s = c.z;
    let sigma = p.sigma0 * pow(2.0, s * p.s_scale);  // in the pixels of the octave
    let level = level_of(s);
    if (p.upright == 1u) {
        let j = atomicAdd(&counts[1], 1u);
        if (j < p.max_oriented) { kps[j] = vec4<f32>(x, y, sigma, 0.0); }
        return;
    }
    let winf = 1.5;
    let xi = i32(x + 0.5); let yi = i32(y + 0.5);
    let W = i32(floor(3.0 * winf * sigma));
    let wsig = winf * sigma;
    var hist: array<f32, 36>;
    for (var i = 0; i < 36; i++) { hist[i] = 0.0; }
    let w = i32(p.w); let h = i32(p.h);
    for (var ys = max(-W, 1 - yi); ys <= min(W, h - 2 - yi); ys++) {
        for (var xs = max(-W, 1 - xi); xs <= min(W, w - 2 - xi); xs++) {
            let dx = f32(xi + xs) - x; let dy = f32(yi + ys) - y;
            let r2 = dx * dx + dy * dy;
            if (r2 >= f32(W * W) + 0.6) { continue; }
            let g = gradient(level, xi + xs, yi + ys);
            let m = length(g);
            let ang = mod2pi(atan2(g.y, g.x));
            let wgt = exp(-r2 / (2.0 * wsig * wsig));
            let fbin = 36.0 * ang / (2.0 * PI);
            let bin = i32(floor(fbin - 0.5));
            let rbin = fbin - f32(bin) - 0.5;
            hist[(bin + 36) % 36] += (1.0 - rbin) * m * wgt;
            hist[(bin + 1 + 36) % 36] += rbin * m * wgt;
        }
    }
    // smoothed six times with [1 1 1]/3, in place as VLFeat
    for (var it = 0; it < 6; it++) {
        var prev = hist[35];
        let first = hist[0];
        for (var i = 0; i < 36; i++) {
            let nxt = select(hist[i + 1], first, i == 35);
            let cur = hist[i];
            hist[i] = (prev + cur + nxt) / 3.0;
            prev = cur;
        }
    }
    var maxh = 0.0;
    for (var i = 0; i < 36; i++) { maxh = max(maxh, hist[i]); }
    // the peaks, the strongest first (up to max_orient)
    var best_th: array<f32, 4>; var best_h: array<f32, 4>; var n = 0u;
    for (var i = 0; i < 36; i++) {
        let h0 = hist[i]; let hm = hist[(i - 1 + 36) % 36]; let hp = hist[(i + 1) % 36];
        if (h0 > 0.8 * maxh && h0 > hm && h0 > hp) {
            let di = -0.5 * (hp - hm) / (hp + hm - 2.0 * h0);
            let th = 2.0 * PI * (f32(i) + di + 0.5) / 36.0;
            // insert by height
            var j = n;
            while (j > 0u && best_h[j - 1u] < h0) {
                if (j < 4u) { best_h[j] = best_h[j - 1u]; best_th[j] = best_th[j - 1u]; }
                j -= 1u;
            }
            if (j < 4u) { best_h[j] = h0; best_th[j] = th; }
            n = min(n + 1u, 4u);
        }
    }
    let take = min(n, p.max_orient);
    for (var i = 0u; i < take; i++) {
        let j = atomicAdd(&counts[1], 1u);
        if (j < p.max_oriented) { kps[j] = vec4<f32>(x, y, sigma, best_th[i]); }
    }
}

@compute @workgroup_size(64)
fn descr(@builtin(global_invocation_id) id: vec3<u32>) {
    let k = id.x;
    if (k >= atomicLoad(&counts[1]) || k >= p.max_oriented) { return; }
    let kp = kps[k];
    let x = kp.x; let y = kp.y; let sigma = kp.z; let th = kp.w;
    let s = log2(max(sigma / p.sigma0, 1e-6)) / p.s_scale;
    let level = level_of(s);
    let magnif = 3.0;
    let SBP = magnif * sigma;
    let W = i32(floor(sqrt(2.0) * SBP * 2.5 + 0.5));
    let xi = i32(x + 0.5); let yi = i32(y + 0.5);
    let st0 = sin(th); let ct0 = cos(th);
    let wsigma = 2.0;
    var d: array<f32, 128>;
    for (var i = 0; i < 128; i++) { d[i] = 0.0; }
    let w = i32(p.w); let h = i32(p.h);
    for (var dyi = max(-W, 1 - yi); dyi <= min(W, h - 2 - yi); dyi++) {
        for (var dxi = max(-W, 1 - xi); dxi <= min(W, w - 2 - xi); dxi++) {
            let g = gradient(level, xi + dxi, yi + dyi);
            let m = length(g);
            let ang = mod2pi(atan2(g.y, g.x));
            let dx = f32(xi + dxi) - x; let dy = f32(yi + dyi) - y;
            let nx = (ct0 * dx + st0 * dy) / SBP;
            let ny = (-st0 * dx + ct0 * dy) / SBP;
            let nt = 8.0 * mod2pi(th - ang) / (2.0 * PI);  // VLFeat's bins run this way round
            let win = exp(-(nx * nx + ny * ny) / (2.0 * wsigma * wsigma));
            let binx = i32(floor(nx - 0.5)); let biny = i32(floor(ny - 0.5)); let bint = i32(floor(nt));
            let rbinx = nx - (f32(binx) + 0.5); let rbiny = ny - (f32(biny) + 0.5); let rbint = nt - f32(bint);
            for (var dbinx = 0; dbinx < 2; dbinx++) {
                for (var dbiny = 0; dbiny < 2; dbiny++) {
                    for (var dbint = 0; dbint < 2; dbint++) {
                        let bx = binx + dbinx; let by = biny + dbiny;
                        if (bx >= -2 && bx < 2 && by >= -2 && by < 2) {
                            let wgt = win * m * abs(1.0 - f32(dbinx) - rbinx) * abs(1.0 - f32(dbiny) - rbiny) * abs(1.0 - f32(dbint) - rbint);
                            let bt = (bint + dbint) % 8;
                            d[(by + 2) * 32 + (bx + 2) * 8 + bt] += wgt;
                        }
                    }
                }
            }
        }
    }
    // VLFeat: unit L2, clipped at 0.2, unit again; COLMAP: L1 root, x 512, bytes
    var n2 = 0.0;
    for (var i = 0; i < 128; i++) { n2 += d[i] * d[i]; }
    n2 = max(sqrt(n2), 1e-12);
    for (var i = 0; i < 128; i++) { d[i] = min(d[i] / n2, 0.2); }
    n2 = 0.0;
    for (var i = 0; i < 128; i++) { n2 += d[i] * d[i]; }
    n2 = max(sqrt(n2), 1e-12);
    var n1 = 0.0;
    for (var i = 0; i < 128; i++) { d[i] = d[i] / n2; n1 += d[i]; }
    n1 = max(n1, 1e-12);
    for (var q = 0u; q < 32u; q++) {
        var word = 0u;
        for (var b = 0u; b < 4u; b++) {
            let v = sqrt(d[q * 4u + b] / n1) * 512.0;
            let u = u32(clamp(round(v), 0.0, 255.0));
            word |= u << (8u * b);
        }
        descs[k * 32u + q] = word;
    }
}
"#;

fn gaussian(sigma: f32) -> (u32, [[f32; 4]; 12]) {
    let radius = (4.0 * sigma).ceil().max(1.0) as usize;
    let radius = radius.min(47);
    let mut w = vec![0.0f32; radius + 1];
    let mut total = 0.0f32;
    for (i, v) in w.iter_mut().enumerate() {
        *v = (-((i * i) as f32) / (2.0 * sigma * sigma)).exp();
        total += if i == 0 { *v } else { 2.0 * *v };
    }
    let mut k = [[0.0f32; 4]; 12];
    for (i, v) in w.iter().enumerate() {
        k[i / 4][i % 4] = v / total;
    }
    (radius as u32, k)
}

pub struct Sift {
    #[allow(dead_code)]
    pub info: AdapterInfo,
    device: wgpu::Device,
    queue: wgpu::Queue,
    blur_h: wgpu::ComputePipeline,
    blur_v: wgpu::ComputePipeline,
    decimate: wgpu::ComputePipeline,
    dog: wgpu::ComputePipeline,
    extrema: wgpu::ComputePipeline,
    orient: wgpu::ComputePipeline,
    descr: wgpu::ComputePipeline,
    l_blur: wgpu::BindGroupLayout,
    l_dog: wgpu::BindGroupLayout,
    l_extrema: wgpu::BindGroupLayout,
    l_keys: wgpu::BindGroupLayout,
    max_binding: u64,
}

fn storage(binding: u32, read_only: bool) -> wgpu::BindGroupLayoutEntry {
    wgpu::BindGroupLayoutEntry {
        binding, visibility: wgpu::ShaderStages::COMPUTE, count: None,
        ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only }, has_dynamic_offset: false, min_binding_size: None },
    }
}

fn uniform(binding: u32) -> wgpu::BindGroupLayoutEntry {
    wgpu::BindGroupLayoutEntry {
        binding, visibility: wgpu::ShaderStages::COMPUTE, count: None,
        ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Uniform, has_dynamic_offset: true,
                                        min_binding_size: Some(std::num::NonZeroU64::new(std::mem::size_of::<Params>() as u64).unwrap()) },
    }
}

impl Sift {
    pub fn new(index: Option<usize>) -> Result<Sift, String> {
        let inst = wgpu::Instance::default();
        let mut all = inst.enumerate_adapters(wgpu::Backends::all());
        if all.is_empty() {
            return Err("no GPU adapter (wgpu found no Metal, Vulkan or DirectX 12 device)".into());
        }
        let chosen = match index {
            Some(i) if i < all.len() => i,
            Some(i) => return Err(format!("GPU adapter {i} does not exist: {} found", all.len())),
            None => (0..all.len()).min_by_key(|&i| (rank(all[i].get_info().device_type), i)).unwrap(),
        };
        let a = &all[chosen];
        let ainfo = a.get_info();
        let info = AdapterInfo { index: chosen, name: ainfo.name.clone(), backend: format!("{:?}", ainfo.backend),
                                 device_type: format!("{:?}", ainfo.device_type) };
        let adapter = all.swap_remove(chosen);
        let limits = adapter.limits();
        let max_binding = limits.max_storage_buffer_binding_size as u64;
        let (device, queue) = pollster::block_on(adapter.request_device(
            &wgpu::DeviceDescriptor { label: Some("ppg_core sift"), required_limits: limits, ..Default::default() }, None))
            .map_err(|e| format!("{}: {e}", info.name))?;
        let module = |label: &str, src: &str| device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some(label), source: wgpu::ShaderSource::Wgsl(src.into()) });
        let m_blur = module("sift_blur", SHADER);
        let m_dog = module("sift_dog", SHADER_DOG);
        let m_ext = module("sift_extrema", SHADER_EXTREMA);
        let m_keys = module("sift_keys", SHADER_KEYS);
        let l_blur = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor { label: None, entries: &[storage(0, true), storage(1, false), uniform(2)] });
        let l_dog = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor { label: None, entries: &[storage(0, true), storage(1, true), storage(2, false), uniform(3)] });
        let l_extrema = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor { label: None, entries: &[storage(0, true), storage(1, true), storage(2, true), storage(3, false), storage(4, false), uniform(5)] });
        let l_keys = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor { label: None, entries: &[storage(0, true), storage(1, true), storage(2, false), storage(3, false), storage(4, false), uniform(5)] });
        let pipe = |layout: &wgpu::BindGroupLayout, module: &wgpu::ShaderModule, entry: &str| {
            let pl = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor { label: None, bind_group_layouts: &[layout], push_constant_ranges: &[] });
            device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
                label: Some(entry), layout: Some(&pl), module, entry_point: Some(entry), compilation_options: Default::default(), cache: None })
        };
        Ok(Sift {
            blur_h: pipe(&l_blur, &m_blur, "blur_h"), blur_v: pipe(&l_blur, &m_blur, "blur_v"), decimate: pipe(&l_blur, &m_blur, "decimate"),
            dog: pipe(&l_dog, &m_dog, "dog"), extrema: pipe(&l_extrema, &m_ext, "extrema"),
            orient: pipe(&l_keys, &m_keys, "orient"), descr: pipe(&l_keys, &m_keys, "descr"),
            l_blur, l_dog, l_extrema, l_keys, info, device, queue, max_binding,
        })
    }

    fn buffer(&self, size: u64, label: &str, extra: wgpu::BufferUsages) -> wgpu::Buffer {
        self.device.create_buffer(&wgpu::BufferDescriptor { label: Some(label), size: size.max(16), usage: wgpu::BufferUsages::STORAGE | extra, mapped_at_creation: false })
    }

    /// Key points ``(x, y, sigma, orientation)`` in the pixels of ``image`` (VLFeat's convention:
    /// the centre of the first pixel at (0, 0)) and their 128-byte descriptors, ``image`` grey
    /// values in [0, 1], row-major ``w x h``.
    pub fn extract(&self, image: &[f32], w: usize, h: usize, o: &Options) -> Result<(Vec<[f32; 4]>, Vec<u8>), String> {
        if image.len() != w * h || w < 8 || h < 8 {
            return Err("image: w x h float values, at least 8 x 8".into());
        }
        let s_res = o.octave_resolution.max(1) as usize;
        let sigma0 = 1.6f32 * 2f32.powf(1.0 / s_res as f32);
        let sigmak = 2f32.powf(1.0 / s_res as f32);
        let sigman = 0.5f32;
        let dsigma0 = sigma0 * (1.0 - 1.0 / (sigmak * sigmak)).sqrt();
        let n_levels = s_res + 3;
        let s_min: i32 = -1;
        // the base of the first octave on the CPU: doubled (as VLFeat), copied, or halved
        let (mut bw, mut bh, mut base) = match o.first_octave {
            -1 => (2 * w, 2 * h, upsample2(image, w, h)),
            0 => (w, h, image.to_vec()),
            n if n > 0 => {
                let mut cur = image.to_vec();
                let (mut cw, mut ch) = (w, h);
                for _ in 0..n { let (nw, nh) = (cw / 2, ch / 2); cur = decimate_cpu(&cur, cw, nw, nh); cw = nw; ch = nh; }
                (cw, ch, cur)
            }
            _ => return Err("first_octave: -1, 0 or positive".into()),
        };
        let mut keypoints: Vec<[f32; 4]> = Vec::new();
        let mut descriptors: Vec<u8> = Vec::new();
        let mut octave = o.first_octave;
        let mut prev_levels: Option<(wgpu::Buffer, usize, usize, u64)> = None; // the finer octave's levels (buffer, w, h, stride)
        for oct in 0..o.num_octaves {
            if bw < 8 || bh < 8 { break; }
            let xper = 2f32.powi(octave);
            let stride_f = ((bw * bh * 4 + 255) / 256) * 64; // floats per level, 256-byte aligned
            let level_bytes = (stride_f * 4) as u64;
            if level_bytes > self.max_binding {
                return Err(format!("the image is too large for this GPU ({} bytes per level, {} allowed)", level_bytes, self.max_binding));
            }
            let levels = self.buffer(level_bytes * n_levels as u64, "levels", wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::COPY_SRC);
            let dogs = self.buffer(level_bytes * (n_levels - 1) as u64, "dogs", wgpu::BufferUsages::empty());
            let tmp = self.buffer(level_bytes, "tmp", wgpu::BufferUsages::empty());
            let cands = self.buffer((MAX_CANDIDATES * 16) as u64, "cands", wgpu::BufferUsages::empty());
            let kps = self.buffer((MAX_ORIENTED * 16) as u64, "kps", wgpu::BufferUsages::COPY_SRC);
            let descs = self.buffer((MAX_ORIENTED * DESC) as u64, "descs", wgpu::BufferUsages::COPY_SRC);
            let counts = self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor { label: Some("counts"), contents: bytemuck::cast_slice(&[0u32; 4]), usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC });
            // the parameters of every dispatch, in slots
            let mut slots: Vec<Params> = Vec::new();
            let mut param = |w_: usize, h_: usize, radius: u32, level: u32, kernel: [[f32; 4]; 12]| -> u32 {
                slots.push(Params { w: w_ as u32, h: h_ as u32, radius, level, peak: o.peak_threshold, edge: o.edge_threshold,
                                    sigma0, sigmak, s_scale: 1.0 / s_res as f32, max_cand: MAX_CANDIDATES as u32,
                                    max_oriented: MAX_ORIENTED as u32, max_orient: o.max_orientations.max(1),
                                    upright: o.upright as u32, pad0: stride_f as u32, pad1: 0, pad2: 0, kernel });
                (slots.len() - 1) as u32
            };
            let zero = [[0.0f32; 4]; 12];
            // the first level: the base (uploaded, or decimated from the finer octave) smoothed to sigma0 * sigmak^s_min
            let sa = sigma0 * sigmak.powi(s_min);
            let sb = if oct == 0 { sigman * 2f32.powi(-octave) } else { sigma0 * sigmak.powi((s_res as i32 + s_min) - s_res as i32) };
            let first_blur = if sa > sb { Some(((sa * sa - sb * sb).sqrt(), gaussian((sa * sa - sb * sb).sqrt()))) } else { None };
            let mut dispatches: Vec<(u8, u32, Vec<(wgpu::Buffer, u64, u64)>, (u32, u32))> = Vec::new(); // kind, slot, bindings (buffer, offset, size), groups
            let img_groups = ((bw as u32 + 15) / 16, (bh as u32 + 15) / 16);
            let lv = |i: usize| (levels.clone(), i as u64 * level_bytes, level_bytes);
            let dg = |i: usize| (dogs.clone(), i as u64 * level_bytes, level_bytes);
            let whole = |b: &wgpu::Buffer| (b.clone(), 0u64, b.size());
            if oct == 0 {
                self.queue.write_buffer(&levels, 0, bytemuck::cast_slice(&base));
            } else if let Some((pl, pw, _ph, pstride)) = prev_levels.take() {
                // decimate level s_min + S of the finer octave into level 0 of this one
                let src_level = (s_res as i32) as usize; // index of s = s_min + S = S - 1 -> level S? (s - s_min) = S
                let s_best = (s_min + s_res as i32).min(s_min + n_levels as i32 - 1) - s_min; // level index
                let _ = src_level;
                let sp = param(bw, bh, 0, 0, zero);
                dispatches.push((2, sp, vec![(pl.clone(), s_best as u64 * pstride, pstride), lv(0)], img_groups));
                let _ = pw;
            }
            if let Some((_s, (radius, kernel))) = first_blur {
                let sp = param(bw, bh, radius, 0, kernel);
                dispatches.push((0, sp, vec![lv(0), whole(&tmp)], img_groups));
                dispatches.push((1, sp, vec![whole(&tmp), lv(0)], img_groups));
            }
            for s in 1..n_levels {
                let sd = dsigma0 * sigmak.powi(s as i32 + s_min);
                let (radius, kernel) = gaussian(sd);
                let sp = param(bw, bh, radius, s as u32, kernel);
                dispatches.push((0, sp, vec![lv(s - 1), whole(&tmp)], img_groups));
                dispatches.push((1, sp, vec![whole(&tmp), lv(s)], img_groups));
            }
            for d in 0..n_levels - 1 {
                let sp = param(bw, bh, 0, d as u32, zero);
                dispatches.push((3, sp, vec![lv(d), lv(d + 1), dg(d)], img_groups));
            }
            for d in 1..n_levels - 2 {
                let sp = param(bw, bh, 0, d as u32, zero);
                dispatches.push((4, sp, vec![dg(d - 1), dg(d), dg(d + 1), whole(&cands), whole(&counts)], (((bw - 2) as u32 + 15) / 16, ((bh - 2) as u32 + 15) / 16)));
            }
            let sp_keys = param(bw, bh, 0, 0, zero);
            let key_groups = ((MAX_CANDIDATES as u32 + 63) / 64, 1);
            dispatches.push((5, sp_keys, vec![whole(&levels), whole(&cands), whole(&counts), whole(&kps), whole(&descs)], key_groups));
            dispatches.push((6, sp_keys, vec![whole(&levels), whole(&cands), whole(&counts), whole(&kps), whole(&descs)], ((MAX_ORIENTED as u32 + 63) / 64, 1)));
            // upload the slots and run
            let mut raw = vec![0u8; slots.len() * SLOT as usize];
            for (i, s) in slots.iter().enumerate() {
                let b = bytemuck::bytes_of(s);
                raw[i * SLOT as usize..i * SLOT as usize + b.len()].copy_from_slice(b);
            }
            let params = self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor { label: Some("sift params"), contents: &raw, usage: wgpu::BufferUsages::UNIFORM });
            let mut enc = self.device.create_command_encoder(&Default::default());
            {
                let mut pass = enc.begin_compute_pass(&Default::default());
                for (kind, slot, binds, groups) in &dispatches {
                    let (pipeline, layout) = match kind {
                        0 => (&self.blur_h, &self.l_blur), 1 => (&self.blur_v, &self.l_blur), 2 => (&self.decimate, &self.l_blur),
                        3 => (&self.dog, &self.l_dog), 4 => (&self.extrema, &self.l_extrema),
                        5 => (&self.orient, &self.l_keys), _ => (&self.descr, &self.l_keys),
                    };
                    let mut entries: Vec<wgpu::BindGroupEntry> = binds.iter().enumerate().map(|(i, (b, off, size))| wgpu::BindGroupEntry {
                        binding: i as u32, resource: wgpu::BindingResource::Buffer(wgpu::BufferBinding { buffer: b, offset: *off, size: std::num::NonZeroU64::new(*size) }) }).collect();
                    entries.push(wgpu::BindGroupEntry { binding: binds.len() as u32, resource: wgpu::BindingResource::Buffer(wgpu::BufferBinding { buffer: &params, offset: 0, size: std::num::NonZeroU64::new(std::mem::size_of::<Params>() as u64) }) });
                    let bg = self.device.create_bind_group(&wgpu::BindGroupDescriptor { label: None, layout, entries: &entries });
                    pass.set_pipeline(pipeline);
                    pass.set_bind_group(0, &bg, &[*slot * SLOT as u32]);
                    pass.dispatch_workgroups(groups.0, groups.1, 1);
                }
            }
            // read back the counts, the key points and the descriptors
            let staging = |size: u64, label: &str| self.device.create_buffer(&wgpu::BufferDescriptor {
                label: Some(label), size, usage: wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST, mapped_at_creation: false });
            let st_counts = staging(16, "st counts");
            let st_kps = staging((MAX_ORIENTED * 16) as u64, "st kps");
            let st_descs = staging((MAX_ORIENTED * DESC) as u64, "st descs");
            enc.copy_buffer_to_buffer(&counts, 0, &st_counts, 0, 16);
            enc.copy_buffer_to_buffer(&kps, 0, &st_kps, 0, (MAX_ORIENTED * 16) as u64);
            enc.copy_buffer_to_buffer(&descs, 0, &st_descs, 0, (MAX_ORIENTED * DESC) as u64);
            self.queue.submit(Some(enc.finish()));
            let read = |b: &wgpu::Buffer| -> Result<Vec<u8>, String> {
                let slice = b.slice(..);
                let (tx, rx) = std::sync::mpsc::channel();
                slice.map_async(wgpu::MapMode::Read, move |r| { let _ = tx.send(r); });
                self.device.poll(wgpu::Maintain::Wait);
                rx.recv().map_err(|e| e.to_string())?.map_err(|e| e.to_string())?;
                let v = slice.get_mapped_range().to_vec();
                b.unmap();
                Ok(v)
            };
            let c: Vec<u32> = bytemuck::cast_slice(&read(&st_counts)?).to_vec();
            let n_or = (c[1] as usize).min(MAX_ORIENTED);
            if n_or > 0 {
                let kp_raw = read(&st_kps)?;
                let kp: &[[f32; 4]] = bytemuck::cast_slice(&kp_raw[..n_or * 16]);
                let ds = read(&st_descs)?;
                for (i, k) in kp.iter().enumerate() {
                    keypoints.push([k[0] * xper, k[1] * xper, k[2] * xper, k[3]]);
                    descriptors.extend_from_slice(&ds[i * DESC..(i + 1) * DESC]);
                }
            }
            prev_levels = Some((levels, bw, bh, level_bytes));
            base = Vec::new();
            bw /= 2;
            bh /= 2;
            octave += 1;
        }
        // COLMAP keeps the key points of the largest scales when there are too many
        if keypoints.len() > o.max_features {
            let mut idx: Vec<usize> = (0..keypoints.len()).collect();
            idx.sort_by(|&a, &b| keypoints[b][2].partial_cmp(&keypoints[a][2]).unwrap_or(std::cmp::Ordering::Equal));
            idx.truncate(o.max_features);
            idx.sort_unstable();
            let kp2: Vec<[f32; 4]> = idx.iter().map(|&i| keypoints[i]).collect();
            let mut d2 = Vec::with_capacity(idx.len() * DESC);
            for &i in &idx { d2.extend_from_slice(&descriptors[i * DESC..(i + 1) * DESC]); }
            keypoints = kp2;
            descriptors = d2;
        }
        // the same order every time (the atomics of the kernels give any): by row, column, scale, orientation
        let mut idx: Vec<usize> = (0..keypoints.len()).collect();
        idx.sort_by(|&a, &b| {
            let (ka, kb) = (&keypoints[a], &keypoints[b]);
            [1usize, 0, 2, 3].iter().map(|&i| ka[i].partial_cmp(&kb[i]).unwrap_or(std::cmp::Ordering::Equal))
                .find(|o| *o != std::cmp::Ordering::Equal).unwrap_or(std::cmp::Ordering::Equal)
        });
        let kp2: Vec<[f32; 4]> = idx.iter().map(|&i| keypoints[i]).collect();
        let mut d2 = Vec::with_capacity(idx.len() * DESC);
        for &i in &idx { d2.extend_from_slice(&descriptors[i * DESC..(i + 1) * DESC]); }
        Ok((kp2, d2))
    }
}

/// VLFeat's doubling: a pixel copied, the one between two the mean (rows, then columns).
fn upsample2(img: &[f32], w: usize, h: usize) -> Vec<f32> {
    let (w2, h2) = (2 * w, 2 * h);
    let mut rows = vec![0.0f32; w2 * h];
    for y in 0..h {
        for x in 0..w {
            let v = img[y * w + x];
            let n = img[y * w + (x + 1).min(w - 1)];
            rows[y * w2 + 2 * x] = v;
            rows[y * w2 + 2 * x + 1] = 0.5 * (v + n);
        }
    }
    let mut out = vec![0.0f32; w2 * h2];
    for y in 0..h {
        let yn = (y + 1).min(h - 1);
        for x in 0..w2 {
            let v = rows[y * w2 + x];
            let n = rows[yn * w2 + x];
            out[(2 * y) * w2 + x] = v;
            out[(2 * y + 1) * w2 + x] = 0.5 * (v + n);
        }
    }
    out
}

fn decimate_cpu(img: &[f32], w: usize, nw: usize, nh: usize) -> Vec<f32> {
    let mut out = vec![0.0f32; nw * nh];
    for y in 0..nh {
        for x in 0..nw {
            out[y * nw + x] = img[(2 * y) * w + 2 * x];
        }
    }
    out
}

/// Sifts per adapter, created on first use.
pub fn sifts() -> &'static std::sync::Mutex<HashMap<i64, std::sync::Arc<Sift>>> {
    static S: std::sync::OnceLock<std::sync::Mutex<HashMap<i64, std::sync::Arc<Sift>>>> = std::sync::OnceLock::new();
    S.get_or_init(|| std::sync::Mutex::new(HashMap::new()))
}

pub fn sift_for(device: i64) -> Result<std::sync::Arc<Sift>, String> {
    let mut all = sifts().lock().map_err(|e| e.to_string())?;
    if let Some(s) = all.get(&device) {
        return Ok(s.clone());
    }
    let s = std::sync::Arc::new(Sift::new(if device < 0 { None } else { Some(device as usize) })?);
    all.insert(device, s.clone());
    Ok(s)
}
