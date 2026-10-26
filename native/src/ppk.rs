//! RTKLIB's post-processing (PPK) inside the core: `ppk_solve` runs what `rnx2rtkp -k conf -o
//! outfile infiles` runs, through `ppg_ppk` of `ppk_shim.c`, built with RTKLIB (vendor/rtklib,
//! BSD 2-clause) by build.rs. RTKLIB keeps its state in globals: one run at a time.

use std::ffi::{c_char, c_int, CString};
use std::sync::Mutex;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

extern "C" {
    fn ppg_ppk(conf: *const c_char, infiles: *const *const c_char, n: c_int, outfile: *const c_char,
               msg: *mut c_char, msglen: c_int) -> c_int;
}

/// RTKLIB is not reentrant: its globals hold the observations, the options and the files of a run.
static RTKLIB: Mutex<()> = Mutex::new(());

const MSGLEN: usize = 1024;

fn c_text(text: &str, what: &str) -> PyResult<CString> {
    CString::new(text).map_err(|_| PyValueError::new_err(format!("{what} holds a NUL character: {text:?}")))
}

/// RTKLIB's post-processing as `rnx2rtkp -k conf -o outfile infiles` (API 8): the options file,
/// the input files (rover observations first, then the base's observations, then navigation
/// files) and the solution file. Returns RTKLIB's code (0 done, 1 aborted, -1 a file named by
/// the options could not be read; -2 the options file could not be read, -3 bad arguments, -4 a
/// compressed input, which RTKLIB would decompress through a shell command) and its last
/// message, the last error it reported if any. RTKLIB returns 0 also when it computed
/// nothing ("error : no obs data"): the caller reads the solution file. Runs without the GIL,
/// one run at a time in the process.
#[pyfunction]
pub fn ppk_solve(py: Python<'_>, conf: &str, infiles: Vec<String>, outfile: &str) -> PyResult<(i32, String)> {
    let conf = c_text(conf, "the options file")?;
    let outfile = c_text(outfile, "the output file")?;
    let files = infiles.iter().map(|f| c_text(f, "an input file")).collect::<PyResult<Vec<CString>>>()?;
    let n = c_int::try_from(files.len()).map_err(|_| PyValueError::new_err("too many input files"))?;
    let (code, msg) = py.allow_threads(move || {
        let pointers: Vec<*const c_char> = files.iter().map(|f| f.as_ptr()).collect();
        let mut msg = vec![0u8; MSGLEN];
        // a run that panicked in Rust leaves nothing half done in RTKLIB: the lock can be taken again
        let _guard = RTKLIB.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        // SAFETY: every pointer is a NUL-terminated string alive until the call returns, msg is
        // MSGLEN bytes and ppg_ppk writes at most msglen bytes, NUL included; the lock keeps
        // RTKLIB's globals to one caller
        let code = unsafe {
            ppg_ppk(conf.as_ptr(), pointers.as_ptr(), n, outfile.as_ptr(), msg.as_mut_ptr() as *mut c_char,
                    MSGLEN as c_int)
        };
        let end = msg.iter().position(|&b| b == 0).unwrap_or(msg.len());
        (code, String::from_utf8_lossy(&msg[..end]).into_owned())
    });
    Ok((code, msg))
}
