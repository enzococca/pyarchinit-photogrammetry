//! Depth-map fusion on the GPU (wgpu): the dense point cloud from the depth maps, as COLMAP's
//! `StereoFusion` makes it, but one thread per pixel instead of a traversal.
//!
//! For every pixel of a reference depth map its 3D point is projected into the covisible depth
//! maps, kept on the device in slots (a cache keyed by image, the least recently used map
//! replaced). A map *agrees* on the point when its depth at that pixel is within `support_tol`
//! of the point's depth there: the number of agreeing maps is the confidence of the point, as
//! `confidence.depth_map_support` counts it. A map is *consistent*, as in COLMAP's fusion, when
//! its own pixel back-projects into the reference within `max_reproj` pixels and `max_depth_rel`
//! of the depth, with normals (central differences of the depth map, towards the camera) within
//! `max_normal`. The consistent pixels are averaged into the point; the point is emitted by the
//! earliest of the maps that hold it (their `order`), so no map emits a point another one made,
//! and kept with at least `min_pixels` pixels.

use std::collections::{HashMap, HashSet, VecDeque};

use bytemuck::{Pod, Zeroable};
use wgpu::util::DeviceExt;

/// Covisible maps checked for one reference map at most.
pub const MAX_NEIGHBOURS: usize = 64;

#[repr(C)]
#[derive(Clone, Copy, Pod, Zeroable)]
struct Cam {
    k: [f32; 4],   // fx, fy, cx, cy of the map
    r0: [f32; 4],  // cam_from_world rows
    r1: [f32; 4],
    r2: [f32; 4],
    t: [f32; 4],
    dims: [u32; 4],  // w, h, offset of the map in the slot buffer (floats), order
}

#[repr(C)]
#[derive(Clone, Copy, Pod, Zeroable)]
struct Params {
    ref_slot: u32,
    w: u32,
    h: u32,
    n_neigh: u32,
    max_reproj: f32,
    max_depth_rel: f32,
    cos_normal: f32,
    support_tol: f32,
    min_pixels: u32,
    ref_order: u32,
    max_points: u32,
    pad: u32,
}

/// A fused point: position and normal (the frame of the maps), the pixel of the reference map
/// (`row * w + col`), `counts` = pixels fused (high 16 bits) and maps agreeing (low 16 bits).
#[repr(C)]
#[derive(Clone, Copy, Pod, Zeroable, Debug)]
pub struct Fused {
    pub x: f32,
    pub y: f32,
    pub z: f32,
    pub nx: f32,
    pub ny: f32,
    pub nz: f32,
    pub pixel: u32,
    pub counts: u32,
}

/// A depth map with its camera: `depth` None when the map is on the device already (`key`).
pub struct Map<'a> {
    pub key: i64,
    pub depth: Option<&'a [f32]>,
    pub w: usize,
    pub h: usize,
    pub k: [f64; 4],
    pub r: [[f64; 3]; 3],
    pub t: [f64; 3],
    pub order: u32,
}

pub struct Options {
    pub min_pixels: u32,
    pub max_reproj: f32,
    pub max_depth_rel: f32,
    pub max_normal_deg: f32,
    pub support_tol: f32,
}

const SHADER: &str = r#"
struct Cam { k: vec4<f32>, r0: vec4<f32>, r1: vec4<f32>, r2: vec4<f32>, t: vec4<f32>, dims: vec4<u32> };
struct Params {
    ref_slot: u32, w: u32, h: u32, n_neigh: u32,
    max_reproj: f32, max_depth_rel: f32, cos_normal: f32, support_tol: f32,
    min_pixels: u32, ref_order: u32, max_points: u32, pad: u32,
};
struct Fused { x: f32, y: f32, z: f32, nx: f32, ny: f32, nz: f32, pixel: u32, counts: u32 };

@group(0) @binding(0) var<uniform> p: Params;
@group(0) @binding(1) var<storage, read> maps: array<f32>;
@group(0) @binding(2) var<storage, read> cams: array<Cam>;
@group(0) @binding(3) var<storage, read> neigh: array<u32>;
@group(0) @binding(4) var<storage, read_write> count: array<atomic<u32>>;
@group(0) @binding(5) var<storage, read_write> out: array<Fused>;

fn depth_at(s: u32, x: i32, y: i32) -> f32 {
    let dims = cams[s].dims;
    if (x < 0 || y < 0 || x >= i32(dims.x) || y >= i32(dims.y)) { return 0.0; }
    return maps[dims.z + u32(y) * dims.x + u32(x)];
}

