"""Paradata: how every product of a chunk was made, as machine-readable provenance (W3C PROV-O
in JSON-LD), so that an orthomosaic in PyArchInit's media keeps the photos, the georeference and
the settings it comes from.

* :func:`provenance` - the JSON-LD graph of a product: the product itself (file, SHA-256, size,
  coordinate system, resolution, active version) as a ``prov:Entity``, the ``prov:Activity`` that
  made it (step, parameters, time) and, step by step, the entities it was derived from, back to
  the photos (how many, aligned, cameras, the days they were taken) and the georeference
  (markers, control and check RMSE, scale bars); the program and its libraries, and the operator
  when given, as agents.
* :func:`write_provenance` - ``<file>.prov.jsonld`` beside a file: the product, an export, the
  copy published in PyArchInit.
* :func:`summary` - the same in a few words, for the notes of PyArchInit's documentation.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

NS = "https://github.com/enzococca/pyarchinit-photogrammetry/ns#"
CONTEXT = {"prov": "http://www.w3.org/ns/prov#", "xsd": "http://www.w3.org/2001/XMLSchema#", "ppg": NS,
           "prov:generatedAtTime": {"@type": "xsd:dateTime"}, "prov:endedAtTime": {"@type": "xsd:dateTime"},
           "prov:wasDerivedFrom": {"@type": "@id"}, "prov:wasGeneratedBy": {"@type": "@id"},
           "prov:used": {"@type": "@id"}, "prov:wasAssociatedWith": {"@type": "@id"},
           "prov:wasAttributedTo": {"@type": "@id"}}
LABELS = {"photos": "Photos", "reconstruction": "Alignment (camera poses and tie points)",
          "georeference": "Georeference", "depth_maps": "Depth maps", "point_cloud": "Dense point cloud",
          "model": "3D model", "model_textured": "Textured 3D model", "dem": "Digital elevation model",
          "orthomosaic": "Orthomosaic"}
STEPS = {"reconstruction": ("align_cameras", "sfm"), "point_cloud": ("build_point_cloud", "dense"),
         "depth_maps": ("build_depth_maps", "dense"), "model": ("build_model", "model"),
         "model_textured": ("build_texture", "texture"), "dem": ("build_dem", "dem"),
         "orthomosaic": ("build_orthomosaic", "orthomosaic")}


def _sources(chunk, product: str) -> list[str]:
    """What a product is made from (the settings it was built with say which)."""
    meta = chunk.meta or {}
    if product == "reconstruction":
        return ["photos"]
    if product in ("point_cloud", "depth_maps"):
        return ["reconstruction", "photos"]
    if product == "model":
        src = (meta.get("model") or {}).get("source", "point_cloud")
        return [{"depth_maps": "reconstruction", "tie_points": "reconstruction"}.get(src, "point_cloud")]
    if product == "model_textured":
        return ["model", "photos"]
    if product == "dem":
        src = (meta.get("dem") or {}).get("source", "point_cloud")
        return [{"tie_points": "reconstruction"}.get(src, src), "georeference"]
    if product == "orthomosaic":
        surface = (meta.get("orthomosaic") or {}).get("surface", "dem")
        return ["model" if surface == "model" else "dem", "photos", "georeference"]
    return ["dem"]  # relief, slope, aspect, coverage, precision: rasters of the DEM


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _when(path: Path | None) -> str | None:
    if path is None or not Path(path).exists():
        return None
    return datetime.fromtimestamp(Path(path).stat().st_mtime).isoformat(timespec="seconds")


def _raster_info(path: Path) -> dict:
    try:
        import rasterio

        with rasterio.open(path) as src:
            return {"ppg:width": src.width, "ppg:height": src.height, "ppg:resolution_m": abs(src.res[0])}
    except Exception:  # not a raster
        return {}


def _photos(chunk) -> dict:
    dates = sorted(str((c.meta or {}).get("datetime"))[:10].replace(":", "-") for c in chunk.cameras
                   if (c.meta or {}).get("datetime"))
    sensors = [{"ppg:label": s.label, "ppg:model": s.model, "ppg:width": s.width, "ppg:height": s.height}
               for s in chunk.sensors.values()]
    return {"ppg:count": len(chunk.cameras), "ppg:aligned": len(chunk.aligned_cameras), "ppg:cameras": sensors,
            "ppg:dates": sorted(set(dates))}


def _georeference(chunk) -> dict:
    from ..markers import gcp

    out = {"ppg:crs": chunk.crs, "ppg:markers": len(chunk.markers), "ppg:scale_bars": len(chunk.scalebars)}
    try:
        errs = gcp.marker_errors(chunk)
    except Exception:  # no alignment yet
        errs = []
    for kind in ("control", "check"):
        e = [x["error"] for x in errs if x["type"] == kind and x["error"] is not None]
        if e:
            out[f"ppg:{kind}_points"] = len(e)
            out[f"ppg:{kind}_rmse_m"] = float(sum(v * v for v in e) / len(e)) ** 0.5
    return out


def _params(value):
    """Settings as JSON values (paths and numpy numbers as text and floats)."""
    if isinstance(value, dict):
        return {str(k): _params(v) for k, v in value.items() if k not in ("names", "image_root")}
    if isinstance(value, (list, tuple)):
        return [_params(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def provenance(chunk, product: str, path: str | os.PathLike | None = None, operator: str | None = None,
               checksum: bool = True) -> dict:
    """The JSON-LD provenance of ``product`` of ``chunk`` (see the module docstring); ``path``:
    the file to describe (default the product of the chunk: an export or a published copy is
    described as itself, derived from the product)."""
    from .. import __version__

    base = f"ppg:chunk/{chunk.key}/"
    software = {"@id": f"ppg:software/{__version__}", "@type": "prov:SoftwareAgent",
                "prov:label": f"pyarchinit-photogrammetry {__version__}", "ppg:libraries": _libraries()}
    agents = [software["@id"]]
    graph = [software]
    if operator:
        person = {"@id": "ppg:operator", "@type": ["prov:Agent", "prov:Person"], "prov:label": operator}
        graph.append(person)
        agents.append(person["@id"])
    done = set()

    def entity(name: str) -> str:
        nid = base + name
        if name in done:
            return nid
        done.add(name)
        node = {"@id": nid, "@type": "prov:Entity", "prov:label": LABELS.get(name, name)}
        if name == "photos":
            node.update(_photos(chunk))
        elif name == "georeference":
            node.update(_georeference(chunk))
        else:
            file = chunk.product_path(name) if name in chunk.products else None
            if file is not None and file.exists():
                node.update({"ppg:file": file.name, "prov:generatedAtTime": _when(file)})
            if name == "reconstruction":
                tie = chunk.tie_points
                if tie is not None:
                    node["ppg:tie_points"] = int(len(tie))
            step, key = STEPS.get(name, (f"build_{name}", name))
            settings = (chunk.meta or {}).get(key) or ((chunk.meta or {}).get("relief") or {}).get(name)
            activity = {"@id": nid + "#activity", "@type": "prov:Activity", "prov:label": step,
                        "prov:endedAtTime": node.get("prov:generatedAtTime"), "prov:wasAssociatedWith": agents,
                        "ppg:parameters": _params(settings or {})}
            sources = [entity(s) for s in _sources(chunk, name)]
            activity["prov:used"] = sources
            node["prov:wasGeneratedBy"] = activity["@id"]
            node["prov:wasDerivedFrom"] = sources
            graph.append(activity)
        graph.append(node)
        return nid

    top = entity(product)
    if path is not None:  # a copy of the product (an export, the file published in PyArchInit)
        path = Path(path)
        node = {"@id": f"ppg:file/{path.name}", "@type": "prov:Entity", "prov:label": path.name,
                "ppg:file": path.name, "prov:wasDerivedFrom": [top], "prov:wasAttributedTo": agents,
                "prov:generatedAtTime": _when(path)}
        target = node
        graph.append(node)
    else:
        target = next(n for n in graph if n.get("@id") == top)
        path = chunk.product_path(product)
    if path is not None and Path(path).exists():
        target.update({"ppg:size_bytes": Path(path).stat().st_size, "ppg:crs": chunk.crs, **_raster_info(Path(path))})
        if checksum:
            target["ppg:sha256"] = _sha256(Path(path))
    versions = ((chunk.meta or {}).get("active_versions") or {}).get(product)
    if versions:
        target["ppg:version"] = versions.get("name")
    return {"@context": CONTEXT, "@graph": [{k: v for k, v in n.items() if v is not None} for n in graph]}


def _libraries() -> dict:
    out = {}
    for name in ("pycolmap", "open3d", "numpy", "rasterio", "cv2"):
        try:
            mod = __import__(name)
            out[name] = str(getattr(mod, "__version__", ""))
        except Exception:  # not installed here
            continue
    return out


def write_provenance(chunk, product: str, path: str | os.PathLike, operator: str | None = None,
                     checksum: bool = True) -> Path:
    """``<path>.prov.jsonld`` with the :func:`provenance` of ``path`` (a copy of ``product``)."""
    path = Path(path)
    out = path.with_name(path.name + ".prov.jsonld")
    doc = provenance(chunk, product, path=path, operator=operator, checksum=checksum)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def summary(chunk, product: str) -> str:
    """A line about how ``product`` was made (for notes and captions)."""
    meta = chunk.meta or {}
    parts = [f"{len(chunk.aligned_cameras)}/{len(chunk.cameras)} photos aligned"]
    geo = _georeference(chunk)
    if "ppg:control_rmse_m" in geo:
        parts.append(f"control RMSE {geo['ppg:control_rmse_m'] * 1000:.1f} mm ({geo['ppg:control_points']} GCP)")
    if "ppg:check_rmse_m" in geo:
        parts.append(f"check RMSE {geo['ppg:check_rmse_m'] * 1000:.1f} mm")
    key = STEPS.get(product, (None, product))[1]
    settings = meta.get(key) or {}
    if settings.get("resolution"):
        parts.append(f"{settings['resolution'] * 100:.2f} cm/px")
    if chunk.crs:
        parts.append(f"CRS {chunk.crs}")
    from .. import __version__

    parts.append(f"pyarchinit-photogrammetry {__version__}")
    return "; ".join(parts)
