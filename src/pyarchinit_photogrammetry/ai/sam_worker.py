"""Segment Anything 2.1 worker process.

This file is run by a separate Python interpreter (the SAM environment that the
application installs on demand, with PyTorch and sam2), so it imports nothing from
pyarchinit_photogrammetry. Protocol on stdin/stdout: each message is an 8-byte
big-endian length followed by a pickle. Requests are dicts with ``op``:

* ``init``   ``{"model", "device"?}`` -> ``{"device"}``
* ``points`` ``{"image", "points", "labels", "box"?}`` -> ``{"mask"}`` (bool HxW)
* ``box``    ``{"image", "box"}`` -> ``{"mask"}``
* ``auto``   ``{"image", "points_per_side", "min_area", "options"?}`` -> ``{"masks"}`` (packed bits);
  ``options`` go to ``SAM2AutomaticMaskGenerator`` (``crop_n_layers``, ``pred_iou_thresh``...)
* ``quit``

The device is CUDA, else Apple's GPU (MPS, with ``PYTORCH_ENABLE_MPS_FALLBACK`` for the few
operations Metal lacks), else the CPU.

Replies are ``{"ok": True, ...}`` or ``{"ok": False, "error": traceback}``.
``PPG_SAM_FAKE=1`` answers with simple synthetic masks without loading torch (tests).
"""
from __future__ import annotations

import os
import pickle
import struct
import sys
import traceback

MODELS = {"tiny": "facebook/sam2.1-hiera-tiny", "small": "facebook/sam2.1-hiera-small",
          "base_plus": "facebook/sam2.1-hiera-base-plus", "large": "facebook/sam2.1-hiera-large"}


def _read(stream):
    head = stream.read(8)
    if len(head) < 8:
        return None
    (n,) = struct.unpack(">Q", head)
    return pickle.loads(stream.read(n))


def _write(stream, obj) -> None:
    data = pickle.dumps(obj, protocol=4)
    stream.write(struct.pack(">Q", len(data)))
    stream.write(data)
    stream.flush()


class _Fake:
    device = "fake"

    def points(self, image, points, labels, box=None):
        import numpy as np

        h, w = image.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w]
        x, y = points[0]
        return (xx - x) ** 2 + (yy - y) ** 2 < (min(h, w) * 0.1) ** 2

    def box(self, image, box):
        import numpy as np

        m = np.zeros(image.shape[:2], bool)
        x0, y0, x1, y1 = (int(round(v)) for v in box)
        m[y0:y1, x0:x1] = True
        return m

    def auto(self, image, points_per_side, min_area, **options):
        h, w = image.shape[:2]
        return [self.box(image, (w * 0.1, h * 0.1, w * 0.4, h * 0.4)), self.box(image, (w * 0.5, h * 0.5, w * 0.9, h * 0.9))]


class _Sam:
    def __init__(self, model: str, device: str | None):
        import torch
        from sam2.sam2_image_predictor import SAM2ImagePredictor

        if device is None:
            device = "cuda" if torch.cuda.is_available() else (
                "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else "cpu")
        self.device = device
        self.predictor = SAM2ImagePredictor.from_pretrained(MODELS.get(model, model), device=device)
        self.model = self.predictor.model
        self._key = None

    def _set(self, image):
        key = (image.shape, int(image[::97, ::97].sum()))
        if key != self._key:
            self.predictor.set_image(image)
            self._key = key

    def points(self, image, points, labels, box=None):
        import numpy as np
        import torch

        self._set(image)
        many = box is None and len(points) == 1  # one click is ambiguous: take SAM's best of three
        with torch.inference_mode():
            masks, scores, _ = self.predictor.predict(point_coords=np.asarray(points, float),
                                                      point_labels=np.asarray(labels, int),
                                                      box=None if box is None else np.asarray(box, float)[None],
                                                      multimask_output=many)
        return masks[int(np.argmax(scores))].astype(bool)

    def box(self, image, box):
        import numpy as np
        import torch

        self._set(image)
        with torch.inference_mode():
            masks, _, _ = self.predictor.predict(box=np.asarray(box, float)[None], multimask_output=False)
        return masks[0].astype(bool)

    def auto(self, image, points_per_side, min_area, **options):
        import torch
        from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator

        gen = SAM2AutomaticMaskGenerator(self.model, points_per_side=points_per_side, min_mask_region_area=min_area,
                                         **options)
        with torch.inference_mode():
            res = gen.generate(image)
        return [r["segmentation"].astype(bool) for r in sorted(res, key=lambda r: -r["area"])]


def main() -> int:
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")  # before torch is imported
    proto_in = os.fdopen(os.dup(0), "rb")
    proto_out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)  # anything printed by torch/huggingface goes to stderr, not into the protocol
    engine = None
    while True:
        req = _read(proto_in)
        if req is None or req.get("op") == "quit":
            return 0
        try:
            op = req["op"]
            if op == "init":
                engine = _Fake() if os.environ.get("PPG_SAM_FAKE") else _Sam(req.get("model", "small"), req.get("device"))
                reply = {"device": engine.device}
            elif engine is None:
                raise RuntimeError("init first")
            elif op == "points":
                reply = {"mask": engine.points(req["image"], req["points"], req["labels"], req.get("box"))}
            elif op == "box":
                reply = {"mask": engine.box(req["image"], req["box"])}
            elif op == "auto":
                import numpy as np

                masks = engine.auto(req["image"], req.get("points_per_side", 32), req.get("min_area", 500),
                                    **(req.get("options") or {}))
                reply = {"masks": [np.packbits(m) for m in masks], "shape": req["image"].shape[:2]}
            else:
                raise ValueError(f"unknown op {op}")
            _write(proto_out, {"ok": True, **reply})
        except Exception:
            _write(proto_out, {"ok": False, "error": traceback.format_exc()})


if __name__ == "__main__":
    sys.exit(main())
