//! Builds RTKLIB (vendor/rtklib, rtklibexplorer's v2.5.1, BSD 2-clause) and the shim that offers
//! its post-processing to Rust (src/ppk_shim.c) into a static library of the extension.
//!
//! The sources are those rnx2rtkp is made of, with the settings of its builds (all the satellite
//! systems, three frequencies, three extended observation codes); RTKLIB's files are not changed.
//! postpos.c is compiled through the shim (see ppk_shim.c).

use std::env;
use std::path::Path;

/// RTKLIB's sources of rnx2rtkp (app/consapp/rnx2rtkp/gcc/makefile), postpos.c apart.
const SOURCES: &[&str] = &[
    "rtkcmn.c", "trace.c", "rinex.c", "rtkpos.c", "solution.c", "lambda.c", "geoid.c", "sbas.c", "preceph.c",
    "pntpos.c", "ephemeris.c", "options.c", "ppp.c", "ppp_ar.c", "rtcm.c", "rtcm2.c", "rtcm3.c", "rtcm3e.c",
    "ionex.c", "tides.c",
];

fn main() {
    let src = Path::new("vendor/rtklib/src");
    let windows = env::var("CARGO_CFG_TARGET_OS").map(|os| os == "windows").unwrap_or(false);

    let mut build = cc::Build::new();
    build.include(src);
    for name in ["ENAGLO", "ENAQZS", "ENAGAL", "ENACMP", "ENAIRN"] {
        build.define(name, None);
    }
    build.define("NFREQ", "3").define("NEXOBS", "3");
    if windows {
        // RTKLIB's WIN32 code; its functions linked in, not imported from a DLL (rtklib.h, EXPORT)
        build.define("WIN32", None).define("WIN_STATIC", None).define("_CRT_SECURE_NO_WARNINGS", None);
    }
    // a third party's code: its warnings are not ours to fix
    build.warnings(false).flag_if_supported("-w");
    for name in SOURCES {
        build.file(src.join(name));
    }
    build.file("src/ppk_shim.c");
    build.compile("rtklib");

    if windows {
        println!("cargo:rustc-link-lib=winmm");
        println!("cargo:rustc-link-lib=ws2_32");
    } else if env::var("CARGO_CFG_TARGET_OS").map(|os| os != "macos" && os != "ios").unwrap_or(true) {
        println!("cargo:rustc-link-lib=m");
    }
    println!("cargo:rerun-if-changed=vendor/rtklib/src");
    println!("cargo:rerun-if-changed=src/ppk_shim.c");
    println!("cargo:rerun-if-changed=build.rs");
}