// the point of a pixel in the camera frame (the centre of the pixel at its integer coordinates)
fn cam_point(s: u32, x: f32, y: f32, d: f32) -> vec3<f32> {
    let k = cams[s].k;
    return vec3<f32>((x - k.z) * d / k.x, (y - k.w) * d / k.y, d);
}

fn rot_world(s: u32, v: vec3<f32>) -> vec3<f32> {  // R^T v
    let c = cams[s];
    return vec3<f32>(c.r0.x * v.x + c.r1.x * v.y + c.r2.x * v.z,
                     c.r0.y * v.x + c.r1.y * v.y + c.r2.y * v.z,
                     c.r0.z * v.x + c.r1.z * v.y + c.r2.z * v.z);
}

fn to_world(s: u32, pc: vec3<f32>) -> vec3<f32> {  // R^T (pc - t)
    return rot_world(s, pc - cams[s].t.xyz);
}

fn to_cam(s: u32, w: vec3<f32>) -> vec3<f32> {  // R w + t
    let c = cams[s];
    return vec3<f32>(dot(c.r0.xyz, w), dot(c.r1.xyz, w), dot(c.r2.xyz, w)) + c.t.xyz;
}

// the normal of the surface at a pixel (world frame, towards the camera); zero where a neighbour is missing
fn normal_at(s: u32, x: i32, y: i32) -> vec3<f32> {
    let d = depth_at(s, x, y);
    let dl = depth_at(s, x - 1, y);
    let dr = depth_at(s, x + 1, y);
    let du = depth_at(s, x, y - 1);
    let dd = depth_at(s, x, y + 1);
    if (d <= 0.0 || dl <= 0.0 || dr <= 0.0 || du <= 0.0 || dd <= 0.0) { return vec3<f32>(0.0); }
    let dx = cam_point(s, f32(x + 1), f32(y), dr) - cam_point(s, f32(x - 1), f32(y), dl);
    let dy = cam_point(s, f32(x), f32(y + 1), dd) - cam_point(s, f32(x), f32(y - 1), du);
    var n = cross(dx, dy);
    let l = length(n);
    if (!(l > 0.0)) { return vec3<f32>(0.0); }
    n = n / l;
    if (dot(n, cam_point(s, f32(x), f32(y), d)) > 0.0) { n = -n; }
    return rot_world(s, n);
}

@compute @workgroup_size(16, 16)
fn fuse(@builtin(global_invocation_id) id: vec3<u32>) {
    let x = i32(id.x);
    let y = i32(id.y);
    if (x >= i32(p.w) || y >= i32(p.h)) { return; }
    let rs = p.ref_slot;
    let d = depth_at(rs, x, y);
    if (d <= 0.0) { return; }
    let X = to_world(rs, cam_point(rs, f32(x), f32(y), d));
    let n_ref = normal_at(rs, x, y);
    let has_normal = dot(n_ref, n_ref) > 0.0;
    var sum = X;
    var nsum = n_ref;
    var pixels = 1u;
    var support = 1u;
    var owned = true;
    for (var i = 0u; i < p.n_neigh; i++) {
        let s = neigh[i];
        let q = to_cam(s, X);
        if (q.z <= 1e-9) { continue; }
        let kk = cams[s].k;
        let u = kk.x * q.x / q.z + kk.z;
        let v = kk.y * q.y / q.z + kk.w;
        let ui = i32(round(u));
        let vi = i32(round(v));
        let dj = depth_at(s, ui, vi);
        if (dj <= 0.0) { continue; }
        if (abs(dj - q.z) <= p.support_tol * q.z) { support += 1u; }
        // consistent as in COLMAP's fusion: the pixel's own point back in the reference
        let Xj = to_world(s, cam_point(s, f32(ui), f32(vi), dj));
        let back = to_cam(rs, Xj);
        if (back.z <= 1e-9) { continue; }
        let kr = cams[rs].k;
        let ub = kr.x * back.x / back.z + kr.z;
        let vb = kr.y * back.y / back.z + kr.w;
        if (length(vec2<f32>(ub - f32(x), vb - f32(y))) > p.max_reproj) { continue; }
        if (abs(back.z - d) > p.max_depth_rel * d) { continue; }
        if (!has_normal) { continue; }
        let nj = normal_at(s, ui, vi);
        if (!(dot(nj, nj) > 0.0) || dot(n_ref, nj) < p.cos_normal) { continue; }
        sum += Xj;
        nsum += nj;
        pixels += 1u;
        if (cams[s].dims.w < p.ref_order) { owned = false; }
    }
    if (pixels < p.min_pixels || !owned) { return; }
    let k = atomicAdd(&count[0], 1u);
    if (k >= p.max_points) { return; }
    let mean = sum / f32(pixels);
    var nn = nsum;
    let ln = length(nn);
    if (ln > 0.0) { nn = nn / ln; }
    out[k] = Fused(mean.x, mean.y, mean.z, nn.x, nn.y, nn.z, u32(y) * p.w + u32(x), (pixels << 16u) | min(support, 65535u));
}
"#;

