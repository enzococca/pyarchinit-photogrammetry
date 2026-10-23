"""Worker process of the learned key points (ALIKED or DISK) and LightGlue.

Run by the separate Python environment of the packaged application (the one of SAM 2.1, with
PyTorch and kornia), so it imports nothing from pyarchinit_photogrammetry, only
``learned_net.py`` beside it. Same protocol as ``sam_worker.py``: on stdin/stdout each message is
an 8-byte big-endian length followed by a pickle. Requests are dicts with ``op``:

* ``init``    ``{"kind", "max_keypoints", "device"?}`` -> ``{"device"}``
* ``extract`` ``{"image"}`` (grey uint8) -> ``{"kp", "desc"}``
* ``match``   ``{"a", "b"}`` (descriptor files) -> ``{"matches"}``
* ``quit``

Replies are ``{"ok": True, ...}`` or ``{"ok": False, "error": traceback}``.
"""
from __future__ import annotations

import os
import pickle
import struct
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _read(stream):
    head = stream.read(8)
    if len(head) < 8:
        return None
    (n,) = struct.unpack(">Q", head)
    return pickle.loads(stream.read(n))


def _write(stream, obj) -> None:
    data = pickle.dumps(obj, protocol=4)
    stream.write(struct.pack(">Q", len(data)) + data)
    stream.flush()


def main() -> None:
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # anything the libraries print must not break the protocol
    feats = None
    while True:
        req = _read(stdin)
        if req is None or req.get("op") == "quit":
            break
        try:
            op = req["op"]
            if op == "init":
                from learned_net import Features

                feats = Features(req.get("kind", "disk"), int(req.get("max_keypoints", 4096)), req.get("device"))
                _write(stdout, {"ok": True, "device": feats.device})
            elif op == "extract":
                kp, desc = feats.extract(req["image"])
                _write(stdout, {"ok": True, "kp": kp, "desc": desc})
            elif op == "match":
                _write(stdout, {"ok": True, "matches": feats.match_files(req["a"], req["b"])})
            else:
                _write(stdout, {"ok": False, "error": f"unknown op {op!r}"})
        except Exception:  # noqa: BLE001 - every error goes back to the program
            _write(stdout, {"ok": False, "error": traceback.format_exc()})


if __name__ == "__main__":
    main()
