//! The GPU through wgpu - Metal on macOS, Vulkan on Linux and Windows (also on ARM), DirectX 12 on
//! Windows - with kernels written once in WGSL: the matching of SIFT descriptors, the biggest
//! cost of the alignment of many photos.
//!
//! The descriptors are COLMAP's: 128 unsigned bytes each (an L2 norm of 512 before the
//! quantisation), so a dot product is an integer below 2^18 and exact in f32. One pass over a
//! pair of photos gives, for every descriptor of A, its best and second-best dot product over B
//! (the rows) and, through partials per tile merged by a second kernel, the same for every
//! descriptor of B over A (the columns): COLMAP's ratio test, distance test and cross check come
//! out of the one pass. A workgroup of 16 x 16 threads takes 128 descriptors of A against every
//! tile of 64 of B, 8 x 4 dot products per thread, the tiles transposed in workgroup memory
//! (word-major) so that a step over one word of the descriptors reads consecutive entries.
//! Apple M4 Pro: 2.06 TFLOP/s, 198 ms for 40 000 x 40 000 descriptors, both directions.
use std::collections::{HashMap, VecDeque};

use wgpu::util::DeviceExt;

pub const DIM: usize = 128;
const TILE_A: usize = 128;
const CACHE_BYTES: usize = 768 << 20; // descriptors kept on the GPU across the pairs of the same photos

#[derive(Clone, Debug)]
pub struct AdapterInfo {
    pub index: usize,
    pub name: String,
    pub backend: String,
    pub device_type: String,
}

#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
struct Params {
    n_a: u32,
    n_b: u32,
    tiles_a: u32,
    _pad: u32,
}

