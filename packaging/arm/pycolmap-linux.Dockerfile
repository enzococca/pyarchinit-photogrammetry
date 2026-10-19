# Builder of the pycolmap wheels for Linux aarch64: the manylinux_2_28 image with the system
# packages of COLMAP's own wheel build (python/ci/install-colmap-almalinux.sh)
FROM quay.io/pypa/manylinux_2_28_aarch64
RUN yum install -y dnf-plugins-core epel-release \
 && yum config-manager --set-enabled powertools \
 && yum install -y gcc-toolset-12-gcc gcc-toolset-12-gcc-c++ gcc-toolset-12-gcc-gfortran kernel-headers \
    perl-IPC-Cmd scl-utils git ninja-build curl zip unzip tar perl \
    libXmu-devel libXi-devel mesa-libGL-devel mesa-libGLU-devel \
 && yum clean all
