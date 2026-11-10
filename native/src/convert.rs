//! Raw receiver logs converted into RINEX inside the core (API 11): `raw_to_rinex` does what RTKLIB's
//! `convbin -r <format> -v <version> [-tr <time>] -o obs -n nav <log>` does, through `ppg_convbin` of ppk_shim.c and
//! convrnx.c of RTKLIB-EX (vendor/rtklib, BSD 2-clause, unchanged). The decoders built are u-blox (rcv/ublox.c),
//! NovAtel (rcv/novatel.c) and RTCM 3 (rtcm*.c); Septentrio SBF, Trimble RT-17 and Unicore are known by name and
//! refused (code -6): their decoders are not part of the package (vendor/rtklib/VERSION says why).

use std::collections::HashMap;
use std::ffi::{c_char, c_double, c_int};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use pyo3::prelude::*;

use crate::ppk::c_text;
use crate::rtklib::{self, MSGLEN};

extern "C" {
    fn ppg_convbin_format(name: *const c_char) -> c_int;
    fn ppg_convbin(format: c_int, infile: *const c_char, approx_gps_seconds: c_double, rinex_version: c_int,
                   navsys: c_int, obs_out: *const c_char, nav_out: *const c_char, counts: *mut c_int,
                   msg: *mut c_char, msglen: c_int) -> c_int;
}

/// Raised by `convert_cancel`, read by the shim's `showmsg` while a conversion runs (RTKLIB stops on it), lowered
/// when a conversion starts.
static CANCEL: AtomicBool = AtomicBool::new(false);

/// For ppk_shim.c: the conversion running now is to stop.
#[no_mangle]
pub extern "C" fn ppg_convert_cancelled() -> c_int {
    c_int::from(CANCEL.load(Ordering::SeqCst))
}

/// The conversion running now (`raw_to_rinex`, any thread) stops at RTKLIB's next status message and leaves no file;
/// nothing happens when none runs, and a later conversion is not affected.
#[pyfunction]
pub fn convert_cancel() {
    CANCEL.store(true, Ordering::SeqCst);
}

/// The SYS_ mask of RTKLIB of a string of system letters (G R E J S C I), or the letter that is none of them.
fn navsys(systems: &str) -> Result<c_int, char> {
    let mut mask = 0;
    for letter in systems.chars() {
        mask |= match letter {
            'G' => 0x01, // SYS_GPS
            'S' => 0x02, // SYS_SBS
            'R' => 0x04, // SYS_GLO
            'E' => 0x08, // SYS_GAL
            'J' => 0x10, // SYS_QZS
            'C' => 0x20, // SYS_CMP
            'I' => 0x40, // SYS_IRN
            other => return Err(other),
        };
    }
    Ok(mask)
}

/// "3.04" as RTKLIB's 304; None unless it is a digit, a dot and two digits.
fn rinex_version_x100(text: &str) -> Option<c_int> {
    let b = text.as_bytes();
    if b.len() != 4 || b[1] != b'.' || ![b[0], b[2], b[3]].iter().all(u8::is_ascii_digit) {
        return None;
    }
    Some(c_int::from(b[0] - b'0') * 100 + c_int::from(b[2] - b'0') * 10 + c_int::from(b[3] - b'0'))
}

fn with_part(path: &str) -> PathBuf {
    let mut name = Path::new(path).as_os_str().to_owned();
    name.push(".part");
    PathBuf::from(name)
}

fn counts_of(counts: [c_int; 4]) -> HashMap<String, i64> {
    ["epochs", "events", "navigation", "errors"]
        .iter()
        .zip(counts)
        .map(|(name, value)| (name.to_string(), i64::from(value)))
        .collect()
}

