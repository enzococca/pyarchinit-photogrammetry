"""The synthetic weak ring of the tests of the calibration rules (rulings R28 and R29): whether its rendering is the
reference one, where its figures were measured, and what the rules must answer on any rendering - decisions
consistent with what the block reported."""
from __future__ import annotations

import hashlib

import pytest

from pyarchinit_photogrammetry.engine import bundle, sfm

# SHA-256 of the texture of the weak ring's scene (write_weak_ring_dataset's defaults, its reduced copies included:
# the photos differ, the scene does not) on macOS, where the figures of its gate were measured
WEAK_RING_TEXTURE = "c0fdc0c92266b238ff996a8136687750bb5c5fdd5db5700faeba5c161841e979"
# the same of the default ring scene (make_ring_scene(seed=0)): tests/test_weak_ring.py's RING_TEXTURE
RING_TEXTURE = "f406ed3e7f8e94096d1839c5418aa8fab7f8bda49bf7a37e0f2e96c7f16603dc"
THRESHOLDS = {"focal": bundle.FOCAL_SD_HOLD, "radial": bundle.GROUP_SD_HOLD_PX,
              "tangential": bundle.GROUP_SD_HOLD_PX, "principal_point": bundle.GROUP_SD_HOLD_PX}


def _sha(texture) -> str:
    return hashlib.sha256(texture.tobytes()).hexdigest()


def reference_rendering(scene) -> bool:
    """Whether the weak ring's scene is the reference one, bit for bit. OpenCV's blur and resampling give a texture
    whose last bits differ by platform, and the weak ring answers that with other tie points: figures millimetres
    apart (k1 -0.040 on macOS, -0.025 on GitHub's Linux and Windows runners, -0.047 to -0.050 on other seeds of the
    same ring; control 1.0 mm on macOS, 6.4-6.9 mm on Windows) and, near a threshold, other decisions. Absolute
    figures and particular decisions are asserted on the reference only."""
    from pyarchinit_photogrammetry.testing.synthetic import make_ring_scene

    return _sha(scene.texture) == WEAK_RING_TEXTURE and _sha(make_ring_scene(seed=0).texture) == RING_TEXTURE


def assert_consistent(check: dict, before: list, after: list) -> None:
    """The decisions of the check of the calibrations (``report["intrinsics"]``) consistent with what the block
    reported, on any rendering (ruling R29): a group whose sd is over its line (or that has none) is held; one under
    it is held only when it was given up for another (its note says so); every group held has its note; a focal
    length walked to its lens prior ends at its target, the target weighed from the estimate, its sd and the prior;
    a focal length held and not walked stays exactly where the alignment put it. ``before``, ``after``: the
    calibrations of the sensors (in the order of ``chunk.sensors``) before and after the optimisation, one sensor
    expected."""
    held, notes = check["held"], check["notes"]
    for group, line in THRESHOLDS.items():
        for sensor, sd in check[f"{group}_sd"]:
            given_up = any(note.startswith(f"sensor {sensor}: {group} held: given up") for note in notes)
            if sd is None or sd > line:
                assert sensor in held[group], (group, sd, held, notes)
            else:
                assert (sensor in held[group]) == given_up, (group, sd, held, notes)
        for sensor in held[group]:
            assert any(note.startswith(f"sensor {sensor}: {group} held") for note in notes), (group, notes)
    walked = {w["sensor"]: w for w in check.get("lens_prior", [])}
    for w in walked.values():
        assert w["target"] == pytest.approx(sfm.lens_target(w["estimate"], w["sd"], w["prior"]), rel=1e-12)
    assert len(before) == len(after) == 1
    sensor = check["focal_sd"][0][0] if check["focal_sd"] else None
    if sensor in walked:
        assert after[0][0] == pytest.approx(walked[sensor]["target"], rel=1e-12)
    elif sensor is not None and sensor in held["focal"]:
        assert after[0][:4] == before[0][:4]  # the focal length (and the principal point, not refined) untouched
