"""Command line interface: ``ppg <command>``.

Every step of the workflow is available, plus ``ppg process`` for the whole
pipeline and ``ppg run script.py`` to execute Python scripts against a project.
"""
from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .core.document import Document
from .core.progress import print_progress

app = typer.Typer(add_completion=False, help="pyarchinit-photogrammetry: open-source photogrammetry for archaeology")
markers_app = typer.Typer(help="Coded targets: printable sheets and turntable mats")
pa_app = typer.Typer(help="PyArchInit database bridge")
plugins_app = typer.Typer(help="Plugins: those found, the index of the plugin repository, installing, enabling")
app.add_typer(markers_app, name="markers")
app.add_typer(pa_app, name="pyarchinit")
app.add_typer(plugins_app, name="plugins")


CLASSES_HELP = "point classes, e.g. 2,64 (2 ground, 3-5 vegetation, 64 masonry, 71 equipment)"


def _classes(text: Optional[str]) -> Optional[list[int]]:
    """``"2,64"`` -> ``[2, 64]`` (None when not given)."""
    if not text:
        return None
    try:
        return [int(v) for v in text.replace(";", ",").replace(" ", ",").split(",") if v]
    except ValueError as exc:
        raise typer.BadParameter(f"class codes are numbers 0-255: {text}") from exc


def _open(project: Path, chunk: Optional[str] = None):
    doc = Document.open(project)
    if not doc.chunks:
        raise typer.BadParameter("the project has no chunks")
    ch = doc.chunk if chunk is None else next(c for c in doc.chunks if c.label == chunk or str(c.key) == chunk)
    return doc, ch


@app.command()
def version():
    """Print the version."""
    typer.echo(__version__)


@app.command()
def new(project: Path, chunk: str = typer.Option("Chunk 1", help="label of the first chunk")):
    """Create a new project file (.ppg)."""
    doc = Document()
    doc.add_chunk(chunk)
    typer.echo(f"created {doc.save(project)}")


@app.command("add-photos")
def add_photos(project: Path, paths: list[Path], chunk: Optional[str] = None, recursive: bool = False):
    """Add photos or folders of photos."""
    doc, ch = _open(project, chunk)
    added = ch.add_photos(paths, recursive=recursive)
    doc.save()
    typer.echo(f"added {len(added)} photos ({len(ch.cameras)} in chunk, {len(ch.sensors)} sensors)")


@app.command("detect-markers")
def detect_markers(project: Path, target_type: str = "aruco_4x4_50", chunk: Optional[str] = None):
    """Detect coded targets in the photos."""
    doc, ch = _open(project, chunk)
    found = ch.detect_markers(target_type, progress=print_progress)
    doc.save()
    typer.echo(f"{len(found)} targets: " + ", ".join(f"{m.label} ({len(m.projections)} photos)" for m in found))


@app.command("import-scans")
def import_scans_cmd(project: Path, files: list[Path], label: Optional[str] = None,
                     crs: Optional[str] = typer.Option(None, help="CRS of the scans (default local)"),
                     max_points: Optional[int] = None):
    """Laser scans (E57, PTX) as the dense cloud of a new chunk (then align-chunks --method points)."""
    from .io.scans import import_scans

    doc = Document.open(project)
    ch = import_scans(doc, files, label=label, crs=crs, max_points=max_points, progress=print_progress)
    doc.save()
    typer.echo(f"{ch.label}: {len(ch.point_cloud)} points from {len(ch.meta['scans'])} scans")


@app.command("import-model")
def import_model_cmd(project: Path, file: Path,
                     chunk: Optional[str] = typer.Option(None, help="chunk to import into (default: the active one)"),
                     new_chunk: Optional[str] = typer.Option(None, "--new-chunk", help="put the model in a new chunk "
                                                             "with this name"),
                     internal: bool = typer.Option(False, help="the file is in the internal frame of the chunk "
                                                   "(its model.ply edited elsewhere)"),
                     up: str = typer.Option("auto", help="vertical axis of the file: auto (Y for glTF), z or y"),
                     scale: float = typer.Option(1.0, help="multiplies the coordinates of the file (0.001 for mm)"),
                     offset: Optional[str] = typer.Option(None, help="X,Y,Z added after the scale (default: the "
                                                          "origin of a GLB of this program, else 0)"),
                     crs: Optional[str] = typer.Option(None, help="CRS of a new or empty chunk (e.g. EPSG:32633)"),
                     overwrite: bool = typer.Option(False, help="replace the current model instead of keeping it "
                                                    "as a version"),
                     name: Optional[str] = typer.Option(None, help="name of the new version of the model")):
    """A mesh of another program (OBJ, PLY, GLB/glTF, STL, ...) as the model of a chunk, with its
    colours and texture; the current model is kept as a version."""
    from .io.mesh_import import import_model, import_model_chunk

    xyz = None
    if offset:
        xyz = [float(v) for v in offset.replace(";", ",").split(",")]
        if len(xyz) != 3:
            raise typer.BadParameter("offset: X,Y,Z")
    kw = {"frame": "internal" if internal else "world", "up": up, "scale": scale, "offset": xyz}
    if new_chunk is not None:
        doc = Document.open(project)
        ch, r = import_model_chunk(doc, file, label=new_chunk or None, crs=crs, progress=print_progress, **kw)
    else:
        doc, ch = _open(project, chunk)
        r = import_model(ch, file, crs=crs, keep=not overwrite, name=name, progress=print_progress, **kw)
    doc.save()
    typer.echo(f"{ch.label}: {r['faces']} faces, {r['vertices']} vertices, {r['pages']} texture pages"
               + (f", previous model kept as version {r['kept']}" if r["kept"] else ""))
    if r["missing_textures"]:
        typer.echo(f"textures not found: {', '.join(r['missing_textures'])}")
    if r["distance"]:
        typer.echo(f"warning: the model lies {r['distance']:.1f} m from the data of the chunk "
                   "(check the coordinate system, --offset, --scale and --up)")


@app.command("cut-model")
def cut_model_cmd(project: Path,
                  horizontal: Optional[float] = typer.Option(None, help="horizontal plane at this height"),
                  vertical: Optional[str] = typer.Option(None, help="vertical plane through the line X1,Y1,X2,Y2 "
                                                         "(above = to the left of the walker from 1 to 2)"),
                  points: Optional[str] = typer.Option(None, help="plane through three points X1,Y1,Z1,X2,Y2,Z2,"
                                                       "X3,Y3,Z3"),
                  plane: Optional[str] = typer.Option(None, help="a point and the normal X,Y,Z,NX,NY,NZ"),
                  dip: Optional[str] = typer.Option(None, help="X,Y,Z,DIP_DIRECTION,DIP (degrees, from north)"),
                  keep: str = typer.Option("above", help="above, below, slab, both or series"),
                  thickness: Optional[float] = typer.Option(None, help="metres: the slab, or each slice of a series"),
                  step: Optional[float] = typer.Option(None, help="metres between the slices of a series"),
                  count: Optional[int] = typer.Option(None, help="slices of a series (default: across the model)"),
                  out: Optional[Path] = typer.Option(None, help="file (.ply/.obj) for a part, the stem of the two "
                                                     "parts, the folder of a series; without it the part kept "
                                                     "becomes the model"),
                  overwrite: bool = typer.Option(False, help="do not keep the current model as a version"),
                  name: Optional[str] = typer.Option(None, help="name of the new version of the model"),
                  cap: bool = typer.Option(True, help="close the solids cut on the plane (each in its colour)"),
                  chunk: Optional[str] = None):
    """Cut the model with a plane: keep one side, a slice, both sides as files or a series of slices."""
    import numpy as np

    from .engine import mesh_cut

    def numbers(text, n, what):
        values = [float(v) for v in text.replace(";", ",").split(",")]
        if len(values) != n:
            raise typer.BadParameter(f"{what}: {n} numbers separated by commas")
        return values

    given = [v is not None for v in (horizontal, vertical, points, plane, dip)]
    if sum(given) != 1:
        raise typer.BadParameter("give one plane: --horizontal, --vertical, --points, --plane or --dip")
    if horizontal is not None:
        p, n = mesh_cut.horizontal(horizontal)
    elif vertical is not None:
        v = numbers(vertical, 4, "--vertical")
        p, n = mesh_cut.vertical(v[:2], v[2:])
    elif points is not None:
        v = numbers(points, 9, "--points")
        p, n = mesh_cut.through_points(v[:3], v[3:6], v[6:])
    elif plane is not None:
        v = numbers(plane, 6, "--plane")
        p, n = np.asarray(v[:3]), np.asarray(v[3:]) / np.linalg.norm(v[3:])
    else:
        v = numbers(dip, 5, "--dip")
        p, n = mesh_cut.dipping(v[:3], v[3], v[4])
    doc, ch = _open(project, chunk)
    r = ch.cut_model(p, n, keep=keep, thickness=thickness, step=step, count=count, out=out,
                     as_version=not overwrite, name=name, cap=cap, progress=print_progress)
    if not r["files"]:
        doc.save()
        typer.echo(f"{ch.label}: model cut, {r['faces']} -> {r['faces_kept']} faces"
                   + (f", previous model kept as version {r['kept']}" if r["kept"] else ""))
    for f in r["files"]:
        typer.echo(f)


@app.command("colour-scan")
def colour_scan(project: Path, scan: str = typer.Option(..., help="label of the chunk to colour"),
                photos: str = typer.Option(..., help="label of the chunk with the aligned photos")):
    """Colour a point cloud (a laser scan) from the aligned photos of another chunk."""
    from .io.scans import colour_from_photos

    doc = Document.open(project)
    pick = {c.label: c for c in doc.chunks}
    r = colour_from_photos(pick[scan], pick[photos], progress=print_progress)
    doc.save()
    typer.echo(f"{r['coloured']} of {r['points']} points coloured from {r['photos']} photos")


@app.command("light-model")
def light_model_cmd(project: Path, out: Path = typer.Argument(..., help="folder for light.glb, light.obj and the "
                                                                      "textures"),
                    faces: int = typer.Option(50_000, help="faces of the light model"),
                    texture: int = typer.Option(4096, help="side of the colour texture and of the normal map"),
                    chunk: Optional[str] = None):
    """The model reduced to a few faces with a colour texture and a normal map baked from the full one."""
    from .engine.normalmap import light_model

    _, ch = _open(project, chunk)
    r = light_model(ch, faces, texture, out, progress=print_progress)
    typer.echo(f"{r['faces']} faces, {r['hits'] * 100:.1f}% of the texels on the full model: {r['glb']}")


@app.command()
def video(project: Path, out: Path, kind: Optional[str] = typer.Option(None, help="model_textured, model, point_cloud"),
          seconds: float = 12.0, fps: int = 25, size: str = typer.Option("1280x720", help="width x height"),
          pitch: float = 35.0, zoom: float = 1.0, turns: float = 1.0,
          backend: str = typer.Option("auto", help="vtk (graphics card), software or auto"),
          chunk: Optional[str] = None):
    """A fly-through video (MP4): a camera orbiting the model or the dense cloud."""
    from .io.flythrough import orbit_video

    _, ch = _open(project, chunk)
    w, h = (int(v) for v in size.lower().split("x"))
    typer.echo(str(orbit_video(ch, out, kind=kind, seconds=seconds, fps=fps, size=(w, h), pitch=pitch, zoom=zoom,
                               turns=turns, backend=backend, progress=print_progress)))


@app.command("batch")
def batch_cmd(project: Path, steps: Path = typer.Argument(..., help="JSON file of the steps"),
              restart: bool = typer.Option(False, help="start again instead of resuming an unfinished batch"),
              retries: int = typer.Option(1, help="attempts more for a failing step"),
              stop_on_error: bool = typer.Option(False, help="stop at the first step that fails")):
    """Run a batch of steps over the chunks, saving the project after every step (an interrupted
    batch goes on from where it stopped)."""
    from . import batch

    doc = Document.open(project)
    report = batch.run_batch(doc, batch.load_steps(steps), resume=not restart, retries=retries,
                             on_error="stop" if stop_on_error else "continue", progress=print_progress)
    for key, err in report["failed"].items():
        typer.echo(f"failed {key}: {err}")
    typer.echo(f"{len(report['done'])} steps done, {len(report['failed'])} failed, {len(report['skipped'])} skipped "
               f"in {report['seconds']:.0f} s")


@app.command("field-check")
def field_check(project: Path, min_photos: int = typer.Option(3, help="photos a point needs"),
                resolution: Optional[float] = typer.Option(None, help="cell of the coverage map (metres)"),
                chunk: Optional[str] = None):
    """Before leaving the site: areas seen by too few photos, photos not aligned or blurred,
    markers in too few photos (writes the coverage map)."""
    from .engine.fieldcheck import field_check as _check

    doc, ch = _open(project, chunk)
    rep = _check(ch, min_photos=min_photos, resolution=resolution, progress=print_progress)
    doc.save()
    if rep.get("cells"):
        typer.echo(f"coverage: {rep['weak_fraction'] * 100:.1f}% of the area ({rep['weak_area']:.2f} m2) seen by fewer "
                   f"than {min_photos} photos; median {rep['median_photos']:.0f} photos: {ch.product_path('coverage')}")
    typer.echo(f"photos: {rep['photos']}, not aligned {len(rep['unaligned'])}"
               + (f": {', '.join(rep['unaligned'])}" if rep["unaligned"] else ""))
    if rep.get("blurred"):
        typer.echo(f"blurred: {', '.join(rep['blurred'])}")
    for m in rep["markers"]:
        typer.echo(f"marker {m['label']}: {m['status']}")


@app.command()
def relief(project: Path, method: str = typer.Option("svf", help="svf, openness_pos, openness_neg, slrm, msrm, "
                                                                "multi_hillshade"),
           radius: Optional[float] = typer.Option(None, help="search radius in metres (default about 20 cells)"),
           directions: int = typer.Option(16, help="directions of the horizon and of the light"),
           min_size: Optional[float] = typer.Option(None, help="msrm: smallest feature (metres)"),
           max_size: Optional[float] = typer.Option(None, help="msrm: largest feature (metres)"),
           chunk: Optional[str] = None):
    """Relief visualisation of the DEM (sky-view factor, openness, local or multi-scale relief,
    multi-directional hillshade): faint walls, ditches and terraces."""
    doc, ch = _open(project, chunk)
    path = ch.build_relief(method, radius=radius, directions=directions, min_size=min_size, max_size=max_size,
                           progress=print_progress)
    doc.save()
    info = ch.meta["relief"][method]
    typer.echo(f"{method}: {path} (radius {info['radius_m']:g} m, values {info['p2']:.4g} to {info['p98']:.4g})")


