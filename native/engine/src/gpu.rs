//! The GPU through wgpu - Metal on macOS, Vulkan on Linux and Windows (also on ARM), DirectX 12 on
//! Windows - with kernels written once in WGSL: the matching of SIFT descriptors, the biggest
//! cost of the alignment of many photos.
//!
//! The descriptors are COLMAP's: 128 unsigned bytes each (an L2 norm of 512 before the
//! quantisation), so a dot product is an integer below 2^18 and exact in f32 (below 2^24 for any
//! bytes). One pass over a pair of photos gives, for every descriptor of A, its best and
//! second-best dot product over B (the rows) and, through partials per tile merged by a second
//! kernel, the same for every descriptor of B over A (the columns): COLMAP's ratio test, distance
//! test and cross check come out of the one pass. A workgroup of 16 x 16 threads takes a tile of
//! descriptors of A against every tile of B, the tiles transposed in workgroup memory
//! (word-major) so that a step over one word of the descriptors reads consecutive entries. The
//! tiles come in two sizes, chosen from the workgroup memory of the adapter (`Tile`): 128 x 64,
//! 8 x 4 dot products per thread, in 32 KiB; or 64 x 32, 4 x 2 per thread, in the 16 KiB of the
//! downlevel limits of wgpu. Either size, and `matching::bests_cpu` on the CPU, gives the same
//! bests bit for bit: the lowest index wins a tie in both directions, and on a tie the second
//! best equals the best. Apple M4 Pro, large tiles: 2.06 TFLOP/s, 198 ms for 40 000 x 40 000
//! descriptors, both directions.
//!
//! The same kernels, made segment-aware (`Matcher::bests_block`), match two blocks of small photos
//! in one dispatch - every photo of A against every photo of B, the photos padded to whole tiles
//! so that no tile holds two, the bests of the rows flushed per photo of B and the partials of
//! the columns merged per photo of A - for the preselection, whose pairs of a thousand
//! descriptors would each take a dispatch of eight workgroups.
use std::collections::{HashMap, VecDeque};
use std::sync::mpsc::TryRecvError;
use std::time::{Duration, Instant};

use wgpu::util::DeviceExt;

pub const DIM: usize = 128;
const CACHE_BYTES: usize = 768 << 20; // descriptors kept on the GPU across the pairs of the same photos

/// The tiles of the matching kernel: `rows` descriptors of A against `cols` of B per workgroup
/// and pass, in workgroup memory. A workgroup has 16 x 16 threads, so a thread holds
/// `rows / 16` x `cols / 16` dot products.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Tile {
    pub rows: usize,
    pub cols: usize,
}

/// The tiles of a GPU with 32 KiB of workgroup memory or more (the kernel as first written).
pub const LARGE_TILE: Tile = Tile { rows: 128, cols: 64 };
/// The tiles that fit 16 KiB of workgroup memory, the downlevel limit of wgpu (GL, some virtual
/// and mobile adapters): half the rows, half the columns, half the scratch.
pub const SMALL_TILE: Tile = Tile { rows: 64, cols: 32 };

impl Tile {
    /// The entries of `scratch`: 16 thread rows by the columns of the tile when the columns are
    /// merged, 16 thread columns by half the rows when the rows are (the same figure for both
    /// sizes).
    const fn scratch(&self) -> usize {
        if 16 * self.cols > 8 * self.rows {
            16 * self.cols
        } else {
            8 * self.rows
        }
    }

    /// The workgroup memory the kernel declares: the two tiles as u32 words (32 per descriptor)
    /// and `scratch` as vec2<u32>.
    pub const fn workgroup_bytes(&self) -> u32 {
        ((self.rows * 32 + 32 * self.cols) * 4 + self.scratch() * 8) as u32
    }
}

/// The tiles the kernels use on an adapter with `workgroup_bytes` of workgroup memory
/// (`max_compute_workgroup_storage_size`): the large ones from 32768 bytes, the small ones from
/// 16384, none below.
pub fn tile_for(workgroup_bytes: u32) -> Option<Tile> {
    [LARGE_TILE, SMALL_TILE].into_iter().find(|tile| tile.workgroup_bytes() <= workgroup_bytes)
}

/// The bytes of descriptors kept on the GPU: ``PPG_GPU_CACHE_MB`` when set (the tests make it
/// small to make the cache full), else ``CACHE_BYTES``.
fn cache_limit() -> usize {
    std::env::var("PPG_GPU_CACHE_MB").ok().and_then(|v| v.trim().parse::<usize>().ok()).map_or(CACHE_BYTES, |mb| mb << 20)
}

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

/// The parameters of the block kernels: the padded rows of A and columns of B in all, the tiles
/// and the photos of each side.
#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
struct BlockParams {
    n_a: u32,
    n_b: u32,
    tiles_a: u32,
    photos_b: u32,
    tiles_b: u32,
    photos_a: u32,
    _pad: [u32; 2],
}

/// The layout of a block of photos on the device: the photos of A one after the other, each
/// padded to whole tiles of `rows` descriptors, those of B padded to whole tiles of `cols`, the
/// empty photos left out; and the table of segments the block kernels read (`Kernel::block`).
struct BlockLayout {
    /// For every photo of A given: its index among the photos sent and its first padded row;
    /// `None` for a photo without descriptors.
    a: Vec<Option<(usize, usize)>>,
    /// The same for B, with the first padded column.
    b: Vec<Option<(usize, usize)>>,
    n_a: usize,
    n_b: usize,
    tiles_a: usize,
    tiles_b: usize,
    photos_a: usize,
    photos_b: usize,
    seg: Vec<[u32; 4]>,
}

