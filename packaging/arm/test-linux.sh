#!/bin/bash
# The tests of pyarchinit-photogrammetry on Linux aarch64 with the pycolmap and Open3D wheels of
# /wheels, in the environment of the CI tests (Ubuntu 24.04, Python 3.12, the native core). Run by
# build-linux.sh in ubuntu:24.04 with the repository in /repo (read only) and a folder of the host
# in /work (the virtual environment, the copy of the repository, the temporary folders).
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive TZ=UTC
export HOME=/work/home TMPDIR=/work/tmp PIP_CACHE_DIR=/work/pip-cache CARGO_HOME=/work/cargo RUSTUP_HOME=/work/rustup
mkdir -p "${HOME}" "${TMPDIR}"
apt-get update
apt-get install -y python3 python3-venv python3-dev build-essential curl git rsync \
    libsqlite3-mod-spatialite libegl1 libgl1 libgomp1 libxkbcommon0 libfontconfig1 libdbus-1-3 libglib2.0-0
rm -rf /var/lib/apt/lists/*
[ -x /work/venv/bin/python ] || python3 -m venv /work/venv
PY=/work/venv/bin/python
${PY} -m pip install --upgrade pip
${PY} -m pip install --force-reinstall --no-deps /wheels/pycolmap-*-cp312-*.whl /wheels/open3d-*-cp312-*.whl
rsync -a --delete --exclude .venv --exclude build --exclude dist --exclude graphify-out --exclude native/target \
    --exclude '*.egg-info' --exclude __pycache__ /repo/ /work/repo/
${PY} -m pip install -e "/work/repo[all,dev]"
if [ "${WITH_NATIVE:-1}" = 1 ] && [ -d /work/repo/native ]; then
    [ -x "${CARGO_HOME}/bin/cargo" ] || curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --no-modify-path
    PATH=${CARGO_HOME}/bin:${PATH} ${PY} -m pip install /work/repo/native
fi
${PY} -m pip list 2>/dev/null | grep -i -E "^(pycolmap|open3d|numpy|opencv|rasterio|pyside6|vtk|pyarchinit)"
cd /work/repo
export QT_QPA_PLATFORM=offscreen PPG_RENDERER=software PYTHONUNBUFFERED=1
${PY} -u -m pytest -q -rfE -p no:cacheprovider --basetemp=/work/pytest ${PYTEST_ARGS:-tests}
