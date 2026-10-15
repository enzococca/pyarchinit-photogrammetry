"""Build the desktop application with PyInstaller and package it for distribution.

    python packaging/build.py [--no-smoke] [--skip-build]

Produces, in ``dist/``:

* macOS:   ``PyArchInit-Photogrammetry-<version>-macos-<arch>.dmg`` (and the ``.app``)
* Windows: ``PyArchInit-Photogrammetry-<version>-windows-x64.zip``
* Linux:   ``PyArchInit-Photogrammetry-<version>-linux-x64.tar.gz``

macOS signing: ``PPG_CODESIGN_IDENTITY="Developer ID Application: Name (TEAMID)"`` signs every
binary with the hardened runtime (``packaging/entitlements.plist``); with
``PPG_NOTARY_PROFILE=<keychain profile>`` the dmg is also notarized and stapled.
``PPG_CODESIGN_IDENTITY=-`` makes an ad-hoc signature (local tests).

The icons (``.icns``, ``.ico``, ``.png``) are rendered from ``gui/resources/logo.svg``.
After building, the application is started with ``--check`` (offscreen) as a smoke test.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "PyArchInit Photogrammetry"
BUILD = ROOT / "packaging" / "build"
DIST = ROOT / "dist"


def version() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from pyarchinit_photogrammetry import __version__

    return __version__


def render_icons() -> None:
    """logo.svg -> icon.png (1024), icon.icns (macOS), icon.ico (Windows)."""
    from PySide6.QtCore import QSize, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    BUILD.mkdir(parents=True, exist_ok=True)
    app = QGuiApplication.instance() or QGuiApplication([])
    svg = QSvgRenderer(str(ROOT / "src" / "pyarchinit_photogrammetry" / "gui" / "resources" / "logo.svg"))
    pngs = {}
    for size in (16, 32, 64, 128, 256, 512, 1024):
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        svg.render(p)
        p.end()
        path = BUILD / f"icon_{size}.png"
        img.save(str(path))
        pngs[size] = path
    shutil.copy(pngs[1024], BUILD / "icon.png")
    if sys.platform == "darwin":
        iconset = BUILD / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        for size in (16, 32, 128, 256, 512):
            shutil.copy(pngs[size], iconset / f"icon_{size}x{size}.png")
            shutil.copy(pngs[size * 2], iconset / f"icon_{size}x{size}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(BUILD / "icon.icns")], check=True)
    try:
        from PIL import Image

        Image.open(pngs[256]).save(BUILD / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128),
                                                               (256, 256)])
    except Exception as exc:  # pragma: no cover
        print("ico not written:", exc)


def run_pyinstaller() -> Path:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(DIST),
           "--workpath", str(BUILD / "work"), str(ROOT / "packaging" / "ppg_gui.spec")]
    print(" ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)
    if sys.platform == "darwin":
        return DIST / f"{NAME}.app"
    return DIST / NAME


def executable(bundle: Path) -> Path:
    if sys.platform == "darwin":
        return bundle / "Contents" / "MacOS" / NAME
    if os.name == "nt":
        return bundle / f"{NAME}.exe"
    return bundle / NAME


def fix_private_dylibs(bundle: Path) -> int:
    """macOS: make every package load the libraries of its own ``.dylibs`` folder.

    Wheels built with delocate reference their private libraries as
    ``@loader_path/.dylibs/<name>``; PyInstaller rewrites them to ``@rpath/<name>`` and adds
    one top-level link per name, so two packages that ship a library with the same name
    (rasterio and pyproj both have ``libproj.25.*.dylib``, with different exports) end up
    sharing one copy and fail to import. Rewriting the references back to ``@loader_path``
    restores the layout of the wheels. Returns the number of patched binaries."""
    if sys.platform != "darwin":
        return 0
    fw = bundle / "Contents" / "Frameworks"
    patched = 0
    for pkg in sorted(p for p in fw.iterdir() if p.is_dir()):
        private = pkg / "__dot__dylibs"
        if not private.is_dir():
            continue
        names = {p.name for p in private.iterdir() if p.suffix == ".dylib"}
        targets = [p for p in pkg.rglob("*") if p.suffix in (".so", ".dylib") and p.is_file()]
        for t in targets:
            out = subprocess.run(["otool", "-L", str(t)], capture_output=True, text=True).stdout
            deps = [ln.split()[0] for ln in out.splitlines()[1:] if ln.strip().startswith("@rpath/")]
            deps = [d for d in deps if d[len("@rpath/"):] in names and (t.parent != private or d[len("@rpath/"):] != t.name)]
            if not deps:
                continue
            rel = os.path.relpath(private, t.parent)
            prefix = "@loader_path/" + ("" if rel == "." else rel + "/")
            cmd = ["install_name_tool"]
            for d in deps:
                cmd += ["-change", d, prefix + d[len("@rpath/"):]]
            subprocess.run(cmd + [str(t)], check=True, capture_output=True)
            subprocess.run(["codesign", "--force", "--sign", "-", str(t)], check=True, capture_output=True)
            patched += 1
    if patched:
        subprocess.run(["codesign", "--force", "--sign", "-", "--deep", str(bundle)], check=True, capture_output=True)
    print(f"private dylib references fixed in {patched} binaries", flush=True)
    return patched


def _macho_files(root: Path) -> list[Path]:
    out = []
    for p in root.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        if p.suffix in (".so", ".dylib") or os.access(p, os.X_OK):
            with open(p, "rb") as f:
                magic = f.read(4)
            if magic in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf"):
                out.append(p)
    return out


def sign_bundle(bundle: Path, identity: str, entitlements: Path | None = None) -> None:
    """Sign every Mach-O inside out with the hardened runtime, then the bundle (macOS).

    ``identity``: a "Developer ID Application: ..." certificate for distribution outside
    the App Store (notarization), or "-" for an ad-hoc signature."""
    entitlements = entitlements or ROOT / "packaging" / "entitlements.plist"
    base = ["codesign", "--force", "--options", "runtime", "--entitlements", str(entitlements), "--sign", identity]
    if identity != "-":
        base.insert(2, "--timestamp")
    files = sorted(_macho_files(bundle / "Contents"), key=lambda p: -len(p.parts))  # deepest first
    for f in files:
        subprocess.run(base + [str(f)], check=True, capture_output=True)
    subprocess.run(base + [str(bundle)], check=True, capture_output=True)
    subprocess.run(["codesign", "--verify", "--strict", "--verbose=2", str(bundle)], check=True)
    print(f"signed {len(files)} binaries and the bundle with {identity!r}", flush=True)


def notarize(path: Path, keychain_profile: str) -> None:
    """Submit to Apple's notary service and staple the ticket (``xcrun notarytool store-credentials``
    creates the keychain profile once, with an app-specific password or an API key)."""
    subprocess.run(["xcrun", "notarytool", "submit", str(path), "--keychain-profile", keychain_profile, "--wait"],
                   check=True)
    subprocess.run(["xcrun", "stapler", "staple", str(path)], check=True)
    print(f"notarized and stapled {path.name}", flush=True)


def smoke_test(bundle: Path) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PPG_RENDERER="software")
    exe = executable(bundle)
    print("smoke test:", exe, flush=True)
    r = subprocess.run([str(exe), "--check"], env=env, timeout=600, capture_output=True, text=True)
    print(r.stdout[-2000:], r.stderr[-2000:])
    if r.returncode != 0 or not all(k in r.stdout for k in ("GUI check: ok", "vtk: ok", "libs: ok")):
        raise SystemExit(f"smoke test failed (exit {r.returncode})")
    r = subprocess.run([str(exe), "--doctor"], env=env, timeout=600, capture_output=True, text=True)
    print(r.stdout[-3000:])
    if r.returncode not in (0, 1) or "[OK] Python" not in r.stdout:
        raise SystemExit(f"doctor failed in the packaged application (exit {r.returncode})")


def package(bundle: Path) -> Path:
    ver = version()
    arch = platform.machine().lower().replace("x86_64", "x64").replace("amd64", "x64")
    if sys.platform == "darwin":
        out = DIST / f"PyArchInit-Photogrammetry-{ver}-macos-{arch}.dmg"
        out.unlink(missing_ok=True)
        staging = BUILD / "dmg"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        subprocess.run(["ditto", str(bundle), str(staging / bundle.name)], check=True)
        os.symlink("/Applications", staging / "Applications")
        subprocess.run(["hdiutil", "create", "-volname", NAME, "-srcfolder", str(staging), "-ov", "-format",
                        "UDZO", str(out)], check=True)
    elif os.name == "nt":
        base = DIST / f"PyArchInit-Photogrammetry-{ver}-windows-{arch}"
        out = Path(shutil.make_archive(str(base), "zip", root_dir=DIST, base_dir=bundle.name))
    else:
        out = DIST / f"PyArchInit-Photogrammetry-{ver}-linux-{arch}.tar.gz"
        with tarfile.open(out, "w:gz") as tar:
            tar.add(bundle, arcname=bundle.name)
    print("package:", out, f"{out.stat().st_size / 1e6:.0f} MB", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-smoke", action="store_true")
    ap.add_argument("--skip-build", action="store_true", help="package the existing dist/ bundle")
    args = ap.parse_args()
    render_icons()
    bundle = (DIST / f"{NAME}.app" if sys.platform == "darwin" else DIST / NAME) if args.skip_build else run_pyinstaller()
    fix_private_dylibs(bundle)
    identity = os.environ.get("PPG_CODESIGN_IDENTITY")
    if sys.platform == "darwin" and identity:
        sign_bundle(bundle, identity)
    if not args.no_smoke:
        smoke_test(bundle)
    out = package(bundle)
    profile = os.environ.get("PPG_NOTARY_PROFILE")
    if sys.platform == "darwin" and identity and identity != "-" and profile:
        if out.suffix == ".dmg":
            subprocess.run(["codesign", "--force", "--timestamp", "--sign", identity, str(out)], check=True)
        notarize(out, profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