/// The two kernels, generated: the inner loop is written out in full (32 accumulators in
/// registers; arrays indexed in loops are kept in memory by some compilers).
fn shader() -> String {
    let mut s = String::new();
    s.push_str(r#"
struct Params { n_a: u32, n_b: u32, tiles_a: u32, pad: u32 };
@group(0) @binding(0) var<storage, read> a: array<u32>;
@group(0) @binding(1) var<storage, read> b: array<u32>;
@group(0) @binding(2) var<storage, read_write> out: array<vec4<u32>>;
@group(0) @binding(3) var<storage, read_write> col_part: array<vec2<u32>>;
@group(0) @binding(4) var<uniform> p: Params;

var<workgroup> a_tile: array<u32, 4096>;
var<workgroup> b_tile: array<u32, 2048>;
var<workgroup> scratch: array<vec2<u32>, 1024>;

fn unpack(v: u32) -> vec4<f32> {
    return vec4<f32>(f32(v & 255u), f32((v >> 8u) & 255u), f32((v >> 16u) & 255u), f32(v >> 24u));
}

@compute @workgroup_size(16, 16)
fn gemm(@builtin(workgroup_id) wg: vec3<u32>, @builtin(local_invocation_id) lid: vec3<u32>,
        @builtin(local_invocation_index) li: u32) {
    let a0 = wg.x * 128u;
    for (var e = 0u; e < 16u; e++) {
        let idx = e * 256u + li;
        let k = idx / 128u;
        let row = idx % 128u;
        let g = a0 + row;
        a_tile[idx] = select(0u, a[g * 32u + k], g < p.n_a);
    }
    workgroupBarrier();
    let tr = lid.y * 8u;
    let tc = lid.x * 4u;
"#);
    for r in 0..8 {
        s.push_str(&format!("    var rb1_{r}: f32 = -1.0; var rb2_{r}: f32 = -1.0; var ri_{r}: u32 = 0u;\n"));
    }
    s.push_str(r#"
    let tiles_b = (p.n_b + 63u) / 64u;
    for (var t = 0u; t < tiles_b; t++) {
        let b0 = t * 64u;
        {
            let col = li / 4u;
            let k0 = (li % 4u) * 8u;
            let g = b0 + col;
            for (var e = 0u; e < 8u; e++) {
                b_tile[(k0 + e) * 64u + col] = select(0u, b[g * 32u + k0 + e], g < p.n_b);
            }
        }
        workgroupBarrier();
"#);
    for r in 0..8 {
        for c in 0..4 {
            s.push_str(&format!("        var acc_{r}{c}: f32 = 0.0;\n"));
        }
    }
    s.push_str("        for (var k = 0u; k < 32u; k++) {\n            let kb = k * 64u + tc;\n            let ka = k * 128u + tr;\n");
    for c in 0..4 {
        s.push_str(&format!("            let bv{c} = unpack(b_tile[kb + {c}u]);\n"));
    }
    for r in 0..8 {
        s.push_str(&format!("            let av{r} = unpack(a_tile[ka + {r}u]);\n"));
    }
    for r in 0..8 {
        for c in 0..4 {
            s.push_str(&format!("            acc_{r}{c} += dot(av{r}, bv{c});\n"));
        }
    }
    s.push_str("        }\n");
    for c in 0..4 {
        s.push_str(&format!("        let jc{c} = b0 + tc + {c}u;\n"));
    }
    for r in 0..8 {
        for c in 0..4 {
            s.push_str(&format!(
                "        if (jc{c} < p.n_b) {{ if (acc_{r}{c} > rb1_{r}) {{ rb2_{r} = rb1_{r}; rb1_{r} = acc_{r}{c}; ri_{r} = jc{c}; }} else if (acc_{r}{c} > rb2_{r}) {{ rb2_{r} = acc_{r}{c}; }} }}\n"));
        }
    }
    for c in 0..4 {
        s.push_str(&format!("        var cb1_{c}: f32 = -1.0; var cb2_{c}: f32 = -1.0; var cr_{c}: u32 = 0u;\n"));
        for r in 0..8 {
            s.push_str(&format!(
                "        if (acc_{r}{c} > cb1_{c}) {{ cb2_{c} = cb1_{c}; cb1_{c} = acc_{r}{c}; cr_{c} = tr + {r}u; }} else if (acc_{r}{c} > cb2_{c}) {{ cb2_{c} = acc_{r}{c}; }}\n"));
        }
        s.push_str(&format!(
            "        scratch[lid.y * 64u + tc + {c}u] = vec2<u32>((u32(max(cb1_{c}, 0.0)) << 8u) | cr_{c}, u32(max(cb2_{c}, 0.0)));\n"));
    }
    s.push_str(r#"
        workgroupBarrier();
        if (li < 64u) {
            let col = li;
            var best: u32 = 0u;
            var second: u32 = 0u;
            for (var y = 0u; y < 16u; y++) {
                let e = scratch[y * 64u + col];
                let sc = e.x >> 8u;
                if (sc > (best >> 8u)) { second = max(best >> 8u, e.y); best = e.x; }
                else { second = max(second, max(sc, e.y)); }
            }
            let j = b0 + col;
            if (j < p.n_b) { col_part[wg.x * p.n_b + j] = vec2<u32>(best, second); }
        }
        workgroupBarrier();
    }
"#);
    for h in 0..2 {
        s.push_str(&format!("    {{\n        workgroupBarrier();\n        if (lid.y / 8u == {h}u) {{\n            let base = lid.x * 64u + (tr - {}u);\n", h * 64));
        for r in 0..8 {
            s.push_str(&format!("            scratch[base + {r}u] = vec2<u32>(u32(max(rb1_{r}, 0.0)), ri_{r});\n"));
        }
        s.push_str(r#"        }
        workgroupBarrier();
        var w_x: u32 = 0u; var w_best: u32 = 0u; var w_col: u32 = 0u; var sob: u32 = 0u;
        if (li < 64u) {
            for (var x = 0u; x < 16u; x++) {
                let e = scratch[x * 64u + li];
                if (e.x > w_best) { sob = w_best; w_best = e.x; w_col = e.y; w_x = x; } else { sob = max(sob, e.x); }
            }
        }
        workgroupBarrier();
"#);
        s.push_str(&format!("        if (lid.y / 8u == {h}u) {{\n            let base = lid.x * 64u + (tr - {}u);\n", h * 64));
        for r in 0..8 {
            s.push_str(&format!("            scratch[base + {r}u] = vec2<u32>(u32(max(rb2_{r}, 0.0)), 0u);\n"));
        }
        s.push_str(&format!(r#"        }}
        workgroupBarrier();
        if (li < 64u) {{
            let row = a0 + {}u + li;
            if (row < p.n_a) {{
                let second = max(sob, scratch[w_x * 64u + li].x);
                out[row] = vec4<u32>(w_best, w_col, second, 0u);
            }}
        }}
    }}
"#, h * 64));
    }
    s.push_str(r#"}

@compute @workgroup_size(256)
fn merge(@builtin(global_invocation_id) gid: vec3<u32>) {
    let j = gid.x;
    if (j >= p.n_b) { return; }
    var best: u32 = 0u;
    var row: u32 = 0u;
    var second: u32 = 0u;
    for (var t = 0u; t < p.tiles_a; t++) {
        let e = col_part[t * p.n_b + j];
        let sc = e.x >> 8u;
        if (sc > best) { second = max(best, e.y); best = sc; row = t * 128u + (e.x & 255u); }
        else { second = max(second, max(sc, e.y)); }
    }
    out[p.n_a + j] = vec4<u32>(best, row, second, 0u);
}
"#);
    s
}

fn instance() -> wgpu::Instance {
    wgpu::Instance::default()
}

fn describe(a: &wgpu::Adapter, index: usize) -> AdapterInfo {
    let info = a.get_info();
    AdapterInfo { index, name: info.name, backend: format!("{:?}", info.backend), device_type: format!("{:?}", info.device_type) }
}

/// The GPUs of this computer, in a fixed order (software rasterisers listed too, chosen last).
pub fn adapters() -> Vec<AdapterInfo> {
    instance().enumerate_adapters(wgpu::Backends::all()).iter().enumerate().map(|(i, a)| describe(a, i)).collect()
}

pub(crate) fn rank(t: wgpu::DeviceType) -> u8 {
    match t {
        wgpu::DeviceType::DiscreteGpu => 0,
        wgpu::DeviceType::IntegratedGpu => 1,
        wgpu::DeviceType::VirtualGpu => 2,
        wgpu::DeviceType::Other => 3,
        wgpu::DeviceType::Cpu => 4,
    }
}

/// A matcher on one GPU: the kernels compiled once, the descriptors of the recent photos kept on
/// the device, the working buffers grown as needed.
pub struct Matcher {
    pub info: AdapterInfo,
    device: wgpu::Device,
    queue: wgpu::Queue,
    gemm: wgpu::ComputePipeline,
    merge: wgpu::ComputePipeline,
    layout: wgpu::BindGroupLayout,
    params: wgpu::Buffer,
    out: Option<(wgpu::Buffer, wgpu::Buffer, u64)>,
    partials: Option<(wgpu::Buffer, u64)>,
    cache: HashMap<u64, (wgpu::Buffer, usize)>,
    order: VecDeque<u64>,
    cached_bytes: usize,
}

impl Matcher {
    /// On the adapter ``index`` of :func:`adapters`, or the best one (a discrete GPU, else an
    /// integrated one; a software rasteriser only when nothing else exists).
    pub fn new(index: Option<usize>) -> Result<Matcher, String> {
        let inst = instance();
        let mut all = inst.enumerate_adapters(wgpu::Backends::all());
        if all.is_empty() {
            return Err("no GPU adapter (wgpu found no Metal, Vulkan or DirectX 12 device)".into());
        }
        let chosen = match index {
            Some(i) if i < all.len() => i,
            Some(i) => return Err(format!("GPU adapter {i} does not exist: {} found", all.len())),
            None => (0..all.len()).min_by_key(|&i| (rank(all[i].get_info().device_type), i)).unwrap(),
        };
        let info = describe(&all[chosen], chosen);
        let adapter = all.swap_remove(chosen);
        let limits = adapter.limits();
        if limits.max_compute_workgroup_storage_size < 32768 {
            return Err(format!("{}: {} bytes of workgroup memory, 32768 needed", info.name, limits.max_compute_workgroup_storage_size));
        }
        let (device, queue) = pollster::block_on(adapter.request_device(
            &wgpu::DeviceDescriptor { label: Some("ppg_core"), required_limits: limits, ..Default::default() }, None))
            .map_err(|e| format!("{}: {e}", info.name))?;
        let module = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("match"), source: wgpu::ShaderSource::Wgsl(shader().into()) });
        let buffer = |binding: u32, read_only: bool| wgpu::BindGroupLayoutEntry {
            binding, visibility: wgpu::ShaderStages::COMPUTE, count: None,
            ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only }, has_dynamic_offset: false, min_binding_size: None },
        };
        let layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor { label: None, entries: &[
            buffer(0, true), buffer(1, true), buffer(2, false), buffer(3, false),
            wgpu::BindGroupLayoutEntry { binding: 4, visibility: wgpu::ShaderStages::COMPUTE, count: None,
                ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Uniform, has_dynamic_offset: false, min_binding_size: None } },
        ] });
        let pipeline_layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor { label: None, bind_group_layouts: &[&layout], push_constant_ranges: &[] });
        let make = |entry: &str| device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some(entry), layout: Some(&pipeline_layout), module: &module, entry_point: Some(entry),
            compilation_options: Default::default(), cache: None });
        let gemm = make("gemm");
        let merge = make("merge");
        let params = device.create_buffer(&wgpu::BufferDescriptor { label: Some("params"), size: 16, usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST, mapped_at_creation: false });
        Ok(Matcher { info, device, queue, gemm, merge, layout, params, out: None, partials: None,
                     cache: HashMap::new(), order: VecDeque::new(), cached_bytes: 0 })
    }

    fn upload(&self, data: &[u8]) -> wgpu::Buffer {
        self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor { label: None, contents: data, usage: wgpu::BufferUsages::STORAGE })
    }

    /// The descriptors ``data`` (n x 128 bytes) on the device: cached under ``key`` (not 0).
    fn descriptors(&mut self, key: u64, data: &[u8]) -> wgpu::Buffer {
        if key == 0 {
            return self.upload(data);
        }
        if let Some((buf, n)) = self.cache.get(&key) {
            if *n == data.len() {
                return buf.clone();
            }
            self.forget(key);
        }
        while self.cached_bytes + data.len() > CACHE_BYTES {
            match self.order.pop_front() {
                Some(old) => self.forget(old),
                None => break,
            }
        }
        let buf = self.upload(data);
        self.cache.insert(key, (buf.clone(), data.len()));
        self.order.push_back(key);
        self.cached_bytes += data.len();
        buf
    }

    fn forget(&mut self, key: u64) {
        if let Some((buf, n)) = self.cache.remove(&key) {
            self.cached_bytes -= n;
            self.order.retain(|k| *k != key);
            buf.destroy();
        }
    }

    /// Drop the descriptors kept on the device.
    pub fn clear(&mut self) {
        for (_, (buf, _)) in self.cache.drain() {
            buf.destroy();
        }
        self.order.clear();
        self.cached_bytes = 0;
    }

    fn working(&mut self, out_size: u64, part_size: u64) -> (wgpu::Buffer, wgpu::Buffer, wgpu::Buffer) {
        if self.out.as_ref().map_or(true, |(_, _, s)| *s < out_size) {
            let out = self.device.create_buffer(&wgpu::BufferDescriptor { label: Some("out"), size: out_size, usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC, mapped_at_creation: false });
            let staging = self.device.create_buffer(&wgpu::BufferDescriptor { label: Some("staging"), size: out_size, usage: wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST, mapped_at_creation: false });
            self.out = Some((out, staging, out_size));
        }
        if self.partials.as_ref().map_or(true, |(_, s)| *s < part_size) {
            let part = self.device.create_buffer(&wgpu::BufferDescriptor { label: Some("partials"), size: part_size, usage: wgpu::BufferUsages::STORAGE, mapped_at_creation: false });
            self.partials = Some((part, part_size));
        }
        let (out, staging, _) = self.out.as_ref().unwrap();
        let (part, _) = self.partials.as_ref().unwrap();
        (out.clone(), staging.clone(), part.clone())
    }

    /// The best and second-best dot products: ``(best, index, second)`` for every descriptor of
    /// A over B, then for every descriptor of B over A.
    pub fn bests(&mut self, a_key: u64, a: &[u8], b_key: u64, b: &[u8]) -> Result<Vec<[u32; 3]>, String> {
        if a.len() % DIM != 0 || b.len() % DIM != 0 {
            return Err("descriptors are 128 bytes each".into());
        }
        let (n_a, n_b) = (a.len() / DIM, b.len() / DIM);
        if n_a == 0 || n_b == 0 {
            return Ok(Vec::new());
        }
        let tiles_a = (n_a + TILE_A - 1) / TILE_A;
        let a_buf = self.descriptors(a_key, a);
        let b_buf = self.descriptors(b_key, b);
        let out_size = ((n_a + n_b) * 16) as u64;
        let part_size = (tiles_a * n_b * 8) as u64;
        let (out, staging, part) = self.working(out_size, part_size);
        self.queue.write_buffer(&self.params, 0, bytemuck::bytes_of(&Params { n_a: n_a as u32, n_b: n_b as u32, tiles_a: tiles_a as u32, _pad: 0 }));
        let bind = self.device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: None, layout: &self.layout,
            entries: &[
                wgpu::BindGroupEntry { binding: 0, resource: a_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 1, resource: b_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 2, resource: out.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 3, resource: part.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 4, resource: self.params.as_entire_binding() },
            ],
        });
        let mut enc = self.device.create_command_encoder(&Default::default());
        {
            let mut pass = enc.begin_compute_pass(&Default::default());
            pass.set_pipeline(&self.gemm);
            pass.set_bind_group(0, &bind, &[]);
            pass.dispatch_workgroups(tiles_a as u32, 1, 1);
            pass.set_pipeline(&self.merge);
            pass.set_bind_group(0, &bind, &[]);
            pass.dispatch_workgroups(((n_b + 255) / 256) as u32, 1, 1);
        }
        enc.copy_buffer_to_buffer(&out, 0, &staging, 0, out_size);
        self.queue.submit(Some(enc.finish()));
        let slice = staging.slice(..out_size);
        let (tx, rx) = std::sync::mpsc::channel();
        slice.map_async(wgpu::MapMode::Read, move |r| { let _ = tx.send(r); });
        self.device.poll(wgpu::Maintain::Wait);
        rx.recv().map_err(|e| e.to_string())?.map_err(|e| e.to_string())?;
        let words: Vec<u32> = bytemuck::cast_slice(&slice.get_mapped_range()).to_vec();
        staging.unmap();
        if a_key == 0 { a_buf.destroy(); }
        if b_key == 0 { b_buf.destroy(); }
        Ok(words.chunks_exact(4).map(|w| [w[0], w[1], w[2]]).collect())
    }

    /// COLMAP's matches of the pair: the ratio test (``max_ratio`` on the distances, the arc
    /// cosines of the normalised dot products), the distance test (``max_distance``) and, with
    /// ``cross_check``, the same tests from B to A with the best of A seen from B being the same.
    pub fn matches(&mut self, a_key: u64, a: &[u8], b_key: u64, b: &[u8], max_ratio: f32, max_distance: f32,
                   cross_check: bool) -> Result<Vec<[u32; 2]>, String> {
        let (n_a, n_b) = (a.len() / DIM, b.len() / DIM);
        let bests = self.bests(a_key, a, b_key, b)?;
        Ok(select_matches(&bests, n_a, n_b, max_ratio, max_distance, cross_check))
    }
}

