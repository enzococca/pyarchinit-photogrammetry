"""Fly-through videos: the model (or the dense cloud) seen from a camera that orbits around it,
written as an MP4 for a talk, a report or a museum screen.

The camera turns around the centre of the survey at ``pitch`` degrees above the horizon and at the
distance that frames it (``zoom`` brings it closer), ``turns`` times in ``seconds``. Frames are
rendered off screen by VTK (with the texture, fast on the graphics card) when it is available, else
by the software renderer of the 3D view (points sampled on the surface with their texture colour
and a soft light, z-buffered); ``backend`` chooses. Written with OpenCV (H.264 when it has it, else
MPEG-4).
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from ..core.progress import Progress, as_progress


def _writer(path: Path, fps: float, size):
    import cv2

    for code in ("avc1", "mp4v"):
        w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*code), fps, size)
        if w.isOpened():
            return w
        w.release()
    raise RuntimeError("no MP4 writer in this OpenCV")


class _Software:
    def __init__(self, sd, size, samples):
        from ..gui.scene import sample_mesh_colors

        if sd.mesh_vertices is not None:
            self.pts, self.cols = sample_mesh_colors(sd, n=samples)
        else:
            self.pts, self.cols = sd.points, sd.colors
        w, h = size
        # points sampled densely enough to leave no gaps at this size of the frame
        self.size = 2 if len(self.pts) > 0.8 * w * h else 3
        self.frame = size

    def render(self, cam) -> np.ndarray:
        from ..gui.scene import splat

        return splat(self.pts, self.cols, cam, *self.frame, size=self.size, background=(28, 30, 34))


class _Vtk:
    def __init__(self, sd, size):
        import pyvista as pv

        self.pl = pv.Plotter(off_screen=True, window_size=list(size))
        self.pl.set_background((28 / 255, 30 / 255, 34 / 255))
        if sd.mesh_vertices is not None:
            f = np.asarray(sd.mesh_faces, np.int64)
            mesh = pv.PolyData(np.asarray(sd.mesh_vertices, float), np.column_stack([np.full(len(f), 3), f]).ravel())
            if sd.mesh_uv is not None and sd.texture is not None:
                mesh.active_texture_coordinates = np.asarray(sd.mesh_uv, float)
                self.pl.add_mesh(mesh, texture=pv.read_texture(str(sd.texture)), smooth_shading=False)
            elif sd.mesh_colors is not None:
                self.pl.add_mesh(mesh, scalars=np.asarray(sd.mesh_colors), rgb=True)
            else:
                self.pl.add_mesh(mesh, color=(0.8, 0.78, 0.74))
        else:
            cloud = pv.PolyData(np.asarray(sd.points, float))
            self.pl.add_mesh(cloud, scalars=np.asarray(sd.colors), rgb=True, point_size=2,
                             render_points_as_spheres=False)

    def render(self, cam) -> np.ndarray:
        self.pl.camera.position = tuple(cam.eye)
        self.pl.camera.focal_point = tuple(cam.target)
        self.pl.camera.up = tuple(-cam.basis()[1])
        self.pl.camera.view_angle = cam.fov
        self.pl.camera.clipping_range = (cam.distance * 0.01, cam.distance * 10)
        return np.asarray(self.pl.screenshot(return_img=True))[..., :3]

    def close(self):
        self.pl.close()


def orbit_video(chunk, path: str | os.PathLike, kind: str | None = None, seconds: float = 12.0, fps: int = 25,
                size=(1280, 720), pitch: float = 35.0, zoom: float = 1.0, turns: float = 1.0,
                backend: str = "auto", samples: int | None = None, progress: Progress | None = None) -> Path:
    """An MP4 of a camera orbiting the survey (see the module docstring). ``kind``: what to show
    (``model_textured``, ``model``, ``point_cloud``...; default the best there is). Returns the path."""
    import cv2

    from ..gui.scene import OrbitCamera, best_view, build_scene

    progress = as_progress(progress)
    kind = kind or best_view(chunk)
    if kind in ("empty", "cameras"):
        raise RuntimeError("nothing to film: build the dense cloud or the model first")
    sd = build_scene(chunk, kind, max_points=3_000_000, max_faces=2_000_000, max_textured_faces=8_000_000)
    size = (int(size[0]) // 2 * 2, int(size[1]) // 2 * 2)  # even sides for the codecs
    if backend == "auto":
        backend = "software" if os.environ.get("PPG_RENDERER") == "software" else "vtk"
    renderer = None
    if backend == "vtk":
        try:
            renderer = _Vtk(sd, size)
            renderer.render(OrbitCamera(sd.center.astype(float), sd.extent, up=sd.up))
        except Exception:  # no off-screen OpenGL here: the software renderer
            renderer, backend = None, "software"
    if renderer is None:
        renderer = _Software(sd, size, samples or int(size[0] * size[1] * 2.5))
    frames = max(int(round(seconds * fps)), 2)
    cam = OrbitCamera(sd.center.astype(float), sd.extent * 1.1 / max(zoom, 1e-3), pitch=pitch, up=sd.up)
    start = cam.yaw
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = _writer(path, fps, size)
    try:
        for i in range(frames):
            progress.check()
            cam.yaw = start + 360.0 * turns * i / frames
            img = renderer.render(cam)
            if img.shape[1] != size[0] or img.shape[0] != size[1]:
                img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
            writer.write(np.ascontiguousarray(img[..., ::-1]))
            if i % 10 == 0:
                progress((i + 1) / frames, f"Video: frame {i + 1}/{frames} ({backend})")
    finally:
        writer.release()
        if isinstance(renderer, _Vtk):
            renderer.close()
    progress(1.0, f"Video: {frames} frames, {frames / fps:.0f} s")
    return path
