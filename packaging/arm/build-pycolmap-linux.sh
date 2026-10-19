#!/bin/bash
# pycolmap wheels for Linux aarch64 (manylinux_2_28), as COLMAP's own wheel CI builds them for
# x86_64 (python/ci/install-colmap-almalinux.sh): vcpkg dependencies, COLMAP installed, then
# pycolmap for each Python, repaired by auditwheel. Run by build-linux.sh in the image of
# pycolmap-linux.Dockerfile, with the COLMAP source in /src (read only), a folder of the host in
# /work (what the build keeps: vcpkg, its binary cache, the build folders) and the output in /out.
set -euxo pipefail
JOBS=${JOBS:-3}
PYTHONS=${PYTHONS:-"cp310-cp310 cp311-cp311 cp312-cp312"}
# everything the build writes goes to /work: the disk of the container stays small
export HOME=/work/home TMPDIR=/work/tmp PIP_CACHE_DIR=/work/pip-cache
mkdir -p "${HOME}" "${TMPDIR}"

if ! rpm -q gcc-toolset-12-gcc-c++ ninja-build >/dev/null 2>&1; then  # not in the builder image
    yum install -y dnf-plugins-core epel-release
    yum config-manager --set-enabled powertools
    yum install -y gcc-toolset-12-gcc gcc-toolset-12-gcc-c++ gcc-toolset-12-gcc-gfortran kernel-headers \
        perl-IPC-Cmd scl-utils git ninja-build curl zip unzip tar perl \
        libXmu-devel libXi-devel mesa-libGL-devel mesa-libGLU-devel
fi
set +u; source scl_source enable gcc-toolset-12; set -u
export CC=/opt/rh/gcc-toolset-12/root/usr/bin/gcc CXX=/opt/rh/gcc-toolset-12/root/usr/bin/g++
export FC=/opt/rh/gcc-toolset-12/root/usr/bin/gfortran
# cmake 4 refuses projects asking for CMake < 3.5, as some vcpkg ports do: the 3.31 series
if [ ! -x /work/tools/bin/cmake ]; then
    /opt/python/cp312-cp312/bin/python -m venv /work/tools
    /work/tools/bin/pip install "cmake==3.31.6" ninja
fi
mkdir -p /work/bin
for t in cmake ctest cpack ninja; do ln -sf /work/tools/bin/$t /work/bin/$t; done
export PATH=/work/bin:${PATH}
${CC} --version
cmake --version
ninja --version

# vcpkg has no prebuilt tools for arm64 Linux: it uses the cmake and ninja of the system
export VCPKG_FORCE_SYSTEM_BINARIES=1
export VCPKG_MAX_CONCURRENCY=${JOBS}
export VCPKG_DEFAULT_BINARY_CACHE=/work/vcpkg-cache
export VCPKG_DISABLE_METRICS=1
mkdir -p "${VCPKG_DEFAULT_BINARY_CACHE}"
if [ ! -x /work/vcpkg/vcpkg ]; then
    rm -rf /work/vcpkg
    git clone https://github.com/microsoft/vcpkg /work/vcpkg
    /work/vcpkg/bootstrap-vcpkg.sh -disableMetrics
fi
mkdir -p /work/triplets
cat > /work/triplets/arm64-linux-release.cmake <<'EOF'
set(VCPKG_TARGET_ARCHITECTURE arm64)
set(VCPKG_CRT_LINKAGE dynamic)
set(VCPKG_LIBRARY_LINKAGE static)
set(VCPKG_CMAKE_SYSTEM_NAME Linux)
set(VCPKG_BUILD_TYPE release)
EOF
export VCPKG_OVERLAY_TRIPLETS=/work/triplets
TOOLCHAIN=/work/vcpkg/scripts/buildsystems/vcpkg.cmake
TRIPLET=arm64-linux-release

# a writable copy of the source (the build writes next to it); the build folder stays, with the
# times of the files kept nothing is compiled again
mkdir -p /work/colmap
cp -a /src/. /work/colmap/
cd /work/colmap
cmake -S . -B build -GNinja \
    -DCUDA_ENABLED=OFF -DONNX_ENABLED=OFF -DGUI_ENABLED=OFF -DCGAL_ENABLED=OFF -DLSD_ENABLED=OFF \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_TOOLCHAIN_FILE=${TOOLCHAIN} -DVCPKG_TARGET_TRIPLET=${TRIPLET} \
    "-DVCPKG_INSTALL_OPTIONS=--clean-buildtrees-after-build;--clean-packages-after-build" \
    -DCMAKE_EXE_LINKER_FLAGS_INIT=-ldl \
    -DFETCHCONTENT_BASE_DIR=/work/fetchcontent
# a few files (PoissonRecon, the solvers) need several GB each: what runs out of memory in parallel
# is compiled again alone
ninja -C build -j "${JOBS}" -k 0 install || ninja -C build -j 1 install
export CMAKE_BUILD_PARALLEL_LEVEL=${PY_JOBS:-2}

mkdir -p /work/wheels-raw /out
for py in ${PYTHONS}; do
    PY=/opt/python/${py}/bin/python
    export PATH=/opt/python/${py}/bin:${PATH}  # ruff and clang-format of the stub generation
    ${PY} python/ci/install-build-requirements.py
    ${PY} -m pip install build
    rm -rf /work/wheels-raw/*
    ${PY} -m build --wheel --no-isolation -o /work/wheels-raw \
        -C cmake.define.CMAKE_TOOLCHAIN_FILE=${TOOLCHAIN} \
        -C cmake.define.VCPKG_TARGET_TRIPLET=${TRIPLET} \
        -C cmake.define.VCPKG_INSTALLED_DIR=/work/colmap/build/vcpkg_installed \
        -C cmake.define.FETCHCONTENT_BASE_DIR=/work/fetchcontent \
        -C build-dir=/work/pybuild/${py} \
        .
    auditwheel repair -w /out /work/wheels-raw/pycolmap-*.whl
    # the wheel imports
    ${PY} -m pip install --force-reinstall /out/pycolmap-*-${py}-*.whl
    ${PY} -c "import pycolmap, numpy; print(pycolmap.__version__, pycolmap.has_cuda); r = pycolmap.Reconstruction(); print(r.summary())"
done
ls -la /out
