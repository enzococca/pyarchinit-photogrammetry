//! The engine crate used from outside, without Python: the adapters are listed and a kernel that
//! needs no GPU (the triangulation of the TIN) answers.

use ppg_engine::{gpu, tin};

#[test]
fn adapters_are_listed_without_panicking() {
    // no adapter is a valid answer (CI runners); 64 is a bound no computer reaches, not a
    // measured figure: the assertions that can fail are those on the fields of each adapter
    let adapters = gpu::adapters();
    assert!(adapters.len() < 64);
    for (position, adapter) in adapters.iter().enumerate() {
        assert_eq!(adapter.index, position);
        // the name is not asserted: some drivers give an empty one
        assert!(!adapter.backend.is_empty(), "adapter {position} has no backend");
        assert!(!adapter.device_type.is_empty(), "adapter {position} has no device type");
    }
}

#[test]
fn tin_of_a_square_has_two_triangles() {
    // four corners of the unit square through the public triangulation of tin.rs:
    // 2 triangles, every vertex index < 4
    let square = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]];
    let triangulated = tin::triangulate(&square, &[]).expect("the unit square is triangulated");
    assert_eq!(triangulated.triangles.len(), 2);
    for triangle in &triangulated.triangles {
        for index in triangle {
            assert!((0..4i64).contains(index), "vertex index {index} of the triangle {triangle:?}");
        }
    }
}