@app.command()
def timeline(project: Path, csv: Optional[Path] = typer.Option(None, help="write the report as CSV"),
             date: Optional[str] = typer.Option(None, help="set the date of --chunk (ISO, e.g. 2026-07-01T09:30; "
                                                           "'' = from the photos)"),
             coregister: bool = typer.Option(True, "--coregister/--no-coregister",
                                             help="coregister the DEMs of the phases before measuring the volumes "
                                                  "(the DEMs of the project are not changed)"),
             coregister_to: str = typer.Option("first", help="coregister every phase onto the 'first' phase or onto "
                                                             "the 'previous' one"),
             chunk: Optional[str] = None):
    """Excavation over time: the chunks with a DEM as phases in date order, the volume removed
    from one to the next and in every unit drawn, the relations suggested by the order of removal."""
    from .archaeo import timeline as tl

    if coregister_to not in tl.COREG_REFERENCES:
        raise typer.BadParameter(f"--coregister-to: one of {', '.join(tl.COREG_REFERENCES)}")
    doc, ch = _open(project, chunk)
    if date is not None:
        tl.set_phase_date(ch, date or None)
        doc.save()
    report = tl.timeline(doc, progress=print_progress, coregister=coregister, reference=coregister_to)
    for p in report["phases"]:
        when = p["date"].strftime("%Y-%m-%d %H:%M") if p["date"] else "no date"
        typer.echo(f"{when}  {p['label']}: {', '.join(p['units']) or 'no units drawn'}")
        if p.get("coregistration"):
            typer.echo(f"    {tl.coregistration_text(p['coregistration'])}")
    for s in report["removed"]:
        typer.echo(f"{s['from']} -> {s['to']}: removed {s['cut_m3']:.3f} m3, added {s['fill_m3']:.3f} m3 "
                   f"over {s['area_m2']:.2f} m2")
        for u in s["units"]:
            typer.echo(f"    {u['name']}: removed {u['cut_m3']:.3f} m3")
    for r in report["relations"]:
        typer.echo(f"{tl._name(r['upper'])} {r['relation']} {tl._name(r['lower'])} "
                   f"({r['overlap_m2']:.2f} m2, {' -> '.join(r['phases'])})")
    if csv is not None:
        csv.write_text(tl.timeline_csv(report), encoding="utf-8")
        typer.echo(f"written {csv}")


def _polygons(texts: Optional[list[str]]):
    """Polygons given on the command line - WKT, a GeoJSON geometry or feature collection, or the
    path of a GeoJSON file - merged (None without)."""
    if not texts:
        return None
    from shapely.geometry import shape
    from shapely.ops import unary_union

    from .engine.crop import as_polygon

    out = []
    for text in texts:
        try:
            is_file = Path(text).is_file()
        except OSError:  # a WKT longer than a file name can be
            is_file = False
        try:
            if is_file:
                text = Path(text).read_text(encoding="utf-8")
            if text.lstrip().startswith("{"):
                data = json.loads(text)
                geoms = ([shape(f["geometry"]) for f in data["features"]] if data.get("type") == "FeatureCollection"
                         else [shape(data.get("geometry", data))])
                out.append(as_polygon(unary_union(geoms)))
            else:
                out.append(as_polygon(text))
        except Exception as exc:  # JSON, GeoJSON or WKT (shapely's GEOSException) that cannot be read
            raise typer.BadParameter(f"not a polygon (WKT, GeoJSON or a GeoJSON file): {text[:60]}") from exc
    return unary_union(out)


@app.command()
def coregister(project: Path,
               reference: str = typer.Option(..., help="the chunk (label or key) whose DEM is the reference"),
               version: Optional[str] = typer.Option(None, help="a kept version of the reference DEM (id) instead "
                                                                "of the active one"),
               exclude_polygon: Optional[list[str]] = typer.Option(
                   None, help="an area that changed (the excavation), left out of the stable ground: WKT, GeoJSON or "
                              "a GeoJSON file; repeat for more"),
               stable_polygon: Optional[list[str]] = typer.Option(
                   None, help="only inside this area the ground may be stable (WKT, GeoJSON or a GeoJSON file)"),
               tilt: bool = typer.Option(True, "--tilt/--no-tilt", help="correct a tilt (plane) besides the shift"),
               max_iter: int = typer.Option(10, help="iterations of the horizontal shift"),
               method: str = typer.Option("auto", help="auto / nuth_kaab (built in) or xdem (pip install "
                                                       "pyarchinit-photogrammetry[coreg])"),
               name: Optional[str] = typer.Option(None, help="name of the coregistered DEM (a version)"),
               search: Optional[float] = typer.Option(None, help="metres of the initial search for large shifts "
                                                                 "(two GPS georeferencings); 0: none; default: "
                                                                 "from the DEMs"),
               chunk: Optional[str] = None):
    """Coregister the DEM of --chunk (default the active one) onto the DEM of --reference over the
    ground that did not change: horizontal shift (Nuth & Kääb), vertical bias and tilt. The result
    becomes the active DEM, the DEM as it was stays among the versions."""
    from .archaeo import coreg as cg

    doc, ch = _open(project, chunk)
    ref = next((c for c in doc.chunks if c.label == reference or str(c.key) == reference), None)
    if ref is None:
        raise typer.BadParameter(f"--reference: no chunk {reference!r}")
    if ref is ch and version is None:
        raise typer.BadParameter("the reference is the chunk itself: give another chunk, or --version")
    out = ch.coregister_dem((ref, version), stable=_polygons(stable_polygon), exclude=_polygons(exclude_polygon),
                            tilt=tilt, max_iter=max_iter, method=method, name=name, search=search,
                            progress=print_progress)
    doc.save()
    typer.echo(f"{ch.label}: DEM coregistered on {out['reference']} -> active DEM '{out['name']}' "
               f"(the DEM as it was kept as version {out['version']})")
    typer.echo(f"    {cg.summary(out)}")


@app.command("us-proposal")
def us_proposal(project: Path, first_us: Optional[int] = typer.Option(None, help="first number (default: after the "
                                                                                 "last US of the site and project)"),
                area: str = typer.Option("1", help="area of the new units"),
                step: float = typer.Option(0.02, help="least elevation step (m) to say which unit covers"),
                cuts: bool = typer.Option(True, help="propose the cut of each fill"),
                apply: bool = typer.Option(False, help="give the units their numbers in the project (and add the cuts)"),
                write: bool = typer.Option(False, help="also write US forms, relations and polygons into PyArchInit"),
                sito: Optional[str] = typer.Option(None, help="site (default: the site of the project)"),
                url: Optional[str] = None, chunk: Optional[str] = None):
    """US numbers, kinds and measurements for the units drawn without a number (SAM), and the
    stratigraphic relations their contacts suggest, for the US forms and the Extended Matrix."""
    from .archaeo import us_proposal as up
    from .archaeo.timeline import units_of

    doc, ch = _open(project, chunk)
    sito = sito or str(doc.meta.get("sito") or "") or None
    records = ((ch.meta or {}).get("drawings") or {}).get("units", [])
    units = units_of(ch)
    todo = [i for i, u in enumerate(units) if not u.us and (u.attributes or {}).get("gid") is None]
    if not todo:
        raise typer.BadParameter("no unit without a US number in the chunk")
    pa = None
    if write:
        if not sito:
            raise typer.BadParameter("--write needs the site: --sito, or the site of the project")
        from .pyarchinit import PyArchInit

        pa = PyArchInit.connect(url)
    if first_us is None:
        used = [u.us for c in doc.chunks for u in units_of(c)]
        first_us = max([int(n) for n in used if n and str(n).isdigit()], default=0) + 1
        if pa is not None:
            first_us = max(first_us, pa.next_us(sito))
    prop = up.propose_for_chunk(ch, [units[i] for i in todo], first_us, area=area, sito=sito, step=step, cuts=cuts,
                                lang="en", progress=print_progress)
    names = {it["key"]: up._name(it["unit"]) for it in prop["units"]}
    for it in prop["units"]:
        m, f = it["measures"], it["fields"]
        z = f" z {m['z_min']:.3f}-{m['z_max']:.3f}" if m.get("z_min") is not None else ""
        colour = f" {m['colour']['chip']}" if m.get("colour") else ""
        typer.echo(f"{'' if it['use'] else '(not used) '}{names[it['key']]}: {f['d_stratigrafica']}, "
                   f"{m['area_m2']:.2f} m2{z}{colour}")
    for r in prop["relations"]:
        step_text = f", step {r['step_m'] * 100:+.1f} cm" if r.get("step_m") is not None else ""
        typer.echo(f"{names[r['upper']]} {r['relation']} {names[r['lower']]} ({r['why']}{step_text})")
    if apply or write:
        for it in prop["units"]:
            if not it["use"]:
                continue
            u = it["unit"]
            if it["kind"] == "unit":
                rec = records[todo[it["source"]]]
                rec.update(us=u.us, tipo=u.tipo, area=u.area, sito=u.sito)
            else:
                records.append(u.to_dict())
                it["record"] = records[-1]
        doc.save()
        typer.echo(f"project: {sum(1 for it in prop['units'] if it['use'])} units numbered")
    if pa is not None:
        out = up.write_proposal(pa, prop, sito)
        chosen = [it for it in prop["units"] if it["use"]]
        polys = [it["unit"] for it in chosen]
        gids = pa.publish_units(polys, sito, crs=ch.crs)
        for it, g in zip(chosen, gids):
            rec = it.get("record") if it["kind"] == "cut" else records[todo[it["source"]]]
            rec.setdefault("attributes", {})["gid"] = g
        doc.save()
        typer.echo(f"PyArchInit: {len(out['created'])} US forms, {out['relations']} relations, {len(gids)} polygons"
                   + (f"; already there: {', '.join(out['existing'])}" if out["existing"] else ""))


@app.command()
def stones(project: Path, out: Path = typer.Option(..., help="drawing: .dxf, .svg or .geojson"),
           elevation: Optional[str] = typer.Option(None, help="name of an elevation (default: the orthomosaic)"),
           min_size: float = typer.Option(0.05, help="smallest stone (metres)"),
           max_size: float = typer.Option(0.8, help="largest stone (metres)"),
           bounds: Optional[str] = typer.Option(None, help="only inside 'xmin,ymin,xmax,ymax' of the raster"),
           thoroughness: str = typer.Option("alta", help="normale, alta, massima"),
           scale: float = typer.Option(20.0, help="SVG: scale 1:N"), model: str = typer.Option("small"),
           chunk: Optional[str] = None):
    """Stone-by-stone drawing with SAM 2.1 on the orthomosaic or on the orthophoto of an
    elevation: the outlines of the stones as DXF (layer PIETRE), SVG at scale or GeoJSON."""
    from .ai.sam_env import make_segmenter
    from .archaeo.stones import export_stones, find_stones
    from .engine.elevation import elevation_paths

    doc, ch = _open(project, chunk)
    if elevation:
        entry = next((e for e in ch.meta.get("elevations", []) if elevation in (e.get("name"), e.get("id"))), None)
        if entry is None:
            raise typer.BadParameter(f"no elevation {elevation!r}")
        raster = elevation_paths(ch, entry)[0]
    else:
        raster = ch.orthomosaic_path
        if raster is None:
            raise typer.BadParameter("build the orthomosaic first")
    box = tuple(float(v) for v in bounds.split(",")) if bounds else None
    found = find_stones(raster, make_segmenter(model), bounds=box, min_size=min_size, max_size=max_size,
                        thoroughness=thoroughness, progress=print_progress)
    export_stones(found, out, scale=scale, title=Path(raster).stem)
    typer.echo(f"{len(found)} stones: {out}")


@app.command()
def munsell(project: Path,
            chart: Optional[str] = typer.Option(None, help="calibrate first with the ColorChecker inside "
                                                           "'xmin,ymin,xmax,ymax' of the orthomosaic"),
            area: Optional[str] = typer.Option(None, help="the colour inside 'xmin,ymin,xmax,ymax'"),
            units: bool = typer.Option(False, help="the colour of every unit drawn on the chunk"),
            chunk: Optional[str] = None):
    """Munsell colour from the orthomosaic (median of an area, shadows and glare left out),
    calibrated with a colour chart photographed in the trench."""
    from shapely.geometry import box

    from .ai.sam import read_orthomosaic
    from .archaeo import munsell as mu
    from .archaeo.timeline import units_of

    doc, ch = _open(project, chunk)
    if ch.orthomosaic_path is None:
        raise typer.BadParameter("build the orthomosaic first")
    if chart:
        b = tuple(float(v) for v in chart.split(","))
        img, _valid, _tr = read_orthomosaic(ch.orthomosaic_path, b, max_side=2400)
        cal = mu.calibrate_chart(img)
        ch.meta["colour_chart"] = {"ccm": cal["ccm"], "loss": cal["loss"], "bounds": list(b)}
        doc.save()
        typer.echo(f"colour chart: mean CIEDE2000 of the patches after correction {cal['loss']:.2f}")
    ccm = (ch.meta.get("colour_chart") or {}).get("ccm")
    targets = []
    if area:
        targets.append((area, box(*(float(v) for v in area.split(",")))))
    if units:
        targets += [(f"{u.tipo or 'US'} {u.us or '?'}", u.geometry) for u in units_of(ch)]
    for name, geom in targets:
        c = mu.area_colour(ch.orthomosaic_path, geom, ccm)
        typer.echo(f"{name}: {c['chip']} ({c['notation']}){'' if ccm else ', not calibrated'}")


@app.command()
def qfield(action: str = typer.Argument(..., help="export or import"), project: Path = typer.Argument(...),
           folder: Path = typer.Argument(..., help="the folder of the package"),
           resolution: float = typer.Option(0.02, help="export: pixel of the orthomosaic on the tablet (metres)"),
           no_project: bool = typer.Option(False, help="export: without the QGIS project (QGIS not needed)"),
           chunk: Optional[str] = None):
    """QField package of a chunk (orthomosaic, DEM, units and spot heights, QGIS project) and the
    edits made on the tablet brought back."""
    from .io import qfield as qf

    doc, ch = _open(project, chunk)
    if action == "export":
        out = qf.export_package(ch, folder, resolution=resolution, project=not no_project, progress=print_progress)
        doc.save()
        typer.echo(f"{out['folder']}: {out['units']} units, {out['quote']} spot heights"
                   + (f", project {out['project'].name}" if out["project"] else ""))
    elif action == "import":
        n = qf.import_edits(ch, folder)
        doc.save()
        for kind, c in n.items():
            typer.echo(f"{kind}: {c['added']} added, {c['changed']} changed, {c['deleted']} deleted")
    else:
        raise typer.BadParameter("export or import")


@app.command()
def paradata(project: Path, product: str = typer.Option("orthomosaic", help="point_cloud, model, dem, orthomosaic..."),
             out: Optional[Path] = typer.Option(None, help="write the JSON-LD here (default: print it)"),
             operator: Optional[str] = typer.Option(None, help="who made it"), chunk: Optional[str] = None):
    """How a product was made, as W3C PROV in JSON-LD: the steps and their settings back to the
    photos and the georeference, the software and the operator."""
    import json as _json

    doc, ch = _open(project, chunk)
    if ch.product_path(product) is None:
        raise typer.BadParameter(f"the chunk has no {product}")
    data = ch.provenance(product, operator=operator)
    text = _json.dumps(data, ensure_ascii=False, indent=1)
    if out is None:
        typer.echo(text)
    else:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"written {out}")


