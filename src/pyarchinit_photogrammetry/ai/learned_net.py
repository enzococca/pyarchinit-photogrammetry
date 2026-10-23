"""Learned key points and LightGlue with kornia: the part that needs PyTorch, on its own.

The program imports it through :mod:`pyarchinit_photogrammetry.engine.learned` when PyTorch and
kornia are installed beside it. The packaged application has no PyTorch: there it runs in the
separate environment of SAM 2.1 through ``learned_worker.py``. So it imports nothing but numpy,
torch and kornia.

ALIKED (Zhao et al. 2023, BSD-3-Clause) or DISK (Tyszkiewicz et al. 2020, Apache-2.0) find the key points and
describe them with a small network; LightGlue (Lindenberger et al. 2023, Apache-2.0) matches two
sets of them with attention and leaves out the pairs it is not sure of. The key points come back
with the centre of the first pixel at 0, as OpenCV.
"""
from __future__ import annotations

from collections import OrderedDict

import numpy as np

KINDS = {"disk": "DISK + LightGlue", "aliked": "ALIKED + LightGlue"}
MAX_KEYPOINTS = 4096
CACHED_FILES = 96  # descriptor files kept in memory while the pairs are matched


def pick_device(name: str | None = None):
    """CUDA, else Apple's GPU (Metal), else the CPU; ``name`` forces one."""
    import torch

    if name:
        return torch.device(name)
    if torch.cuda.is_available():
        return torch.device("cuda")
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


class Features:
    """The extractor of one kind and LightGlue for it, on the device."""

    def __init__(self, kind: str = "disk", max_keypoints: int = MAX_KEYPOINTS, device: str | None = None):
        import kornia.feature as KF
        import torch

        if kind not in KINDS:
            raise ValueError(f"unknown learned features {kind!r}: {', '.join(KINDS)}")
        self.kind = kind
        self.max_keypoints = int(max_keypoints)
        self.dev = pick_device(device)
        self._torch = torch
        self._KF = KF
        if kind == "aliked":
            # threshold 0: the strongest points up to the limit, whatever their score (thermal frames have weak ones)
            self.net = KF.ALIKED.from_pretrained("aliked-n16", max_num_keypoints=self.max_keypoints,
                                                 detection_threshold=0.0, device=self.dev).eval()
        else:
            self.net = KF.DISK.from_pretrained("depth", device=self.dev).eval()
        self.matcher = KF.LightGlueMatcher(kind).to(self.dev).eval()
        self._files: OrderedDict[str, tuple] = OrderedDict()

    @property
    def device(self) -> str:
        return self.dev.type

    def extract(self, grey: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Key points ``(N, 2)`` float32 (x, y) and descriptors ``(N, D)`` float16 of a grey image
        (uint8, or float in 0..1)."""
        torch = self._torch
        img = np.asarray(grey)
        scale = 255.0 if img.dtype == np.uint8 else 1.0
        t = torch.from_numpy(np.ascontiguousarray(img, np.float32) / scale)[None, None].repeat(1, 3, 1, 1).to(self.dev)
        with torch.inference_mode():
            if self.kind == "disk":
                out = self.net(t, n=self.max_keypoints, pad_if_not_divisible=True)
            else:
                out = self.net(t)
        kp = out[0].keypoints.float().cpu().numpy().astype(np.float32)
        desc = out[0].descriptors.float().cpu().numpy().astype(np.float16)
        h, w = img.shape[:2]
        inside = (kp[:, 0] <= w - 1) & (kp[:, 1] <= h - 1)  # DISK pads to a multiple of 16
        return np.ascontiguousarray(kp[inside]), np.ascontiguousarray(desc[inside])

    def match(self, kp1, desc1, size1, kp2, desc2, size2) -> np.ndarray:
        """LightGlue's matches ``(M, 2)`` uint32 (indices into the two sets); ``size`` = (height, width)
        of the image the points were found on."""
        torch, KF = self._torch, self._KF
        if len(kp1) < 2 or len(kp2) < 2:
            return np.zeros((0, 2), np.uint32)
        dev = self.dev
        k1 = torch.from_numpy(np.ascontiguousarray(kp1, np.float32)).to(dev)
        k2 = torch.from_numpy(np.ascontiguousarray(kp2, np.float32)).to(dev)
        d1 = torch.from_numpy(np.ascontiguousarray(desc1, np.float32)).to(dev)
        d2 = torch.from_numpy(np.ascontiguousarray(desc2, np.float32)).to(dev)
        with torch.inference_mode():
            laf1 = KF.laf_from_center_scale_ori(k1[None], torch.ones(1, len(k1), 1, 1, device=dev))
            laf2 = KF.laf_from_center_scale_ori(k2[None], torch.ones(1, len(k2), 1, 1, device=dev))
            _dist, idx = self.matcher(d1, d2, laf1, laf2, hw1=tuple(int(v) for v in size1),
                                      hw2=tuple(int(v) for v in size2))
        return np.ascontiguousarray(idx.cpu().numpy().astype(np.uint32).reshape(-1, 2))

    def _load(self, path: str) -> tuple:
        got = self._files.get(path)
        if got is None:
            with np.load(path) as z:
                got = (z["kp"], z["desc"], tuple(int(v) for v in z["size"]))
            self._files[path] = got
            if len(self._files) > CACHED_FILES:
                self._files.popitem(last=False)
        else:
            self._files.move_to_end(path)
        return got

    def match_files(self, a: str, b: str) -> np.ndarray:
        """:meth:`match` on two files written by the extraction (``kp``, ``desc``, ``size``)."""
        ka, da, sa = self._load(str(a))
        kb, db, sb = self._load(str(b))
        return self.match(ka, da, sa, kb, db, sb)