impl BlockLayout {
    /// The layout of photos of `a` and `b` descriptors each (counts) with the tiles `tile`.
    fn new(tile: Tile, a: &[usize], b: &[usize]) -> BlockLayout {
        let mut layout = BlockLayout { a: Vec::with_capacity(a.len()), b: Vec::with_capacity(b.len()), n_a: 0, n_b: 0,
                                       tiles_a: 0, tiles_b: 0, photos_a: 0, photos_b: 0, seg: Vec::new() };
        let mut tiles_b = Vec::new();
        let mut photos_a = Vec::new();
        for &n in a {
            if n == 0 {
                layout.a.push(None);
                continue;
            }
            let tiles = n.div_ceil(tile.rows);
            let (begin, end) = (layout.n_a, layout.n_a + n);
            for _ in 0..tiles {
                layout.seg.push([layout.photos_a as u32, begin as u32, end as u32, 0]);
            }
            photos_a.push([layout.tiles_a as u32, tiles as u32, 0, 0]);
            layout.a.push(Some((layout.photos_a, begin)));
            layout.n_a += tiles * tile.rows;
            layout.tiles_a += tiles;
            layout.photos_a += 1;
        }
        for &n in b {
            if n == 0 {
                layout.b.push(None);
                continue;
            }
            let tiles = n.div_ceil(tile.cols);
            let (begin, end) = (layout.n_b, layout.n_b + n);
            for t in 0..tiles {
                let last = u32::from(t + 1 == tiles);
                tiles_b.push([layout.photos_b as u32, begin as u32, end as u32, last]);
            }
            layout.b.push(Some((layout.photos_b, begin)));
            layout.n_b += tiles * tile.cols;
            layout.tiles_b += tiles;
            layout.photos_b += 1;
        }
        layout.seg.extend(tiles_b);
        layout.seg.extend(photos_a);
        layout
    }

    /// The bytes of the two working buffers of the block: the results (the bests of every
    /// padded row of A for every photo of B, then of every padded column of B for every photo
    /// of A, 16 bytes each) and the partials of the columns (8 bytes for every tile of A and
    /// every padded column of B).
    fn bytes(&self) -> (u64, u64) {
        let out = (self.n_a * self.photos_b + self.photos_a * self.n_b) as u64 * 16;
        let partials = (self.tiles_a * self.n_b) as u64 * 8;
        (out, partials)
    }
}

/// What differs between the kernel of a pair and the kernel of a block, in the code the two
/// share: which rows of the tile of A and which columns of the tile of B being visited are
/// valid, and where the bests of a row are written.
struct Seg {
    /// One past the last valid row of A of this workgroup's tile: every row below `n_a` for a
    /// pair, the end of the photo the tile belongs to for a block.
    a_end: &'static str,
    /// One past the last valid column of the tile of B being visited.
    b_end: &'static str,
    /// The entry of `out` that takes the bests of the row `row`.
    row_out: &'static str,
    /// The column written with them: its index in B for a pair, within its photo for a block.
    col: &'static str,
}

const PAIR: Seg = Seg { a_end: "p.n_a", b_end: "p.n_b", row_out: "row", col: "w_col" };
const BLOCK: Seg = Seg { a_end: "sa.z", b_end: "sb.z", row_out: "row * p.photos_b + sb.x", col: "w_col - sb.y" };

/// The code of a kernel for `tile`, generated: the inner loop is written out in full (the
/// accumulators in registers; arrays indexed in loops are kept in memory by some compilers), and
/// the sizes of the tiles, of the loads and of the merges follow the tile.
///
/// A thread of the 16 x 16 holds the rows `tr .. tr + R` of the tile of A (R = rows / 16) against
/// the columns `tc .. tc + C` (C = cols / 16) of every tile of B, so the columns a thread visits
/// are those whose index modulo the tile width falls in its C: the merge of the rows over the
/// threads therefore compares indices on equal scores, to make the lowest index win as a scan
/// does. The columns are merged over thread rows and tiles that hold rows in increasing order, so
/// there a strict comparison is the lowest index.
struct Kernel {
    tile: Tile,
    per_row: usize,
    per_col: usize,
    half: usize,
    b_threads: usize,
    b_loads: usize,
    a_loads: usize,
}

impl Kernel {
    fn new(tile: Tile) -> Kernel {
        let Tile { rows, cols } = tile;
        let (per_row, per_col) = (rows / 16, cols / 16); // dot products of a thread: per_row x per_col
        let half = rows / 2; // rows merged at a time: scratch holds 16 threads x half
        let scratch = tile.scratch();
        let a_words = rows * 32;
        let a_loads = a_words / 256; // words of the A tile each thread loads
        let b_threads = 256 / cols; // threads that load one column of the B tile ...
        let b_loads = 32 / b_threads; // ... and the words each of them loads
        debug_assert!(rows % 16 == 0 && cols % 16 == 0 && a_words % 256 == 0 && 256 % cols == 0);
        debug_assert!(32 % b_threads == 0 && half == 8 * per_row && rows <= 256);
        debug_assert!(scratch >= 16 * cols && scratch >= 16 * half);
        Kernel { tile, per_row, per_col, half, b_threads, b_loads, a_loads }
    }

