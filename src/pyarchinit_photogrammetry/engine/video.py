"""Photos from a video: frames chosen for the reconstruction, with GPS from DJI subtitles.

:func:`extract_frames` keeps a frame each time the view has moved by a share of its width since
the last one kept (``overlap`` of the frame still in common, measured by matching ORB points on
reduced frames), and among the frames around that moment the sharpest (motion blur); or simply one
every ``every`` seconds. :func:`read_srt` reads the position of every frame from the ``.SRT``
subtitles of DJI drones (the three layouts in use: ``[latitude: ...] [longitude: ...]``,
``GPS(lon,lat,alt)`` and ``GPS (lon, lat, alt)``); :func:`add_video` adds the frames to a chunk
with those positions as camera references.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import cv2
import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress
from .quality import sharpness

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".lrf"}


def _seconds(stamp: str) -> float:
    h, m, rest = stamp.strip().split(":")
    s, _, ms = rest.replace(".", ",").partition(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int((ms or "0").ljust(3, "0")[:3]) / 1000.0


def read_srt(path: str | os.PathLike) -> list[tuple[float, float, float, float | None]]:
    """``(time s, lon, lat, alt)`` of every subtitle of a DJI ``.SRT`` that has a position
    (``alt``: the absolute altitude when given, else the relative one, else None)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    out = []
    for block in re.split(r"\n\s*\n", text):
        times = re.search(r"(\d+:\d+:\d+[,.]\d+)\s*-->", block)
        if not times:
            continue
        t = _seconds(times.group(1))
        lat = re.search(r"\[latitude\s*:\s*(-?[\d.]+)\]", block)
        lon = re.search(r"\[longitude\s*:\s*(-?[\d.]+)\]", block)
        if lat and lon:
            alt = re.search(r"abs_alt\s*:\s*(-?[\d.]+)", block) or re.search(r"\[altitude\s*:\s*(-?[\d.]+)\]", block) \
                or re.search(r"rel_alt\s*:\s*(-?[\d.]+)", block)
            out.append((t, float(lon.group(1)), float(lat.group(1)), float(alt.group(1)) if alt else None))
            continue
        gps = re.search(r"GPS\s*\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*(?:,\s*(-?[\d.]+))?", block)  # lon first
        if gps:
            alt = re.search(r"\bH\s+(-?[\d.]+)\s*m", block) or re.search(r"BAROMETER\s*:\s*(-?[\d.]+)", block)
            z = float(gps.group(3)) if gps.group(3) else (float(alt.group(1)) if alt else None)
            out.append((t, float(gps.group(1)), float(gps.group(2)), z))
    return [p for p in out if not (p[1] == 0.0 and p[2] == 0.0)]  # no fix yet


def position_at(track, t: float):
    """The position of a track (:func:`read_srt`) at time ``t``, linearly between its samples."""
    if not track:
        return None
    times = np.array([p[0] for p in track])
    i = int(np.searchsorted(times, t))
    if i <= 0:
        return track[0][1:]
    if i >= len(track):
        return track[-1][1:]
    a, b = track[i - 1], track[i]
    f = (t - a[0]) / max(b[0] - a[0], 1e-9)
    alt = None if a[3] is None or b[3] is None else a[3] + f * (b[3] - a[3])
    return (a[1] + f * (b[1] - a[1]), a[2] + f * (b[2] - a[2]), alt)


class _Motion:
    """How far the view moved since a reference frame: median shift of matched ORB points on a
    copy at most 640 px wide, as a share of the width; 1 and not reliable when too few points
    match (a moved frame, or a view that changed completely)."""

    def __init__(self):
        self.orb = cv2.ORB_create(1500)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        self.ref = None
        self.ref_sharpness = 0.0

    @staticmethod
    def _small(frame):
        h, w = frame.shape[:2]
        f = min(1.0, 640 / w)
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.resize(grey, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA) if f < 1 else grey

    def set_reference(self, frame) -> None:
        small = self._small(frame)
        self.ref = (small.shape[1], *self.orb.detectAndCompute(small, None))
        self.ref_sharpness = sharpness(frame, max_side=640)

    def shift(self, frame) -> tuple[float, bool]:
        """``(shift, reliable)``: the share of the width the view moved, and whether enough points
        matched to say so."""
        if self.ref is None or self.ref[2] is None:
            return 1.0, False
        small = self._small(frame)
        kp, des = self.orb.detectAndCompute(small, None)
        if des is None or len(kp) < 20:
            return 1.0, False
        matches = self.matcher.match(self.ref[2], des)
        if len(matches) < 20:
            return 1.0, False
        d = [np.hypot(*(np.subtract(kp[m.trainIdx].pt, self.ref[1][m.queryIdx].pt))) for m in matches]
        return float(np.median(d)) / self.ref[0], True