@app.command()
def precision(project: Path, marker_px: float = typer.Option(0.5, help="how well markers are measured (pixels)"),
              chunk: Optional[str] = None):
    """Precision of cameras, markers and tie points from the covariance of the bundle adjustment,
    and the precision map of the DEM (sigma Z) when there is one."""
    doc, ch = _open(project, chunk)
    rep = ch.estimate_precision(marker_px=marker_px, progress=print_progress)
    doc.save()

    def mm(v):
        return " / ".join(f"{x * 1000:.2f}" for x in v) if v else "-"

    typer.echo(f"datum: {rep['datum']} (held: {rep['held']}); measurement in the photos {rep['sigma0_px']:.3f} px")
    tp, cams = rep["tie_points"], rep["cameras"]
    typer.echo(f"tie points ({tp['count']:,}): median X/Y/Z {mm(tp['median'])} mm, 90th percentile {mm(tp['p90'])} mm")
    typer.echo(f"cameras ({cams['count']}): median X/Y/Z {mm(cams['median'])} mm, largest {mm(cams['max'])} mm")
    for key, sigma in rep["markers"].items():
        typer.echo(f"{ch.marker_by_key(int(key)).label}: X/Y/Z {mm(sigma)} mm")
    if ch.product_path("dem_precision") is not None:
        typer.echo(f"precision of the DEM: {ch.product_path('dem_precision')}")


@app.command("refine-markers")
def refine_markers(project: Path, marker: Optional[list[str]] = typer.Option(None, help="marker label (repeat it); "
                                                                                      "default: all"),
                   min_score: float = typer.Option(0.8, help="correlation needed to accept a flag (0-1)"),
                   chunk: Optional[str] = None):
    """Look for the estimated marker projections (blue flags) in the photos by image correlation:
    those found become measurements."""
    import numpy as np

    from .markers.gcp import resolve_marker
    from .markers.refine import refine_markers as _refine

    doc, ch = _open(project, chunk)
    markers = [resolve_marker(ch, m) for m in marker] if marker else None
    report = _refine(ch, markers, min_score=min_score, progress=print_progress)
    doc.save()
    for key, r in report.items():
        m = ch.marker_by_key(key)
        moved = [np.hypot(dx, dy) for dx, dy, _s in r["refined"].values()]
        typer.echo(f"{m.label}: {len(r['refined'])} found" + (f" (moved {np.median(moved):.2f} px)" if moved else "")
                   + (f", {len(r['failed'])} left to confirm" if r["failed"] else ""))


@app.command()
def align(project: Path, quality: str = typer.Option("high", help="the accuracy: ultra (photos at full size), high "
                                                                  "(1/2), medium (1/4), low (1/8), lowest (1/16)"),
          pairs: str = typer.Option("auto", help="the older word: auto, exhaustive (every pair), generic (the pairs "
                                                 "that overlap, found on small images first), reference (nearest by "
                                                 "GPS), sequential (video); --generic/--no-generic and --reference "
                                                 "replace it"),
          generic: Optional[bool] = typer.Option(None, "--generic/--no-generic",
                                                 help="generic preselection (default: from 40 photos up)"),
          reference: str = typer.Option("none", help="reference preselection: none, source (the GPS of the photos), "
                                                     "estimated (a previous alignment) or sequential (capture order)"),
          neighbours: int = typer.Option(20, help="pairs of every photo with its nearest, or those around it"),
          keypoints: int = 40000, tiepoints: int = 8000,
          masks: str = typer.Option("keypoints", help="apply the masks of the photos to: none, keypoints, tiepoints"),
          stationary: bool = typer.Option(False, help="exclude stationary tie points (turntable, fixed camera)"),
          guided: bool = typer.Option(False, help="guided image matching"),
          adaptive: bool = typer.Option(False, help="adaptive camera model fitting"),
          gps: Optional[bool] = typer.Option(None, "--gps/--no-gps",
                                             help="the GPS positions of the photos hold the block during the "
                                                  "incremental alignment (default: when they exist)"),
          keep: bool = typer.Option(False, help="keep the current alignment and add the photos not yet in it"),
          mapper: str = typer.Option("auto", help="auto, incremental (one photo after the other), global (all at "
                                                  "once, much faster with many photos) or hierarchical (clusters of "
                                                  "photos on the cores, then merged)"),
          ba_ratio: Optional[float] = typer.Option(None, help="the incremental mapper adjusts the whole block when it "
                                                               "grows by this ratio (default 1.1; 1.3 adjusts a third "
                                                               "as often)"),
          ba_refinements: Optional[int] = typer.Option(None, help="refinements of every global adjustment (default 5)"),
          gpu: Optional[str] = typer.Option(None, help="the GPU for the matching, shared with the CPU: auto (the "
                                                       "best one), off, an adapter index or part of its name "
                                                       "(default: PPG_GPU, else auto; ppg doctor lists them)"),
          matcher: str = typer.Option("sift", help="the points: sift (COLMAP), or learned key points matched by "
                                                   "LightGlue, disk or aliked (thermal frames, surfaces with little "
                                                   "texture; need PyTorch and kornia, the [learned] extra)"),
          chunk: Optional[str] = None):
    """Match photos and align cameras, with the choices of Metashape's Align Photos."""
    doc, ch = _open(project, chunk)
    ch.match_photos(quality=quality, pair_selection=pairs, keypoint_limit=keypoints, tiepoint_limit=tiepoints,
                    generic=generic, reference=None if reference == "none" else reference, neighbours=neighbours,
                    apply_masks=masks, guided_matching=guided, exclude_stationary=stationary, reset=not keep,
                    gpu=gpu, matcher=matcher, progress=print_progress)
    n = ch.align_cameras(progress=print_progress, mapper=mapper, reset=not keep, use_gps=gps, adaptive_fitting=adaptive,
                         ba_global_ratio=ba_ratio, ba_global_refinements=ba_refinements)
    doc.save()
    typer.echo(f"aligned {n}/{len(ch.cameras)} photos")


@app.command()
def optimize(project: Path, markers: bool = typer.Option(True, help="control points (3 or more) and control scale "
                                                                   "bars (2 or more, or with control points) take part "
                                                                   "in the adjustment; check points and bars are reported"),
             marker_weight: int = typer.Option(4, help="a marker measurement counts as this many tie point observations"),
             principal_point: bool = False,
             engine: str = typer.Option("colmap", help="colmap, or ppg: our own adjustment (control points by their "
                                                       "accuracy, bars as distances, GNSS, rolling shutter)"),
             gnss: str = typer.Option("off", help="with --engine ppg: off, plain (the GNSS positions of the photos) or "
                                                  "bias (with a bias per flight)"),
             rolling_shutter: bool = typer.Option(False, help="with --engine ppg: a velocity per photo while it is read"),
             chunk: Optional[str] = None):
    """Optimize the cameras (global bundle adjustment), on the control points and scale bars when there are any."""
    doc, ch = _open(project, chunk)
    err = ch.optimize_cameras(fit_principal_point=principal_point, use_markers=markers, marker_weight=marker_weight,
                              progress=print_progress, engine=engine, gnss=gnss, rolling_shutter=rolling_shutter)
    if any(m.reference.location for m in ch.markers) or ch.scalebars:
        ch.update_transform()
    doc.save()
    rep = ch.meta["sfm"]["optimize"]
    text = f"reprojection error {err:.3f} px"
    b, a = rep.get("before", {}), rep.get("after", {})
    if rep.get("control_points"):
        text += (f", {rep['control_points']} control points: RMSE {b['control']} -> {a['control']} m"
                 + (f", check {b['check']} -> {a['check']} m" if a.get("check") is not None else ""))
    if rep.get("scale_bars"):
        text += (f", {rep['scale_bars']} scale bars: RMSE {b['scalebar_control']} -> {a['scalebar_control']} m"
                 + (f", check bars {b['scalebar_check']} -> {a['scalebar_check']} m"
                    if a.get("scalebar_check") is not None else ""))
    for flight, bias in (rep.get("gnss_bias") or {}).items():
        text += f", GNSS bias of flight {flight}: " + " ".join(f"{v:+.3f}" for v in bias) + " m"
    if rep.get("rolling_shutter_m"):
        text += f", rolling shutter: median {rep['rolling_shutter_m']['median']:.3f} m per frame"
    typer.echo(text)


@app.command("clean-tie-points")
def clean_tie_points(project: Path,
                     max_reprojection_error: Optional[float] = typer.Option(
                         None, help="px: remove the points with a larger mean reprojection error"),
                     min_image_count: Optional[int] = typer.Option(
                         None, help="remove the points seen by fewer photos"),
                     max_reconstruction_uncertainty: Optional[float] = typer.Option(
                         None, help="remove the points with a more elongated error ellipsoid (Metashape: 10-15)"),
                     max_projection_accuracy: Optional[float] = typer.Option(
                         None, help="px: remove the points measured on larger keypoints (own alignments only)"),
                     optimize: bool = typer.Option(False, help="optimize the cameras afterwards"),
                     dry_run: bool = typer.Option(False, help="only show the metrics and what would be removed"),
                     chunk: Optional[str] = None):
    """Gradual selection of the tie points (as in Metashape): remove the worst ones."""
    import numpy as np

    from .engine.tiepoints import select_tie_points, tie_point_metrics

    criteria = dict(max_reprojection_error=max_reprojection_error, min_image_count=min_image_count,
                    max_reconstruction_uncertainty=max_reconstruction_uncertainty,
                    max_projection_accuracy=max_projection_accuracy)
    if all(v is None for v in criteria.values()):
        raise typer.BadParameter("give at least one criterion (see --help)")
    doc, ch = _open(project, chunk)
    metrics = tie_point_metrics(ch)
    typer.echo(f"{len(metrics['ids']):,} tie points in {metrics['images']} photos, mean reprojection error "
               f"{metrics['mean_reprojection_error']:.3f} px" + (" (model written from imported poses)"
                                                                  if metrics["synthetic"] else ""))
    for name in ("reprojection_error", "image_count", "reconstruction_uncertainty", "projection_accuracy"):
        v = metrics[name]
        if v is None:
            typer.echo(f"  {name}: not available")
            continue
        v = v[np.isfinite(v)]
        if len(v):
            q = np.percentile(v, [50, 95])
            typer.echo(f"  {name}: median {q[0]:.3g}, 95% {q[1]:.3g}, max {v.max():.3g}")
    sel = select_tie_points(ch, metrics=metrics, **criteria)
    for name, n in sel["by_criterion"].items():
        typer.echo(f"  selected by {name}: " + ("not available" if n is None else f"{n:,}"))
    typer.echo(f"selected {sel['selected']:,} of {sel['total']:,} ({sel['percent']:.1f}%)"
               + (f"; {sel['images_below_minimum']} photos would keep too few points" if sel["images_below_minimum"]
                  else ""))
    if dry_run:
        return
    out = ch.clean_tie_points(optimize=optimize, progress=print_progress, **criteria)
    doc.save()
    text = (f"removed {out['removed']:,}, {out['remaining']:,} left; reprojection error "
            f"{out['reprojection_error_before']:.3f} -> {out['reprojection_error_after']:.3f} px")
    if "optimize_error" in out:
        text += f"; after the optimization {out['optimize_error']:.3f} px"
    if out.get("georeference", {}).get("control_rmse") is not None:
        text += f", control RMSE {out['georeference']['control_rmse']:.4f} m"
    typer.echo(text)


@app.command()
def scalebar(project: Path,
             markers: Optional[str] = typer.Option(None, help="the two markers 'A,B' (labels or target codes)"),
             distance: Optional[float] = typer.Option(None, help="their distance in metres"),
             accuracy: float = typer.Option(0.001, help="accuracy of the distance (m): weighs the bars"),
             check: bool = typer.Option(False, help="a check bar: measures the error, does not scale"),
             import_csv: Optional[Path] = typer.Option(None, "--import", help="scale bars from a CSV: marker_a, "
                                                                               "marker_b, distance[, accuracy[, check]]"),
             remove: Optional[str] = typer.Option(None, help="take a bar away: its label, or 'all'"),
             list_bars: bool = typer.Option(False, "--list", help="list the scale bars with their errors"),
             update: bool = typer.Option(True, help="scale the chunk after a change"),
             chunk: Optional[str] = None):
    """Scale bars, as in Metashape: the known distance between two markers scales the chunk -
    in local coordinates without control points (objects, trenches) - and every bar gets its
    error; several bars are averaged, check bars only measure."""
    from .markers import gcp

    doc, ch = _open(project, chunk)
    changed = False
    if remove:
        if remove == "all":
            ch.scalebars = []
        else:
            ch.remove_scalebar(remove)
        changed = True
    if import_csv:
        out = ch.import_scalebars(import_csv, accuracy=accuracy)
        typer.echo(f"{len(out['bars'])} scale bars imported" + (f"; markers missing for {', '.join(out['missing'])}"
                                                                 if out["missing"] else ""))
        changed = changed or bool(out["bars"])
    if markers:
        names = [m.strip() for m in markers.split(",") if m.strip()]
        if len(names) != 2 or distance is None:
            raise typer.BadParameter("give --markers A,B and --distance")
        sb = ch.add_scalebar(names[0], names[1], distance, accuracy, checkpoint=check)
        typer.echo(f"scale bar {sb.label}: {sb.distance} m" + (" (check)" if sb.checkpoint else ""))
        changed = True
    if changed and update and any(c.aligned for c in ch.cameras):
        rep = ch.update_transform()
        typer.echo(f"scale: method {rep['method']}, CRS {rep['crs']}, bar RMSE {rep['scalebar_control_rmse']}, "
                   f"check bar RMSE {rep['scalebar_check_rmse']}")
    if list_bars or changed:
        for e in gcp.scalebar_errors(ch):
            err = "-" if e["error"] is None else f"{e['error'] * 1000:+.1f} mm"
            typer.echo(f"{e['label']}: {e['marker_a']}-{e['marker_b']} {e['distance']:.4f} m, {e['type']}, error {err}"
                       + ("" if e["enabled"] else " (not used)"))
    if changed:
        doc.save()


@app.command()
def reference(project: Path, csv_file: Path, crs: Optional[str] = None, columns: str = "nxyz",
              delimiter: Optional[str] = None, skip_rows: int = 0, chunk: Optional[str] = None):
    """Import marker coordinates (GCP) and georeference the chunk."""
    doc, ch = _open(project, chunk)
    n = ch.import_reference(csv_file, crs=crs, columns=columns, delimiter=delimiter, skip_rows=skip_rows)
    report = ch.update_transform()
    doc.save()
    typer.echo(f"{n} reference points, method {report['method']}, CRS {report['crs']}, "
               f"control RMSE {report['control_rmse']}, check RMSE {report['check_rmse']}")


SKIP_HELP = "leave out the photos that cannot be read (e.g. kept online only by Dropbox) instead of stopping"


