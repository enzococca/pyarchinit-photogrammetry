"""A visible trackball for the 3D view, as in Metashape: a sphere drawn around the centre of rotation
(the pivot) with three rings. Inside the sphere a drag turns the scene freely (Shoemake's arcball), on
a ring it turns it about that ring's axis only, outside the sphere it rolls it about the line of sight.

The behaviour follows the visible behaviour and the public manual of Metashape; the code is ours. The
same :class:`Trackball` drives both canvases: each gives it its camera as an :class:`OrbitCamera`
(``get_cam``) and takes it back (``set_cam``), and tells the size of the view in widget pixels."""
from __future__ import annotations

import time

import numpy as np

from .scene import OrbitCamera

RADIUS = 0.4  # of the short side of the view
RING_REACH = 8.0  # pixels from a ring that grab it
FADE_AFTER = 1.5  # seconds after the last movement of the mouse
FADE_TIME = 0.4
KEY_ANGLE = 15.0  # degrees of an arrow key
KEY_PAN = 0.1  # of the height of the view, Shift + arrow
ZOOM_STEP = 0.88  # distance factor of one notch of the wheel
MODES = ("auto", "always", "never")
SETTING = "view/trackball"
# soft colours: the ring about the scene's x, the one about its y, the horizontal one about up
RING_COLOURS = {"x": (235, 120, 120), "y": (130, 205, 130), "up": (120, 165, 245)}
OUTLINE_COLOUR = (215, 215, 215)


