# Builder of the Open3D wheels for Linux aarch64: arm64 Ubuntu 20.04 (glibc 2.31, as the x86_64
# wheels of Open3D 0.19) with the system packages of Open3D's docker/Dockerfile.openblas; the
# context is a folder with Open3D's util/install_deps_ubuntu.sh
FROM arm64v8/ubuntu:20.04
ENV DEBIAN_FRONTEND=noninteractive TZ=UTC SUDO=command
COPY install_deps_ubuntu.sh /tmp/
RUN apt-get update \
 && apt-get install -y wget libgl1 libgomp1 build-essential git rsync ca-certificates \
 && bash /tmp/install_deps_ubuntu.sh assume-yes no-filament-deps \
 && rm -rf /var/lib/apt/lists/*