fn distance(score: u32) -> f32 {
    (score as f32 / (512.0 * 512.0)).min(1.0).acos()
}

fn passes(best: u32, second: u32, max_ratio: f32, max_distance: f32) -> bool {
    let d1 = distance(best);
    d1 <= max_distance && d1 < max_ratio * distance(second)
}

/// The matches out of the bests of both directions (:meth:`Matcher::bests`).
pub fn select_matches(bests: &[[u32; 3]], n_a: usize, n_b: usize, max_ratio: f32, max_distance: f32,
                      cross_check: bool) -> Vec<[u32; 2]> {
    let mut out = Vec::new();
    if bests.len() < n_a + n_b {
        return out;
    }
    for i in 0..n_a {
        let [best, j, second] = bests[i];
        if best == 0 || !passes(best, second, max_ratio, max_distance) {
            continue;
        }
        if cross_check {
            let [cb, ci, cs] = bests[n_a + j as usize];
            if ci as usize != i || !passes(cb, cs, max_ratio, max_distance) {
                continue;
            }
        }
        out.push([i as u32, j]);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_from_bests() {
        // two descriptors of A, three of B: A0 <-> B1 clearly, A1 ambiguous between B0 and B2
        let s = |cos: f32| (cos * 512.0 * 512.0) as u32;
        let bests = vec![[s(0.95), 1, s(0.5)], [s(0.9), 0, s(0.89)], // rows
                         [s(0.9), 1, s(0.3)], [s(0.95), 0, s(0.4)], [s(0.89), 1, s(0.2)]]; // columns
        assert_eq!(select_matches(&bests, 2, 3, 0.8, 0.7, true), vec![[0, 1]]);
        assert_eq!(select_matches(&bests, 2, 3, 0.8, 0.7, false), vec![[0, 1]]);
        assert_eq!(select_matches(&bests, 2, 3, 0.8, 0.1, true), Vec::<[u32; 2]>::new());
    }
}
