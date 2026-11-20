"""The pairs of photos a run verified, and what a second run lost of them: ``ppg bench pairs A B``.

A matching ends in COLMAP's database (``database.db`` in the sfm folder of the chunk, written by the classic matching
and by the native one alike): one row of ``two_view_geometries`` per pair of photos it looked at, with the count of
the matches that survived the geometric verification (``rows``) and the configuration of the pair (``config``).
A pair is **verified** when its row has at least :data:`MIN_INLIERS` inlier matches - COLMAP's ``min_num_inliers``:
below it the pair is DEGENERATE and its matches are dropped - and a configuration the mapper takes: not UNDEFINED,
DEGENERATE or WATERMARK (:data:`NOT_VERIFIED`). The count of verified pairs is a result of the bench
(``verified_pairs``, :func:`.steps.matching_results`), and :func:`compare` sets the pairs of two runs side by side,
by the names of the photos (each database numbers them as its extractor finished them): what the second run
**lost** are the pairs verified in the first and not in the second, what it **gained** the other way round. The
databases are opened read-only and never written.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..engine import sfm

# the least inlier matches of a verified pair: COLMAP's min_num_inliers of the two-view geometry
# (pycolmap.TwoViewGeometryOptions; a test compares them), below which a pair is DEGENERATE and its matches dropped
MIN_INLIERS = 15
# the configurations of a two-view geometry the mapper does not take: UNDEFINED, DEGENERATE, WATERMARK
# (pycolmap.TwoViewGeometryConfiguration; a test compares them)
NOT_VERIFIED = frozenset({0, 1, 7})
DATABASE = "database.db"


def verified(db: Path) -> set[tuple[str, str]]:
    """The verified pairs of the database at ``db``, each as the names of its two photos, the lower first: the rows
    of ``two_view_geometries`` with :data:`MIN_INLIERS` inlier matches or more and a configuration not in
    :data:`NOT_VERIFIED`. The database is opened read-only."""
    con = sqlite3.connect(sfm.read_only_uri(db), uri=True)
    try:
        names = {int(i): str(n) for i, n in con.execute("SELECT image_id, name FROM images")}
        rows = con.execute("SELECT pair_id, rows, config FROM two_view_geometries").fetchall()
    finally:
        con.close()
    pairs = set()
    for pair_id, inliers, config in rows:
        if int(inliers or 0) < MIN_INLIERS or int(config) in NOT_VERIFIED:
            continue
        a, b = divmod(int(pair_id), sfm.COLMAP_MAX_IMAGES)
        pairs.add(tuple(sorted((names[a], names[b]))))
    return pairs


def compare(db_a: Path, db_b: Path) -> dict:
    """The verified pairs of two databases (:func:`verified`), by the names of the photos: ``verified_a``,
    ``verified_b``, ``lost`` (verified in A, not in B) with ``lost_percent`` (of the pairs of A, to two decimals;
    0.0 when A has none) and ``gained`` (verified in B, not in A)."""
    a, b = verified(db_a), verified(db_b)
    lost = len(a - b)
    return {"verified_a": len(a), "verified_b": len(b), "lost": lost,
            "lost_percent": round(100.0 * lost / len(a), 2) if a else 0.0, "gained": len(b - a)}


def describe(result: dict) -> str:
    """The one line ``ppg bench pairs`` prints for the dict of :func:`compare`: numbers only, no path and no photo
    name (``verified A 6058, verified B 6040, lost 31 (0.51 %), gained 13``)."""
    return (f"verified A {result['verified_a']}, verified B {result['verified_b']}, lost {result['lost']} "
            f"({result['lost_percent']:.2f} %), gained {result['gained']}")


def database(folder: Path, label: str) -> Path:
    """The one ``database.db`` under ``folder``, a kept work folder of a run (``OUT/<name>-<i>``: the project's
    database lies under it). Refused (ValueError, the text naming ``label`` - ``A`` or ``B`` - and never the path)
    when ``folder`` is not a folder or holds no ``database.db`` or more than one."""
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError(f"{label} is not a folder: give the kept work folder of a run (OUT/<name>-<i>, --keep)")
    found = sorted(p for p in folder.rglob(DATABASE) if p.is_file())
    if len(found) != 1:
        raise ValueError(f"{label} holds {len(found)} {DATABASE} files under it: exactly one is needed, the "
                         f"project's of a kept run")
    return found[0]