fn buffer_entry(binding: u32, read_only: bool) -> wgpu::BindGroupLayoutEntry {
    wgpu::BindGroupLayoutEntry {
        binding, visibility: wgpu::ShaderStages::COMPUTE, count: None,
        ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only }, has_dynamic_offset: false, min_binding_size: None },
    }
}

pub struct Fuser {
    pub name: String,
    device: wgpu::Device,
    queue: wgpu::Queue,
    layout: wgpu::BindGroupLayout,
    pipeline: wgpu::ComputePipeline,
    max_binding: u64,
    slot_floats: usize,
    slots: usize,
    maps: Option<wgpu::Buffer>,
    cams: Option<wgpu::Buffer>,
    slot_of: HashMap<i64, usize>,
    key_of: Vec<Option<i64>>,
    dims_of: Vec<(usize, usize)>,
    lru: VecDeque<i64>,
    out: Option<(wgpu::Buffer, wgpu::Buffer, usize)>,  // points, staging, capacity (points)
}

impl Fuser {
    pub fn new(index: Option<usize>) -> Result<Fuser, String> {
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
        let max_binding = limits.max_storage_buffer_binding_size as u64;
        let (device, queue) = pollster::block_on(adapter.request_device(
            &wgpu::DeviceDescriptor { label: Some("ppg_core fusion"), required_limits: limits, ..Default::default() }, None))
            .map_err(|e| format!("{name}: {e}"))?;
        let module = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("fusion"), source: wgpu::ShaderSource::Wgsl(SHADER.into()) });
        let layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor { label: None, entries: &[
            wgpu::BindGroupLayoutEntry { binding: 0, visibility: wgpu::ShaderStages::COMPUTE, count: None,
                ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Uniform, has_dynamic_offset: false,
                                                min_binding_size: wgpu::BufferSize::new(std::mem::size_of::<Params>() as u64) } },
            buffer_entry(1, true), buffer_entry(2, true), buffer_entry(3, true), buffer_entry(4, false), buffer_entry(5, false),
        ] });
        let pipeline_layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor { label: None, bind_group_layouts: &[&layout], push_constant_ranges: &[] });
        let pipeline = device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some("fuse"), layout: Some(&pipeline_layout), module: &module, entry_point: Some("fuse"),
            compilation_options: Default::default(), cache: None });
        Ok(Fuser { name, device, queue, layout, pipeline, max_binding, slot_floats: 0, slots: 0, maps: None, cams: None,
                   slot_of: HashMap::new(), key_of: Vec::new(), dims_of: Vec::new(), lru: VecDeque::new(), out: None })
    }

    /// The keys of the maps on the device.
    pub fn cached(&self) -> Vec<i64> {
        self.lru.iter().copied().collect()
    }

    /// Drop every map kept on the device.
    pub fn clear(&mut self) {
        self.slot_of.clear();
        self.lru.clear();
        for k in self.key_of.iter_mut() {
            *k = None;
        }
    }

    /// Slots large enough for `floats` per map and as many as the device allows (up to `wanted`).
    fn ensure_slots(&mut self, floats: usize, wanted: usize) -> Result<(), String> {
        let floats = floats.max(1);
        let stride = ((floats + 63) / 64) * 64;  // 256-byte aligned rows of floats
        let fit = (self.max_binding / (stride as u64 * 4)) as usize;
        let slots = wanted.min(fit).max(2);
        if stride <= self.slot_floats && slots <= self.slots && self.maps.is_some() {
            return Ok(());
        }
        if fit < 2 {
            return Err(format!("a depth map of {floats} pixels does not fit this GPU ({} bytes per binding)", self.max_binding));
        }
        let stride = stride.max(self.slot_floats);
        let slots = slots.max(self.slots).min(fit);
        self.maps = Some(self.device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("depth maps"), size: (slots * stride * 4) as u64,
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST, mapped_at_creation: false }));
        self.cams = Some(self.device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("cameras"), size: (slots * std::mem::size_of::<Cam>()) as u64,
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST, mapped_at_creation: false }));
        self.slot_floats = stride;
        self.slots = slots;
        self.key_of = vec![None; slots];
        self.dims_of = vec![(0, 0); slots];
        self.slot_of.clear();
        self.lru.clear();
        Ok(())
    }

    /// The slot of a map: the one it has (its data replaced when given again: a key is unique
    /// within one workspace only), or a free (or the least recently used, not `in_use`) slot it
    /// is uploaded to.
    fn place(&mut self, m: &Map, in_use: &HashSet<i64>) -> Result<usize, String> {
        if let Some(&s) = self.slot_of.get(&m.key) {
            self.lru.retain(|k| *k != m.key);
            self.lru.push_back(m.key);
            if m.depth.is_none() {
                return Ok(s);
            }
            self.key_of[s] = None;
            self.slot_of.remove(&m.key);
            self.lru.retain(|k| *k != m.key);
        }
        let depth = m.depth.ok_or_else(|| format!("depth map {} is not on the GPU and was not given", m.key))?;
        if depth.len() != m.w * m.h {
            return Err(format!("depth map {}: {} values for {} x {}", m.key, depth.len(), m.w, m.h));
        }
        if depth.len() > self.slot_floats {
            return Err(format!("depth map {} is larger than the slots ({} > {})", m.key, depth.len(), self.slot_floats));
        }
        let slot = match self.key_of.iter().position(|k| k.is_none()) {
            Some(s) => s,
            None => {
                let victim = self.lru.iter().copied().find(|k| !in_use.contains(k))
                    .ok_or_else(|| "every slot of the GPU is in use by this call".to_string())?;
                self.lru.retain(|k| *k != victim);
                self.slot_of.remove(&victim).ok_or_else(|| "slot bookkeeping".to_string())?
            }
        };
        let f = |v: f64| v as f32;
        let cam = Cam {
            k: [f(m.k[0]), f(m.k[1]), f(m.k[2]), f(m.k[3])],
            r0: [f(m.r[0][0]), f(m.r[0][1]), f(m.r[0][2]), 0.0],
            r1: [f(m.r[1][0]), f(m.r[1][1]), f(m.r[1][2]), 0.0],
            r2: [f(m.r[2][0]), f(m.r[2][1]), f(m.r[2][2]), 0.0],
            t: [f(m.t[0]), f(m.t[1]), f(m.t[2]), 0.0],
            dims: [m.w as u32, m.h as u32, (slot * self.slot_floats) as u32, m.order],
        };
        let maps = self.maps.as_ref().ok_or("no slots")?;
        let cams = self.cams.as_ref().ok_or("no slots")?;
        self.queue.write_buffer(maps, (slot * self.slot_floats * 4) as u64, bytemuck::cast_slice(depth));
        self.queue.write_buffer(cams, (slot * std::mem::size_of::<Cam>()) as u64, bytemuck::bytes_of(&cam));
        self.key_of[slot] = Some(m.key);
        self.dims_of[slot] = (m.w, m.h);
        self.slot_of.insert(m.key, slot);
        self.lru.push_back(m.key);
        Ok(slot)
    }

    /// The order of a map already on the device may change between calls: keep it current.
    fn set_order(&self, slot: usize, order: u32) {
        if let Some(cams) = self.cams.as_ref() {
            let off = (slot * std::mem::size_of::<Cam>() + 5 * 16 + 12) as u64;
            self.queue.write_buffer(cams, off, bytemuck::bytes_of(&order));
        }
    }

    /// The fused points of `reference` checked against `neighbours` (the first `MAX_NEIGHBOURS`,
    /// or as many as the slots of the device hold).
    pub fn fuse(&mut self, reference: &Map, neighbours: &[Map], o: &Options) -> Result<Vec<Fused>, String> {
        let given = |m: &Map| m.depth.map(|d| d.len()).unwrap_or(0);
        let floats = neighbours.iter().map(given).fold(given(reference), usize::max).max(self.slot_floats);
        self.ensure_slots(floats, MAX_NEIGHBOURS + 1)?;
        let n_neigh = neighbours.len().min(MAX_NEIGHBOURS).min(self.slots - 1);
        let neighbours = &neighbours[..n_neigh];
        let in_use: HashSet<i64> = std::iter::once(reference.key).chain(neighbours.iter().map(|m| m.key)).collect();
        let ref_slot = self.place(reference, &in_use)?;
        self.set_order(ref_slot, reference.order);
        let (w, h) = self.dims_of[ref_slot];
        let mut slots = Vec::with_capacity(n_neigh);
        for m in neighbours {
            let s = self.place(m, &in_use)?;
            self.set_order(s, m.order);
            slots.push(s as u32);
        }
        if slots.is_empty() {
            slots.push(ref_slot as u32);  // a dummy binding: n_neigh 0 reads none of it
        }
        let n = w * h;
        if n == 0 {
            return Ok(Vec::new());
        }
        let dev = &self.device;
        if self.out.as_ref().map_or(true, |(_, _, cap)| *cap < n) {
            let bytes = (n * std::mem::size_of::<Fused>()) as u64;
            let out = dev.create_buffer(&wgpu::BufferDescriptor { label: Some("fused"), size: bytes,
                usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC, mapped_at_creation: false });
            let st = dev.create_buffer(&wgpu::BufferDescriptor { label: Some("fused staging"), size: bytes,
                usage: wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST, mapped_at_creation: false });
            self.out = Some((out, st, n));
        }
        let (out, st_out, _) = self.out.as_ref().unwrap();
        let params = Params {
            ref_slot: ref_slot as u32, w: w as u32, h: h as u32, n_neigh: n_neigh as u32,
            max_reproj: o.max_reproj, max_depth_rel: o.max_depth_rel, cos_normal: o.max_normal_deg.to_radians().cos(),
            support_tol: o.support_tol, min_pixels: o.min_pixels.max(1), ref_order: reference.order, max_points: n as u32, pad: 0,
        };
        let params_buf = dev.create_buffer_init(&wgpu::util::BufferInitDescriptor { label: Some("fusion params"),
            contents: bytemuck::bytes_of(&params), usage: wgpu::BufferUsages::UNIFORM });
        let neigh_buf = dev.create_buffer_init(&wgpu::util::BufferInitDescriptor { label: Some("neighbours"),
            contents: bytemuck::cast_slice(&slots), usage: wgpu::BufferUsages::STORAGE });
        let count = dev.create_buffer_init(&wgpu::util::BufferInitDescriptor { label: Some("count"),
            contents: bytemuck::cast_slice(&[0u32; 4]), usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC });
        let st_count = dev.create_buffer(&wgpu::BufferDescriptor { label: Some("count staging"), size: 16,
            usage: wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST, mapped_at_creation: false });
        let bind = dev.create_bind_group(&wgpu::BindGroupDescriptor { label: None, layout: &self.layout, entries: &[
            wgpu::BindGroupEntry { binding: 0, resource: params_buf.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 1, resource: self.maps.as_ref().unwrap().as_entire_binding() },
            wgpu::BindGroupEntry { binding: 2, resource: self.cams.as_ref().unwrap().as_entire_binding() },
            wgpu::BindGroupEntry { binding: 3, resource: neigh_buf.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 4, resource: count.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 5, resource: out.as_entire_binding() },
        ] });
        let mut enc = dev.create_command_encoder(&Default::default());
        {
            let mut pass = enc.begin_compute_pass(&Default::default());
            pass.set_pipeline(&self.pipeline);
            pass.set_bind_group(0, &bind, &[]);
            pass.dispatch_workgroups((w as u32 + 15) / 16, (h as u32 + 15) / 16, 1);
        }
        enc.copy_buffer_to_buffer(&count, 0, &st_count, 0, 16);
        enc.copy_buffer_to_buffer(out, 0, st_out, 0, (n * std::mem::size_of::<Fused>()) as u64);
        self.queue.submit(Some(enc.finish()));
        let total = {
            let slice = st_count.slice(..);
            let (tx, rx) = std::sync::mpsc::channel();
            slice.map_async(wgpu::MapMode::Read, move |r| { let _ = tx.send(r); });
            dev.poll(wgpu::Maintain::Wait);
            rx.recv().map_err(|e| e.to_string())?.map_err(|e| e.to_string())?;
            let v: Vec<u32> = bytemuck::cast_slice(&slice.get_mapped_range()).to_vec();
            st_count.unmap();
            (v[0] as usize).min(n)
        };
        let mut points = Vec::with_capacity(total);
        if total > 0 {
            let bytes = (total * std::mem::size_of::<Fused>()) as u64;
            let slice = st_out.slice(..bytes);
            let (tx, rx) = std::sync::mpsc::channel();
            slice.map_async(wgpu::MapMode::Read, move |r| { let _ = tx.send(r); });
            dev.poll(wgpu::Maintain::Wait);
            rx.recv().map_err(|e| e.to_string())?.map_err(|e| e.to_string())?;
            points.extend_from_slice(bytemuck::cast_slice::<u8, Fused>(&slice.get_mapped_range()));
            st_out.unmap();
        }
        points.sort_unstable_by_key(|f| f.pixel);  // the same order every time (the atomics give any)
        Ok(points)
    }
}