@app.command()
def dense(project: Path, quality: str = "medium", filter_mode: str = "moderate",
          backend: str = typer.Option("auto", help="auto, cpu (two-view SGBM), patchmatch (multi-view, native core: "
                                                   "more precise, slower), colmap (CUDA), openmvs"),
          skip_unreadable: bool = typer.Option(False, help=SKIP_HELP),
          gpu: Optional[str] = typer.Option(None, help="the GPU for the PatchMatch depth maps: auto, off, an adapter "
                                                       "index or part of its name (default: PPG_GPU, else auto)"),
          chunk: Optional[str] = None):
    """Depth maps and dense point cloud (with the confidence of every point)."""
    doc, ch = _open(project, chunk)
    ch.build_depth_maps(quality=quality, filter_mode=filter_mode, backend=backend, progress=print_progress,
                        skip_unreadable=skip_unreadable, gpu=gpu)
    pc = ch.build_point_cloud(progress=print_progress, gpu=gpu)
    doc.save()
    typer.echo(f"{len(pc):,} points")


@app.command()
def mesh(project: Path,
         source: str = typer.Option("point_cloud", help="point_cloud, tie_points or depth_maps (TSDF fusion)"),
         surface: str = "arbitrary", faces: str = "medium",
         voxel_size: Optional[float] = typer.Option(None, help="depth_maps: voxel in the chunk units "
                                                               "(default: twice the depth-map pixel)"),
         classes: Optional[str] = typer.Option(None, help="dense cloud: only these " + CLASSES_HELP),
         texture: bool = True,
         texture_size: int = typer.Option(4096, help="side of the texture in pixels; above 8192 it is split into "
                                                     "pages of 8192 (16384: 4 pages, 32768: 16)"),
         colour: Optional[bool] = typer.Option(None, "--colour/--no-colour",
                                               help="colour correction of the photos (default: if calibrated)"),
         skip_unreadable: bool = typer.Option(False, help=SKIP_HELP), chunk: Optional[str] = None):
    """Mesh (Poisson, height field or fusion of the depth maps) and texture."""
    doc, ch = _open(project, chunk)
    m = ch.build_model(source=source, surface=surface, face_count=int(faces) if faces.isdigit() else faces,
                       voxel_size=voxel_size, classes=_classes(classes), progress=print_progress)
    if texture:
        ch.build_texture(texture_size=texture_size, progress=print_progress, skip_unreadable=skip_unreadable,
                         colour_correction=colour)
    doc.save()
    typer.echo(f"{len(m.faces):,} faces")


@app.command()
def dem(project: Path, resolution: Optional[float] = None, source: str = "point_cloud",
        classes: Optional[str] = typer.Option(None, help="only these " + CLASSES_HELP + "; 2 alone: terrain model"),
        chunk: Optional[str] = None):
    """Digital elevation model (GeoTIFF)."""
    doc, ch = _open(project, chunk)
    p = ch.build_dem(resolution=resolution, source=source, classes=_classes(classes), progress=print_progress)
    doc.save()
    typer.echo(str(p))


@app.command()
def ortho(project: Path, resolution: Optional[float] = None, surface: str = "dem",
          blending: str = typer.Option("mosaic", help="mosaic (seamless), disabled (one photo per cell) or average"),
          colour: Optional[bool] = typer.Option(None, "--colour/--no-colour",
                                                help="colour correction of the photos (default: if calibrated)"),
          ghosting: bool = typer.Option(False, help="leave out people and vehicles seen by a few photos"),
          fill_holes: bool = typer.Option(True, help="fill holes of at most 100 cells from their edges"),
          hide_classes: Optional[str] = typer.Option(None, help="transparent where the surface is of these "
                                                                + CLASSES_HELP),
          skip_unreadable: bool = typer.Option(False, help=SKIP_HELP),
          max_angle: float = typer.Option(70.0, help="cells seen only from further than this from the vertical "
                                                     "(degrees) stay transparent"),
          chunk: Optional[str] = None):
    """Orthomosaic (GeoTIFF RGBA), any size: made tile by tile."""
    doc, ch = _open(project, chunk)
    p = ch.build_orthomosaic(resolution=resolution, surface=surface, blending=blending,
                             hide_classes=_classes(hide_classes), progress=print_progress,
                             skip_unreadable=skip_unreadable, max_angle=max_angle, colour_correction=colour,
                             ghosting=ghosting, fill_holes=fill_holes)
    doc.save()
    typer.echo(str(p))


@app.command()
def process(project: Path, photos: list[Path], gcp: Optional[Path] = None, crs: Optional[str] = None,
            targets: Optional[str] = "aruco_4x4_50", quality: str = "high", dense_quality: str = "medium",
            dem_resolution: Optional[float] = None, ortho_resolution: Optional[float] = None,
            texture: bool = True):
    """Whole pipeline: photos -> alignment -> georeferencing -> cloud -> mesh -> DEM -> orthomosaic -> report."""
    doc = Document.open(project) if project.exists() else Document()
    ch = doc.chunk or doc.add_chunk("Chunk 1")
    doc.save(project)
    ch.add_photos(photos)
    if targets:
        ch.detect_markers(targets, progress=print_progress)
    ch.match_photos(quality=quality, progress=print_progress)
    ch.align_cameras(progress=print_progress)
    if gcp:
        ch.import_reference(gcp, crs=crs)
    if gcp or any(c.reference.location for c in ch.cameras) or ch.scalebars:
        rep = ch.update_transform()
        typer.echo(f"georeferencing: {rep['method']} control RMSE {rep['control_rmse']}")
    doc.save()
    ch.build_depth_maps(quality=dense_quality, progress=print_progress)
    ch.build_point_cloud(progress=print_progress)
    ch.build_model(progress=print_progress)
    if texture:
        ch.build_texture(progress=print_progress)
    ch.build_dem(resolution=dem_resolution, progress=print_progress)
    ch.build_orthomosaic(resolution=ortho_resolution, progress=print_progress)
    report = ch.generate_report(project.with_suffix(".report.html"))
    doc.save()
    typer.echo(f"done - report: {report}")