    /// The declarations: `params` as the fields of the `Params` struct, `more` the bindings after
    /// the five every kernel has, the three arrays of workgroup memory and `unpack`.
    fn declarations(&self, params: &str, more: &str) -> String {
        let (a_words, b_words, scratch) = (self.tile.rows * 32, self.tile.cols * 32, self.tile.scratch());
        format!(r#"
struct Params {{ {params} }};
@group(0) @binding(0) var<storage, read> a: array<u32>;
@group(0) @binding(1) var<storage, read> b: array<u32>;
@group(0) @binding(2) var<storage, read_write> out: array<vec4<u32>>;
@group(0) @binding(3) var<storage, read_write> col_part: array<vec2<u32>>;
@group(0) @binding(4) var<uniform> p: Params;
{more}
var<workgroup> a_tile: array<u32, {a_words}>;
var<workgroup> b_tile: array<u32, {b_words}>;
var<workgroup> scratch: array<vec2<u32>, {scratch}>;

fn unpack(v: u32) -> vec4<f32> {{
    return vec4<f32>(f32(v & 255u), f32((v >> 8u) & 255u), f32((v >> 16u) & 255u), f32(v >> 24u));
}}
"#)
    }

    /// The tile of A of this workgroup loaded (its rows from `a0`, those at `a_end` and beyond
    /// zero), the rows and columns of the thread, and the running bests of its rows: nothing seen
    /// is -1 with the column `0xffffffff`, so that a thread that saw no valid column loses every
    /// tie of the merge.
    fn a_tile(&self, a_end: &str) -> String {
        let (rows, a_loads, per_row, per_col) = (self.tile.rows, self.a_loads, self.per_row, self.per_col);
        let mut s = format!(r#"    let a0 = wg.x * {rows}u;
    for (var e = 0u; e < {a_loads}u; e++) {{
        let idx = e * 256u + li;
        let k = idx / {rows}u;
        let row = idx % {rows}u;
        let g = a0 + row;
        a_tile[idx] = select(0u, a[g * 32u + k], g < {a_end});
    }}
    workgroupBarrier();
    let tr = lid.y * {per_row}u;
    let tc = lid.x * {per_col}u;
"#);
        for r in 0..per_row {
            s.push_str(&format!("    var rb1_{r}: f32 = -1.0; var rb2_{r}: f32 = -1.0; "));
            s.push_str(&format!("var ri_{r}: u32 = 0xffffffffu;\n"));
        }
        s
    }

    /// The running bests of the rows of the thread set back to nothing seen (assignments: a
    /// `var` here would declare new variables in the block and leave the outer ones as they are).
    fn reset_rows(&self) -> String {
        (0..self.per_row)
            .map(|r| format!("            rb1_{r} = -1.0; rb2_{r} = -1.0; ri_{r} = 0xffffffffu;\n"))
            .collect()
    }

    /// The tile `t` of B, from the column `b0`, loaded (its columns at `b_end` and beyond zero).
    fn b_tile(&self, b_end: &str) -> String {
        let (cols, b_threads, b_loads) = (self.tile.cols, self.b_threads, self.b_loads);
        format!(r#"        {{
            let col = li / {b_threads}u;
            let k0 = (li % {b_threads}u) * {b_loads}u;
            let g = b0 + col;
            for (var e = 0u; e < {b_loads}u; e++) {{
                b_tile[(k0 + e) * {cols}u + col] = select(0u, b[g * 32u + k0 + e], g < {b_end});
            }}
        }}
        workgroupBarrier();
"#)
    }

    /// The dot products of the thread's rows and columns over the two tiles.
    fn dot_products(&self) -> String {
        let (rows, cols) = (self.tile.rows, self.tile.cols);
        let mut s = String::new();
        for r in 0..self.per_row {
            for c in 0..self.per_col {
                s.push_str(&format!("        var acc_{r}{c}: f32 = 0.0;\n"));
            }
        }
        s.push_str(&format!(
            "        for (var k = 0u; k < 32u; k++) {{\n            let kb = k * {cols}u + tc;\n            let ka = k * {rows}u + tr;\n"));
        for c in 0..self.per_col {
            s.push_str(&format!("            let bv{c} = unpack(b_tile[kb + {c}u]);\n"));
        }
        for r in 0..self.per_row {
            s.push_str(&format!("            let av{r} = unpack(a_tile[ka + {r}u]);\n"));
        }
        for r in 0..self.per_row {
            for c in 0..self.per_col {
                s.push_str(&format!("            acc_{r}{c} += dot(av{r}, bv{c});\n"));
            }
        }
        s.push_str("        }\n");
        s
    }

    /// The running bests of the thread's rows updated with the valid columns of the tile.
    fn row_update(&self, b_end: &str) -> String {
        let mut s = String::new();
        for c in 0..self.per_col {
            s.push_str(&format!("        let jc{c} = b0 + tc + {c}u;\n"));
        }
        for r in 0..self.per_row {
            for c in 0..self.per_col {
                s.push_str(&format!(
                    "        if (jc{c} < {b_end}) {{ if (acc_{r}{c} > rb1_{r}) {{ rb2_{r} = rb1_{r}; rb1_{r} = acc_{r}{c}; ri_{r} = jc{c}; }} else if (acc_{r}{c} > rb2_{r}) {{ rb2_{r} = acc_{r}{c}; }} }}\n"));
            }
        }
        s
    }

    /// The bests of the tile's columns over the thread's rows, merged over the 16 thread rows
    /// through `scratch` and written to the partials of this tile of A (the row within the tile
    /// packed in the low 8 bits), for the valid columns.
    fn column_partials(&self, b_end: &str) -> String {
        let cols = self.tile.cols;
        let mut s = String::new();
        for c in 0..self.per_col {
            s.push_str(&format!("        var cb1_{c}: f32 = -1.0; var cb2_{c}: f32 = -1.0; var cr_{c}: u32 = 0u;\n"));
            for r in 0..self.per_row {
                s.push_str(&format!(
                    "        if (acc_{r}{c} > cb1_{c}) {{ cb2_{c} = cb1_{c}; cb1_{c} = acc_{r}{c}; cr_{c} = tr + {r}u; }} else if (acc_{r}{c} > cb2_{c}) {{ cb2_{c} = acc_{r}{c}; }}\n"));
            }
            s.push_str(&format!(
                "        scratch[lid.y * {cols}u + tc + {c}u] = vec2<u32>((u32(max(cb1_{c}, 0.0)) << 8u) | cr_{c}, u32(max(cb2_{c}, 0.0)));\n"));
        }
        s.push_str(&format!(r#"
        workgroupBarrier();
        if (li < {cols}u) {{
            let col = li;
            var best: u32 = 0u;
            var second: u32 = 0u;
            for (var y = 0u; y < 16u; y++) {{
                let e = scratch[y * {cols}u + col];
                let sc = e.x >> 8u;
                if (sc > (best >> 8u)) {{ second = max(best >> 8u, e.y); best = e.x; }}
                else {{ second = max(second, max(sc, e.y)); }}
            }}
            let j = b0 + col;
            if (j < {b_end}) {{ col_part[wg.x * p.n_b + j] = vec2<u32>(best, second); }}
        }}
        workgroupBarrier();
"#));
        s
    }

    /// The running bests of the rows merged across the 16 thread columns, half the tile at a
    /// time through `scratch`, and written out for the valid rows: the best, its column, the
    /// second best (the second of the winning thread or the best of another).
    fn row_merge(&self, seg: &Seg) -> String {
        let (half, per_row) = (self.half, self.per_row);
        let (a_end, row_out, col) = (seg.a_end, seg.row_out, seg.col);
        let mut s = String::new();
        for h in 0..2 {
            let first = h * half; // the first row of this half of the tile
            s.push_str(&format!(
                "    {{\n        workgroupBarrier();\n        if (lid.y / 8u == {h}u) {{\n            let base = lid.x * {half}u + (tr - {first}u);\n"));
            for r in 0..per_row {
                s.push_str(&format!("            scratch[base + {r}u] = vec2<u32>(u32(max(rb1_{r}, 0.0)), ri_{r});\n"));
            }
            s.push_str(&format!(r#"        }}
        workgroupBarrier();
        var w_x: u32 = 0u; var w_best: u32 = 0u; var w_col: u32 = 0xffffffffu; var sob: u32 = 0u;
        if (li < {half}u) {{
            for (var x = 0u; x < 16u; x++) {{
                let e = scratch[x * {half}u + li];
                if (e.x > w_best || (e.x == w_best && e.y < w_col)) {{ sob = w_best; w_best = e.x; w_col = e.y; w_x = x; }}
                else {{ sob = max(sob, e.x); }}
            }}
        }}
        workgroupBarrier();
"#));
            s.push_str(&format!(
                "        if (lid.y / 8u == {h}u) {{\n            let base = lid.x * {half}u + (tr - {first}u);\n"));
            for r in 0..per_row {
                s.push_str(&format!("            scratch[base + {r}u] = vec2<u32>(u32(max(rb2_{r}, 0.0)), 0u);\n"));
            }
            s.push_str(&format!(r#"        }}
        workgroupBarrier();
        if (li < {half}u) {{
            let row = a0 + {first}u + li;
            if (row < {a_end}) {{
                let second = max(sob, scratch[w_x * {half}u + li].x);
                out[{row_out}] = vec4<u32>(w_best, {col}, second, 0u);
            }}
        }}
    }}
"#));
        }
        s
    }

    /// The two kernels of a pair: `gemm`, one workgroup per tile of A over every tile of B, the
    /// bests of the rows written at the end and the partials of the columns per tile; `merge`,
    /// the partials of every column merged over the tiles of A.
    fn pair(&self) -> String {
        let (rows, cols) = (self.tile.rows, self.tile.cols);
        let mut s = self.declarations("n_a: u32, n_b: u32, tiles_a: u32, pad: u32", "");
        s.push_str(r#"
@compute @workgroup_size(16, 16)
fn gemm(@builtin(workgroup_id) wg: vec3<u32>, @builtin(local_invocation_id) lid: vec3<u32>,
        @builtin(local_invocation_index) li: u32) {
"#);
        s.push_str(&self.a_tile(PAIR.a_end));
        s.push_str(&format!("    let tiles_b = (p.n_b + {}u) / {cols}u;\n", cols - 1));
        s.push_str(&format!("    for (var t = 0u; t < tiles_b; t++) {{\n        let b0 = t * {cols}u;\n"));
        s.push_str(&self.b_tile(PAIR.b_end));
        s.push_str(&self.dot_products());
        s.push_str(&self.row_update(PAIR.b_end));
        s.push_str(&self.column_partials(PAIR.b_end));
        s.push_str("    }\n");
        s.push_str(&self.row_merge(&PAIR));
        s.push_str(&format!(r#"}}

@compute @workgroup_size(256)
fn merge(@builtin(global_invocation_id) gid: vec3<u32>) {{
    let j = gid.x;
    if (j >= p.n_b) {{ return; }}
    var best: u32 = 0u;
    var row: u32 = 0u;
    var second: u32 = 0u;
    for (var t = 0u; t < p.tiles_a; t++) {{
        let e = col_part[t * p.n_b + j];
        let sc = e.x >> 8u;
        if (sc > best) {{ second = max(best, e.y); best = sc; row = t * {rows}u + (e.x & 255u); }}
        else {{ second = max(second, max(sc, e.y)); }}
    }}
    out[p.n_a + j] = vec4<u32>(best, row, second, 0u);
}}
"#));
        s
    }

    /// The two kernels of a block of photos, the photos of A concatenated and padded to whole
    /// tiles of rows, those of B to whole tiles of columns, so that no tile holds two photos
    /// (`BlockLayout`). `seg` describes the tiles: for every tile of A its photo, the first and
    /// one past the last of its rows; for every tile of B its photo, its first and one past its
    /// last column, and whether it is the last tile of its photo; for every photo of A its first
    /// tile and how many it has. `gemm_block` runs the rows of its tile of A over every tile of B
    /// as `gemm` does, but flushes the bests of the rows at the last tile of each photo of B
    /// (into `out[row * photos_b + photo]`, the column counted within the photo) and starts them
    /// again; the partials of the columns are those of `gemm`. `merge_block` merges the partials
    /// of every valid column of B over the tiles of each photo of A (into `out[n_a * photos_b +
    /// photo_a * n_b + column]`, the row counted within the photo).
    fn block(&self) -> String {
        let (rows, cols) = (self.tile.rows, self.tile.cols);
        let params = "n_a: u32, n_b: u32, tiles_a: u32, photos_b: u32, tiles_b: u32, photos_a: u32, \
                      pad0: u32, pad1: u32";
        let mut s = self.declarations(params, "@group(0) @binding(5) var<storage, read> seg: array<vec4<u32>>;");
        s.push_str(r#"
@compute @workgroup_size(16, 16)
fn gemm_block(@builtin(workgroup_id) wg: vec3<u32>, @builtin(local_invocation_id) lid: vec3<u32>,
              @builtin(local_invocation_index) li: u32) {
    let sa = seg[wg.x];
"#);
        s.push_str(&self.a_tile(BLOCK.a_end));
        s.push_str("    for (var t = 0u; t < p.tiles_b; t++) {\n        let sb = seg[p.tiles_a + t];\n");
        s.push_str(&format!("        let b0 = t * {cols}u;\n"));
        s.push_str(&self.b_tile(BLOCK.b_end));
        s.push_str(&self.dot_products());
        s.push_str(&self.row_update(BLOCK.b_end));
        s.push_str(&self.column_partials(BLOCK.b_end));
        s.push_str("        if (sb.w == 1u) {\n");
        s.push_str(&self.row_merge(&BLOCK));
        s.push_str(&self.reset_rows());
        s.push_str("        }\n    }\n");
        s.push_str(&format!(r#"}}

@compute @workgroup_size(256)
fn merge_block(@builtin(global_invocation_id) gid: vec3<u32>) {{
    let pa = gid.y;
    let j = gid.x;
    if (j >= p.n_b || pa >= p.photos_a) {{ return; }}
    if (j >= seg[p.tiles_a + j / {cols}u].z) {{ return; }}
    let sp = seg[p.tiles_a + p.tiles_b + pa];
    var best: u32 = 0u;
    var row: u32 = 0u;
    var second: u32 = 0u;
    for (var k = 0u; k < sp.y; k++) {{
        let e = col_part[(sp.x + k) * p.n_b + j];
        let sc = e.x >> 8u;
        if (sc > best) {{ second = max(best, e.y); best = sc; row = k * {rows}u + (e.x & 255u); }}
        else {{ second = max(second, max(sc, e.y)); }}
    }}
    out[p.n_a * p.photos_b + pa * p.n_b + j] = vec4<u32>(best, row, second, 0u);
}}
"#));
        s
    }
}

/// The two kernels of a pair for `tile` (`Kernel::pair`).
fn shader(tile: Tile) -> String {
    Kernel::new(tile).pair()
}

/// The two kernels of a block for `tile` (`Kernel::block`).
fn block_shader(tile: Tile) -> String {
    Kernel::new(tile).block()
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

/// The environment variable of a test hook: set, the kernels of a block are refused as a backend
/// compiler would refuse them, so that a test sees the preselection leave the device to the others
/// on a computer whose GPU takes them.
pub const BLOCK_REFUSED_VAR: &str = "PPG_TEST_BLOCK_KERNEL_REFUSED";

/// A storage buffer of the bind group layout of the kernels.
fn storage_entry(binding: u32, read_only: bool) -> wgpu::BindGroupLayoutEntry {
    wgpu::BindGroupLayoutEntry {
        binding, visibility: wgpu::ShaderStages::COMPUTE, count: None,
        ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only }, has_dynamic_offset: false,
                                        min_binding_size: None },
    }
}

/// The uniform buffer of the parameters of the kernels.
fn uniform_entry(binding: u32) -> wgpu::BindGroupLayoutEntry {
    wgpu::BindGroupLayoutEntry {
        binding, visibility: wgpu::ShaderStages::COMPUTE, count: None,
        ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Uniform, has_dynamic_offset: false,
                                        min_binding_size: None },
    }
}

fn uniform_buffer(device: &wgpu::Device, size: u64, label: &str) -> wgpu::Buffer {
    device.create_buffer(&wgpu::BufferDescriptor {
        label: Some(label), size, usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
        mapped_at_creation: false })
}

/// The kernels of a block (`Kernel::block`): the two pipelines, their bind group layout (the
/// bindings of the pair kernels and the table of segments) and their parameters.
struct BlockKernels {
    gemm: wgpu::ComputePipeline,
    merge: wgpu::ComputePipeline,
    layout: wgpu::BindGroupLayout,
    params: wgpu::Buffer,
}

impl BlockKernels {
    /// The kernels built on `device` for `tile`, under error scopes: what the shader compiler of
    /// the backend or the pipeline refuses comes back as the error, instead of the panic of an
    /// uncaptured error of wgpu.
    fn build(device: &wgpu::Device, tile: Tile) -> Result<BlockKernels, String> {
        device.push_error_scope(wgpu::ErrorFilter::Validation);
        device.push_error_scope(wgpu::ErrorFilter::Internal);
        let module = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("match_block"), source: wgpu::ShaderSource::Wgsl(block_shader(tile).into()) });
        let layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor { label: None, entries: &[
            storage_entry(0, true), storage_entry(1, true), storage_entry(2, false), storage_entry(3, false),
            uniform_entry(4), storage_entry(5, true),
        ] });
        let pipeline_layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
            label: None, bind_group_layouts: &[&layout], push_constant_ranges: &[] });
        let make = |entry: &str| device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some(entry), layout: Some(&pipeline_layout), module: &module, entry_point: Some(entry),
            compilation_options: Default::default(), cache: None });
        let gemm = make("gemm_block");
        let merge = make("merge_block");
        let internal = pollster::block_on(device.pop_error_scope());
        let validation = pollster::block_on(device.pop_error_scope());
        if let Some(problem) = validation.or(internal) {
            return Err(format!("the block kernels are refused: {problem}"));
        }
        Ok(BlockKernels { gemm, merge, layout, params: uniform_buffer(device, 32, "block_params") })
    }
}

/// A matcher on one GPU: the kernels compiled once for the tiles the adapter's workgroup memory
/// takes, the descriptors of the recent photos kept on the device, the working buffers grown as
/// needed.
pub struct Matcher {
    pub info: AdapterInfo,
    tile: Tile,
    device: wgpu::Device,
    queue: wgpu::Queue,
    gemm: wgpu::ComputePipeline,
    merge: wgpu::ComputePipeline,
    layout: wgpu::BindGroupLayout,
    params: wgpu::Buffer,
    /// The kernels of a block, built at the first `bests_block` (or `prepare_block`): `None` until
    /// then, the error kept when the backend refused them.
    block: Option<Result<BlockKernels, String>>,
    out: Option<(wgpu::Buffer, wgpu::Buffer, u64)>,
    partials: Option<(wgpu::Buffer, u64)>,
    cache: HashMap<u64, (wgpu::Buffer, usize)>,
    order: VecDeque<u64>, // least recently used first
    cached_bytes: usize,
    limit: usize,
}

impl Matcher {
    /// On the adapter ``index`` of :func:`adapters`, or the best one (a discrete GPU, else an
    /// integrated one; a software rasteriser only when nothing else exists). The tiles of the
    /// kernels follow the adapter's workgroup memory (:func:`tile_for`): an adapter below 16384
    /// bytes is an error naming its figure and the one needed.
    pub fn new(index: Option<usize>) -> Result<Matcher, String> {
        Self::open(index, None)
    }

    /// As :meth:`new`, with the kernels of an adapter that had ``bytes`` of workgroup memory: the
    /// small tiles for 16384, the large ones for 32768 - the small path forced on a large GPU, for
    /// the tests and ``ppg-bench``. An error when the adapter has less than ``bytes``.
    pub fn with_workgroup_bytes(index: Option<usize>, bytes: u32) -> Result<Matcher, String> {
        Self::open(index, Some(bytes))
    }

    /// The workgroup memory the kernels of this matcher declare: 32768 bytes with the large
    /// tiles, 16384 with the small ones.
    pub fn workgroup_bytes(&self) -> u32 {
        self.tile.workgroup_bytes()
    }

    /// The tiles of the kernels of this matcher: ``rows`` descriptors of A by ``cols`` of B per
    /// workgroup and pass (the row within a tile is packed in 8 bits in the partials).
    pub fn tile(&self) -> Tile {
        self.tile
    }

    fn open(index: Option<usize>, bytes: Option<u32>) -> Result<Matcher, String> {
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
        let available = limits.max_compute_workgroup_storage_size;
        let needed = SMALL_TILE.workgroup_bytes();
        let tile = match bytes {
            Some(asked) if asked > available => {
                let name = &info.name;
                return Err(format!("{name}: {available} bytes of workgroup memory, {asked} asked"));
            }
            Some(asked) => tile_for(asked)
                .ok_or_else(|| format!("{asked} bytes of workgroup memory asked, {needed} needed"))?,
            None => tile_for(available)
                .ok_or_else(|| format!("{}: {available} bytes of workgroup memory, {needed} needed", info.name))?,
        };
        let (device, queue) = pollster::block_on(adapter.request_device(
            &wgpu::DeviceDescriptor { label: Some("ppg_core"), required_limits: limits, ..Default::default() }, None))
            .map_err(|e| format!("{}: {e}", info.name))?;
        let module = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("match"), source: wgpu::ShaderSource::Wgsl(shader(tile).into()) });
        let layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor { label: None, entries: &[
            storage_entry(0, true), storage_entry(1, true), storage_entry(2, false), storage_entry(3, false),
            uniform_entry(4),
        ] });
        let pipeline_layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor { label: None, bind_group_layouts: &[&layout], push_constant_ranges: &[] });
        let make = |entry: &str| device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some(entry), layout: Some(&pipeline_layout), module: &module, entry_point: Some(entry),
            compilation_options: Default::default(), cache: None });
        let gemm = make("gemm");
        let merge = make("merge");
        let params = uniform_buffer(&device, 16, "params");
        Ok(Matcher { info, tile, device, queue, gemm, merge, layout, params, block: None, out: None, partials: None,
                     cache: HashMap::new(), order: VecDeque::new(), cached_bytes: 0, limit: cache_limit() })
    }

    /// The kernels of a block built now, if they are not yet: an error when the backend refuses
    /// them (the pair kernels are untouched by it, and `bests_block` gives the same error). The
    /// preselection asks before it makes a device a worker, so that a device without them is
    /// left to the others.
    pub fn prepare_block(&mut self) -> Result<(), String> {
        self.block_kernels().map(|_| ())
    }

    /// The kernels of a block, built at the first call under error scopes (a shader a backend
    /// compiler refuses, a pipeline it cannot build) and kept, the error too. The test hook
    /// `BLOCK_REFUSED_VAR` refuses them as a backend would.
    fn block_kernels(&mut self) -> Result<&BlockKernels, String> {
        if self.block.is_none() {
            let built = if std::env::var_os(BLOCK_REFUSED_VAR).is_some() {
                Err(format!("the block kernels are refused for the test ({BLOCK_REFUSED_VAR} is set)"))
            } else {
                BlockKernels::build(&self.device, self.tile)
            };
            self.block = Some(built.map_err(|e| format!("{}: {e}", self.info.name)));
        }
        self.block.as_ref().expect("set above").as_ref().map_err(Clone::clone)
    }

    fn upload(&self, data: &[u8]) -> wgpu::Buffer {
        self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor { label: None, contents: data, usage: wgpu::BufferUsages::STORAGE })
    }

    /// The descriptors ``data`` (n x 128 bytes) on the device: cached under ``key`` (not 0), the
    /// photos used least recently forgotten first - never ``keep``, the other photo of the pair:
    /// its buffer is about to be bound (a destroyed buffer in a bind group is a validation error
    /// of wgpu, which stopped the matching of 48-megapixel photos once the cache was full).
    fn descriptors(&mut self, key: u64, data: &[u8], keep: u64) -> wgpu::Buffer {
        if key == 0 {
            return self.upload(data);
        }
        if let Some((buf, n)) = self.cache.get(&key) {
            if *n == data.len() {
                let buf = buf.clone();
                self.order.retain(|k| *k != key); // used now: the last to be forgotten
                self.order.push_back(key);
                return buf;
            }
            self.forget(key);
        }
        while self.cached_bytes + data.len() > self.limit {
            let victim = self.order.iter().position(|k| *k != keep || keep == 0);
            match victim.and_then(|i| self.order.remove(i)) {
                Some(old) => self.forget(old),
                None => break, // only the other photo of the pair is left: the cache goes over its limit
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
    /// A over B, then for every descriptor of B over A (ties: the lowest index, a second best equal
    /// to the best); nothing when either set is empty. The same as `matching::bests_cpu`.
    pub fn bests(&mut self, a_key: u64, a: &[u8], b_key: u64, b: &[u8]) -> Result<Vec<[u32; 3]>, String> {
        if a.len() % DIM != 0 || b.len() % DIM != 0 {
            return Err("descriptors are 128 bytes each".into());
        }
        let (n_a, n_b) = (a.len() / DIM, b.len() / DIM);
        if n_a == 0 || n_b == 0 {
            return Ok(Vec::new());
        }
        let tiles_a = n_a.div_ceil(self.tile.rows);
        let a_buf = self.descriptors(a_key, a, b_key);
        let b_buf = self.descriptors(b_key, b, a_key);
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
        let words: Vec<u32> = read_back(&self.device, &staging, out_size)?;
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

    /// The most bytes a working buffer of a block may take on this device: the storage binding
    /// it allows, or its largest buffer when that is less.
    pub fn block_limit(&self) -> u64 {
        let limits = self.device.limits();
        u64::from(limits.max_storage_buffer_binding_size).min(limits.max_buffer_size)
    }

    /// The bytes of the largest working buffer [`bests_block`](Self::bests_block) needs for
    /// photos of `a` and `b` descriptors each (the counts), with the tiles of this matcher: to be
    /// held under [`block_limit`](Self::block_limit) by whoever sizes the blocks.
    pub fn block_bytes(&self, a: &[usize], b: &[usize]) -> u64 {
        let (out, partials) = BlockLayout::new(self.tile, a, b).bytes();
        out.max(partials)
    }

    /// The bests of every pair of two blocks of photos in one dispatch: for the photos `a[i]` and
    /// `b[j]` (n x 128 bytes each) the entry `i * b.len() + j` of the answer is what
    /// [`bests`](Self::bests) gives for that pair, bit for bit (nothing for a pair with an empty
    /// photo). The descriptors are not kept on the device. An error when a set is not whole
    /// descriptors, when the working buffers of the block are above
    /// [`block_limit`](Self::block_limit), naming the bytes, or when the backend refuses the
    /// kernels of a block (built at the first call: [`prepare_block`](Self::prepare_block)).
    pub fn bests_block(&mut self, a: &[&[u8]], b: &[&[u8]]) -> Result<Vec<Vec<[u32; 3]>>, String> {
        if a.iter().chain(b).any(|d| d.len() % DIM != 0) {
            return Err("descriptors are 128 bytes each".into());
        }
        self.block_kernels()?;
        let counts = |set: &[&[u8]]| -> Vec<usize> { set.iter().map(|d| d.len() / DIM).collect() };
        let (count_a, count_b) = (counts(a), counts(b));
        let layout = BlockLayout::new(self.tile, &count_a, &count_b);
        let mut result = vec![Vec::new(); a.len() * b.len()];
        if layout.photos_a == 0 || layout.photos_b == 0 {
            return Ok(result);
        }
        let (out_size, part_size) = layout.bytes();
        let limit = self.block_limit();
        if out_size.max(part_size) > limit {
            return Err(format!("a block of {} x {} photos needs a working buffer of {} bytes, more than the {limit} \
                                this device binds", layout.photos_a, layout.photos_b, out_size.max(part_size)));
        }
        let padded = |set: &[&[u8]], map: &[Option<(usize, usize)>], n: usize| -> Vec<u8> {
            let mut bytes = vec![0u8; n * DIM];
            for (d, m) in set.iter().zip(map) {
                if let Some((_, begin)) = m {
                    bytes[begin * DIM..begin * DIM + d.len()].copy_from_slice(d);
                }
            }
            bytes
        };
        let a_buf = self.upload(&padded(a, &layout.a, layout.n_a));
        let b_buf = self.upload(&padded(b, &layout.b, layout.n_b));
        let seg_buf = self.upload(bytemuck::cast_slice(&layout.seg));
        let (out, staging, part) = self.working(out_size, part_size);
        let kernels = match &self.block {
            Some(Ok(kernels)) => kernels,
            _ => unreachable!("the kernels of a block were built above"),
        };
        let params = BlockParams { n_a: layout.n_a as u32, n_b: layout.n_b as u32, tiles_a: layout.tiles_a as u32,
                                   photos_b: layout.photos_b as u32, tiles_b: layout.tiles_b as u32,
                                   photos_a: layout.photos_a as u32, _pad: [0; 2] };
        self.queue.write_buffer(&kernels.params, 0, bytemuck::bytes_of(&params));
        let bind = self.device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: None, layout: &kernels.layout,
            entries: &[
                wgpu::BindGroupEntry { binding: 0, resource: a_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 1, resource: b_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 2, resource: out.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 3, resource: part.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 4, resource: kernels.params.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 5, resource: seg_buf.as_entire_binding() },
            ],
        });
        let mut enc = self.device.create_command_encoder(&Default::default());
        {
            let mut pass = enc.begin_compute_pass(&Default::default());
            pass.set_pipeline(&kernels.gemm);
            pass.set_bind_group(0, &bind, &[]);
            pass.dispatch_workgroups(layout.tiles_a as u32, 1, 1);
            pass.set_pipeline(&kernels.merge);
            pass.set_bind_group(0, &bind, &[]);
            pass.dispatch_workgroups(layout.n_b.div_ceil(256) as u32, layout.photos_a as u32, 1);
        }
        enc.copy_buffer_to_buffer(&out, 0, &staging, 0, out_size);
        self.queue.submit(Some(enc.finish()));
        let words: Vec<u32> = read_back(&self.device, &staging, out_size)?;
        a_buf.destroy();
        b_buf.destroy();
        seg_buf.destroy();
        let entry = |k: usize| [words[4 * k], words[4 * k + 1], words[4 * k + 2]];
        let columns_from = layout.n_a * layout.photos_b;
        for (i, ma) in layout.a.iter().enumerate() {
            for (j, mb) in layout.b.iter().enumerate() {
                let (Some((pa, begin_a)), Some((pb, begin_b))) = (ma, mb) else { continue };
                let (n_a, n_b) = (count_a[i], count_b[j]);
                let mut pair = Vec::with_capacity(n_a + n_b);
                pair.extend((0..n_a).map(|r| entry((begin_a + r) * layout.photos_b + pb)));
                pair.extend((0..n_b).map(|c| entry(columns_from + pa * layout.n_b + begin_b + c)));
                result[i * b.len() + j] = pair;
            }
        }
        Ok(result)
    }
}

/// Block until every submission of the queue of `device` has completed, reading its fence
/// (``Maintain::Poll``) until the queue is empty. Not ``Maintain::Wait``: in wgpu 24 it gives up
/// after 60 s without saying so and treats the submission as done - the results are mapped and
/// read before the kernel wrote them, and the queue is destroyed under the kernel (the panic "We
/// timed out while waiting on the last successful submission to complete!" and an access violation
/// on WARP, where 4096 x 4096 descriptors take minutes). The fence is read in a tight loop for the
/// first millisecond (a small pair answers within it), then with pauses that grow to a
/// millisecond, so a long kernel costs no core. Every kernel of the engine waits through this
/// function (the matcher, the SIFT).
pub(crate) fn wait_idle(device: &wgpu::Device) {
    let started = Instant::now();
    let mut pause = Duration::from_micros(50);
    while !device.poll(wgpu::Maintain::Poll).is_queue_empty() {
        if started.elapsed() < Duration::from_millis(1) {
            std::thread::yield_now();
        } else {
            std::thread::sleep(pause);
            pause = (pause * 2).min(Duration::from_millis(1));
        }
    }
}

/// The first `size` bytes of the staging buffer `buffer` (``MAP_READ``) once the submissions
/// before this call have completed, as a vector of `T` (words, key points, bytes): mapped for
/// reading, waited for with [`wait_idle`], copied into a vector whose alignment is the type's
/// whatever the mapping's, and unmapped. The mapping is done by the poll that finds the copy
/// complete, at the latest by the one after the queue is empty. `size` is a multiple of the size
/// of `T` (a last partial element would come back zero-padded).
pub(crate) fn read_back<T: bytemuck::NoUninit + bytemuck::AnyBitPattern>(device: &wgpu::Device, buffer: &wgpu::Buffer,
                                                                          size: u64) -> Result<Vec<T>, String> {
    let slice = buffer.slice(..size);
    let (tx, rx) = std::sync::mpsc::channel();
    slice.map_async(wgpu::MapMode::Read, move |r| { let _ = tx.send(r); });
    let mapped = loop {
        wait_idle(device);
        match rx.try_recv() {
            Ok(mapped) => break mapped,
            Err(TryRecvError::Empty) => std::thread::yield_now(),
            Err(TryRecvError::Disconnected) => return Err("the results of the kernel were not mapped".into()),
        }
    };
    mapped.map_err(|e| e.to_string())?;
    // copied into a vector of T (what bytemuck's pod_collect_to_vec does, behind a feature this
    // crate is not built with): the mapping's alignment never reaches the caller
    let bytes = slice.get_mapped_range();
    let mut data = vec![T::zeroed(); bytes.len().div_ceil(std::mem::size_of::<T>())];
    bytemuck::cast_slice_mut::<T, u8>(&mut data)[..bytes.len()].copy_from_slice(&bytes);
    drop(bytes);
    buffer.unmap();
    Ok(data)
}

fn distance(score: u32) -> f32 {
    (score as f32 / (512.0 * 512.0)).min(1.0).acos()
}

fn passes(best: u32, second: u32, max_ratio: f32, max_distance: f32) -> bool {
    let d1 = distance(best);
    d1 <= max_distance && d1 < max_ratio * distance(second)
}

/// The quality of every match of `matches` (pairs of indices into A and B) out of the bests of
/// the pair: the ratio of the two best distances of its descriptor of A, lower for a match that
/// stands out - the order of the PROSAC sampling of the verification (`geometry::two_view`). A
/// second best of 0 is the distance of orthogonal descriptors, so the ratio is always finite.
pub fn match_quality(bests: &[[u32; 3]], matches: &[[u32; 2]]) -> Vec<f32> {
    matches
        .iter()
        .map(|m| {
            let [best, _, second] = bests[m[0] as usize];
            distance(best) / distance(second)
        })
        .collect()
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
    fn the_tiles_fill_their_workgroup_memory_and_are_chosen_from_it() {
        assert_eq!(LARGE_TILE.workgroup_bytes(), 32768);
        assert_eq!(SMALL_TILE.workgroup_bytes(), 16384);
        assert_eq!(tile_for(65536), Some(LARGE_TILE));
        assert_eq!(tile_for(32768), Some(LARGE_TILE));
        assert_eq!(tile_for(32767), Some(SMALL_TILE));
        assert_eq!(tile_for(16384), Some(SMALL_TILE));
        assert_eq!(tile_for(16383), None);
        // the kernels declare the arrays the figures count: words of the two tiles, entries of scratch
        let small = shader(SMALL_TILE);
        for declared in ["a_tile: array<u32, 2048>", "b_tile: array<u32, 1024>", "scratch: array<vec2<u32>, 512>"] {
            assert!(small.contains(declared), "the small kernel has no {declared}");
        }
        let large = shader(LARGE_TILE);
        for declared in ["a_tile: array<u32, 4096>", "b_tile: array<u32, 2048>", "scratch: array<vec2<u32>, 1024>"] {
            assert!(large.contains(declared), "the large kernel has no {declared}");
        }
        // the block kernels declare the same arrays, their entry points and the table of segments
        for tile in [SMALL_TILE, LARGE_TILE] {
            let block = block_shader(tile);
            let same = shader(tile);
            for declared in ["a_tile: array<u32, ", "b_tile: array<u32, ", "scratch: array<vec2<u32>, "] {
                let line = |s: &str| s.lines().find(|l| l.contains(declared)).map(str::to_owned);
                assert_eq!(line(&block), line(&same), "{declared} of the block kernel");
            }
            let named = ["fn gemm_block(", "fn merge_block(", "var<storage, read> seg: array<vec4<u32>>", "sb.w == 1u"];
            for name in named {
                assert!(block.contains(name), "the block kernel has no {name}");
            }
        }
    }

    #[test]
    fn a_block_is_laid_out_in_whole_tiles_with_the_empty_photos_left_out() {
        let layout = BlockLayout::new(LARGE_TILE, &[300, 0, 129], &[65, 0, 1]);
        assert_eq!(layout.a, vec![Some((0, 0)), None, Some((1, 384))]);
        assert_eq!(layout.b, vec![Some((0, 0)), None, Some((1, 128))]);
        // 300 rows take three tiles of 128, 129 take two; 65 columns take two tiles of 64, 1 takes one
        assert_eq!((layout.n_a, layout.tiles_a, layout.photos_a), (640, 5, 2));
        assert_eq!((layout.n_b, layout.tiles_b, layout.photos_b), (192, 3, 2));
        // the segments: the tiles of A (photo, first row, one past the last), those of B (with the
        // last tile of each photo flagged), the photos of A (first tile, tiles)
        let tiles_a = [[0, 0, 300, 0], [0, 0, 300, 0], [0, 0, 300, 0], [1, 384, 513, 0], [1, 384, 513, 0]];
        let tiles_b = [[0, 0, 65, 0], [0, 0, 65, 1], [1, 128, 129, 1]];
        let photos_a = [[0, 3, 0, 0], [3, 2, 0, 0]];
        let seg: Vec<[u32; 4]> = tiles_a.iter().chain(&tiles_b).chain(&photos_a).copied().collect();
        assert_eq!(layout.seg, seg);
        // the results: 640 rows x 2 photos of B plus 2 photos of A x 192 columns, 16 bytes each;
        // the partials: 5 tiles x 192 columns, 8 bytes each
        assert_eq!(layout.bytes(), ((640 * 2 + 2 * 192) * 16, 5 * 192 * 8));
        let empty = BlockLayout::new(SMALL_TILE, &[0, 0], &[10]);
        assert_eq!((empty.photos_a, empty.photos_b, empty.n_b, empty.tiles_b), (0, 1, 32, 1));
    }

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
