"""Where the program keeps what it downloads and decompresses for the GNSS work: the folder named by ``PPG_GNSS_DIR``
or, when that is not set, ``gnss`` in the data folder of the program. Only the folders are here; the files of the
stations come to them later."""
from __future__ import annotations

import os
from pathlib import Path


def gnss_dir() -> Path:
    """The folder of the GNSS data of the program: the one ``PPG_GNSS_DIR`` names (the tests name a temporary one) or
    ``gnss`` in the data folder of the program (``<data>/gnss``). It is not made."""
    named = os.environ.get("PPG_GNSS_DIR", "").strip()
    if named:
        return Path(named)
    from ..ai.sam_env import data_dir

    return data_dir() / "gnss"


def cache_dir(kind: str) -> Path:
    """The cache of one kind of file - ``"rinex"``: the decompressed copies of RINEX files - which is ``cache/<kind>``
    in :func:`gnss_dir`, made when it is not there; a ``ValueError`` when ``kind`` is not the name of a folder."""
    if not kind or kind in (".", "..") or "/" in kind or "\\" in kind:
        raise ValueError(f"{kind!r} is not the name of a cache folder")
    folder = gnss_dir() / "cache" / kind
    folder.mkdir(parents=True, exist_ok=True)
    return folder