@app.command("align-chunks")
def align_chunks(project: Path,
                 chunks: Optional[list[str]] = typer.Argument(None, help="chunks to align (labels or keys); "
                                                                         "default: the other enabled chunks"),
                 reference: Optional[str] = typer.Option(None, "--reference", "-r",
                                                         help="reference chunk (label or key); default: the active one"),
                 method: str = typer.Option("markers", help="markers, points or cameras"),
                 fix_scale: bool = typer.Option(False, help="rigid: keep the scale of each chunk (chunks already scaled)"),
                 initial: str = typer.Option("auto", help="points: initial position (auto, markers, reference, "
                                                          "cameras, global)"),
                 as_json: bool = typer.Option(False, "--json", help="print the whole report as JSON")):
    """Align chunks to the frame and CRS of a reference chunk (markers, points or cameras)."""
    doc = Document.open(project)
    if doc.chunk is None:
        raise typer.BadParameter("the project has no chunks")
    try:
        report = doc.align_chunks(reference if reference is not None else doc.chunk, chunks or None, method=method,
                                  fix_scale=fix_scale, initial=initial, progress=print_progress)
    except (ValueError, KeyError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    doc.save()
    if as_json:
        typer.echo(json.dumps(report, indent=1))
        return
    for c in report["chunks"]:
        rmse = "-" if c.get("rmse") is None else f"{c['rmse']:.4g}"
        extra = f", initial position {c['initial']}" if c.get("initial") else ""
        typer.echo(f"{c['label']} -> {report['reference']['label']}: {c['method']}, {c['correspondences']} "
                   f"correspondences, RMSE {rmse}, scale {c['scale']:.6g}{extra}")
        if c.get("stale_products"):
            typer.echo(f"  build again: {', '.join(c['stale_products'])}")


@app.command("merge-chunks")
def merge_chunks(project: Path,
                 chunks: Optional[list[str]] = typer.Argument(None, help="chunks to merge (labels or keys), the first "
                                                                         "gives frame and CRS; default: all enabled"),
                 label: Optional[str] = typer.Option(None, help="label of the new chunk"),
                 markers: bool = typer.Option(True, help="merge the markers with the same label"),
                 tie_points: bool = typer.Option(True, help="merge the tie points"),
                 point_clouds: bool = typer.Option(True, help="merge the dense clouds"),
                 models: bool = typer.Option(False, help="merge the models (meshes)"),
                 activate: bool = typer.Option(False, help="make the merged chunk the active one")):
    """Merge chunks into a new one (photos, markers, tie points, dense clouds, models)."""
    doc = Document.open(project)
    items = chunks or [c for c in doc.chunks if c.enabled]
    try:
        merged = doc.merge_chunks(items, label=label, merge_markers=markers, merge_tie_points=tie_points,
                                  merge_point_clouds=point_clouds, merge_models=models, progress=print_progress)
    except (ValueError, KeyError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    if activate:
        doc.chunk = merged
    doc.save()
    typer.echo(f"[{merged.key}] {merged.label}: {len(merged.cameras)} photos, {len(merged.markers)} markers, "
               f"CRS {merged.crs}, products: {', '.join(sorted(merged.products)) or '-'}")
    for w in merged.meta["merge"]["warnings"]:
        typer.echo(f"  warning: {w}")


@app.command()
def export(project: Path, ortho: Optional[Path] = None, dem: Optional[Path] = None, model: Optional[Path] = None,
           cloud: Optional[Path] = None, cameras: Optional[Path] = None, markers: Optional[Path] = None,
           report: Optional[Path] = None,
           cog: Optional[str] = typer.Option(None, help="--ortho/--dem as Cloud Optimized GeoTIFF with this "
                                                       "compression: jpeg, webp, zstd, deflate, lzw"),
           tiles: Optional[Path] = typer.Option(None, help="folder for the model as Cesium 3D Tiles"),
           i3s: Optional[Path] = typer.Option(None, help="the model as an I3S scene layer package (.slpk) for "
                                                         "ArcGIS and CesiumJS"),
           i3s_local: bool = typer.Option(False, help="--i3s in the projected CRS of the chunk (ArcGIS local "
                                                      "scenes) instead of WGS84"),
           classes: Optional[str] = typer.Option(None, help="--cloud: only these " + CLASSES_HELP),
           bbox: Optional[str] = typer.Option(None, help="only inside 'xmin,ymin,xmax,ymax' (ortho, dem, model, "
                                                         "cloud)"),
           wkt: Optional[str] = typer.Option(None, help="only inside this WKT polygon"),
           chunk: Optional[str] = None):
    """Export products in world coordinates (optionally cut by a box or a polygon: see also `ppg crop`).
    A model ending in .glb is a binary glTF, a cloud ending in .copc.laz a Cloud Optimized Point Cloud."""
    _, ch = _open(project, chunk)
    region = _region(None, bbox, wkt, None) if (bbox or wkt) else None
    if tiles:
        typer.echo(str(ch.export_3dtiles(tiles, progress=print_progress)))
    if i3s:
        typer.echo(str(ch.export_i3s(i3s, mode="local" if i3s_local else "global", progress=print_progress)))
    for value, fn in ((ortho, lambda path: ch.export_orthomosaic(path, region=region, cog=cog)),
                      (dem, lambda path: ch.export_dem(path, region=region, cog=cog if cog not in ("jpeg", "webp")
                                                       else "zstd")),
                      (model, lambda path: ch.export_model(path, region=region)),
                      (cloud, lambda path: ch.export_point_cloud(path, classes=_classes(classes), region=region)),
                      (markers, ch.export_reference), (report, ch.generate_report)):
        if value:
            typer.echo(str(fn(value)))
    if cameras:
        typer.echo(str(ch.export_cameras(cameras, fmt="csv" if cameras.suffix == ".csv" else "colmap")))


def _language(lang: Optional[str]) -> Optional[str]:
    if lang and lang.strip().lower()[:2] not in ("it", "en"):
        raise typer.BadParameter(f"--lang: it or en, not {lang!r}")
    return lang.strip().lower()[:2] if lang else None


@app.command()
def report(project: Path,
           pdf: Optional[Path] = typer.Option(None, help="the PDF to write (default: <project>_report.pdf beside the "
                                                         "project when --html is not given)"),
           html: Optional[Path] = typer.Option(None, help="also, or only, the report as an HTML page"),
           lang: Optional[str] = typer.Option(None, help="it or en (default: the language of the interface or of the "
                                                         "system)"),
           chunk: Optional[str] = typer.Option(None, help="only this chunk (label or key); default: every enabled chunk"),
           title: Optional[str] = typer.Option(None, help="title on the first page"),
           description: str = typer.Option("", help="text on the first page")):
    """The processing report: survey, calibration, accuracy of the references, processing parameters,
    products with their figures, multispectral and thermal products, measuring points."""
    from .io.pdf_report import generate_pdf_report
    from .io.report import generate_report

    language = _language(lang)
    doc = Document.open(project)
    if not doc.chunks:
        raise typer.BadParameter("the project has no chunks")
    chunks = None
    if chunk is not None:
        found = next((c for c in doc.chunks if c.label == chunk or str(c.key) == chunk), None)
        if found is None:
            raise typer.BadParameter(f"no chunk {chunk!r} in the project")
        chunks = [found]
    if pdf is None and html is None:
        pdf = project.with_name(project.stem + "_report.pdf")
    if pdf is not None:
        typer.echo(str(generate_pdf_report(doc, pdf, language=language, chunks=chunks, title=title,
                                           description=description, progress=print_progress)))
    if html is not None:
        target = chunks[0] if chunks else doc
        typer.echo(str(generate_report(target, html, language=language)))


@app.command()
def tables(project: Path, out: Path = typer.Argument(..., help="the Excel workbook to write (.xlsx)"),
           chunk: Optional[str] = typer.Option(None, help="label or key of the chunk (default: the active one)"),
           lang: Optional[str] = typer.Option(None, help="language of the headers: it or en (default: of the "
                                                         "interface or of the system)")):
    """Every table of a chunk in one Excel workbook, a sheet each: cameras, sensors, markers, scale bars,
    products, point classes, versions, parameters, multispectral bands, indices and zones, thermal measure,
    measuring points, spot heights, units, stones, proxies, sections."""
    from .io.tables import export_tables

    language = _language(lang)
    _, ch = _open(project, chunk)
    typer.echo(str(export_tables(ch, out, language=language, progress=print_progress)))


def _numbers(text: Optional[str], count: int, what: str) -> Optional[tuple]:
    """``"16,2.0"`` -> ``(16.0, 2.0)`` (None when not given)."""
    if not text:
        return None
    try:
        values = tuple(float(v) for v in text.replace(";", ",").split(",") if v.strip())
    except ValueError as exc:
        raise typer.BadParameter(f"{what}: numbers separated by commas, got {text!r}") from exc
    if len(values) != count:
        raise typer.BadParameter(f"{what}: {count} numbers separated by commas, got {text!r}")
    return values


def _region(polygon: Optional[str], bbox: Optional[str], wkt: Optional[str], geojson: Optional[Path]):
    """The crop polygon from one of the options (chunk CRS coordinates)."""
    from .engine.crop import as_polygon

    given = [v for v in (polygon, bbox, wkt, geojson) if v]
    if len(given) != 1:
        raise typer.BadParameter("give exactly one of --polygon, --bbox, --wkt, --geojson")
    if polygon:
        try:
            ring = [tuple(float(c) for c in pt.split(",")) for pt in polygon.split()]
        except ValueError as exc:
            raise typer.BadParameter("--polygon: 'x1,y1 x2,y2 x3,y3 ...'") from exc
        return as_polygon(ring)
    if bbox:
        return as_polygon(_numbers(bbox, 4, "--bbox"))
    if wkt:
        return as_polygon(wkt)
    data = json.loads(Path(geojson).read_text())
    if data.get("type") == "FeatureCollection":
        from shapely.geometry import shape
        from shapely.ops import unary_union

        return as_polygon(unary_union([shape(f["geometry"]) for f in data["features"]]))
    return as_polygon(data)


@app.command()
def relink(project: Path, folder: Path, only_unreadable: bool = typer.Option(
               False, help="only the photos that cannot be read now"), chunk: Optional[str] = None):
    """Point the photos to the files with the same names in FOLDER (and its subfolders)."""
    doc, ch = _open(project, chunk)
    r = ch.relink_photos(folder, only_unreadable=only_unreadable)
    doc.save()
    typer.echo(f"{r['relinked']} photos linked, {len(r['not_found'])} not found, {r['unreadable']} still unreadable; "
               f"photo folder {r['image_root']}")


@app.command()
def confidence(project: Path, cloud: bool = typer.Option(True, help="the dense cloud"),
               model: bool = typer.Option(False, help="the vertices of the model"),
               tol: float = typer.Option(1.0, help="depth tolerance, percent of the distance"),
               chunk: Optional[str] = None):
    """Confidence (depth maps agreeing on each point), as Metashape: stored in the PLY files."""
    doc, ch = _open(project, chunk)
    for target, wanted in (("point_cloud", cloud), ("model", model)):
        if wanted:
            info = ch.compute_confidence(target, tol=tol / 100.0, progress=print_progress)
            hist = " ".join(f"{c}:{n}" for c, n in sorted(info["histogram"].items()))
            typer.echo(f"{target}: {info['points']:,} points, mean {info['mean']:.1f}, "
                       f"{info['low_percent']:.1f}% at 2 or less ({hist})")
    doc.save()


@app.command("clean-cloud")
def clean_cloud(project: Path,
                min_confidence: int = typer.Option(0, help="remove the points confirmed by fewer depth maps"),
                statistical: Optional[str] = typer.Option(None, help="isolated points: 'neighbours,std_ratio' "
                                                                       "(e.g. 16,2)"),
                groups: Optional[str] = typer.Option(None, help="detached groups: 'distance,min_points' "
                                                                "(distance 0 = automatic, e.g. 0,1000)"),
                sparse: Optional[str] = typer.Option(None, help="points without neighbours: 'radius,min_neighbours' "
                                                                "(radius 0 = automatic)"),
                remove_classes: Optional[str] = typer.Option(None, help="remove the " + CLASSES_HELP),
                color: Optional[str] = typer.Option(None, help="remove a colour: 'r,g,b,tolerance'"),
                dry_run: bool = typer.Option(False, help="only count what would be removed"),
                chunk: Optional[str] = None):
    """Remove noise from the dense cloud (Metashape's filters)."""
    from .engine.cleaning import point_filters, summarize

    filters = {}
    if min_confidence:
        filters["min_confidence"] = min_confidence
    st = _numbers(statistical, 2, "--statistical")
    if st:
        filters["statistical"] = (int(st[0]), st[1])
    gr = _numbers(groups, 2, "--groups")
    if gr:
        filters["groups"] = (gr[0], int(gr[1]))
    sp = _numbers(sparse, 2, "--sparse")
    if sp:
        filters["sparse"] = (sp[0], int(sp[1]))
    if remove_classes:
        filters["remove_classes"] = _classes(remove_classes)
    col = _numbers(color, 4, "--color")
    if col:
        filters["color"] = (col[:3], col[3])
    if not filters:
        raise typer.BadParameter("give at least one filter (see --help)")
    doc, ch = _open(project, chunk)
    if dry_run:
        pc = ch.point_cloud
        info = summarize(point_filters(ch, pc, progress=print_progress, **filters), len(pc))
    else:
        info = ch.clean_point_cloud(progress=print_progress, **filters)
        doc.save()
    typer.echo(f"{'would remove' if dry_run else 'removed'} {info['removed']:,} of {info['total']:,} points "
               + ", ".join(f"{k} {n:,}" for k, n in info["by_filter"].items()))


@app.command("clean-model")
def clean_model(project: Path,
                min_component: float = typer.Option(1.0, help="remove pieces below this percent of the largest "
                                                              "(0: keep them)"),
                min_confidence: int = typer.Option(0, help="remove faces with a vertex confirmed by fewer depth maps"),
                max_edge: float = typer.Option(0.0, help="remove faces with an edge this many times the median"),
                repair: bool = typer.Option(True, help="degenerate and duplicate faces"),
                fill_holes: float = typer.Option(0.0, help="close holes up to this radius (chunk units)"),
                smooth: int = typer.Option(0, help="Taubin smoothing iterations"),
                target_faces: int = typer.Option(0, help="decimate to this number of faces"),
                chunk: Optional[str] = None):
    """Clean the model (Metashape's gradual selection, close holes, smooth, decimate)."""
    doc, ch = _open(project, chunk)
    info = ch.clean_model(min_component=min_component / 100.0 or None, min_confidence=min_confidence or None,
                          max_edge=max_edge or None, repair=repair, fill_holes=fill_holes or None, smooth=smooth,
                          target_faces=target_faces or None, progress=print_progress)
    doc.save()
    typer.echo(f"{info['faces']:,} faces (before {info['faces_before']:,}); removed "
               + ", ".join(f"{k} {n:,}" for k, n in info["by_filter"].items()))


@app.command()
def crop(project: Path,
         polygon: Optional[str] = typer.Option(None, help="'x1,y1 x2,y2 x3,y3 ...' in the chunk CRS"),
         bbox: Optional[str] = typer.Option(None, help="'xmin,ymin,xmax,ymax'"),
         wkt: Optional[str] = typer.Option(None, help="a WKT polygon"),
         geojson: Optional[Path] = typer.Option(None, help="a GeoJSON file (its polygons, merged)"),
         out: Optional[Path] = typer.Option(None, help="folder for the cropped exports"),
         in_project: bool = typer.Option(False, help="crop the products of the project instead (the originals "
                                                     "are kept as versions)"),
         products: str = typer.Option("dem,orthomosaic,model,point_cloud", help="what to crop"),
         zmin: Optional[float] = None, zmax: Optional[float] = None,
         name: str = typer.Option("ritaglio", help="start of the file names"),
         model_format: str = typer.Option("ply", help="ply or obj"),
         cloud_format: str = typer.Option("las", help="las or ply"),
         chunk: Optional[str] = None):
    """Cut the DEM, orthomosaic, model and dense cloud with a polygon: exported, or in the project."""
    from .core import versions

    region = _region(polygon, bbox, wkt, geojson)
    wanted = [p.strip() for p in products.split(",") if p.strip()]
    unknown = set(wanted) - {"dem", "orthomosaic", "model", "point_cloud"}
    if unknown:
        raise typer.BadParameter(f"unknown products: {', '.join(sorted(unknown))}")
    if bool(out) == in_project:
        raise typer.BadParameter("give --out FOLDER to export, or --in-project")
    doc, ch = _open(project, chunk)
    wanted = [k for k in wanted if ch.product_path(k) is not None and ch.product_path(k).exists()]
    if in_project:
        for k in wanted:
            versions.keep_active(ch, k, move=False)
            if k == "dem":
                ch.crop_dem(region)
            elif k == "orthomosaic":
                ch.crop_orthomosaic(region)
            elif k == "model":
                ch.crop_model(region, zmin=zmin, zmax=zmax)
            else:
                ch.crop_point_cloud(region=region, zmin=zmin, zmax=zmax)
            versions.set_active_name(ch, k, "Ritagliato")
            typer.echo(f"{k}: cropped in the project")
        doc.save()
        return
    out.mkdir(parents=True, exist_ok=True)
    for k in wanted:
        if k == "dem":
            path = ch.export_dem(out / f"{name}_dem.tif", region=region)
        elif k == "orthomosaic":
            path = ch.export_orthomosaic(out / f"{name}_ortofoto.tif", region=region)
        elif k == "model":
            path = ch.export_model(out / f"{name}_modello.{model_format}", region=region, zmin=zmin, zmax=zmax)
        else:
            path = ch.export_point_cloud(out / f"{name}_nuvola.{cloud_format}", region=region, zmin=zmin, zmax=zmax)
        typer.echo(str(path))


@app.command()
def patch(project: Path,
          photos: Optional[str] = typer.Option(None, help="labels of the photos for the area, comma separated"),
          polygon: Optional[str] = typer.Option(None, help="'x1,y1 x2,y2 x3,y3 ...' in the chunk CRS"),
          bbox: Optional[str] = typer.Option(None, help="'xmin,ymin,xmax,ymax'"),
          wkt: Optional[str] = typer.Option(None, help="a WKT polygon"),
          geojson: Optional[Path] = typer.Option(None, help="a GeoJSON file (its polygons, merged)"),
          exclude: bool = typer.Option(False, help="every photo but these"),
          ground: bool = typer.Option(False, help="project on the ground around the area instead of the DEM: trees, "
                                                  "vehicles and people as the photo shows them; fills DEM holes"),
          texture: bool = typer.Option(False, help="also colour the texture of the model in the area"),
          suggest: bool = typer.Option(False, help="list the photos that frame the area, best first"),
          remove: Optional[str] = typer.Option(None, help="take a patch away: its id, or 'all'"),
          list_patches: bool = typer.Option(False, "--list", help="list the patches"),
          chunk: Optional[str] = None):
    """Patches of the orthomosaic: the colours of an area from chosen photos (as Metashape's
    "assign images"). The orthomosaic is updated only where patches changed."""
    from .engine import patches as P

    doc, ch = _open(project, chunk)
    if list_patches:
        for p in P.patches(ch):
            how = "only" if p["mode"] == "assign" else "all but"
            typer.echo(f"{p['id']}: {how} {', '.join(p['photos'])} on the {p.get('surface', 'dem')}, "
                       f"{len(p['polygon'])} vertices")
        return
    if remove:
        if remove == "all":
            P.clear_patches(ch)
        else:
            P.remove_patch(ch, remove)
        typer.echo(str(ch.update_orthomosaic(progress=print_progress)))
        doc.save()
        return
    region = _region(polygon, bbox, wkt, geojson)
    if region.geom_type != "Polygon":
        region = max(region.geoms, key=lambda g: g.area)
    ring = [list(xy[:2]) for xy in region.exterior.coords[:-1]]
    if suggest:
        for d in P.photos_for_area(ch, ring):
            typer.echo(f"{d['label']}: frames {100 * d['coverage']:.0f}% of the area, {d['angle']:.0f} deg from the "
                       f"vertical, {100 * d['gsd']:.2f} cm/px, the orthomosaic takes {100 * d['used']:.0f}% from it")
        return
    if not photos:
        raise typer.BadParameter("give --photos (--suggest lists the photos of the area)")
    labels = [x.strip() for x in photos.split(",") if x.strip()]
    mode = "exclude" if exclude else "assign"
    p = P.add_patch(ch, ring, labels, mode, "ground" if ground else "dem")
    typer.echo(f"patch {p['id']}")
    typer.echo(str(ch.update_orthomosaic(progress=print_progress)))
    if texture:
        r = ch.patch_texture(labels, polygon=ring, mode=mode, progress=print_progress)
        typer.echo(f"texture: {r['faces']} faces")
    doc.save()


@app.command()
def info(project: Path):
    """Summary of the project."""
    doc = Document.open(project)
    for ch in doc.chunks:
        typer.echo(f"[{ch.key}] {ch.label}: {len(ch.cameras)} photos, {len(ch.aligned_cameras)} aligned, "
                   f"{len(ch.markers)} markers, CRS {ch.crs}, products: {', '.join(ch.products) or '-'}")


@app.command()
def section(project: Path, x1: float, y1: float, x2: float, y2: float, out: Path = Path("sezione.dxf"),
            source: str = "model", label: str = "Sezione",
            scale: float = typer.Option(0.0, help="SVG drawing scale 1:N (0: chosen from the size of the section)"),
            classes: Optional[str] = typer.Option(None, help="only the parts on these " + CLASSES_HELP),
            chunk: Optional[str] = None):
    """Vertical section along a trace (world coordinates) to DXF/SVG."""
    from .archaeo import sections as sec_mod

    _, ch = _open(project, chunk)
    s = sec_mod.section(ch, (x1, y1), (x2, y2), source=source, label=label, classes=_classes(classes))
    p = s.to_svg(out, scale=scale or None) if out.suffix.lower() == ".svg" else s.to_dxf(out)
    typer.echo(f"{p} ({len(s.polylines)} polylines, length {s.length:.3f} m)")


@app.command()
def contours(project: Path, interval: float = typer.Option(0.0, help="metres (0: a round value for the relief)"),
             out: Path = Path("curve.dxf"), chunk: Optional[str] = None):
    """Contour lines from the DEM (DXF, GeoPackage or GeoJSON)."""
    from .archaeo import terrain

    _, ch = _open(project, chunk)
    lines = terrain.contours(ch.dem_path, interval or terrain.suggest_interval(ch.dem_path))
    typer.echo(f"{terrain.export_contours(lines, out, crs=ch.crs)} ({len(lines)} lines)")


def _pottery_measure(mesh_path: Path, axis: Optional[str], units: str, rim: bool):
    """Profile and measurements of a mesh (``axis``: 'x,y,z,dx,dy,dz', 'up' or 'down' for the Z of
    the mesh with the rim up or down, None: estimated from the normals)."""
    import trimesh

    from .archaeo import pottery as pot

    m = trimesh.load(str(mesh_path), force="mesh")
    f = {"m": 1.0, "cm": 0.01, "mm": 0.001}[units]
    if f != 1.0:
        m.apply_scale(f)
    ax = None
    if axis in ("up", "down"):
        ax = pot.Axis(m.vertices.mean(axis=0), (0, 0, 1 if axis == "up" else -1))
    elif axis:
        v = [float(x) for x in axis.split(",")]
        ax = pot.Axis(v[:3], v[3:])
    return pot.measure(m, ax, has_rim=rim)


AXIS_HELP = "'x,y,z,dx,dy,dz' to impose the rotation axis, 'up'/'down': the Z of the mesh, rim up/down"


@app.command()
def pottery(mesh_path: Path, out: Path = Path("profilo.svg"), scale: float = 1.0, title: Optional[str] = None,
            axis: Optional[str] = typer.Option(None, help=AXIS_HELP),
            units: str = typer.Option("m", help="units of the mesh: m, cm or mm"),
            rim: bool = typer.Option(True, help="the piece keeps its rim (rim diameter, % preserved, signature)")):
    """Profile drawing (SVG, DXF or PNG) and measurements of a vessel/sherd model: rim diameter and
    percentage preserved, maximum and base diameter, height, wall thickness."""
    from .archaeo import pottery as pot

    prof, meas = _pottery_measure(mesh_path, axis, units, rim)
    pot.draw(prof, out, scale=scale, title=title or mesh_path.stem, measures=meas)
    meas.pop("signature", None)
    typer.echo(json.dumps(meas, indent=1))


@app.command()
def classify(project: Path, chunk: Optional[str] = None):
    """Classify the dense cloud (ground, vegetation, structures, noise)."""
    from .ai.classify import class_statistics, classify_chunk

    doc, ch = _open(project, chunk)
    pc = classify_chunk(ch)
    doc.save()
    typer.echo(json.dumps(class_statistics(pc)))


@app.command("classify-units")
def classify_units(project: Path,
                   pyarchinit_site: Optional[str] = typer.Option(None, help="the US polygons of this PyArchInit site "
                                                                            "(default: the units drawn in the project)"),
                   csv: Optional[Path] = typer.Option(None, help="write the statistics per US to this CSV"),
                   keep_manual: bool = typer.Option(True, help="keep the classes corrected by hand"),
                   chunk: Optional[str] = None):
    """One class per stratigraphic unit (points inside its polygon) and statistics per US."""
    from .ai.classify import classify_by_units, units_table_csv
    from .archaeo.stratigraphy import UnitPolygon

    doc, ch = _open(project, chunk)
    if pyarchinit_site:
        from .pyarchinit import PyArchInit

        units = PyArchInit.connect().us_polygons(pyarchinit_site, crs=ch.crs)
    else:
        units = [UnitPolygon.from_dict(d) for d in (ch.meta.get("drawings") or {}).get("units", [])]
    if not units:
        raise typer.BadParameter("no units: draw them in the project or give --pyarchinit-site")
    rows = classify_by_units(ch, units, keep_manual=keep_manual)
    doc.save()
    for r in rows:
        z = "" if r["z_min"] is None else f", z {r['z_min']:.3f} - {r['z_max']:.3f}"
        typer.echo(f"{r['code']:>3} {r['name']}: {r['points']:,} points, {r['area_m2']:.2f} m2{z}")
    if csv:
        typer.echo(str(units_table_csv(rows, csv)))


@app.command("classify-train")
def classify_train(project: Path, model: Path = typer.Argument(..., help="classifier file to write (.ppgclf)"),
                   labels: str = typer.Option("manual", help="manual: points labelled by hand; "
                                                             "all: every classified point"),
                   method: str = typer.Option("random_forest", help="random_forest or gradient_boosting"),
                   scales: Optional[str] = typer.Option(None, help="neighbourhood radii in metres, e.g. 0.05,0.15,0.4 "
                                                                    "(default: 5, 14 and 40 times the point spacing)"),
                   trees: int = typer.Option(100, help="trees of the forest (boosting: iterations)"),
                   max_per_class: int = typer.Option(20000, help="training samples per class at most"),
                   jobs: int = typer.Option(4, help="parallel threads"), seed: int = 0, chunk: Optional[str] = None):
    """Train a point classifier on the points labelled by hand and save it."""
    from .ai.learn import train_on_chunk

    doc, ch = _open(project, chunk)
    radii = [float(v) for v in scales.split(",")] if scales else None
    clf = train_on_chunk(ch, labels=labels, method=method, scales=radii, n_estimators=trees,
                         max_per_class=max_per_class, n_jobs=jobs, random_state=seed, progress=print_progress)
    path = clf.save(model)
    doc.save()
    typer.echo(clf.summary())
    typer.echo(f"saved {path}")


@app.command("classify-apply")
def classify_apply(project: Path, model: Path = typer.Argument(..., help="classifier file (.ppgclf)"),
                   min_confidence: float = typer.Option(0.0, help="below this probability a point stays "
                                                                  "unclassified (1)"),
                   overwrite_manual: bool = typer.Option(False, help="reclassify the points labelled by hand as well"),
                   jobs: int = typer.Option(4, help="parallel threads"), chunk: Optional[str] = None):
    """Classify the dense cloud with a saved classifier (points labelled by hand are kept)."""
    from .ai.classify import class_statistics
    from .ai.learn import ClassifierModel, classify_chunk_with_model

    doc, ch = _open(project, chunk)
    clf = ClassifierModel.load(model)
    pc, _ = classify_chunk_with_model(ch, clf, overwrite_manual=overwrite_manual, min_confidence=min_confidence,
                                      n_jobs=jobs, progress=print_progress)
    doc.save()
    typer.echo(json.dumps(class_statistics(pc)))


@app.command()
def ifc(project: Path, out: Path, sito: Optional[str] = None, pyarchinit_units: bool = False,
        chunk: Optional[str] = None):
    """Export the model to IFC4 (optionally one element per US from PyArchInit)."""
    from .bim.ifc import export_chunk_ifc

    _, ch = _open(project, chunk)
    units, records = None, None
    if pyarchinit_units:
        from .pyarchinit import PyArchInit

        pa = PyArchInit.connect()
        units, records = pa.us_polygons(sito, crs=ch.crs), pa.us_records(sito)
    typer.echo(str(export_chunk_ifc(ch, out, units=units, us_records=records, site=sito)))


@app.command()
def run(script: Path, project: Optional[Path] = None, args: list[str] = typer.Argument(None)):
    """Run a Python script; ``pyarchinit_photogrammetry.app.document`` is the opened project."""
    from .app import app as application

    if project:
        application.open_document(project)
    sys.argv = [str(script)] + list(args or [])
    runpy.run_path(str(script), run_name="__main__")


@app.command()
def demo(folder: Path = typer.Argument(Path("ppg_demo"), help="destination folder"),
         process: bool = typer.Option(False, help="also run the whole pipeline (about three minutes)"),
         pyarchinit: bool = typer.Option(True, help="create the demo PyArchInit database"),
         pottery: bool = typer.Option(True, help="also the turntable photos and project of the bowl")):
    """Write demo data: trench photos + GCPs, a ready project, pottery meshes and turntable photos, a PyArchInit database."""
    from .demo import create_demo

    d = create_demo(folder, process=process, with_pyarchinit=pyarchinit, with_pottery=pottery, progress=print_progress)
    typer.echo(f"Project:    {d.project}")
    typer.echo(f"Pottery:    {d.vessel}  {d.sherd}")
    if d.pottery_project:
        typer.echo(f"Turntable:  {d.pottery_project}" + (f"  profile: {d.pottery_profile}" if d.pottery_profile else ""))
    if d.pyarchinit_home:
        typer.echo(f"PyArchInit: export PYARCHINIT_HOME={d.pyarchinit_home}")
    typer.echo(f"Read {d.folder / 'LEGGIMI.txt'}; open it in the interface with: ppg-gui {d.project}")


@app.command()
def gui(project: Optional[Path] = typer.Argument(None)):
    """Open the desktop interface (needs the [gui] extra)."""
    from .gui import main as gui_main

    raise SystemExit(gui_main([str(project)] if project else []))


@app.command("import-metashape")
def import_metashape(psx: Path, project: Path, chunk: Optional[int] = typer.Option(None, help="only this chunk (index)")):
    """Project from a Metashape .psx: aligned cameras, calibration, markers, scale bars, tie points, model,
    exported DEM/ortho."""
    from .integrations.metashape import import_psx
    from .markers import gcp

    doc = import_psx(psx, project, chunk_index=chunk, progress=print_progress)
    for ch in doc.chunks:
        typer.echo(f"{ch.label}: {len(ch.aligned_cameras)}/{len(ch.cameras)} cameras, CRS {ch.crs}, "
                   f"products {sorted(ch.products)}" + (f", {len(ch.markers)} markers" if ch.markers else ""))
        for e in gcp.scalebar_errors(ch):
            err = "-" if e["error"] is None else f"{e['error'] * 1000:+.1f} mm"
            typer.echo(f"  scale bar {e['label']}: {e['distance']:.4f} m, {e['type']}, error {err}")


@app.command("import-webodm")
def import_webodm(source: Path, project: Path):
    """Project from a WebODM/ODM task folder or its all.zip (orthophoto, DEM, point cloud, mesh, camera poses and
    the GCPs as markers)."""
    from .integrations.webodm import import_task

    doc = import_task(source, project, progress=print_progress)
    ch = doc.chunk
    imp = ch.meta.get("import", {})
    typer.echo(f"{ch.label}: CRS {ch.crs}, products {sorted(ch.products)}"
               + (f", {imp['cameras']} cameras" if imp.get("cameras") else "")
               + (f", {imp['gcp']['markers']} GCPs" if imp.get("gcp") else ""))
    for w in imp.get("warnings", []):
        typer.echo(f"warning: {w}")


@app.command("import-photo3d")
def import_photo3d(session: Path, project: Optional[Path] = typer.Argument(None)):
    """Project from a Photo3D capture session folder (photos, targets, GCPs), ready to process."""
    from .integrations.photo3d import import_session

    doc = import_session(session, project)
    typer.echo(f"Project {doc.path}: {len(doc.chunk.cameras)} photos, {len(doc.chunk.markers)} markers")


@app.command("photo3d-job")
def photo3d_job(session: Optional[Path] = typer.Argument(None), preset: Optional[str] = typer.Argument(None)):
    """Engine entry point for the Photo3D server (PROGRESS lines, metashape/result.json)."""
    from .integrations.photo3d import main as p3d_main

    args = [str(session)] if session else []
    if preset:
        args.append(preset)
    raise SystemExit(p3d_main(args))


@app.command()
def doctor():
    """Check the environment (libraries, known pitfalls) and print what to fix."""
    from .doctor import main as doctor_main

    raise SystemExit(doctor_main())


@app.command()
def quality(project: Path, disable_below: Optional[float] = typer.Option(
        None, help="disable the photos under this quality (0.5 suggested)"), chunk: Optional[str] = None):
    """Image quality (sharpness, 0-1 relative to the sharpest photos) of every photo."""
    from .engine.quality import estimate_quality, low_quality

    doc, ch = _open(project, chunk)
    q = estimate_quality(ch, progress=print_progress)
    for cam in sorted(ch.cameras, key=lambda c: q.get(c.key, 1.0)):
        if cam.key in q:
            typer.echo(f"{q[cam.key]:.2f}  {cam.label}")
    if disable_below is not None:
        low = low_quality(ch, disable_below)
        for cam in low:
            cam.enabled = False
        typer.echo(f"{len(low)} photos disabled (quality < {disable_below})")
    doc.save()


@app.command("add-video")
def add_video(project: Path, video_path: Path, overlap: float = typer.Option(0.7, help="share of a frame in "
                                                                                       "common with the next"),
              every: Optional[float] = typer.Option(None, help="one frame every N seconds instead"),
              max_frames: Optional[int] = None, folder: Optional[Path] = None, chunk: Optional[str] = None):
    """Frames of a video added as photos (the DJI .SRT beside it gives their GPS positions)."""
    from .engine.video import add_video as _add

    doc, ch = _open(project, chunk)
    cams = _add(ch, video_path, folder, overlap=overlap, every=every, max_frames=max_frames, progress=print_progress)
    doc.save()
    typer.echo(f"{len(cams)} frames added")


@app.command("add-panoramas")
def add_panoramas(project: Path, paths: list[Path], fov: float = typer.Option(100.0, help="field of view of every "
                                                                                          "face (degrees)"),
                  size: Optional[int] = typer.Option(None, help="side of the faces in pixels (default: the "
                                                                    "resolution of the panorama)"),
                  faces: Optional[str] = typer.Option(None, help="faces to keep, e.g. front,right,back,left,up"),
                  chunk: Optional[str] = None):
    """360-degree (equirectangular) photos added as the six faces of a cube, aligned as a rig."""
    from .engine.panorama import add_panoramas as _add

    doc, ch = _open(project, chunk)
    cams = _add(ch, paths, size=size, fov=fov, faces=[f.strip() for f in faces.split(",")] if faces else None,
                progress=print_progress)
    doc.save()
    typer.echo(f"{len({c.meta['panorama'] for c in cams})} panoramas added ({len(cams)} faces)")


@app.command("lens-model")
def lens_model(project: Path, model: str = typer.Argument(..., help="OPENCV, OPENCV_FISHEYE, FULL_OPENCV, PINHOLE, "
                                                                    "..."),
               sensor: Optional[int] = typer.Option(None, help="sensor key (default: all)"),
               chunk: Optional[str] = None):
    """Camera model of the sensors (e.g. OPENCV_FISHEYE for a fisheye not recognised from EXIF)."""
    doc, ch = _open(project, chunk)
    for key in ([sensor] if sensor is not None else list(ch.sensors)):
        s = ch.set_sensor_model(key, model.upper())
        typer.echo(f"sensor {key} ({s.label}): {s.model}")
    doc.save()
    typer.echo("align the photos again")


@app.command("calibrate-colours")
def calibrate_colours(project: Path, vignetting: bool = True, samples: int = 20000, chunk: Optional[str] = None):
    """Exposure gain of every photo and vignetting of every lens (used by orthomosaic and texture)."""
    doc, ch = _open(project, chunk)
    info = ch.calibrate_colours(vignetting=vignetting, samples=samples, progress=print_progress)
    doc.save()
    gains = [sum(g) / 3 for g in info["gains"].values()]
    typer.echo(f"{info['observations']:,} observations; colour scatter {info['rms_before']:.3f} -> "
               f"{info['rms_after']:.3f}; gains {min(gains):.2f}-{max(gains):.2f}")
    for sensor, b in info["vignetting"].items():
        typer.echo(f"sensor {sensor}: falloff at the corners {sum(b):+.3f} (log)")


@app.command()
def update(download: bool = typer.Option(False, help="download the installer of this system"),
           folder: Path = typer.Option(None, help="folder for the download (default: Downloads)")):
    """Look for a newer release on GitHub and say how to install it."""
    from . import __version__, updates
    from .core.progress import print_progress

    try:
        release = updates.check(__version__)
    except updates.UpdateError as exc:
        typer.echo(f"Update check failed: {exc}", err=True)
        raise typer.Exit(1)
    if release is None:
        typer.echo(f"{__version__} is the latest version.")
        return
    typer.echo(f"Version {release.version} is available (this is {__version__}): {release.url}")
    kind = updates.install_kind()
    if kind == "pip":
        typer.echo(f"Update with: {updates.PIP_COMMAND}")
    elif kind == "source":
        typer.echo("Update with: git pull")
    asset = updates.asset_for_platform(release)
    if download:
        if asset is None:
            typer.echo("No installer for this system in the release.", err=True)
            raise typer.Exit(1)
        typer.echo(str(updates.download(asset, folder, progress=print_progress)))


# ------------------------------------------------------------------ markers
@markers_app.command("sheet")
def markers_sheet(out: Path, target_type: str = "aruco_4x4_50", codes: str = "0-11", size_mm: float = 40.0,
                  page: str = "A4"):
    """Printable sheet of coded targets (PDF or PNG)."""
    from .markers.targets import generate_sheet

    a, _, b = codes.partition("-")
    code_list = list(range(int(a), int(b or a) + 1))
    for p in generate_sheet(out, target_type, code_list, marker_size_mm=size_mm, page=page):
        typer.echo(str(p))


@markers_app.command("scalebar")
def markers_scalebar(out: Path, target_type: str = "aruco_4x4_50", distance_mm: float = 200.0, size_mm: float = 40.0,
                     page: str = "A4", count: int = 1):
    """Printable scale bars: two coded targets at a known distance (and the CSV to import with
    ppg scalebar --import once the targets are detected)."""
    from .markers.targets import generate_scale_bar

    files, csv = generate_scale_bar(out, target_type, distance_mm=distance_mm, marker_size_mm=size_mm, page=page,
                                    count=count)
    for p in files + [csv]:
        typer.echo(str(p))


@markers_app.command("turntable")
def markers_turntable(out: Path, target_type: str = "aruco_4x4_50", n: int = 12, diameter_mm: float = 180.0,
                      size_mm: float = 22.0, page: str = "A3"):
    """Turntable mat with a ring of targets; also writes the reference CSV."""
    from .markers.targets import generate_turntable_mat

    files, layout = generate_turntable_mat(out, target_type, n, diameter_mm, size_mm, page)
    csv = out.with_suffix(".csv")
    csv.write_text("".join(f"{code},{x:.5f},{y:.5f},{z:.5f}\n" for code, (x, y, z) in layout.items()))
    for p in files + [csv]:
        typer.echo(str(p))


# ---------------------------------------------------------------- pyarchinit
@pa_app.command("sites")
def pa_sites(url: Optional[str] = None):
    """List the sites of the PyArchInit database."""
    from .pyarchinit import PyArchInit

    for s in PyArchInit.connect(url).sites():
        typer.echo(f"{s['id_sito']}\t{s['sito']}\t{s.get('comune') or ''}")


@pa_app.command("publish")
def pa_publish(project: Path, sito: str, nome_doc: str, ortho: bool = True, dem: bool = False, model: bool = False,
               link_us: Optional[str] = typer.Option(None, help="area:us pairs, e.g. '1:101,1:102'"),
               url: Optional[str] = None, chunk: Optional[str] = None):
    """Save orthomosaic / DEM / model into PyArchInit (media, documentation, footprint, US links)."""
    from .pyarchinit import PyArchInit

    _, ch = _open(project, chunk)
    pa = PyArchInit.connect(url)
    links = [tuple(x.split(":")) for x in link_us.split(",")] if link_us else None
    if ortho:
        typer.echo(json.dumps(pa.publish_orthomosaic(ch, sito, nome_doc, link_us=links)))
    if dem:
        typer.echo(json.dumps(pa.publish_dem(ch, sito, f"DEM {nome_doc}", link_us=links)))
    if model:
        typer.echo(json.dumps(pa.publish_model(ch, sito, f"Modello {nome_doc}", link_us=links)))


@pa_app.command("pottery-search")
def pa_pottery_search(mesh_path: Path, sito: Optional[str] = typer.Option(None, help="only this site"),
                      limit: int = 10, axis: Optional[str] = typer.Option(None, help=AXIS_HELP),
                      units: str = typer.Option("m", help="units of the mesh: m, cm or mm"),
                      url: Optional[str] = None):
    """The pottery forms most like a vessel or a sherd (dimensions, and profile where saved)."""
    from .pyarchinit import PyArchInit

    _, meas = _pottery_measure(mesh_path, axis, units, True)

    def cm(key):
        return None if meas.get(key) is None else meas[key] * 100.0

    rows = PyArchInit.connect(url).search_pottery(
        rim_cm=cm("rim_diameter_m"), max_cm=cm("max_diameter_m"), base_cm=cm("base_diameter_m"),
        height_cm=cm("height_m") if meas.get("full_profile") else None, signature=meas.get("signature"), sito=sito,
        limit=limit)
    for r in rows:
        dims = " ".join(f"{k.replace('diametro_', '')}={r[k]:g}" for k in ("diametro_rim", "diametro_max",
                                                                           "diametro_bottom", "diametro_height")
                        if r.get(k) is not None)
        typer.echo(f"{r['similarity_pct']:5.1f}%  id_rep {r['id_rep']}  {r.get('sito')}  US {r.get('us')}  "
                   f"{r.get('specific_shape') or r.get('form') or ''}  {dims}  [{', '.join(r['compared'])}]")


@pa_app.command("pottery-save")
def pa_pottery_save(mesh_path: Path, sito: str, area: str = "", us: str = "",
                    id_rep: Optional[int] = typer.Option(None, help="update this pottery form (default: a new one)"),
                    form: Optional[str] = None, part: Optional[str] = typer.Option(None, help="specific_form"),
                    shape: Optional[str] = typer.Option(None, help="specific_shape"), ware: Optional[str] = None,
                    scale: float = 1.0, title: Optional[str] = None, overwrite: bool = False,
                    axis: Optional[str] = typer.Option(None, help=AXIS_HELP),
                    units: str = typer.Option("m", help="units of the mesh: m, cm or mm"),
                    rim: bool = typer.Option(True, help="the piece keeps its rim"), url: Optional[str] = None):
    """Measurements (cm) and drawing of a vessel or a sherd in a pottery form of PyArchInit."""
    from .pyarchinit import PyArchInit

    prof, meas = _pottery_measure(mesh_path, axis, units, rim)
    fields = {k: v for k, v in (("form", form), ("specific_form", part), ("specific_shape", shape), ("ware", ware))
              if v}
    out = PyArchInit.connect(url).save_pottery(sito, meas, id_rep=id_rep, area=area, us=us, profile=prof,
                                               title=title or mesh_path.stem, scale=scale, overwrite=overwrite,
                                               **fields)
    typer.echo(json.dumps(out, indent=1, default=str))


@plugins_app.command("new")
def plugins_new(name: str = typer.Argument(..., help="name of the plugin: letters, digits and dashes"),
                directory: str = typer.Option(".", "--dir", help="where the project folder ppg-NAME is written"),
                title: str = typer.Option("", help="shown in the interface (the name by default)"),
                description: str = typer.Option("", help="what the plugin does, in one line"),
                author: str = typer.Option("", help="the author"),
                email: str = typer.Option("", help="e-mail of the author"),
                parts: str = typer.Option("action,background,command,exporter",
                                          help="what the plugin starts with, comma-separated: action (in the "
                                               "menu Plugin), background (with the progress bar), command (ppg "
                                               "plugin NAME), exporter (File > Esporta)")):
    """Write the project of a new plugin to start from: a working plugin with the parts chosen,
    its tests, READMEs, licence and release workflow."""
    from .plugins import scaffold

    chosen = [p.strip() for p in parts.split(",") if p.strip()]
    try:
        target = scaffold.create(directory, name, title=title, description=description, author=author, email=email,
                                 parts=chosen)
    except (ValueError, FileExistsError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo(f"plugin project written in {target}\n" + scaffold.next_steps(target))


@plugins_app.command("list")
def plugins_list():
    """The plugins found (installed by the program or with pip), enabled or not."""
    from .plugins import Manager

    m = Manager()
    m.discover()
    if not m.plugins:
        typer.echo("No plugin. `ppg plugins available` lists those of the plugin repository.")
    for i in m.plugins:
        typer.echo(f"{i.name:28} {i.version:10} {'enabled' if i.enabled else 'disabled':9} {i.source}")


@plugins_app.command("available")
def plugins_available():
    """The plugins of the index of the plugin repository."""
    from .plugins import index

    try:
        entries = index.fetch()
    except index.PluginError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    for e in entries:
        problem = e.problem()
        typer.echo(f"{e.name:28} {e.version:10} {e.title or e.description}" + (f"  [{problem}]" if problem else ""))


@plugins_app.command("install")
def plugins_install(name: str = typer.Argument(..., help="name of the plugin in the index")):
    """Install a plugin of the index into the plugin folder (active from the next start)."""
    from .plugins import index

    try:
        entry = next((e for e in index.fetch() if e.name == name), None)
        if entry is None:
            raise index.PluginError(f"no plugin {name!r} in the index")
        path = index.install(entry, progress=print_progress)
    except index.PluginError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo(f"{name} {entry.version} installed in {path}")


@plugins_app.command("remove")
def plugins_remove(name: str):
    """Delete a plugin installed by the program."""
    from .plugins import Manager

    m = Manager()
    m.discover()
    try:
        m.remove(name)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo(f"{name} removed")


@plugins_app.command("enable")
def plugins_enable(name: str):
    """Enable a plugin from the next start."""
    from .plugins import Manager

    Manager().set_enabled(name, True)
    typer.echo(f"{name} enabled")


@plugins_app.command("disable")
def plugins_disable(name: str):
    """Disable a plugin from the next start (it stays installed)."""
    from .plugins import Manager

    Manager().set_enabled(name, False)
    typer.echo(f"{name} disabled")


@app.command("plugin", context_settings={"allow_extra_args": True, "ignore_unknown_options": True,
                                         "help_option_names": []})
def plugin_command(ctx: typer.Context, name: Optional[str] = typer.Argument(None, help="command of a plugin")):
    """Run a command of a plugin: ``ppg plugin NAME [ARGS]...``; without NAME, the commands available.
    ``--help`` goes to the plugin (``ppg plugin NAME --help`` is the plugin's own help)."""
    if name in ("--help", "-h"):
        name = None
    from .plugins import Manager

    m = Manager()
    m.load()
    for i in m.plugins:
        if i.error:
            typer.echo(f"plugin {i.name} not loaded: {i.error}", err=True)
    commands = m.commands
    if not name:
        if not commands:
            typer.echo("No plugin gives commands. `ppg plugins list` lists the plugins found.")
        for c in sorted(commands.values(), key=lambda c: c.name):
            typer.echo(f"{c.name:24} {c.help}  ({c.plugin})")
        return
    if name not in commands:
        typer.echo(f"no plugin gives the command {name!r}", err=True)
        raise typer.Exit(2)
    result = commands[name].function(*ctx.args)
    raise typer.Exit(result if isinstance(result, int) else 0)


@app.command()
def multispectral(project: Path, what: str = typer.Argument(..., help="add, panel, reflectance, index, zoning, bands"),
                  source: Optional[Path] = typer.Argument(None, help="add: the folder of the captures; panel: the "
                                                                     "photo of the panel (a reference-band file or "
                                                                     "its capture name)"),
                  reference: Optional[str] = typer.Option(None, help="add: the band to align (Red by default)"),
                  thermal: bool = typer.Option(False, help="add: the frames of a thermal camera (every radiometric image "
                                                           "without a band: FLIR JPEGs, TIFFs of temperatures or integers)"),
                  region: Optional[str] = typer.Option(None, help="panel: x0,y0,x1,y1 of the panel in the images (without "
                                                                  "it, the panel the camera found: MicaSense)"),
                  reflectance: Optional[float] = typer.Option(None, help="panel: its known reflectance (0-1; without it, "
                                                                         "the reflectances the camera read, else 0.5)"),
                  index: str = typer.Option("NDVI", help="index/zoning: one of the indices, or a formula over the band names"),
                  formula: Optional[str] = typer.Option(None, help="index: a formula instead of a known index"),
                  classes: int = typer.Option(3, help="zoning: how many classes"),
                  method: str = typer.Option("quantiles", help="zoning: quantiles, equal, jenks (natural breaks: the most "
                                                               "homogeneous classes) or kmeans"),
                  out: Optional[Path] = typer.Option(None, help="zoning: the GeoPackage to write (default beside the project)"),
                  resolution: Optional[float] = typer.Option(None, help="reflectance: metres per pixel (the orthomosaic's)"),
                  balance: bool = typer.Option(True, help="reflectance: bring the photos to the same reflectance where "
                                                          "they overlap (against the strips between flight lines)"),
                  dls: bool = typer.Option(True, help="panel: the sun sensor (DLS) follows the light during the flight "
                                                      "when the panel's capture and the flight have it (the panel gives "
                                                      "the level, the DLS the changes)"),
                  chunk: Optional[str] = None):
    """Multispectral captures: add (one file per band, side by side), the calibration panel, the
    reflectance orthomosaic (a band per band), an index raster, the zoning polygons."""
    from .engine import multispectral as ms

    doc, ch = _open(project, chunk)
    if what == "add":
        if source is None:
            raise typer.BadParameter("add needs the folder of the captures")
        cams = ms.add_captures(ch, [source], reference=reference, recursive=True, thermal=thermal)
        doc.save()
        info = ch.meta["multispectral"]
        typer.echo(f"{len(cams)} captures added ({info['captures']} in the chunk), bands {', '.join(info['bands'])}, "
                   f"reference {info['reference']}" + (f", radiometry {info['radiometry']}" if info.get("radiometry") else ""))
    elif what == "bands":
        info = ch.meta.get("multispectral") or {}
        typer.echo(json.dumps({k: v for k, v in info.items() if k != "registration"}, indent=1))
    elif what == "panel":
        found = ms.panel_captures(ch) if not region else []
        if source is None and found:
            cam = found[0]
        elif source is None:
            raise typer.BadParameter("panel needs the photo of the panel and --region x0,y0,x1,y1 (no capture of the "
                                     "chunk has a panel the camera found)")
        else:
            cam = next((c for c in ms.multispectral_cameras(ch)
                        if c.meta.get("capture") == str(source) or Path(c.path).stem == Path(source).stem
                        or Path(source).resolve() in {Path(p).resolve() for p in c.meta["bands"].values()}), None)
        if cam is None:
            raise typer.BadParameter(f"{source} is not a capture of the chunk")
        if not region and cam not in found:
            raise typer.BadParameter("the camera found no panel in this capture: give --region x0,y0,x1,y1")
        if region and reflectance is None:
            reflectance = 0.5
        factors = ms.calibrate_panel(ch, cam, [float(v) for v in region.split(",")] if region else None, reflectance,
                                     follow_dls=None if dls else False)
        doc.save()
        typer.echo("panel factors: " + ", ".join(f"{b} {f:.4g}" for b, f in factors.items()))
        panel = ch.meta["multispectral"]["panel"]
        if panel.get("follow_dls"):
            at_panel = ", ".join(f"{b} {v:.4g}" for b, v in panel["irradiance"].items())
            typer.echo(f"radiometry: panel+dls (the panel gives the level, the sun sensor follows the light; "
                       f"its irradiance at the panel, W/m2/nm: {at_panel})")
        else:
            typer.echo("radiometry: panel" + (" (the sun sensor left out)" if panel.get("irradiance") else ""))
    elif what == "reflectance":
        path = ms.build_reflectance(ch, resolution=resolution, balance=balance, progress=print_progress)
        doc.save()
        spread = (ch.meta["multispectral"].get("balance") or {}).get("spread") or {}
        typer.echo(f"reflectance orthomosaic: {path}" + (" (photos balanced: " + ", ".join(
            f"{b} {v * 100:.1f} %" for b, v in spread.items()) + ")" if spread else ""))
    elif what == "index":
        if formula is None and index not in ms.INDICES:
            raise typer.BadParameter(f"{index}: one of {', '.join(ms.INDICES)} or --formula")
        path = ms.index_raster(ch, index, formula)
        doc.save()
        typer.echo(f"{index}: {path}")
    elif what == "zoning":
        if method not in ms.ZONING_METHODS:
            raise typer.BadParameter(f"--method: {', '.join(ms.ZONING_METHODS)}")
        target = out or project.parent / f"{project.stem}_{index}_zone.gpkg"
        summary = ms.export_zoning(ch, index, target, classes=classes, method=method)
        doc.save()
        typer.echo(json.dumps(summary, indent=1))
    else:
        raise typer.BadParameter("what: add, bands, panel, reflectance, index or zoning")


@app.command("thermal-sdk")
def thermal_sdk(install: bool = typer.Option(False, help="download DJI's Thermal SDK from DJI into the program's data"),
                accept_license: bool = typer.Option(False, "--accept-license",
                                                    help="you accept DJI's terms: the MIT licence for the samples and "
                                                         "utilities such as dji_irp, DJI's SDK EULA for the rest"),
                folder: Optional[Path] = typer.Option(None, help="install: another folder (default: <app data>/dji_tsdk)")):
    """DJI's Thermal SDK, which reads the temperatures of DJI's R-JPEGs: how the frames would be decoded
    here, or (``--install --accept-license``) the SDK downloaded from DJI. It is not shipped with the
    program; on macOS it runs in Docker Desktop."""
    from .engine import thermal as th

    if install:
        if not accept_license:
            typer.echo(f"DJI's Thermal SDK is DJI's: its samples and utilities (dji_irp) are under the MIT licence, the "
                       f"rest under DJI's SDK EULA ({th.SDK_EULA_URL}). Run again with --accept-license to download it "
                       f"from DJI ({th.SDK_URL}, about 74 MB).")
            raise typer.Exit(1)
        path = th.install_sdk(progress=print_progress, folder=folder)
        typer.echo(f"DJI's Thermal SDK in {path}")
    typer.echo(json.dumps(th.decoder_status(), indent=1))


@app.command()
def thermal(project: Path, what: str = typer.Argument(..., help="status, params, build, anomalies or export"),
            target: Optional[Path] = typer.Argument(None, help="export: the file to write (.tif, .csv, .jpg)"),
            emissivity: Optional[float] = typer.Option(None, help="params: emissivity (factory 0.95)"),
            reflected: Optional[float] = typer.Option(None, help="params: reflected temperature, degrees (factory 22)"),
            ambient: Optional[float] = typer.Option(None, help="params: ambient temperature, degrees (factory 22)"),
            humidity: Optional[float] = typer.Option(None, help="params: relative humidity, percent (70)"),
            distance: Optional[float] = typer.Option(None, help="params: distance to the ground, metres (default: the flight height, 1-25)"),
            scale: Optional[float] = typer.Option(None, help="params: TIFFs of integers (WIRIS and others): degrees = value x scale + offset"),
            offset: Optional[float] = typer.Option(None, help="params: the offset of that conversion, degrees (centikelvin: "
                                                              "--scale 0.01 --offset -273.15)"),
            resolution: Optional[float] = typer.Option(None, help="build: metres per pixel (default: a thermal pixel on the ground)"),
            balance: bool = typer.Option(True, help="build: take away the offsets between frames"),
            radius: float = typer.Option(2.0, help="anomalies: the radius of the surroundings, metres"),
            fmt: Optional[str] = typer.Option(None, "--format", help="export: tif (degrees, float32), tif16 (centikelvin), "
                                                                     "csv, or flir (FLIR radiometric JPEGs in tiles with world files)"),
            tile: int = typer.Option(2048, help="export flir: the side of the tiles, cells (at most 2800)"),
            palette: str = typer.Option("iron", help="export flir: the colours of the picture (iron, inferno, rainbow, grey)"),
            chunk: Optional[str] = None):
    """Thermal frames (a DJI R-JPEG camera, MicaSense LWIR, temperature TIFFs) added with
    ``ppg multispectral PROJECT add``: the measure's parameters, the temperature orthomosaic in
    degrees Celsius (DJI's Thermal SDK decodes the R-JPEGs; on macOS it runs in Docker), its
    anomalies, the exports that keep degrees."""
    from .engine import thermal as th

    doc, ch = _open(project, chunk)
    if what == "status":
        st = th.decoder_status()
        typer.echo(json.dumps({"decoder": st, "params": th.params_of(ch),
                               "thermal": {k: v for k, v in (ch.meta.get("thermal") or {}).items() if k != "registration"}},
                              indent=1, default=str))
    elif what == "params":
        given = {k: v for k, v in (("emissivity", emissivity), ("reflected", reflected), ("ambient", ambient),
                                    ("humidity", humidity), ("distance", distance), ("scale", scale), ("offset", offset))
                 if v is not None}
        typer.echo(json.dumps(th.set_params(ch, **given), indent=1))
        doc.save()
    elif what == "build":
        path = th.build_thermal(ch, resolution=resolution, balance=balance, progress=print_progress)
        doc.save()
        info = ch.meta["thermal"]
        typer.echo(f"temperature orthomosaic: {path} ({info['resolution'] * 100:.1f} cm/px"
                   + (f", {info['stats']['p2']:.1f} - {info['stats']['p98']:.1f} degrees" if info.get("stats") else "")
                   + (f", offsets between frames {info['offsets']['spread']:.2f} degrees" if info.get("offsets") else "") + ")")
    elif what == "anomalies":
        path = th.anomalies(ch, radius_m=radius)
        doc.save()
        typer.echo(f"thermal anomalies: {path}")
    elif what == "export":
        if target is None:
            raise typer.BadParameter("export needs the file to write")
        typer.echo(str(th.export(ch, target, fmt, tile=tile, palette=palette)))
    else:
        raise typer.BadParameter("what: status, params, build, anomalies or export")


@app.command()
def satellite(project: Path, what: str = typer.Argument(..., help="info, ortho or dsm"),
              scene: Path = typer.Argument(..., help="the scene (GeoTIFF with its RPC, or a sidecar beside it)"),
              scene_b: Optional[Path] = typer.Argument(None, help="dsm: the second scene of the stereo pair"),
              more: Optional[list[Path]] = typer.Argument(None, help="dsm: more scenes: the pairs are chosen among "
                                                                     "them and their surfaces fused"),
              rpc: Optional[Path] = typer.Option(None, help="the RPC file of the scene (_RPC.TXT, .RPB, RPC_*.XML)"),
              rpc_b: Optional[Path] = typer.Option(None, help="dsm: the RPC file of the second scene"),
              name: Optional[str] = typer.Option(None, help="name of the version kept in the project"),
              resolution: Optional[float] = typer.Option(None, help="metres per cell (default: the scene's GSD)"),
              height_offset: float = typer.Option(0.0, help="added to the DEM heights before the RPC: the geoid "
                                                            "undulation when the DEM is orthometric"),
              height_range: Optional[str] = typer.Option(None, help="dsm: zmin,zmax of the ground (project heights)"),
              rgb: Optional[str] = typer.Option(None, help="ortho: the bands shown as red,green,blue (1-based)"),
              resampling: str = typer.Option("cubic", help="ortho: cubic, linear or nearest"),
              use_dem: bool = typer.Option(True, "--dem/--no-dem", help="dsm: the reference heights from the DEM of the chunk"),
              correct: Optional[bool] = typer.Option(None, "--correct/--no-correct",
                                                     help="dsm: correct the RPCs against each other with tie points "
                                                          "(default: with three scenes or more)"),
              max_pairs: int = typer.Option(6, help="dsm, three scenes or more: the pairs fused at most"),
              min_angle: float = typer.Option(5.0, help="dsm: least intersection angle of a pair (degrees)"),
              max_angle: float = typer.Option(30.0, help="dsm: largest intersection angle of a pair (degrees)"),
              chunk: Optional[str] = None):
    """Satellite scenes over the survey: info (size, GSD, footprint), ortho (the scene
    orthorectified on the DEM of the chunk, kept as a version of the orthomosaic), dsm (a surface
    model from a stereo pair, or from three scenes or more: RPCs corrected against each other,
    pairs chosen by angle and date, surfaces fused; kept as a version of the DEM)."""
    from .engine import satellite as sat

    more = list(more or [])
    if correct is None:
        correct = bool(more)
    doc, ch = _open(project, chunk)
    if what == "info":
        info = sat.scene_info(scene, ch.crs, rpc=rpc)
        info.pop("footprint")
        typer.echo(json.dumps(info, indent=1, default=str))
    elif what == "ortho":
        bands = [int(v) for v in rgb.split(",")] if rgb else None
        r = sat.ortho_scene(ch, scene, name=name, rpc=rpc, resolution=resolution, height_offset=height_offset,
                            rgb=bands, resampling=resampling, progress=print_progress)
        doc.save()
        typer.echo(f"{r['width']} x {r['height']} px at {r['resolution']:g} m, {r['filled'] * 100:.0f}% with data: "
                   f"{r['path']} (version {r['version']} of the orthomosaic)")
    elif what == "dsm":
        if scene_b is None:
            raise typer.BadParameter("dsm needs the two scenes of the pair")
        rng = tuple(float(v) for v in height_range.split(",")) if height_range else None
        if more or correct:
            scenes = [scene, scene_b, *more]
            r = sat.dsm_from_scenes(ch, scenes, name=name, rpcs=[rpc, rpc_b] + [None] * len(more), resolution=resolution,
                                    height_range=rng, height_offset=height_offset, use_dem=use_dem,
                                    correct_bias=correct, max_pairs=max_pairs, min_angle=min_angle, max_angle=max_angle,
                                    progress=print_progress)
            doc.save()
            b = r["bias"]
            for pr in r["pairs"]:
                typer.echo(f"pair {pr['pair'][0] + 1}-{pr['pair'][1] + 1}: {pr['angle']:.1f} degrees"
                           + (f", {pr['days']:.0f} days" if pr["days"] is not None else "")
                           + f", base to height {pr['base_to_height']:.2f}")
            if b is not None:
                typer.echo(f"RPC correction: {b['tracks']} tie points, residuals {b['rms_before'] or 0:.2f} -> "
                           f"{b['rms_after'] or 0:.2f} px RMS" + ("" if b["applied"] else " (not applied: too few)"))
            typer.echo(f"{r['width']} x {r['height']} cells at {r['resolution']:g} m from {len(r['pairs'])} pairs, "
                       f"{r['filled'] * 100:.0f}% with a height: {r['path']} (version {r['version']} of the DEM)")
            return
        r = sat.dsm_from_pair(ch, scene, scene_b, name=name, rpc_a=rpc, rpc_b=rpc_b, resolution=resolution,
                              height_range=rng, height_offset=height_offset, use_dem=use_dem,
                              progress=print_progress)
        doc.save()
        typer.echo(f"{r['width']} x {r['height']} cells at {r['resolution']:g} m, base to height "
                   f"{r['base_to_height']:.2f}, {r['matched'] * 100:.0f}% matched: {r['path']} (version {r['version']} of the DEM)")
    else:
        raise typer.BadParameter("what: info, ortho or dsm")


@app.command()
def node(folder: Path, once: bool = typer.Option(False, help="leave when no unit of work is left"),
         gpu: Optional[str] = typer.Option(None, help="the GPUs for the depth maps: auto, all, off, an adapter"),
         map: list[str] = typer.Option([], "--map", help="a path of the main computer and what it is here, "  # noqa: A002
                                                        "SRC=DST (the project folder is mapped by itself)")):
    """Work for a project of another computer: FOLDER is its <project>.ppg.files folder on the
    shared disk. Key points and PatchMatch depth maps come here in units of a few photos."""
    from .engine import nodes

    folder = Path(folder)
    if folder.name == NODES_FOLDER_NAME:
        folder = folder.parent
    if not folder.is_dir():
        raise typer.BadParameter(f"{folder} is not a folder")
    typer.echo(f"node {nodes.host_name()} on {folder}" + (" (once)" if once else " (Ctrl+C to stop)"))
    try:
        n = nodes.serve(folder, once=once, gpu=gpu, maps=map, log=typer.echo)
    except KeyboardInterrupt:  # pragma: no cover - the user
        n = 0
        typer.echo("stopped")
    typer.echo(f"{n} units done")


NODES_FOLDER_NAME = "nodes"


@app.command()
def remote(target: str, project: Path,
           photos: Optional[Path] = typer.Option(None, help="the folder of the photos to copy there too (the "
                                                            "key points need them; the depth maps do not)"),
           gpu: Optional[str] = typer.Option(None, help="the GPUs of the other computer: auto, all, off"),
           every: float = typer.Option(10.0, help="seconds between the copies back and forth"),
           dry_run: bool = typer.Option(False, help="print the commands instead of running them")):
    """A node on another computer over SSH (rsync and ssh of the system, your keys): the project
    folder, and the photos if given, go to TARGET (user@host:/folder); the node runs there while
    this command copies the units of work there and their results back, until the node leaves.
    Run it beside the step (ppg align, ppg dense, the window) that hands out the work."""
    import shlex
    import subprocess
    import time as _time

    from .engine import nodes

    host, _, remote_dir = target.partition(":")
    if not host or not remote_dir:
        raise typer.BadParameter("TARGET is user@host:/folder")
    project = Path(project).resolve()
    files_dir = project.parent / (project.name + ".files")
    if not files_dir.is_dir():
        raise typer.BadParameter(f"{files_dir} does not exist: open the project once")
    nodes.folder(files_dir)
    remote_dir = remote_dir.rstrip("/")
    remote_files = f"{remote_dir}/{files_dir.name}"
    maps = []
    if photos is not None:
        photos = Path(photos).resolve()
        maps.append(f"{photos}={remote_dir}/{photos.name}")

    def run(cmd, background=False):
        typer.echo("$ " + " ".join(shlex.quote(c) for c in cmd))
        if dry_run:
            return None
        if background:
            return subprocess.Popen(cmd, close_fds=False)
        return subprocess.run(cmd, check=False, close_fds=False)

    rsync = ["/usr/bin/rsync", "-a", "--exclude", "nodes/taken", "--exclude", "nodes/hello"]
    run(["/usr/bin/ssh", host, f"mkdir -p {shlex.quote(remote_dir)}"])
    run(rsync + [str(project), f"{host}:{remote_dir}/"])
    run(rsync + ["--delete", str(files_dir) + "/", f"{host}:{remote_files}/"])
    if photos is not None:
        run(rsync + [str(photos) + "/", f"{host}:{remote_dir}/{photos.name}/"])
    node_cmd = f"ppg node {shlex.quote(remote_files)} --once" + (f" --gpu {shlex.quote(gpu)}" if gpu else "") \
        + "".join(f" --map {shlex.quote(m)}" for m in maps)
    proc = run(["/usr/bin/ssh", host, node_cmd], background=True)
    if dry_run:
        return
    try:
        while proc.poll() is None:
            _time.sleep(every)
            run(rsync + [str(files_dir) + "/nodes/todo/", f"{host}:{remote_files}/nodes/todo/"])
            run(rsync + [str(files_dir) + "/", f"{host}:{remote_files}/", "--include", "*/", "--include", "*.bin",
                         "--include", "*.txt", "--exclude", "*"])
            run(rsync + [f"{host}:{remote_files}/", str(files_dir) + "/", "--include", "nodes/", "--include", "nodes/done/",
                         "--include", "nodes/done/*", "--include", "nodes/failed/", "--include", "nodes/failed/*",
                         "--include", "*/", "--include", "*.geometric.bin", "--exclude", "*"])
    finally:
        if proc.poll() is None:
            proc.terminate()
    typer.echo("the remote node left")


def main():  # pragma: no cover - console entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
