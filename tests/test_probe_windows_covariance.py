"""TEMPORARY probe (branch windows-covariance): what the covariance of the nadir grid gives on this platform."""
import platform

import numpy as np

from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.engine import bundle

from test_optimize_undetermined import F, _block, _slide_along_the_valley, needs_core


@needs_core
def test_probe_the_covariance_of_the_nadir_grid(tmp_path, monkeypatch):
    real = bundle.intrinsics_covariance
    calls = []

    def spy(pb, *args, **kwargs):
        free = [list(f) for f in pb.free_intrinsics]
        try:
            matrix, counts = real(pb, *args, **kwargs)
        except RuntimeError as exc:
            calls.append((free, "ERR " + str(exc)))
            raise
        d = np.diag(matrix)
        calls.append((free, f"ok k={len(d)} diag={np.array2string(d, precision=4)} "
                            f"sd_f/F={np.sqrt(abs(d[0])) / pb.cameras[0][1][0]:.6g}"))
        return matrix, counts

    monkeypatch.setattr(bundle, "intrinsics_covariance", spy)
    print(f"\nPROBE platform {platform.platform()} numpy {np.__version__} core api {_native.api()}")
    _doc, ch = _block(tmp_path / "nadir")
    _slide_along_the_valley(ch, 1.3)
    for marker in ch.markers:
        marker.reference.enabled = False
    for cam in ch.cameras:
        cam.reference.enabled = False
    ch.sensors[0].focal_source, ch.sensors[0].focal_prior = "exif", F
    ch.optimize_cameras(engine="ppg")
    for free, what in calls:
        print(f"PROBE free {free}: {what}")
    intr = ch.meta["sfm"]["optimize"]["intrinsics"]
    print(f"PROBE lens_prior {intr['lens_prior']}")
    print(f"PROBE notes {intr['notes']}")
    print(f"PROBE params {list(ch.sensors[0].params)}")
