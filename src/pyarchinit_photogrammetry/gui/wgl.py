"""The OpenGL version a window gets from the system's ``opengl32.dll`` on Windows, asked the way
VTK asks it (a plain context, then a 3.2 core one through ``wglCreateContextAttribsARB``).

VTK draws with OpenGL 3.2 or later. Windows on ARM computers get it from their graphics driver or
from Microsoft's *OpenCL, OpenGL, and Vulkan Compatibility Pack* (OpenGL over Direct3D 12); a
machine without either - a virtual machine without a 3D driver, a CI runner - has only Windows'
own OpenGL 1.1, where VTK cannot draw: the 3D view must then use the software renderer.
Nothing here imports Qt or VTK; any failure of the probe answers ``None`` (unknown)."""

from __future__ import annotations

import re
import sys

VTK_MINIMUM = (3, 2)

_GL_VERSION = 0x1F02
_GL_RENDERER = 0x1F01


def parse_version(text: str | None) -> tuple[int, int] | None:
    """``"4.6.0 NVIDIA 555.85"`` -> ``(4, 6)``; ``"OpenGL ES 3.2 ..."`` -> ``(3, 2)``."""
    m = re.search(r"(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def opengl_version() -> tuple[tuple[int, int], str, str] | None:
    """((major, minor), version string, renderer) of the best context VTK could create, or
    ``None`` off Windows or when the probe itself fails."""
    if sys.platform != "win32":
        return None
    try:
        return _probe()
    except Exception:
        return None


def vtk_can_draw() -> bool | None:
    """True/False when the probe answers, None when it cannot tell."""
    found = opengl_version()
    return None if found is None else found[0] >= VTK_MINIMUM


def _probe():  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    gdi32 = ctypes.WinDLL("gdi32")
    gl = ctypes.WinDLL("opengl32")
    HANDLE = ctypes.c_void_p
    user32.CreateWindowExW.restype = HANDLE
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       HANDLE, HANDLE, HANDLE, ctypes.c_void_p]
    user32.GetDC.restype = HANDLE
    user32.GetDC.argtypes = [HANDLE]
    user32.ReleaseDC.argtypes = [HANDLE, HANDLE]
    user32.DestroyWindow.argtypes = [HANDLE]

    class PFD(ctypes.Structure):
        _fields_ = [("nSize", wintypes.WORD), ("nVersion", wintypes.WORD), ("dwFlags", wintypes.DWORD)]
        _fields_ += [(n, ctypes.c_ubyte) for n in (
            "iPixelType", "cColorBits", "cRedBits", "cRedShift", "cGreenBits", "cGreenShift", "cBlueBits",
            "cBlueShift", "cAlphaBits", "cAlphaShift", "cAccumBits", "cAccumRedBits", "cAccumGreenBits",
            "cAccumBlueBits", "cAccumAlphaBits", "cDepthBits", "cStencilBits", "cAuxBuffers", "iLayerType",
            "bReserved")]
        _fields_ += [(n, wintypes.DWORD) for n in ("dwLayerMask", "dwVisibleMask", "dwDamageMask")]

    gdi32.ChoosePixelFormat.argtypes = [HANDLE, ctypes.POINTER(PFD)]
    gdi32.SetPixelFormat.argtypes = [HANDLE, ctypes.c_int, ctypes.POINTER(PFD)]
    gl.wglCreateContext.restype = HANDLE
    gl.wglCreateContext.argtypes = [HANDLE]
    gl.wglMakeCurrent.argtypes = [HANDLE, HANDLE]
    gl.wglDeleteContext.argtypes = [HANDLE]
    gl.wglGetProcAddress.restype = ctypes.c_void_p
    gl.wglGetProcAddress.argtypes = [ctypes.c_char_p]
    gl.glGetString.restype = ctypes.c_char_p
    gl.glGetString.argtypes = [ctypes.c_uint]

    def strings():
        v = gl.glGetString(_GL_VERSION)
        r = gl.glGetString(_GL_RENDERER)
        v = v.decode("ascii", "replace") if v else ""
        return parse_version(v) or (0, 0), v, r.decode("ascii", "replace") if r else ""

    WS_POPUP = 0x80000000
    hwnd = user32.CreateWindowExW(0, "STATIC", "ppg-opengl-probe", WS_POPUP, 0, 0, 1, 1, None, None, None, None)
    if not hwnd:
        return None
    hdc = user32.GetDC(hwnd)
    contexts = []
    try:
        pfd = PFD()
        pfd.nSize = ctypes.sizeof(PFD)
        pfd.nVersion = 1
        pfd.dwFlags = 0x4 | 0x20 | 0x1  # draw to window, OpenGL, double buffer
        pfd.cColorBits = 32
        pfd.cDepthBits = 24
        fmt = gdi32.ChoosePixelFormat(hdc, ctypes.byref(pfd))
        if not fmt or not gdi32.SetPixelFormat(hdc, fmt, ctypes.byref(pfd)):
            return None
        legacy = gl.wglCreateContext(hdc)
        if not legacy or not gl.wglMakeCurrent(hdc, legacy):
            return None
        contexts.append(legacy)
        best = strings()
        if best[0] < VTK_MINIMUM:  # as VTK: a 3.2 core context, where the driver offers one
            address = gl.wglGetProcAddress(b"wglCreateContextAttribsARB")
            if address:
                create = ctypes.WINFUNCTYPE(HANDLE, HANDLE, HANDLE, ctypes.POINTER(ctypes.c_int))(address)
                attribs = (ctypes.c_int * 7)(0x2091, 3, 0x2092, 2, 0x9126, 0x1, 0)
                core = create(hdc, None, attribs)
                if core:
                    contexts.append(core)
                    if gl.wglMakeCurrent(hdc, core):
                        found = strings()
                        if found[0] > best[0]:
                            best = found
        return best
    finally:
        gl.wglMakeCurrent(None, None)
        for c in contexts:
            gl.wglDeleteContext(c)
        user32.ReleaseDC(hwnd, hdc)
        user32.DestroyWindow(hwnd)