/// A raw receiver log converted into RINEX observations and navigation messages, as RTKLIB-EX's
/// `convbin -r <format> -v <version> -o obs -n nav src` converts it (API 11), with `-tr` from `approx_gps_seconds`
/// (seconds since 1980-01-06 in GPS time; needed by RTCM 3, which carries the time of the week only) and the systems
/// of `systems` (letters G R E J S C I; convbin's `-y` leaves the others out). `format`: "ubx" (u-blox), "rtcm3",
/// "novatel" (OEM4/V/6/7, OEMStar), and "sbf", "rt17", "unicore", which are known and refused (code -6): their
/// decoders are not part of this package. `rinex_version` "3.00" to "3.05" (one navigation file for every system).
///
/// Returns the shim's code (0 done; 1 cancelled by `convert_cancel`; -1 `src` could not be read or an output not
/// written; -2 no observations decoded: not a log of that format, or damaged; -3 bad arguments: a format not known,
/// a path of 1000 bytes or more or with '*' or '%', which RTKLIB would replace, no approximate time for RTCM 3, a
/// version or systems out of range; -5 the thread could not be put in the "C" locale for numbers; -6 a decoder not
/// built), RTKLIB's last status line or the error, and the counts {"epochs", "events", "navigation", "errors"} of
/// RTKLIB (events: the time marks written as event records, flag 5). The files are written beside `obs` and `nav`
/// with ".part" after their names and renamed when the conversion is done: on any other code neither is there, and
/// a `nav` of an earlier run is removed when the log has no navigation message. Runs without the GIL, behind the
/// lock that `ppk_solve` takes too, in the "C" numeric locale of the calling thread.
#[pyfunction]
#[pyo3(signature = (format, src, obs, nav, approx_gps_seconds=None, rinex_version="3.04", systems="GREJC"))]
#[allow(clippy::too_many_arguments)]
pub fn raw_to_rinex(py: Python<'_>, format: &str, src: &str, obs: &str, nav: &str, approx_gps_seconds: Option<f64>,
                    rinex_version: &str, systems: &str) -> PyResult<(i32, String, HashMap<String, i64>)> {
    let refused = |message: String| Ok((-3, message, counts_of([0; 4])));
    let c_format = c_text(format, "the format")?;
    // SAFETY: a NUL-terminated string alive during the call; the function only reads it
    let code = unsafe { ppg_convbin_format(c_format.as_ptr()) };
    if code < 0 {
        return refused(format!("error : the format {format:?} is not one of ubx, rtcm3, novatel, sbf, rt17, unicore"));
    }
    let Some(version) = rinex_version_x100(rinex_version) else {
        return refused(format!("error : RINEX version {rinex_version}: 3.00 to 3.05 are written here"));
    };
    let mask = match navsys(systems) {
        Ok(mask) => mask,
        Err(letter) => return refused(format!("error : {letter:?} is not a satellite system (G R E J S C I)")),
    };
    let approx = approx_gps_seconds.filter(|t| t.is_finite() && *t > 0.0).unwrap_or(0.0);
    let infile = c_text(src, "the input file")?;
    let (obs_part, nav_part) = (with_part(obs), with_part(nav));
    let obs_c = c_text(&obs_part.to_string_lossy(), "the observation file")?;
    let nav_c = c_text(&nav_part.to_string_lossy(), "the navigation file")?;
    let (obs, nav) = (PathBuf::from(obs), PathBuf::from(nav));

    let (code, message, counts) = py.allow_threads(move || {
        let mut msg = vec![0u8; MSGLEN];
        let mut counts: [c_int; 4] = [0; 4];
        let _guard = rtklib::lock(); // shared with ppk_solve (ppk.rs)
        let _ = fs::remove_file(&obs_part); // a part left by a process that died
        let _ = fs::remove_file(&nav_part);
        CANCEL.store(false, Ordering::SeqCst);
        // SAFETY: every pointer is a NUL-terminated string alive until the call returns, counts holds the 4 ints the
        // shim writes, msg is MSGLEN bytes and the shim writes at most msglen bytes, NUL included; the lock keeps
        // RTKLIB's globals to one caller
        let mut code = unsafe {
            ppg_convbin(code, infile.as_ptr(), approx, version, mask, obs_c.as_ptr(), nav_c.as_ptr(),
                        counts.as_mut_ptr(), msg.as_mut_ptr() as *mut c_char, MSGLEN as c_int)
        };
        let mut message = rtklib::message(&msg);
        if code == 0 {
            let renamed = fs::rename(&obs_part, &obs).map_err(|e| (obs.clone(), e)).and_then(|()| {
                if nav_part.exists() {
                    fs::rename(&nav_part, &nav).map_err(|e| (nav.clone(), e))
                } else {
                    // no navigation message in the log: no file, and none of an earlier run beside the new one
                    match fs::remove_file(&nav) {
                        Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err((nav.clone(), e)),
                        _ => Ok(()),
                    }
                }
            });
            if let Err((path, error)) = renamed {
                let _ = fs::remove_file(&obs);
                code = -1;
                message = format!("error : cannot write {}: {error}", path.display());
            }
        }
        if code != 0 {
            let _ = fs::remove_file(&obs_part);
            let _ = fs::remove_file(&nav_part);
        }
        (code, message, counts)
    });
    Ok((code, message, counts_of(counts)))
}
