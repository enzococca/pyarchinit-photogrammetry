//! What the RTKLIB functions of the core share: RTKLIB keeps its state in globals (the options, the observations and
//! the files of a run, and the shim's messages), so its post-processing (`ppk_solve`, ppk.rs) and its conversion of
//! raw logs (`raw_to_rinex`, convert.rs) run one at a time in the process, behind one lock.

use std::sync::{Mutex, MutexGuard};

static RTKLIB: Mutex<()> = Mutex::new(());

/// The lock of RTKLIB's globals. A run that panicked in Rust leaves nothing half done in RTKLIB: the lock can be
/// taken again.
pub fn lock() -> MutexGuard<'static, ()> {
    RTKLIB.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// The size of the message buffers given to the shim.
pub const MSGLEN: usize = 1024;

/// The text the shim wrote into `msg`, up to its NUL.
pub fn message(msg: &[u8]) -> String {
    let end = msg.iter().position(|&b| b == 0).unwrap_or(msg.len());
    String::from_utf8_lossy(&msg[..end]).into_owned()
}