def rotation(axis, angle: float) -> np.ndarray:
    """Rotation matrix of ``angle`` radians about ``axis`` (right hand)."""
    a = np.asarray(axis, np.float64)
    n = np.linalg.norm(a)
    if n < 1e-15 or angle == 0.0:
        return np.eye(3)
    x, y, z = a / n
    c, s = np.cos(angle), np.sin(angle)
    C = 1 - c
    return np.array([[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
                     [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
                     [z * x * C - y * s, z * y * C + x * s, c + z * z * C]])


def arcball_vector(x: float, y: float, cx: float, cy: float, r: float) -> np.ndarray:
    """A screen point on the arcball, in camera axes (right, down, forward): inside the circle on the
    half of the sphere facing the viewer, outside on its rim."""
    v = np.array([(x - cx) / r, (y - cy) / r, 0.0])
    d2 = v[0] ** 2 + v[1] ** 2
    if d2 >= 1.0:
        v[:2] /= np.sqrt(d2)
    else:
        v[2] = -np.sqrt(1.0 - d2)  # towards the viewer
    return v


def scene_axes(up) -> dict[str, np.ndarray]:
    """The axes of the three rings: the scene's x and y made level, and up."""
    up = np.asarray(up, np.float64) / np.linalg.norm(up)
    x = np.array([1.0, 0, 0]) if abs(up[0]) < 0.9 else np.array([0.0, 1, 0])
    x = x - (x @ up) * up
    x /= np.linalg.norm(x)
    return {"x": x, "y": np.cross(up, x), "up": up}


class Trackball:
    """The trackball of a canvas. ``canvas`` gives ``get_cam()``, ``set_cam(cam)``, ``view_size()``
    (widget pixels), ``scene_up()`` and ``redraw()``; ``pivot`` is in the coordinates shown."""

    def __init__(self, canvas):
        self.canvas = canvas
        self.pivot: np.ndarray | None = None
        self.mode = "auto"
        self._last_move = -1e9  # the mouse over the view (time.monotonic)
        self._inside = False
        self._drag: tuple | None = None  # (kind, data, last x, last y)
        self.hover: str | None = None  # the ring under the mouse
        self.hidden = False  # never in a frame taken from the view (lasso, picks, screenshots)

    # ----------------------------------------------------------- visibility
    def opacity(self, now: float | None = None) -> float:
        if self.mode == "never" or self.hidden:
            return 0.0
        if self.mode == "always" or self._drag is not None:
            return 1.0
        if not self._inside:
            return 0.0
        t = (time.monotonic() if now is None else now) - self._last_move
        if t <= FADE_AFTER:
            return 1.0
        return float(max(0.0, 1.0 - (t - FADE_AFTER) / FADE_TIME))

    def fading(self) -> bool:
        """The trackball is still on its way out: the canvas redraws it until it is gone."""
        return self.mode == "auto" and self._inside and self.opacity() > 0.0

    def touched(self, x: float | None = None, y: float | None = None) -> None:
        self._inside = True
        self._last_move = time.monotonic()
        if x is not None and self._drag is None:
            hit = self.hit(x, y)
            self.hover = hit[1] if hit[0] == "ring" else None

    def left(self) -> None:
        self._inside = False
        self.hover = None

    # ----------------------------------------------------------- geometry
    def _pivot(self, cam: OrbitCamera) -> np.ndarray:
        if self.pivot is None:
            self.pivot = cam.target.copy()
        return self.pivot

    def focal(self, cam: OrbitCamera, h: int) -> float:
        return 0.5 * h / np.tan(np.radians(cam.fov) / 2)

    def geometry(self, cam: OrbitCamera | None = None, samples: int = 96) -> dict:
        """Where the trackball is on the screen: the pivot (``centre``, pixels, and ``depth``), the
        radius in pixels and in scene units, and the three rings: ``(axis, points, xy, ok, front)``.
        The sphere is drawn as the arcball sees it, without perspective: every ring is projected at
        the pivot's depth (so it stays inside the outline); ``points`` are the 3D points with that
        projection (flattened onto the plane of the pivot facing the viewer), ``front`` the half on
        the viewer's side."""
        cam = cam if cam is not None else self.canvas.get_cam()
        w, h = self.canvas.view_size()
        p = self._pivot(cam)
        x, y, z, ok = cam.project(p[None], w, h)
        depth = float(z[0]) if ok[0] else float(cam.distance)
        centre = (float(x[0]), float(y[0])) if ok[0] else (w / 2.0, h / 2.0)
        r_px = RADIUS * min(w, h)
        r_world = r_px * depth / self.focal(cam, h)
        R = cam.basis()
        t = np.linspace(0, 2 * np.pi, samples, endpoint=False)
        rings = {}
        for name, a in scene_axes(self.canvas.scene_up()).items():
            u = np.cross(a, [1.0, 0, 0]) if abs(a[0]) < 0.9 else np.cross(a, [0, 1.0, 0])
            u /= np.linalg.norm(u)
            v = np.cross(a, u)
            local = (np.outer(np.cos(t), u) + np.outer(np.sin(t), v)) @ R.T  # camera axes, unit sphere
            xy = np.c_[centre[0] + r_px * local[:, 0], centre[1] + r_px * local[:, 1]]
            flat = p + r_world * (np.outer(local[:, 0], R[0]) + np.outer(local[:, 1], R[1]))
            rings[name] = (a, flat, xy, np.full(samples, bool(ok[0])), local[:, 2] <= 0)
        return {"centre": centre, "depth": depth, "radius": r_px, "world_radius": r_world, "rings": rings,
                "size": (w, h)}

    def hit(self, x: float, y: float, geo: dict | None = None) -> tuple[str, str | None]:
        """What a press at (x, y) grabs: ("ring", name), ("free", None) inside the sphere, ("roll", None)."""
        geo = geo or self.geometry()
        best, name = RING_REACH, None
        for key, (_a, _pts, xy, ok, _front) in geo["rings"].items():
            if not ok.any():
                continue
            a, b = xy, np.roll(xy, -1, axis=0)
            good = ok & np.roll(ok, -1)
            ab = b - a
            t = np.clip(((x - a[:, 0]) * ab[:, 0] + (y - a[:, 1]) * ab[:, 1]) / np.maximum((ab ** 2).sum(1), 1e-12),
                        0, 1)
            d = np.hypot(a[:, 0] + t * ab[:, 0] - x, a[:, 1] + t * ab[:, 1] - y)
            d = np.where(good, d, np.inf)
            if d.min() <= best:
                best, name = float(d.min()), key
        if name is not None:
            return "ring", name
        cx, cy = geo["centre"]
        return ("free", None) if np.hypot(x - cx, y - cy) <= geo["radius"] else ("roll", None)

    # ----------------------------------------------------------- operations
    def turn(self, axis, angle: float) -> None:
        """Turn the scene by ``angle`` radians about ``axis`` (world) through the pivot: the camera
        goes the other way around it."""
        cam = self.canvas.get_cam()
        cam.rotate(rotation(axis, -angle), self._pivot(cam))
        self.canvas.set_cam(cam)

    def pan(self, dx: float, dy: float) -> None:
        """Move the scene by (dx, dy) pixels; the pivot moves with the view, so the sphere keeps its
        place on the screen (the scene point at the pivot's depth follows the cursor)."""
        cam = self.canvas.get_cam()
        geo = self.geometry(cam)
        k = geo["depth"] / self.focal(cam, geo["size"][1])
        R = cam.basis()
        shift = -R[0] * dx * k - R[1] * dy * k
        cam.target = cam.target + shift
        self.pivot = self._pivot(cam) + shift
        self.canvas.set_cam(cam)

    def zoom(self, steps: float, x: float | None = None, y: float | None = None, dolly: bool = False) -> None:
        """Zoom by ``steps`` notches toward the point under (x, y): camera and pivot move toward
        the point of the cursor's line of sight at the pivot's depth, by the zoom factor, so the
        pivot keeps its place on the screen. ``dolly``: the camera moves along its line of sight
        and the pivot stays where it is."""
        k = ZOOM_STEP ** steps
        cam = self.canvas.get_cam()
        p = self._pivot(cam)
        if dolly:
            cam.distance *= k
            self.canvas.set_cam(cam)
            return
        w, h = self.canvas.view_size()
        if x is None:
            x, y = w / 2.0, h / 2.0
        f = self.focal(cam, h)
        R = cam.basis()
        eye = cam.eye
        depth = float((p - eye) @ R[2])
        if depth <= 0:
            depth = cam.distance
        c = eye + R.T @ np.array([(x - w / 2) / f, (y - h / 2) / f, 1.0]) * depth
        cam.target = c + k * (cam.target - c)
        cam.distance *= k
        self.pivot = c + k * (p - c)
        self.canvas.set_cam(cam)

    def set_pivot(self, point, recentre: bool = False) -> None:
        """A new centre of rotation; the view does not move unless ``recentre``."""
        self.pivot = np.asarray(point, np.float64).copy()
        if recentre:
            self.centre_on_pivot()
        else:
            self.canvas.redraw()

    def centre_on_pivot(self) -> None:
        """Move the view so that the pivot is in its middle, at the same depth."""
        cam = self.canvas.get_cam()
        p = self._pivot(cam)
        R = cam.basis()
        depth = float((p - cam.eye) @ R[2])
        cam.target = p.copy()
        if depth > 0:
            cam.distance = depth
        self.canvas.set_cam(cam)

    def look_along(self, direction) -> None:
        """The view along ``direction`` (world) around the pivot, the scene's up at the top."""
        cam = self.canvas.get_cam()
        p = self._pivot(cam)
        up = scene_axes(self.canvas.scene_up())["up"]
        cam.R = OrbitCamera.looking(direction, up, scene_axes(up)["x"])
        cam.target = p.copy()
        self.canvas.set_cam(cam)

    # ----------------------------------------------------------- mouse
    def press(self, x: float, y: float, kind: str) -> None:
        """Start a drag: ``kind`` "rotate" (left button: free, ring or roll from where it starts) or "pan"."""
        if kind == "pan":
            self._drag = ("pan", None, x, y)
        else:
            what, ring = self.hit(x, y)
            self._drag = (what, ring, x, y)
        self.touched()

    def move(self, x: float, y: float) -> bool:
        """The drag goes on to (x, y); False when nothing is dragged."""
        if self._drag is None:
            return False
        what, ring, x0, y0 = self._drag
        self._drag = (what, ring, x, y)
        self.touched()
        if what == "pan":
            self.pan(x - x0, y - y0)
            return True
        cam = self.canvas.get_cam()
        geo = self.geometry(cam)
        cx, cy = geo["centre"]
        R = cam.basis()
        if what == "roll":
            a0, a1 = np.arctan2(y0 - cy, x0 - cx), np.arctan2(y - cy, x - cx)
            self.turn(R[2], float(np.angle(np.exp(1j * (a1 - a0)))))
            return True
        v0 = arcball_vector(x0, y0, cx, cy, geo["radius"])
        v1 = arcball_vector(x, y, cx, cy, geo["radius"])
        if what == "ring":
            a = geo["rings"][ring][0]
            ac = R @ a
            p0, p1 = v0 - (v0 @ ac) * ac, v1 - (v1 @ ac) * ac
            if np.linalg.norm(p0) < 1e-9 or np.linalg.norm(p1) < 1e-9:
                return True
            angle = float(np.arctan2(np.cross(p0, p1) @ ac, p0 @ p1))
            self.turn(a, angle)
            return True
        axis = np.cross(v0, v1)
        n = np.linalg.norm(axis)
        if n < 1e-12:
            return True
        angle = float(np.arctan2(n, np.clip(v0 @ v1, -1, 1)))
        self.turn(R.T @ (axis / n), angle)
        return True

    def release(self) -> None:
        self._drag = None
        self.touched()

    @property
    def dragging(self) -> bool:
        return self._drag is not None

    # ----------------------------------------------------------- keys
    def key(self, name: str, shift: bool = False) -> bool:
        """A key of the view: "left", "right", "up", "down" (Shift: pan), "plus", "minus", "front",
        "left_side"; True when it was used."""
        w, h = self.canvas.view_size()
        cam = self.canvas.get_cam()
        step = np.radians(KEY_ANGLE)
        axes = scene_axes(self.canvas.scene_up())
        if name in ("left", "right", "up", "down"):
            if shift:
                d = KEY_PAN * h
                self.pan(*{"left": (d, 0), "right": (-d, 0), "up": (0, d), "down": (0, -d)}[name])
            elif name in ("left", "right"):
                self.turn(axes["up"], step if name == "right" else -step)
            else:
                self.turn(cam.basis()[0], step if name == "down" else -step)
        elif name in ("plus", "minus"):
            self.zoom(1.0 if name == "plus" else -1.0)
        elif name == "front":
            self.look_along(axes["y"])
        elif name == "left_side":
            self.look_along(axes["x"])
        else:
            return False
        self.touched()
        return True
