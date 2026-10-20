#!/bin/bash
# ODM, the engine of WebODM, on a folder of photos: the same resolutions as the other programs
# (features on images at 1/2, dense cloud from depth maps at 1/4; DSM and orthophoto at the
# resolutions of the reference, 15 and 3.8 cm unless given), every stage timed by ODM itself
# (benchmark.txt) and the whole run here.
#
#   bench_odm.sh PHOTOS OUT NAME [more ODM options, e.g. --dem-resolution 7 --orthophoto-resolution 1.7]
set -euo pipefail
PHOTOS="$1"; OUT="$2"; NAME="$3"; shift 3
mkdir -p "$OUT/$NAME"
start=$(date +%s)
docker run --rm -v "$OUT:/datasets" -v "$PHOTOS:/datasets/$NAME/images:ro" opendronemap/odm \
    --project-path /datasets "$NAME" --feature-quality high --pc-quality medium \
    --dsm --dem-resolution 15 --orthophoto-resolution 3.8 --time "$@" 2>&1 | tee "$OUT/$NAME.log"
end=$(date +%s)
echo "BENCH total: $((end - start)) s" | tee -a "$OUT/$NAME.log"
