"""The OpenGL probe of Windows on ARM (gui/wgl.py): VTK needs OpenGL 3.2."""

import sys

from pyarchinit_photogrammetry.gui import wgl


def test_versions_read_from_the_driver_strings():
    assert wgl.parse_version("4.6.0 NVIDIA 555.85") == (4, 6)
    assert wgl.parse_version("1.1.0") == (1, 1)
    assert wgl.parse_version("4.2 (Compatibility Profile) Mesa 24.2.0") == (4, 2)
    assert wgl.parse_version("") is None and wgl.parse_version(None) is None
    assert (1, 1) < wgl.VTK_MINIMUM <= (3, 3)


def test_the_probe_answers_only_on_windows():
    if sys.platform != "win32":
        assert wgl.opengl_version() is None and wgl.vtk_can_draw() is None