def extract_frames(video: str | os.PathLike, folder: str | os.PathLike, overlap: float = 0.7,
                   every: float | None = None, max_frames: int | None = None, window: float = 0.4,
                   progress: Progress | None = None) -> list[dict]:
    """Frames of ``video`` written as JPEG in ``folder`` (``<stem>_<frame>.jpg``): a new one when the
    view has moved by ``1 - overlap`` of its width (the sharpest in the ``window`` seconds after
    that moment), or one every ``every`` seconds. Returns ``[{"path", "frame", "time"}]``."""
    progress = as_progress(progress)
    video = Path(video)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"cannot read the video {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    look = max(1, int(round(fps / 10)))  # frames looked at: ten a second
    span = max(1, int(round(window * fps)))
    motion = _Motion()
    kept: list[dict] = []
    pending = None  # (deadline frame, best sharpness, best frame index, best image)
    index = -1

    def keep(image, frame_no) -> None:
        name = folder / f"{video.stem}_{frame_no:06d}.jpg"
        cv2.imwrite(str(name), image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        kept.append({"path": name, "frame": frame_no, "time": frame_no / fps})
        motion.set_reference(image)

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            index += 1
            if index % look:
                continue
            progress.check()
            if every:
                if not kept or index >= kept[-1]["frame"] + every * fps:
                    keep(frame, index)
            elif pending is not None:
                s = sharpness(frame, max_side=640)
                if s > pending[1]:
                    pending = (pending[0], s, index, frame.copy())
                if index >= pending[0]:
                    keep(pending[3], pending[2])
                    pending = None
            elif not kept:
                pending = (index + span, sharpness(frame, max_side=640), index, frame.copy())
            else:
                moved, reliable = motion.shift(frame)
                if moved >= 1.0 - overlap:
                    s = sharpness(frame, max_side=640)
                    # few points in common: the view changed, or this frame is blurred - only the
                    # first starts a new photo
                    if reliable or s >= 0.6 * motion.ref_sharpness:
                        pending = (index + span, s, index, frame.copy())
            if max_frames and len(kept) >= max_frames:
                break
            if total and index % (look * 20) == 0:
                progress(min(index / total, 0.999), f"Video {video.name}: {len(kept)} frames kept")
        if pending is not None and (not max_frames or len(kept) < max_frames):
            keep(pending[3], pending[2])
    finally:
        cap.release()
    progress(1.0, f"Video {video.name}: {len(kept)} frames")
    return kept


def add_video(chunk: Chunk, video: str | os.PathLike, folder: str | os.PathLike | None = None,
              srt: str | os.PathLike | None = None, overlap: float = 0.7, every: float | None = None,
              max_frames: int | None = None, accuracy: tuple[float, float, float] = (3.0, 3.0, 5.0),
              progress: Progress | None = None) -> list:
    """Frames of a video added to ``chunk`` as photos (in ``folder``, by default
    ``<video stem>_fotogrammi`` beside the video); with the DJI ``.SRT`` (``srt``, or the file with
    the same name beside the video) every frame gets its position as camera reference."""
    progress = as_progress(progress)
    video = Path(video)
    folder = Path(folder) if folder is not None else video.with_name(f"{video.stem}_fotogrammi")
    frames = extract_frames(video, folder, overlap, every, max_frames, progress=progress.sub(0.0, 0.9))
    cams = chunk.add_photos([f["path"] for f in frames])
    if srt is None:
        for cand in (video.with_suffix(".SRT"), video.with_suffix(".srt")):
            if cand.exists():
                srt = cand
                break
    track = read_srt(srt) if srt is not None and Path(srt).exists() else []
    if track:
        from ..gnss.apply import set_camera_position

        by_path = {Path(f["path"]).resolve(): f for f in frames}
        for cam in cams:
            f = by_path.get(Path(cam.path).resolve())
            pos = position_at(track, f["time"]) if f else None
            if pos is None:
                continue
            lon, lat, alt = pos
            set_camera_position(cam, [lon, lat, alt if alt is not None else 0.0], "EPSG:4326", list(accuracy), "video")
    for cam in cams:
        cam.meta["video"] = video.name
    progress(1.0, f"{len(cams)} frames added" + (f" with GPS from {Path(srt).name}" if track else ""))
    return cams
