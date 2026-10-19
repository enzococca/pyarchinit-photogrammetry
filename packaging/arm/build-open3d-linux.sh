#!/bin/bash
# Open3D 0.19.0 wheels for Linux aarch64, as Open3D's docker/Dockerfile.openblas builds them on
# arm64 Ubuntu 20.04 (glibc 2.31: manylinux_2_31_aarch64, like its x86_64 wheels), without the
# new GUI (Filament): OpenBLAS, VTK and Embree from source, ISPC off as on every ARM build.
# Run by build-linux.sh in the image of open3d-linux.Dockerfile, with the Open3D source in /src
# (read only), a folder of the host in /work (Miniconda, the build folder) and the output in /out.
set -euxo pipefail
JOBS=${JOBS:-3}
PYVERS=${PYVERS:-"3.10 3.11 3.12"}
export DEBIAN_FRONTEND=noninteractive TZ=UTC SUDO=command
# everything the build writes goes to /work (a folder of the host): the disk of the container stays small
export HOME=/work/home TMPDIR=/work/tmp PIP_CACHE_DIR=/work/pip-cache
mkdir -p "${HOME}" "${TMPDIR}"

if ! dpkg -s xorg-dev gfortran rsync >/dev/null 2>&1; then  # not in the builder image
    apt-get update
    apt-get install -y wget libgl1 libgomp1 build-essential git rsync ca-certificates
    bash /src/util/install_deps_ubuntu.sh assume-yes no-filament-deps
    rm -rf /var/lib/apt/lists/*
fi

if [ ! -x /work/miniconda3/bin/conda ]; then
    rm -rf /work/miniconda3  # what an interrupted installation left
    wget -q https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-aarch64.sh -O "${TMPDIR}/miniconda.sh"
    bash "${TMPDIR}/miniconda.sh" -b -p /work/miniconda3
fi
CONDA=/work/miniconda3/bin/conda
${CONDA} config --set always_yes yes
${CONDA} tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main 2>/dev/null || true
${CONDA} tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r 2>/dev/null || true

# the CMake series Open3D 0.19 was released with works; a recent 3.x too
if [ ! -x /work/o3d-tools/bin/cmake ]; then
    /work/miniconda3/bin/python -m venv /work/o3d-tools
    /work/o3d-tools/bin/pip install "cmake==3.31.6"
fi
mkdir -p /work/o3d-bin
for t in cmake ctest cpack; do ln -sf /work/o3d-tools/bin/$t /work/o3d-bin/$t; done
export PATH=/work/o3d-bin:${PATH}
cmake --version

rsync -a --delete --exclude build /src/ /work/open3d/
cd /work/open3d
# On ARM Open3D links the static libgfortran.a, whose thread_unit is an initial-exec TLS variable:
# the whole TLS block of the module (12 KB, OpenBLAS's gemv buffers) then has to fit in the static
# TLS of glibc and the import fails ("cannot allocate memory in static TLS block"). The shared
# libgfortran.so, as on x86-64, put into the wheel by auditwheel.
perl -0pi -e 's/if\(LINUX_AARCH64 OR APPLE_AARCH64\)(\n\s+# Find libgfortran\.a)/if(APPLE_AARCH64)$1/' \
    3rdparty/find_dependencies.cmake
grep -B1 "# Find libgfortran.a and libgcc.a" 3rdparty/find_dependencies.cmake | grep -q "if(APPLE_AARCH64)"
mkdir -p /out
for v in ${PYVERS}; do
    env=/work/miniconda3/envs/py${v}
    [ -x ${env}/bin/python ] || ${CONDA} create -p ${env} python=${v}
    ${env}/bin/python -m pip install -U -r python/requirements.txt -r python/requirements_build.txt
    # the setup.py of Open3D 0.19 and its wheel 0.38 import pkg_resources, gone from setuptools 81
    ${env}/bin/python -m pip install "setuptools<81"
    cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DDEVELOPER_BUILD=OFF \
        -DBUILD_GUI=OFF \
        -DBUILD_WEBRTC=OFF \
        -DBUILD_JUPYTER_EXTENSION=OFF \
        -DBUILD_EXAMPLES=OFF \
        -DBUILD_UNIT_TESTS=OFF \
        -DBUILD_ISPC_MODULE=OFF \
        -DBUILD_CUDA_MODULE=OFF \
        -DBUILD_PYTORCH_OPS=OFF \
        -DBUILD_TENSORFLOW_OPS=OFF \
        -DBUNDLE_OPEN3D_ML=OFF \
        -DPython3_ROOT=${env} \
        -DPython3_EXECUTABLE=${env}/bin/python \
        -DCMAKE_INSTALL_PREFIX=/work/open3d_install
    # what runs out of memory in parallel is compiled again alone
    make -C build -j"${JOBS}" -k pip-package || make -C build -j1 pip-package
    ${env}/bin/python -m pip install auditwheel patchelf
    rm -rf /work/wheelhouse
    PATH=${env}/bin:${PATH} ${env}/bin/python -m auditwheel repair --plat manylinux_2_31_aarch64 -w /work/wheelhouse \
        build/lib/python_package/pip_package/open3d-*-cp${v/./}-*.whl
    cp /work/wheelhouse/open3d-*.whl /out/
    ${env}/bin/python -m pip install --force-reinstall /out/open3d-*-cp${v/./}-*.whl
    ${env}/bin/python - <<'EOF'
import numpy as np
import open3d as o3d

print("open3d", o3d.__version__, "cuda", o3d.core.cuda.is_available())
# what pyarchinit-photogrammetry uses: raycasting (Embree), Poisson, FPFH, the voxel block grid
box = o3d.t.geometry.TriangleMesh.create_box()
scene = o3d.t.geometry.RaycastingScene()
scene.add_triangles(box)
rays = o3d.core.Tensor([[0.5, 0.5, 5, 0, 0, -1]], dtype=o3d.core.Dtype.Float32)
print("ray hit at", scene.cast_rays(rays)["t_hit"].numpy())
pts = np.random.default_rng(0).normal(size=(4000, 3))
pts /= np.linalg.norm(pts, axis=1, keepdims=True)
pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts))
pc.estimate_normals()
pc.orient_normals_consistent_tangent_plane(10)
mesh, _ = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pc, depth=6)
print("poisson faces", len(mesh.triangles))
f = o3d.pipelines.registration.compute_fpfh_feature(pc, o3d.geometry.KDTreeSearchParamHybrid(radius=0.3, max_nn=50))
print("fpfh", f.data.shape)
vbg = o3d.t.geometry.VoxelBlockGrid(("tsdf", "weight"), (o3d.core.float32, o3d.core.float32), ((1), (1)), 0.01, 16, 1000)
print("voxel block grid ok", vbg is not None)
EOF
done
ls -la /out
