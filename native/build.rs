//! Builds RTKLIB (vendor/rtklib, rtklibexplorer's v2.5.1, BSD 2-clause) and the shim that offers
//! its post-processing to Rust (src/ppk_shim.c) into a static library of the extension.
//!
//! The sources are those rnx2rtkp is made of, with the settings of its builds (all the satellite
//! systems, three frequencies, three extended observation codes); RTKLIB's files are not changed.
//! postpos.c is compiled through the shim (see ppk_shim.c). sofa.c, which rnx2rtkp's makefile also
//! lists, is not here: it holds routines of the IAU SOFA library (own licence), which rtkcmn.c calls
//! only if SUNPOS_SOFA or MOONPOS_SOFA is defined, and neither is (see vendor/rtklib/VERSION).
//!
//! Every compiler builds it as C99 (old GCCs default to C89, which RTKLIB's loops are not) and
//! without contractions: a fused multiply-add rounds once where RTKLIB, as written, rounds twice,
//! and the integer ambiguities of a kinematic solution are sensitive to that last bit (on RTKLIB's
//! test data 115 fixed epochs out of 120 with FMA, 40 without). Without contractions the arm64
//! builds (where GCC and Clang contract by default) round as the x86 ones and as RTKLIB's own
//! builds; the maths library of each system (sin, cos, exp) and the optimiser still move such a
//! marginal case a little (39 to 49 fixed epochs on that data, a static solution 120 of 120 on all).

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
    // C99, and multiplications and additions rounded one by one as written (see above)
    let compiler = build.get_compiler();
    if compiler.is_like_msvc() {
        // no contractions under /fp:precise from Visual Studio 2022 (/fp:contract would add them)
        build.flag("/fp:precise");
        if compiler.is_like_clang_cl() {
            // clang's precise model contracts within an expression
            build.flag("/clang:-ffp-contract=off");
        }
    } else if compiler.is_like_gnu() || compiler.is_like_clang() {
        build.flag("-std=gnu99").flag("-ffp-contract=off");
    }
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
