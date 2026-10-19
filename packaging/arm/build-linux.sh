#!/bin/bash
# Wheels of pycolmap 4.2.1 and Open3D 0.19.0 for Linux aarch64, which their projects do not
# publish, built in Docker on an arm64 machine (a Mac with Apple Silicon, an arm64 Linux host,
# the ubuntu-24.04-arm runners of GitHub): the sources at their tags, two builder images, the
# work in folders of the host (build/arm: Docker's own disk only holds the images; on macOS a
# case-sensitive disk image that grows as it fills, since Linux packages have names that differ
# only in case - `hdiutil detach build/arm` closes it), the wheels in dist/arm. JOBS: compilers in
# parallel (a few COLMAP and Open3D files need several GB each: 3 in a Docker of 8 GB); PYTHONS:
# the versions of Python.
#
#   packaging/arm/build-linux.sh                  # pycolmap and Open3D for Python 3.10-3.12
#   PYTHONS=3.12 packaging/arm/build-linux.sh pycolmap
#   packaging/arm/build-linux.sh test             # the tests of this project with the wheels (3.12)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
WORK=${WORK:-$ROOT/build/arm}
OUT=${OUT:-$ROOT/dist/arm}
JOBS=${JOBS:-3}
PYTHONS=${PYTHONS:-"3.10 3.11 3.12"}
COLMAP_TAG=4.2.1
OPEN3D_TAG=v0.19.0
WHAT=${1:-all}
if [ "$(uname)" = Darwin ] && ! mount | grep -q " on $WORK (apfs"; then
    [ -f "$WORK.sparseimage" ] || hdiutil create -size 100g -type SPARSE -fs "Case-sensitive APFS" -volname ppg-arm \
        "$WORK.sparseimage"
    mkdir -p "$WORK"
    hdiutil attach -nobrowse -mountpoint "$WORK" "$WORK.sparseimage"
fi
mkdir -p "$WORK/src" "$OUT"

if [ "$WHAT" = all ] || [ "$WHAT" = pycolmap ]; then
    [ -d "$WORK/src/colmap" ] || git clone --depth 1 --branch "$COLMAP_TAG" https://github.com/colmap/colmap "$WORK/src/colmap"
    docker build -t ppg-arm/pycolmap-builder -f "$HERE/pycolmap-linux.Dockerfile" "$HERE"
    tags=$(for v in $PYTHONS; do t=cp${v/./}; printf '%s-%s ' "$t" "$t"; done)
    mkdir -p "$WORK/pycolmap"
    docker run --rm -v "$WORK/pycolmap:/work" -v "$WORK/src/colmap:/src:ro" -v "$OUT:/out" -v "$HERE:/scripts:ro" \
        -e JOBS="$JOBS" -e PY_JOBS="${PY_JOBS:-2}" -e PYTHONS="$tags" ppg-arm/pycolmap-builder \
        bash /scripts/build-pycolmap-linux.sh
fi
if [ "$WHAT" = all ] || [ "$WHAT" = open3d ]; then
    [ -d "$WORK/src/open3d" ] || git clone --depth 1 --branch "$OPEN3D_TAG" https://github.com/isl-org/Open3D "$WORK/src/open3d"
    mkdir -p "$WORK/open3d-context"
    cp "$WORK/src/open3d/util/install_deps_ubuntu.sh" "$WORK/open3d-context/"
    docker build -t ppg-arm/open3d-builder -f "$HERE/open3d-linux.Dockerfile" "$WORK/open3d-context"
    mkdir -p "$WORK/open3d"
    docker run --rm -v "$WORK/open3d:/work" -v "$WORK/src/open3d:/src:ro" -v "$OUT:/out" -v "$HERE:/scripts:ro" \
        -e JOBS="$JOBS" -e PYVERS="$PYTHONS" ppg-arm/open3d-builder bash /scripts/build-open3d-linux.sh
fi
if [ "$WHAT" = test ]; then
    mkdir -p "$WORK/test"
    docker run --rm -v "$WORK/test:/work" -v "$ROOT:/repo:ro" -v "$OUT:/wheels:ro" -v "$HERE:/scripts:ro" \
        ubuntu:24.04 bash /scripts/test-linux.sh
fi
ls -l "$OUT"
