"""Command line interface: ``ppg <command>``.

Every step of the workflow is available, plus ``ppg process`` for the whole
pipeline and ``ppg run script.py`` to execute Python scripts against a project.
"""
from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from typing import NoReturn, Optional

import typer

from . import __version__
from .core.document import Document
from .core.progress import print_progress

app = typer.Typer(add_completion=False, help="pyarchinit-photogrammetry: open-source photogrammetry for archaeology")
markers_app = typer.Typer(help="Coded targets: printable sheets and turntable mats")
pa_app = typer.Typer(help="PyArchInit database bridge")
plugins_app = typer.Typer(help="Plugins: those found, the index of the plugin repository, installing, enabling")
gnss_app = typer.Typer(help="GNSS positions of the photos: their quality per flight, DJI MRK files, PPK with a base "
                            "(your own, a permanent station, or placed by a static baseline or PPP), positions files, "
                            "the coordinate system and the heights of the survey")
app.add_typer(markers_app, name="markers")
app.add_typer(pa_app, name="pyarchinit")
app.add_typer(plugins_app, name="plugins")
photos_app = typer.Typer(help="Enable or disable photos: a disabled photo keeps its pose, the next steps leave it out")
app.add_typer(gnss_app, name="gnss")
app.add_typer(photos_app, name="photos")
assistant_app = typer.Typer(help="The assistant from the command line: a question about a project, the models of a "
                                 "server or service, the evaluation of a model on the evaluation set")
app.add_typer(assistant_app, name="assistant")
tin_app = typer.Typer(help="TIN (triangulated irregular network) of a chunk: from the DEM, the ground of the dense "
                           "cloud or the spot heights, with break lines; DEM, contours, volumes and exports from it")
app.add_typer(tin_app, name="tin")


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
    if chunk is None:
        return doc, doc.chunk
    ch = next((c for c in doc.chunks if c.label == chunk or str(c.key) == chunk), None)
    if ch is None:
        names = ", ".join(f"{c.label!r} (key {c.key})" for c in doc.chunks)
        raise typer.BadParameter(f"no chunk {chunk!r} in the project; its chunks: {names}", param_hint="--chunk")
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
def add_photos(project: Path, paths: list[Path], chunk: Optional[str] = None, recursive: bool = False,
               prefer_raw: bool = typer.Option(False, help="of a raw and a JPEG of the same shot, take the raw"),
               raw_format: str = typer.Option("jpg", help="developed copy of a raw file: jpg or tif"),
               raw_exposure: float = typer.Option(1.0, help="exposure of the developed raw files, EV"),
               raw_tone: str = typer.Option("auto", help="auto, camera (the contrast of a camera JPEG), local (and local "
                                                                "contrast: flat phone raws), file (the look a ProRAW "
                                                                "DNG carries) or linear"),
               raw_half_size: bool = typer.Option(False, help="develop the raw files at half size (faster)"),
               raw_detail: str = typer.Option("auto", help="detail brought out as a phone does: auto or 0 to 4")):
    """Add photos or folders of photos. Camera raw files (DNG, CR2, NEF, ARW...) are developed into the project."""
    doc, ch = _open(project, chunk)
    added = ch.add_photos(paths, recursive=recursive, prefer="raw" if prefer_raw else "jpeg", progress=print_progress,
                          raw={"format": raw_format, "exposure": raw_exposure, "tone": raw_tone,
                               "half_size": raw_half_size, "detail": raw_detail})
    doc.save()
    typer.echo(f"added {len(added)} photos ({len(ch.cameras)} in chunk, {len(ch.sensors)} sensors)")


@app.command("develop-raw")
def develop_raw(project: Path, chunk: Optional[str] = None,
                raw_format: Optional[str] = typer.Option(None, help="jpg or tif (default: as they were added)"),
                raw_exposure: Optional[float] = typer.Option(None, help="exposure, EV (default: as they were added)"),
                raw_tone: Optional[str] = typer.Option(None, help="auto, camera, local, file or linear (default: as they "
                                                                  "were added)"),
                raw_detail: Optional[str] = typer.Option(None, help="auto or 0 to 4 (default: as they were added)"),
                force: bool = typer.Option(False, help="develop again the copies that are up to date too")):
    """Develop again the camera raw files of the photos: missing copies, changed raws, other settings."""
    doc, ch = _open(project, chunk)
    r = ch.develop_raw_photos({"format": raw_format, "exposure": raw_exposure, "tone": raw_tone,
                               "detail": raw_detail}, force=force,
                              progress=print_progress)
    doc.save()
    typer.echo(f"developed {r['developed']} raw files"
               + (f"; raw file not found: {', '.join(r['missing'])}" if r["missing"] else ""))


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


@app.command("elevation")
def elevation_cmd(ctx: typer.Context, project: Path,
                  trace: Optional[str] = typer.Option(None, help="vertical plane on the trace X1,Y1,X2,Y2 (chunk CRS)"),
                  side: Optional[str] = typer.Option(None, help="with --trace: a point X,Y on the viewer's side "
                                                     "(default: where the photos are)"),
                  markers: Optional[str] = typer.Option(None, help="plane of the markers A,B,C[,...] (labels, three "
                                                        "or more): u from A towards B, 0 at A"),
                  vertical: Optional[str] = typer.Option(None, help="with --markers: the vertical axis C,D (labels); "
                                                         "default perpendicular to A-B, upwards"),
                  facing: Optional[str] = typer.Option(None, help="the plane of a view: NX,NY,NZ from the model "
                                                       "towards the viewer, through --centre"),
                  centre: Optional[str] = typer.Option(None, help="with --facing: the point X,Y,Z the plane goes "
                                                       "through"),
                  up: str = typer.Option("0,0,1", help="with --facing: the up of the view"),
                  circle: Optional[str] = typer.Option(None, help="a cylindrical development around the circle "
                                                       "fitted to plan points X1,Y1,X2,Y2,X3,Y3[,...] (a well, a "
                                                       "tower)"),
                  inside: bool = typer.Option(True, "--inside/--outside", help="with --circle: seen from the axis "
                                              "(a well) or from outside (a tower)"),
                  axis: str = typer.Option("vertical", help="with --circle: the axis - vertical (v = the height), "
                                           "region (the Z of the chunk's region), crest (normal to the plane of "
                                           "the tower's crest), points (--axis-points)"),
                  axis_points: Optional[str] = typer.Option(None, help="with --axis points: X,Y,Z,X,Y,Z of the "
                                                            "bottom and the top of the axis (chunk CRS)"),
                  scale: str = typer.Option("cylinder", help="with --circle: cylinder (u = the arc on the "
                                            "cylinder) or wall (u = the length along the wall)"),
                  section: bool = typer.Option(False, help="a section: what stands in front of the plane is taken "
                                               "away and the line of the cut saved as the profile"),
                  front: float = typer.Option(0.5, help="metres kept in front of the plane (not for a section)"),
                  back: float = typer.Option(0.5, help="metres kept behind the plane"),
                  resolution: Optional[float] = typer.Option(None, help="metres per pixel (default: the GSD)"),
                  like: Optional[str] = typer.Option(None, help="the frame and grid of an existing elevation (id or "
                                                     "name): the same plane or cylinder - a development scaled on the "
                                                     "wall keeps its length along the wall -, resolution, bounds, depth "
                                                     "(--front/--back taken from it), kind and mosaic settings, so the "
                                                     "two compare pixel by pixel (another campaign); no other option "
                                                     "of the surface with it"),
                  like_chunk: Optional[str] = typer.Option(None, help="with --like: the chunk of that elevation "
                                                           "(label or key; default --chunk)"),
                  same_frame: bool = typer.Option(False, help="with --like and two LOCAL chunks: they are in the same "
                                                  "frame (aligned, merged, oriented on the same markers); without it "
                                                  "an elevation of another local chunk is refused"),
                  texture: Optional[bool] = typer.Option(None, "--texture/--photos",
                                                         help="the orthophoto from the texture of the model, or from "
                                                              "the photos (default: the texture when the chunk has no "
                                                              "aligned photo)"),
                  name: Optional[str] = None, chunk: Optional[str] = None):
    """Orthophoto and relief of the model on a plane - an elevation or a section: on a trace, on the
    plane of markers with the axes chosen, or on the plane of a view - or on a cylinder (--circle: a
    well, a tower; its axis vertical, the region's, the crest's or through two points; scaled on the
    cylinder or on the wall). With --facing the whole model is kept (a section: all behind the plane).
    With --like the frame and grid of an elevation already built (of this chunk or of --like-chunk)."""
    import numpy as np

    from .engine import elevation as E

    def numbers(text, n, what):
        values = [float(v) for v in text.replace(";", ",").split(",")]
        if len(values) != n:
            raise typer.BadParameter(f"{what}: {n} numbers separated by commas")
        return np.asarray(values)

    if like is not None:
        given = [("--inside/--outside" if n == "inside" else f"--{n.replace('_', '-')}") for n in ("trace", "side", "markers", "vertical", "facing", "centre", "up",
                                                       "circle", "inside", "axis", "axis_points", "scale", "section",
                                                       "front", "back", "resolution")
                 if getattr(ctx.get_parameter_source(n), "name", "DEFAULT") not in ("DEFAULT", "DEFAULT_MAP")]
        if given:
            raise typer.BadParameter(f"--like takes the surface, grid, depth and kind of that elevation: not with "
                                     f"{', '.join(given)}")
        doc, ch = _open(project, chunk)
        source = ch
        if like_chunk is not None:
            source = next((c for c in doc.chunks if c.label == like_chunk or str(c.key) == like_chunk), None)
            if source is None:
                raise typer.BadParameter(f"no chunk {like_chunk!r} in the project", param_hint="--like-chunk")
        found = [e for e in E.elevations(source) if e.get("id") == like] or \
            [e for e in E.elevations(source) if e.get("name") == like]
        if len(found) != 1:
            names = ", ".join(f"{e.get('name')!r} ({e.get('id')})" for e in E.elevations(source)) or "none"
            raise typer.BadParameter(f"{'no' if not found else 'more than one'} elevation {like!r} in the chunk "
                                     f"{source.label!r}; its elevations: {names}", param_hint="--like")
        try:
            entry = E.build_like(ch, found[0], name=name, progress=print_progress, source=source,
                                 same_frame=same_frame)
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1)
        doc.save()
        ortho, relief = E.elevation_paths(ch, entry)
        w, h = entry["size"]
        typer.echo(f"{entry['name']}: {w} x {h} px at {entry['resolution']:.4g} m/px on the grid of "
                   f"{found[0].get('name')!r} ({found[0]['id']})"
                   + (f"; scaled on the wall, {E.wall_length(entry):.3f} m all round" if entry.get("scale") == "wall"
                      else "")
                   + ("; the source is stale (built in a transform of another scale)" if entry.get("grid_of_stale")
                      else ""))
        typer.echo(f"{ortho}\n{relief}")
        return
    if like_chunk is not None or same_frame:
        raise typer.BadParameter("--like-chunk and --same-frame go with --like")
    if sum(v is not None for v in (trace, markers, facing, circle)) != 1:
        raise typer.BadParameter("give one surface: --trace, --markers, --facing, --circle or --like")
    if circle is None and (axis != "vertical" or axis_points is not None or scale != "cylinder"):
        raise typer.BadParameter("--axis, --axis-points and --scale are for a cylinder: give --circle")
    if axis not in E.AXIS_METHODS:
        raise typer.BadParameter(f"--axis: one of {', '.join(E.AXIS_METHODS)}")
    if scale not in ("cylinder", "wall"):
        raise typer.BadParameter("--scale: cylinder or wall")
    if circle is not None and section:
        raise typer.BadParameter("a section is made on a plane, not on --circle")
    doc, ch = _open(project, chunk)
    depth = (-back, front)
    if circle is not None:
        xy = [float(v) for v in circle.replace(";", ",").split(",")]
        if len(xy) < 6 or len(xy) % 2:
            raise typer.BadParameter("--circle: three or more X,Y points, separated by commas")
        ends = numbers(axis_points, 6, "--axis-points").reshape(2, 3) if axis_points else None
        if axis == "points" and ends is None:
            raise typer.BadParameter("--axis points needs --axis-points X,Y,Z,X,Y,Z")
        try:
            frame = E.cylinder_from_circle(ch, np.asarray(xy).reshape(-1, 2), inside=inside, axis=axis,
                                           axis_points=ends, depth=(min(-back, -3.0), max(front, 3.0)))
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1)
        if frame.tilt:
            t = frame.tilt
            typer.echo(f"axis {t['method']}: inclined {t['tilt_deg']:.2f} degrees towards azimuth "
                       f"{t['azimuth_deg']:.1f}" + (f", crest residual {t['residual_m'] * 100:.1f} cm"
                                                    if t.get("residual_m") is not None else ""))
    elif trace is not None:
        v = numbers(trace, 4, "--trace")
        frame = E.plane_from_line(ch, v[:2], v[2:], numbers(side, 2, "--side") if side else None)
    elif markers is not None:
        by_label = {m.label: m for m in ch.markers if m.position is not None}
        labels = [s.strip() for s in markers.split(",") if s.strip()]
        missing = [s for s in labels if s not in by_label]
        if missing or len(labels) < 3:
            raise typer.BadParameter(f"three markers with a position at least; not found or not placed: {missing}")
        pos = {s: ch.to_world(by_label[s].position[None])[0] for s in labels}
        c = d = None
        if vertical:
            cd = [s.strip() for s in vertical.split(",")]
            if len(cd) != 2 or any(s not in by_label for s in cd):
                raise typer.BadParameter("--vertical: two placed markers C,D")
            c, d = (ch.to_world(by_label[s].position[None])[0] for s in cd)
        cams = ch.to_world(np.array([cam.center for cam in ch.aligned_cameras]))
        frame = E.plane_from_markers(list(pos.values()), pos[labels[0]], pos[labels[1]], c, d,
                                     toward=cams.mean(axis=0))
    else:
        if centre is None:
            raise typer.BadParameter("--facing needs --centre")
        frame = E.plane_from_view(numbers(centre, 3, "--centre"), numbers(facing, 3, "--facing"),
                                  numbers(up, 3, "--up"))
        depth = None
    entry = E.build_elevation(ch, frame, resolution, name, depth, kind="section" if section else "elevation",
                              progress=print_progress, scale=scale, texture=texture)
    doc.save()
    ortho, relief = E.elevation_paths(ch, entry)
    w, h = entry["size"]
    typer.echo(f"{entry['name']}: {w} x {h} px at {entry['resolution']:.4g} m/px"
               + ("; v = height" if entry.get("v_is_height") else "")
               + (f"; scaled on the wall, {E.wall_length(entry):.3f} m all round" if entry.get("scale") == "wall"
                  else "")
               + (f"; profile: {len(E.read_profile(ch, entry))} lines" if entry.get("profile") else ""))
    typer.echo(f"{ortho}\n{relief}")


@app.command("region-markers")
def region_markers_cmd(project: Path,
                       a: str = typer.Argument(..., help="first marker (label, key or target code): u from A to B"),
                       b: str = typer.Argument(..., help="second marker"),
                       c: str = typer.Argument(..., help="third marker"),
                       thickness: float = typer.Option(..., help="metres of region along the normal of the "
                                                       "plane, centred on it"),
                       extent: str = typer.Option("region", help="size in the plane: 'region' (the region there "
                                                  "was, else the tie points) or 'tie_points'"),
                       chunk: Optional[str] = None):
    """The reconstruction region as a thin box on the plane of three markers (an object, a wall):
    the axes of the plane and its normal towards the photos."""
    if extent not in ("region", "tie_points"):
        raise typer.BadParameter("--extent: region or tie_points")
    doc, ch = _open(project, chunk)
    try:
        region = ch.region_from_markers(a, b, c, thickness, extent)
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    doc.save()
    typer.echo("region on the plane of %s, %s, %s: %.3f x %.3f x %.3f m, centre %.3f %.3f %.3f"
               % (a, b, c, *region.size, *region.center))


@app.command("normal-map")
def normal_map_cmd(project: Path,
                   source: str = typer.Option("auto", help="model, dem or auto (the model when there is one)"),
                   chunk: Optional[str] = None):
    """The normal map on the grid of the orthomosaic (same pixels, same coordinates): an RGBA
    GeoTIFF, red east, green north, blue up; the product normal_map of the chunk."""
    if source not in ("auto", "dem", "model"):
        raise typer.BadParameter("--source: auto, dem or model")
    doc, ch = _open(project, chunk)
    try:
        path = ch.build_normal_map(source, progress=print_progress)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    doc.save()
    typer.echo(f"normal map from the {ch.meta.get('normal_map', {}).get('source', source)}: {path}")


@app.command("duplicate-chunk")
def duplicate_chunk_cmd(project: Path, chunk: Optional[str] = None,
                        photos: Optional[Path] = typer.Option(None, help="text file with the names of the photos "
                                                              "to keep, one a line"),
                        area: Optional[str] = typer.Option(None, help="polygon (WKT, GeoJSON or a GeoJSON file) in "
                                                           "the coordinates of the chunk: the photos framing it"),
                        label: Optional[str] = typer.Option(None, help="name of the new chunk (default: "
                                                            "'<name> (copia)')")):
    """A copy of the chunk with all its photos or some of them, with their poses: depth maps, dense
    cloud and the rest can be built on it again without aligning. Products are not copied."""
    from .engine.chunks import cameras_in_area

    if photos is not None and area is not None:
        raise typer.BadParameter("give --photos or --area, not both")
    doc, ch = _open(project, chunk)
    cams = None
    if photos is not None:
        names = [n.strip() for n in photos.read_text(encoding="utf-8").splitlines() if n.strip()]
        try:
            cams = [ch.camera(n) for n in _photo_names(ch, names)]
        except KeyError as exc:
            raise typer.BadParameter(f"no photo {exc.args[0]!r} in the chunk {ch.label}") from exc
    elif area is not None:
        polygon = _polygons([area])
        try:
            cams = cameras_in_area(ch, polygon)
        except RuntimeError as exc:
            raise typer.BadParameter(str(exc)) from exc
        if not cams:
            raise typer.BadParameter("no photo frames the area")
    try:
        dup = doc.duplicate_chunk(ch, cams, label=label)
    except (ValueError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    doc.save()
    typer.echo(f"chunk {dup.label}: {len(dup.cameras)} photos of {len(ch.cameras)}, {len(dup.markers)} markers")


def _photo_names(ch, names: list[str]) -> list:
    """Names of photos of the command line: a label, else a key when it is a number."""
    labels = {c.label for c in ch.cameras}
    return [n if n in labels or not n.isdigit() else int(n) for n in names]


def _photos_enabled(project: Path, names: list[str], chunk: Optional[str], enabled: bool) -> None:
    doc, ch = _open(project, chunk)
    try:
        n = ch.set_cameras_enabled(_photo_names(ch, names), enabled)
    except KeyError as exc:
        raise typer.BadParameter(f"no photo {exc.args[0]!r} in the chunk {ch.label}") from exc
    doc.save()
    typer.echo(f"{n} photos {'enabled' if enabled else 'disabled'} in {ch.label}")


@photos_app.command("enable")
def photos_enable(project: Path, names: list[str] = typer.Argument(..., help="photo names (or keys)"),
                  chunk: Optional[str] = None):
    """Enable photos: the next alignment and builds use them."""
    _photos_enabled(project, names, chunk, True)


@photos_app.command("disable")
def photos_disable(project: Path, names: list[str] = typer.Argument(..., help="photo names (or keys)"),
                   chunk: Optional[str] = None):
    """Disable photos: they keep their pose, the next alignment and builds leave them out."""
    _photos_enabled(project, names, chunk, False)


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
def calc(project: Path, formula: Optional[str] = typer.Argument(None, help='e.g. "dem - dem_before_excavation" or '
                                                                       '"(reflectance@NIR - reflectance@Red) / '
                                                                       '(reflectance@NIR + reflectance@Red)"'),
         name: Optional[str] = typer.Option(None, help="the name of the result (letters, digits and _)"),
         grid: Optional[str] = typer.Option(None, help="the raster whose grid the result takes (default: the first "
                                                       "one of the formula)"),
         resolution: Optional[float] = typer.Option(None, help="another resolution of that grid (metres)"),
         list_: bool = typer.Option(False, "--list", help="list the rasters of the chunk with their names"),
         chunk: Optional[str] = None):
    """Raster calculator: a formula, as in the raster calculator of QGIS, on any raster of the chunk
    (products, versions, bands, indices, the rasters of the plugins, earlier results), kept as
    <chunk data>/calculator/NAME.tif. --list shows the names of the rasters for the formulas."""
    from .engine import rastercalc

    doc, ch = _open(project, chunk)
    if list_ or not formula:
        for r in rastercalc.rasters(ch):
            bands = "" if len(r.bands) == 1 else "  bands " + ", ".join(
                f"@{k}" + (f" {b}" if b else "") for k, b in enumerate(r.bands, 1))
            typer.echo(f"{r.name:28s} {r.width}x{r.height} at {r.resolution:.4g}  {r.label} [{r.source}]{bands}")
        return
    if not name:
        raise typer.BadParameter("give the result a --name")
    try:
        info = rastercalc.calculate(ch, formula, name, grid=grid, resolution=resolution, progress=print_progress)
    except rastercalc.FormulaError as exc:
        raise typer.BadParameter(str(exc)) from None
    doc.save()
    st = info["stats"]
    typer.echo(f"{name}: {ch.data_dir / info['file']} ({info['size'][0]}x{info['size'][1]} on the grid of "
               f"{info['grid']}" + (f", values {st['min']:.4g} to {st['max']:.4g}" if st.get("cells") else ", no data")
               + ")")


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


def _triples(text: str) -> list[list[float]]:
    """``"xA,yA,zA;xB,yB,zB[;xC,yC,zC]"`` -> two or three points (``typer.BadParameter`` otherwise)."""
    try:
        points = [[float(v) for v in part.split(",")] for part in text.split(";") if part.strip()]
    except ValueError as exc:
        raise typer.BadParameter(f"--points: numbers x,y,z separated by ';': {text}") from exc
    if len(points) not in (2, 3) or any(len(p) != 3 for p in points):
        raise typer.BadParameter(f"--points: two or three points x,y,z separated by ';': {text}")
    return points


def _orient_lines(result: dict, dry_run: bool) -> list[str]:
    """What ``ppg orient`` prints of the result of ``engine.orient.orient``."""
    from .core import stamps

    d = result["describe"]
    t = d["translation"]
    lines = [f"tilt {d['tilt_deg']:.1f}° towards {d['tilt_direction_deg']:.1f}°, heading {d['heading_deg']:.1f}°",
             f"translation {t[0]:.3f}, {t[1]:.3f}, {t[2]:.3f}", f"scale {d['scale']:.6f}"]
    moved = {k: n for k, n in (result.get("moved") or {}).items() if n}
    if moved:
        lines.append("moved: " + ", ".join(f"{k} {n}" for k, n in moved.items()))
    if result.get("stale"):
        lines.append("stale (to build again): " + ", ".join(stamps.english_name(x) for x in result["stale"]))
    if result.get("aligned"):
        lines.append("aligned chunks moved: " + ", ".join(result["aligned"]))
    if result.get("aligned_left"):
        lines.append("aligned chunks left: " + ", ".join(result["aligned_left"]))
    for key, title in (("kept", "kept"), ("sent_out", "sent out")):
        for item in result.get(key) or []:
            names = item.get("names") or []
            if key == "sent_out" and item["what"] in ("qfield", "exports"):
                title_ = "if already sent out (not recorded by the project)"
            else:
                title_ = title
            lines.append(f"{title_}: {item['what']}" + (f" ({', '.join(names)})" if names else ""))
    if result.get("weak_markers"):
        lines.append("weak markers (seen in fewer than two photos): " + ", ".join(result["weak_markers"]))
    if result.get("cameras_replaced"):
        lines.append(f"photo positions replaced: {len(result['cameras_replaced'])}")
    if result.get("bar_dropped"):
        lines.append(f"typed distance dropped: {result['bar_dropped']['distance']} m (it was of other points)")
    if result.get("plugins_unaware"):
        lines.append("data of plugins not moved: " + ", ".join(result["plugins_unaware"]))
    if dry_run:
        lines.append("(dry run, nothing saved)")
    return lines


@app.command()
def orient(project: Path,
           points: Optional[str] = typer.Option(None, help="A, B[, C] in the coordinates of the chunk: "
                                                           "'xA,yA,zA;xB,yB,zB[;xC,yC,zC]' (x along A->B, the plane "
                                                           "of the three points horizontal)"),
           markers: Optional[str] = typer.Option(None, help="the markers A,B[,C] (labels or keys of placed markers)"),
           plane_from: Optional[str] = typer.Option(None, help="a plan polygon (WKT, GeoJSON or a file of them): the "
                                                               "plane fitted to the points of the model inside it "
                                                               "becomes horizontal; with --points of two points "
                                                               "A->B sets x"),
           x_kept: bool = typer.Option(False, "--x-kept", help="x as the current x (projected on the new "
                                                                "horizontal), not along A->B"),
           quota: float = typer.Option(0.0, help="the height of the new origin: A with --points or --markers; with "
                                                 "--plane-from the plane, at A's foot on it (or at the plane's "
                                                 "centre without A): A itself keeps its height above the plane"),
           origin: Optional[str] = typer.Option(None, help="X,Y of A (or of the plane) in the new system "
                                                           "(default 0,0)"),
           distance: Optional[float] = typer.Option(None, help="the length of A-B in the new system (a scale bar "
                                                               "on the orientation markers)"),
           flip: bool = typer.Option(False, "--flip", help="upside down: z on the other side of the plane"),
           aligned: bool = typer.Option(True, "--aligned/--no-aligned", help="move the chunks aligned to this one "
                                                                             "with it"),
           chunk: Optional[str] = None,
           dry_run: bool = typer.Option(False, "--dry-run", help="say what would happen, save nothing")):
    """Orient the local system of a chunk (no coordinates of the world): level it on three points
    or on a plane, set x, the height and the origin, optionally the scale. Everything drawn in
    the chunk follows; DEM and orthomosaic are to be built again. Three orientation markers keep
    the orientation through a new alignment."""
    from .engine import orient as orient_mod

    given = [x is not None for x in (points, markers, plane_from)]
    if not any(given):
        raise typer.BadParameter("give --points, --markers or --plane-from")
    if markers is not None and (points is not None or plane_from is not None):
        raise typer.BadParameter("--markers goes alone (not with --points or --plane-from)")
    pts = _triples(points) if points is not None else None
    if plane_from is not None and pts is not None and len(pts) != 2:
        raise typer.BadParameter("with --plane-from, --points gives A and B (two points) to set x")
    origin_xy = (0.0, 0.0)
    if origin is not None:
        try:
            origin_xy = tuple(float(v) for v in origin.split(","))
        except ValueError as exc:
            raise typer.BadParameter(f"--origin: X,Y: {origin}") from exc
        if len(origin_xy) != 2:
            raise typer.BadParameter(f"--origin: X,Y: {origin}")
    names = [v.strip() for v in markers.split(",") if v.strip()] if markers is not None else None
    if names is not None and len(names) not in (2, 3):
        raise typer.BadParameter("--markers: two or three markers A,B[,C]")
    doc, ch = _open(project, chunk)
    if names is not None:  # the markers named: a usage error when one is not in the chunk
        from .markers import gcp

        try:
            for name in names:
                gcp.resolve_marker(ch, name)
        except KeyError as exc:
            raise typer.BadParameter(f"--markers: {exc.args[0] if exc.args else exc}") from exc
    kw = dict(x_along_ab=not x_kept, quota=quota, origin_xy=origin_xy, distance=distance, flip=flip,
              move_aligned=aligned, dry_run=dry_run)
    try:
        if names is not None:
            kw["markers"] = names
        if plane_from is not None:
            orient_mod.check(ch, cameras=False)
            xyz = orient_mod.points_in_plan(ch, _polygons([plane_from]))
            kw.update(plane_xyz=xyz, toward=orient_mod.toward_cameras(ch, xyz), points=pts)
        else:
            kw["points"] = pts
        result = orient_mod.orient(ch, **kw)
    except orient_mod.OrientRefused as exc:
        typer.echo(f"{exc.reason}: {exc}", err=True)
        if exc.names:
            typer.echo("    " + ", ".join(exc.names), err=True)
        raise typer.Exit(code=1) from exc
    if not dry_run:
        doc.save()
        if result.get("state") is not None:  # nothing will undo it from here: its copies go
            orient_mod.drop(result["state"])
    typer.echo(f"{ch.label}: " + ("orientation (dry run)" if dry_run else "oriented"))
    for line in _orient_lines(result, dry_run):
        typer.echo(f"    {line}")


@app.command("drawings3d")
def drawings3d(project: Path,
               action: str = typer.Argument(..., help="list, export or import"),
               file: Optional[Path] = typer.Argument(None, help="import: the GeoPackage, GeoJSON or DXF to read"),
               out: Optional[Path] = typer.Option(None, help="export: the file to write"),
               layer: Optional[str] = typer.Option(None, help="list/export: only this layer; import: the layer the "
                                                              "drawings go in (default: the file's layer column, else "
                                                              "the name of the file)"),
               fmt: Optional[str] = typer.Option(None, "--format", help="export: dxf, gpkg, geojson or csv (default: "
                                                                         "by the suffix of --out)"),
               chunk: Optional[str] = None):
    """The free drawings in 3D of a chunk (lines, polygons and points in user layers): list them (3D length or
    area), export them (DXF 3D, GeoPackage with Z, GeoJSON, CSV of the vertices) or import a GeoPackage, GeoJSON or
    DXF (features without Z take the heights of the DEM; the project is saved)."""
    from .archaeo.measures import feature_measures
    from .core import free_drawings as fd
    from .io import free_drawings as fio

    action = action.lower()
    if action not in ("list", "export", "import"):
        raise typer.BadParameter(f"the action is list, export or import, not {action!r}")
    if fmt is not None and fmt.lower() not in fio.FORMATS:
        raise typer.BadParameter(f"--format: one of {', '.join(fio.FORMATS)}")
    doc, ch = _open(project, chunk)
    if action == "list":
        typer.echo("id\tlayer\tkind\tname\tvertices\tlength_3d\tarea_3d")
        for rec in fd.features(ch, layer):
            try:
                m = feature_measures(rec)
            except ValueError:
                m = {}
            length = m.get("length_3d", m.get("perimeter_3d"))
            area = m.get("area_3d")
            typer.echo("\t".join([rec["id"], rec["layer"], rec["kind"], rec.get("name") or "", str(len(rec["xyz"])),
                                  "-" if length is None else f"{length:.3f}", "-" if area is None else f"{area:.3f}"]))
        return
    if action == "export":
        if out is None:
            raise typer.BadParameter("export needs --out FILE")
        if fmt is None and out.suffix.lower() not in fio.SUFFIXES:
            raise typer.BadParameter(f"--out: {out.suffix or 'no suffix'} is not one of {', '.join(fio.FORMATS)}: "
                                     "give --format")
        n = len(fd.features(ch, layer))
        if not n:
            typer.echo(f"{ch.label}: no free drawing" + (f" in the layer {layer}" if layer else ""), err=True)
            raise typer.Exit(code=1)
        written = fio.export(ch, out, fmt=fmt.lower() if fmt else None, layer=layer)
        typer.echo(f"{ch.label}: {n} free drawings written to {written}")
        return
    if file is None:
        raise typer.BadParameter("import needs the FILE to read")
    if not file.exists():
        raise typer.BadParameter(f"{file} does not exist")
    try:
        got = fio.import_features(ch, file, layer=layer)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if got["added"]:
        doc.save()
    typer.echo(f"{ch.label}: {got['added']} free drawings imported from {file.name} ({got['dem_z']} with the heights "
               f"of the DEM, {got['skipped']} skipped" + (f", {got['holes_dropped']} holes of polygons left out"
                                                         if got.get("holes_dropped") else "") + ")")
    if got.get("rfc7946"):
        typer.echo(f"{file.name}: GeoJSON without a crs member read as WGS 84 (RFC 7946) and converted to {ch.crs}")


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
    """Match photos and align cameras, with the choices of Metashape's Align Photos. What would keep the alignment
    from placing the chunk by the positions of the photos (the grid of a geoid missing) stops it before the
    matching."""
    from .markers import gcp

    doc, ch = _open(project, chunk)
    try:
        gcp.preflight(ch, gps)
    except ValueError as exc:
        _gnss_failed(exc)
    ch.match_photos(quality=quality, pair_selection=pairs, keypoint_limit=keypoints, tiepoint_limit=tiepoints,
                    generic=generic, reference=None if reference == "none" else reference, neighbours=neighbours,
                    apply_masks=masks, guided_matching=guided, exclude_stationary=stationary, reset=not keep,
                    gpu=gpu, matcher=matcher, progress=print_progress)
    n = ch.align_cameras(progress=print_progress, mapper=mapper, reset=not keep, use_gps=gps, adaptive_fitting=adaptive,
                         ba_global_ratio=ba_ratio, ba_global_refinements=ba_refinements)
    doc.save()
    typer.echo(f"aligned {n}/{len(ch.cameras)} photos")
    _say_stale_ends(doc)


def _say_stale_ends(doc) -> None:
    """The ends of ties left out after a new alignment (placed without photos in the old frame), said."""
    from .engine import ties as ties_mod

    for item in ties_mod.stale_ends(doc):
        typer.echo("warning: " + ties_mod.render(item["message"]) + f" - ppg ties confirm {item['id']}")


@app.command()
def optimize(project: Path, markers: bool = typer.Option(True, help="control points (3 or more) and control scale "
                                                                   "bars (2 or more, or with control points) take part "
                                                                   "in the adjustment; check points and bars are reported"),
             marker_weight: int = typer.Option(4, help="a marker measurement counts as this many tie point observations"),
             principal_point: bool = False,
             engine: str = typer.Option("colmap", help="colmap, or ppg: our own adjustment (control points by their "
                                                       "accuracy, bars as distances, GNSS, rolling shutter)"),
             gnss: str = typer.Option("auto", help="the GNSS positions of the photos: auto (the accurate ones - fix, float, "
                                                   "PPK, 0.5 m or better - hold the block when no fixed points do: "
                                                   "fewer than 3 control points, fewer than 2 control bars; the focal "
                                                   "length of the alignment is then kept), off; with --engine ppg also "
                                                   "plain (every position by its accuracy) or bias (with a bias per "
                                                   "flight), both refining the focal length"),
             rolling_shutter: bool = typer.Option(False, help="with --engine ppg: a velocity per photo while it is read"),
             chunk: Optional[str] = None):
    """Optimize the cameras (global bundle adjustment), on the control points and scale bars when there are any, else
    held by the accurate GNSS positions of the photos; an optimisation that takes the cameras away from those
    positions is undone (it bent the block, or the positions disagree with the control points: switch off the camera
    references to keep it)."""
    from .engine.sfm import GNSS_MODES

    if gnss not in GNSS_MODES:
        raise typer.BadParameter(f"one of {', '.join(GNSS_MODES)}", param_hint="--gnss")
    if engine == "colmap" and gnss in ("plain", "bias"):
        raise typer.BadParameter(f"{gnss} needs --engine ppg (COLMAP's adjustment takes auto or off)",
                                 param_hint="--gnss")
    doc, ch = _open(project, chunk)
    err = ch.optimize_cameras(fit_principal_point=principal_point, use_markers=markers, marker_weight=marker_weight,
                              progress=print_progress, engine=engine, gnss=gnss, rolling_shutter=rolling_shutter)
    if any(m.reference.location for m in ch.markers) or ch.scalebars:
        ch.update_transform()
    doc.save()
    rep = ch.meta["sfm"]["optimize"]
    offsets = rep.get("camera_offsets")
    if rep.get("reverted"):  # the report describes the alignment kept; the run undone is under "undone"
        typer.echo(f"reprojection error {err:.3f} px (the alignment kept); the optimisation was undone: the cameras "
                   f"ended {offsets['after']:.3f} m off their GNSS positions ({offsets['before']:.3f} m before) - it "
                   "bent the block, or the positions disagree with the control points. To keep such an optimisation, "
                   "switch off the camera references (in the window: Reference pane, right click, Do not use the "
                   "references of all the cameras; in Python: camera.reference.enabled = False) and optimise again")
        return
    text = f"reprojection error {err:.3f} px"
    held = rep.get("gnss") or {}
    if held.get("photos"):
        text += (f", held by the GNSS positions of {held['photos']} photos ("
                 + ", ".join(f"{state} {n}" for state, n in sorted(held["states"].items(), key=lambda s: -s[1])) + ")")
    elif held.get("termination") and not held.get("usable"):
        text += (f", the adjustment held by the GNSS positions failed ({held['termination']}): the block is left as "
                 "the alignment made it")
    if rep.get("focal_held"):
        text += ", focal length of the alignment held"
    if offsets:
        text += f", camera offsets from their GNSS positions {offsets['before']:.3f} -> {offsets['after']:.3f} m (RMS)"
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
         fill_holes: Optional[float] = typer.Option(None, help="height field: close the holes up to this gap between "
                                                               "measured points, in metres (interpolated surface; "
                                                               "default: none)"),
         texture: bool = True,
         texture_size: int = typer.Option(4096, help="side of the texture in pixels; above 8192 it is split into "
                                                     "pages of 8192 (16384: 4 pages, 32768: 16)"),
         colour: Optional[bool] = typer.Option(None, "--colour/--no-colour",
                                               help="colour correction of the photos (default: if calibrated)"),
         skip_unreadable: bool = typer.Option(False, help=SKIP_HELP), chunk: Optional[str] = None):
    """Mesh (Poisson, height field or fusion of the depth maps) and texture."""
    doc, ch = _open(project, chunk)
    m = ch.build_model(source=source, surface=surface, face_count=int(faces) if faces.isdigit() else faces,
                       voxel_size=voxel_size, classes=_classes(classes), fill_holes=fill_holes,
                       progress=print_progress)
    report = ch.meta.get("model") or {}
    if report.get("holes_filled"):
        typer.echo(f"{report['holes_filled']} holes closed: {report['interpolated_area']:.2f} m2 in plan are "
                   "interpolated, not measured")
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
          texture: Optional[bool] = typer.Option(None, "--texture/--photos",
                                                 help="from the texture of the model, or from the photos (default: "
                                                      "the texture when the chunk has no aligned photo)"),
          supersample: Optional[int] = typer.Option(None, help="--texture: rays per cell side (default: as many as "
                                                               "texels in a cell, at most 4)"),
          chunk: Optional[str] = None):
    """Orthomosaic (GeoTIFF RGBA), any size: made tile by tile. A chunk without aligned photos (an imported or merged
    model) gets it from the texture of its model."""
    doc, ch = _open(project, chunk)
    if texture is None and not ch.aligned_cameras:
        typer.echo("note: the chunk has no aligned photos: the orthophoto is made from the texture of the model")
    p = ch.build_orthomosaic(resolution=resolution, surface=surface, blending=blending,
                             hide_classes=_classes(hide_classes), progress=print_progress,
                             skip_unreadable=skip_unreadable, max_angle=max_angle, colour_correction=colour,
                             ghosting=ghosting, fill_holes=fill_holes, texture=texture, supersample=supersample)
    doc.save()
    typer.echo(str(p))


@app.command()
def process(project: Path, photos: list[Path], gcp: Optional[Path] = None, crs: Optional[str] = None,
            targets: Optional[str] = "aruco_4x4_50", quality: str = "high", dense_quality: str = "medium",
            dem_resolution: Optional[float] = None, ortho_resolution: Optional[float] = None,
            surface: str = typer.Option("arbitrary", help="surface of the model: arbitrary or height_field"),
            fill_holes: Optional[float] = typer.Option(None, help="height field: close the holes up to this gap "
                                                                  "between measured points, in metres (interpolated "
                                                                  "surface; default: none)"),
            texture: bool = True):
    """Whole pipeline: photos -> alignment -> georeferencing -> cloud -> mesh -> DEM -> orthomosaic -> report."""
    from .markers import gcp as references  # not `gcp`: the option of the file

    doc = Document.open(project) if project.exists() else Document()
    ch = doc.chunk or doc.add_chunk("Chunk 1")
    doc.save(project)
    ch.add_photos(photos)
    if targets:
        ch.detect_markers(targets, progress=print_progress)
    try:
        references.preflight(ch)  # what would keep the alignment from placing the chunk: before the matching
    except ValueError as exc:
        _gnss_failed(exc)
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
    if fill_holes and surface != "height_field":
        typer.echo("--fill-holes closes the holes of the height field only: add --surface height_field", err=True)
    ch.build_model(surface=surface, fill_holes=fill_holes or None, progress=print_progress)
    model = ch.meta.get("model") or {}
    if model.get("holes_filled"):
        typer.echo(f"{model['holes_filled']} holes closed: {model['interpolated_area']:.2f} m2 in plan are "
                   "interpolated, not measured")
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
                 check: bool = typer.Option(False, "--check", help="measure each chunk against the reference once "
                                            "aligned: overlap and distances of its points"),
                 as_json: bool = typer.Option(False, "--json", help="print the whole report as JSON")):
    """Align chunks to the frame and CRS of a reference chunk (markers, points or cameras)."""
    doc = Document.open(project)
    if doc.chunk is None:
        raise typer.BadParameter("the project has no chunks")
    try:
        report = doc.align_chunks(reference if reference is not None else doc.chunk, chunks or None, method=method,
                                  fix_scale=fix_scale, initial=initial, progress=print_progress, check=check)
    except (ValueError, KeyError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    doc.save()
    if as_json:
        typer.echo(json.dumps(report, indent=1))
        return
    for c in report["chunks"]:
        if check:
            typer.echo(_distance_line(c.get("distances"), c["label"], report["reference"]["label"]))
        rmse = "-" if c.get("rmse") is None else f"{c['rmse']:.4g}"
        extra = f", initial position {c['initial']}" if c.get("initial") else ""
        typer.echo(f"{c['label']} -> {report['reference']['label']}: {c['method']}, {c['correspondences']} "
                   f"correspondences, RMSE {rmse}, scale {c['scale']:.6g}{extra}")
        if c.get("stale_products"):
            typer.echo(f"  build again: {', '.join(c['stale_products'])}")


def _distance_line(d: dict | None, label: str, reference: str) -> str:
    """One line of the distances of a chunk to a reference (:func:`engine.chunks.cloud_distances`)."""
    if d is None:
        return f"{label} -> {reference}: nothing to measure (no dense cloud, tie points or model)"
    where = "" if d["placed"] else " (local frame and system of the world: compared as they are)"
    if d["median"] is None:
        return (f"{label} -> {reference}: no overlap ({d['points']} points, none over or under the reference)"
                + where)
    return (f"{label} -> {reference}: overlap {d['overlap'] * 100:.1f} % within {d['threshold']:.4g}, median distance "
            f"{d['median']:+.4g}, NMAD {d['nmad']:.4g}, p95 {d['p95']:.4g} ({d['points']} points, "
            f"{d['covered'] * 100:.0f} % over the reference)" + where)


@app.command("chunk-distances")
def chunk_distances(project: Path,
                    chunks: Optional[list[str]] = typer.Argument(None, help="chunks to measure (labels or keys); "
                                                                            "default: the other enabled chunks"),
                    reference: Optional[str] = typer.Option(None, "--reference", "-r",
                                                            help="reference chunk (label or key); default: the active "
                                                                 "one"),
                    max_points: int = typer.Option(200_000, help="points read from each chunk"),
                    threshold: Optional[float] = typer.Option(None, help="overlap: points within this distance "
                                                                         "(default: 3 spacings of the reference)"),
                    as_json: bool = typer.Option(False, "--json", help="print the results as JSON")):
    """How far the points of chunks are from the surface of a reference chunk: median, NMAD, 95th percentile of the
    distance and the share of points within the threshold (the overlap), in the units of the reference's system."""
    from .engine.chunks import DistanceSurface, cloud_distances, resolve_chunk

    doc = Document.open(project)
    if doc.chunk is None:
        raise typer.BadParameter("the project has no chunks")
    try:
        ref = resolve_chunk(doc, reference if reference is not None else doc.chunk)
        todo = [resolve_chunk(doc, c) for c in chunks] if chunks else [c for c in doc.chunks
                                                                       if c is not ref and c.enabled]
        surface = DistanceSurface(ref, max_points)
        out = [cloud_distances(ref, ch, max_points, threshold, surface=surface) for ch in todo if ch is not ref]
    except (ValueError, KeyError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(out, indent=1))
        return
    for d in out:
        typer.echo(_distance_line(d, d["label"], d["reference"]))


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


@app.command("merge-models")
def merge_models_cmd(project: Path,
                     chunks: list[str] = typer.Argument(..., help="chunks whose models are merged (labels or keys); "
                                                                  "the first gives frame and CRS"),
                     label: Optional[str] = typer.Option(None, help="label of the new chunk (default Modello unito)"),
                     colour: str = typer.Option("none", help="colours of the sources matched to the reference on "
                                                             "their overlaps: none, gain (RGB gain and offset) or lab "
                                                             "(mean and spread in CIE Lab)"),
                     reference: Optional[str] = typer.Option(None, help="the chunk whose colours the others take "
                                                                        "(default the first)"),
                     match_distance: Optional[float] = typer.Option(None, help="metres between two vertices taken "
                                                                               "for the same place in --colour"),
                     remove_overlaps: bool = typer.Option(False, help="faces of a source lying on the surface of a "
                                                                      "source before it in --priority left out"),
                     priority: Optional[str] = typer.Option(None, help="order of the sources for --remove-overlaps "
                                                                       "(comma separated; default the order given)"),
                     overlap_distance: Optional[float] = typer.Option(None, help="metres from a kept surface within "
                                                                                 "which a face is an overlap"),
                     pages: str = typer.Option("keep", help="texture pages: keep (as they are) or repack (packed "
                                                            "together on pages of at most --page-size)"),
                     page_size: int = typer.Option(8192, help="side of a repacked page in pixels"),
                     max_page: Optional[int] = typer.Option(None, help="pages reduced to at most this side (pixels)"),
                     activate: bool = typer.Option(False, help="make the new chunk the active one")):
    """The models of several chunks as ONE textured model of a new chunk (each in its chunk's frame; open edges
    and gaps left open), the source chunk of every face kept as its group (export-model --groups source)."""
    from .engine.model_merge import merge_models

    doc = Document.open(project)
    order = [c.strip() for c in priority.split(",") if c.strip()] if priority else None
    try:
        merged, r = merge_models(doc, chunks, label=label, reference=reference, colour=colour,
                                 match_distance=match_distance, remove_overlaps=remove_overlaps, priority=order,
                                 overlap_distance=overlap_distance, pages=pages, page_size=page_size,
                                 max_page=max_page, progress=print_progress)
    except (ValueError, KeyError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    if activate:
        doc.chunk = merged
    doc.save()
    typer.echo(f"[{merged.key}] {merged.label}: {r['faces']:,} faces, {r['vertices']:,} vertices, {r['pages']} "
               f"texture pages ({r['page_layout']})")
    for s in r["sources"]:
        typer.echo(f"  {s['chunk']}: {s['kept']:,} of {s['faces']:,} faces kept")
    for c in r["colours"]:
        if c["fit"]:
            typer.echo(f"  colours of {c['chunk']}: {c['pairs']} pairs, difference {c['fit']['before']:.1f} -> "
                       f"{c['fit']['after']:.1f} (Lab)")
    for w in r["warnings"]:
        typer.echo(f"  warning: {w}")


@app.command()
def append(project: Path,
           sources: list[Path] = typer.Argument(..., help="projects to append: .ppg, Metashape .psx, WebODM all.zip "
                                                          "or task folder"),
           chunks: Optional[str] = typer.Option(None, help="only the chunks with these names (comma separated) of the "
                                                           ".ppg and .psx sources"),
           webodm: bool = typer.Option(False, help="every source is a WebODM/ODM task (all.zip or task folder)")):
    """Append other projects, each chunk of them as a new chunk of PROJECT (as Metashape's File > Append).

    The sources are only read; the chunks keep their frame and CRS: align or merge them next (align-chunks,
    merge-chunks). A source that fails does not stop the others (exit code 1 at the end)."""
    from .core.append import append_projects, write_sections

    doc = Document.open(project)
    wanted = [c.strip() for c in chunks.split(",") if c.strip()] if chunks else None
    results = append_projects(doc, sources, chunks=wanted, webodm=webodm, progress=print_progress)
    if any(r.chunks for r in results):
        doc.save()
        for prefix, sections in results.sections:
            added = write_sections(doc, sections, prefix)
            if added:
                typer.echo(f"{prefix}: {len(added)} sections added")
    doc.settle_removals()  # the journal of the append: the project as saved lists the chunks
    for r in results:
        if r.error:
            typer.echo(f"{r.path}: not appended: {r.error}", err=True)
            continue
        for ch in r.chunks:
            products = ", ".join(sorted(k for k in ch.products if k != "reconstruction")) or "-"
            typer.echo(f"{r.path.name}: [{ch.key}] {ch.label}: {len(ch.cameras)} photos, CRS {ch.crs}, "
                       f"products: {products}")
        if r.ties:
            typer.echo(f"{r.path.name}: {r.ties} ties between the chunks appended, copied with them")
        for w in r.warnings:
            typer.echo(f"  warning: {w}")
        for name in r.ties_dropped:
            typer.echo(f"  warning: tie «{name}» not copied: it goes to a chunk not appended")
        if r.heights:
            typer.echo(f"{r.path.name}: {r.heights} stable heights between the chunks appended, copied with them")
        for name in r.heights_dropped:
            typer.echo(f"  warning: stable height «{name}» not copied: it goes to a chunk not appended")
    typer.echo("Nothing was aligned: use align-chunks or merge-chunks to bring the chunks together.")
    if any(r.error for r in results):
        raise typer.Exit(1)


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
           groups: str = typer.Option("none", help="--model OBJ: one group (o/g) per source chunk of a merged model "
                                                   "or object of an imported OBJ (source), per connected piece "
                                                   "(component), or none"),
           labels: Optional[Path] = typer.Option(None, help="--model OBJ: a group per face label, from a .npy file or "
                                                            "a text file with one label per line (one per face)"),
           max_texture: Optional[int] = typer.Option(None, help="--model: texture pages reduced to at most this side "
                                                                "(pixels) in the copies"),
           chunk: Optional[str] = None):
    """Export products in world coordinates (optionally cut by a box or a polygon: see also `ppg crop`).
    A model ending in .glb is a binary glTF, a cloud ending in .copc.laz a Cloud Optimized Point Cloud."""
    _, ch = _open(project, chunk)
    face_labels = None
    if labels is not None:
        import numpy as np

        face_labels = (np.load(labels, allow_pickle=False) if labels.suffix.lower() == ".npy"
                       else np.array([ln.strip() for ln in labels.read_text(encoding="utf-8").splitlines() if ln.strip()]))
    region = _region(None, bbox, wkt, None) if (bbox or wkt) else None
    if tiles:
        typer.echo(str(ch.export_3dtiles(tiles, progress=print_progress)))
    if i3s:
        typer.echo(str(ch.export_i3s(i3s, mode="local" if i3s_local else "global", progress=print_progress)))
    for value, fn in ((ortho, lambda path: ch.export_orthomosaic(path, region=region, cog=cog)),
                      (dem, lambda path: ch.export_dem(path, region=region, cog=cog if cog not in ("jpeg", "webp")
                                                       else "zstd")),
                      (model, lambda path: ch.export_model(path, region=region, groups=groups, labels=face_labels,
                                                           max_texture=max_texture)),
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
            versions.set_active_name(ch, k, versions.label("Ritagliato"))
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


def _elevation_id(ch, which: str) -> str:
    """The id of an elevation or section from its id or its name (a unique match)."""
    from .engine import elevation as E

    entries = E.elevations(ch)
    for e in entries:
        if e["id"] == which:
            return e["id"]
    found = [e for e in entries if e.get("name") == which]
    if len(found) == 1:
        return found[0]["id"]
    names = ", ".join(f"{e.get('name')} ({e['id']})" for e in entries) or "none"
    why = "is ambiguous" if found else "is not an elevation of the chunk"
    typer.echo(f"error: '{which}' {why}; the elevations are: {names}", err=True)
    raise typer.Exit(1)


@app.command()
def patch(project: Path,
          photos: Optional[str] = typer.Option(None, help="labels of the photos for the area, comma separated"),
          polygon: Optional[str] = typer.Option(None, help="'x1,y1 x2,y2 x3,y3 ...' in the chunk CRS "
                                                           "(with --elevation: in metres of the elevation, u,v)"),
          bbox: Optional[str] = typer.Option(None, help="'xmin,ymin,xmax,ymax'"),
          wkt: Optional[str] = typer.Option(None, help="a WKT polygon"),
          geojson: Optional[Path] = typer.Option(None, help="a GeoJSON file (its polygons, merged)"),
          exclude: bool = typer.Option(False, help="every photo but these"),
          ground: bool = typer.Option(False, help="project on the ground around the area instead of the DEM: trees, "
                                                  "vehicles and people as the photo shows them; fills DEM holes "
                                                  "(not on an elevation)"),
          texture: bool = typer.Option(False, help="also colour the texture of the model in the area "
                                                   "(ignored on an elevation)"),
          suggest: bool = typer.Option(False, help="list the photos that frame the area, best first"),
          remove: Optional[str] = typer.Option(None, help="take a patch away: its id, or 'all'"),
          list_patches: bool = typer.Option(False, "--list", help="list the patches"),
          elevation: Optional[str] = typer.Option(None, help="work on the patches of this elevation or section "
                                                             "(its id or its name) instead of the orthomosaic"),
          chunk: Optional[str] = None):
    """Patches of the orthomosaic, or of an elevation: the colours of an area from chosen photos (as Metashape's
    "assign images"). The orthophoto is updated only where patches changed."""
    from .engine import patches as P

    doc, ch = _open(project, chunk)
    frame = None
    if elevation:
        if ground:
            typer.echo("error: --ground does not apply to an elevation (its surface is the relief)", err=True)
            raise typer.Exit(1)
        if texture:
            typer.echo("note: --texture is ignored on an elevation")
            texture = False
        frame = _elevation_id(ch, elevation)

    def update():
        if not frame:
            return ch.update_orthomosaic(progress=print_progress)
        try:  # the elevation was built skipping the photos that cannot be read: so is its update
            return ch.update_elevation(frame, progress=print_progress, skip_unreadable=True)
        except RuntimeError as exc:  # e.g. its orthophoto is missing
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from None

    if list_patches:
        for p in P.patches(ch, frame=frame):
            how = "only" if p["mode"] == "assign" else "all but"
            typer.echo(f"{p['id']}: {how} {', '.join(p['photos'])} on the {'relief' if frame else p.get('surface', 'dem')}, "
                       f"{len(p['polygon'])} vertices")
        return
    if remove:
        if remove == "all":
            P.clear_patches(ch, frame=frame)
        else:
            P.remove_patch(ch, remove, frame=frame)
        typer.echo(str(update()))
        doc.save()
        return
    region = _region(polygon, bbox, wkt, geojson)
    if region.geom_type != "Polygon":
        region = max(region.geoms, key=lambda g: g.area)
    ring = [list(xy[:2]) for xy in region.exterior.coords[:-1]]
    if suggest:
        for d in P.photos_for_area(ch, ring, frame=frame):
            towards, product = ("view", "the elevation") if frame else ("vertical", "the orthomosaic")
            typer.echo(f"{d['label']}: frames {100 * d['coverage']:.0f}% of the area, {d['angle']:.0f} deg from the "
                       f"{towards}, {100 * d['gsd']:.2f} cm/px, {product} takes {100 * d['used']:.0f}% from it")
        return
    if not photos:
        raise typer.BadParameter("give --photos (--suggest lists the photos of the area)")
    labels = [x.strip() for x in photos.split(",") if x.strip()]
    mode = "exclude" if exclude else "assign"
    p = P.add_patch(ch, ring, labels, mode, "ground" if ground else "dem", frame=frame)
    typer.echo(f"patch {p['id']}")
    typer.echo(str(update()))
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
    if not info.get("applied", True):
        typer.echo(f"{info['observations']:,} observations; colour scatter {info['rms_before']:.3f}: the correction "
                   "does not lower it and is not applied")
        return
    gains = [sum(g) / 3 for g in info["gains"].values()]
    typer.echo(f"{info['observations']:,} observations; colour scatter {info['rms_before']:.3f} -> "
               f"{info['rms_after']:.3f}; gains {min(gains):.2f}-{max(gains):.2f}")
    if info.get("from_neighbours"):
        typer.echo(f"{len(info['from_neighbours'])} photos with too few observations take the gain of their neighbours")
    from .engine.colour import _falloff

    for sensor, b in info["vignetting"].items():
        reach = min(1.0, (info.get("vignetting_radius") or {}).get(sensor) or 1.0)
        typer.echo(f"sensor {sensor}: falloff {float(_falloff(b, reach)):+.3f} (log) at {reach:.2f} of the half diagonal")


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
    """The plugins of the index of the plugin repository (for those installed: is there a newer version)."""
    from .plugins import Manager, index, updates

    try:
        entries = index.fetch()
    except index.PluginError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    manager = Manager()
    installed = {i.name: i for i in manager.discover()}
    ready = updates.staged(manager.folder)
    for e in entries:
        problem = e.problem()
        line = f"{e.name:28} {e.version:10} {e.title or e.description}" + (f"  [{problem}]" if problem else "")
        info = installed.get(e.name)
        what = updates.status(e, info, ready, manager.folder)[0] if info is not None else ""
        if what == "staged":
            line += f"  (update to {e.version} ready: active from the next start)"
        elif what == "available":
            line += f"  (installed {info.version}: can be updated)"
        typer.echo(line)


@plugins_app.command("update")
def plugins_update(dry_run: bool = typer.Option(False, "--dry-run", help="only say which plugins could be updated")):
    """Download the newer versions of the plugins installed by the program (active from the next start).

    A plugin installed with pip is never touched; a version that needs a newer program is only reported."""
    from .plugins import index, updates

    try:
        result = updates.run(update=not dry_run)
    except index.PluginError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    for name, old, new in result.updated:
        typer.echo(f"{name} {old} -> {new} (active from the next start)")
    for name, old, new in result.pending:
        typer.echo(f"{name} {old} -> {new} ready: active from the next start")
    for name, old, new in result.available:
        typer.echo(f"{name} {old} -> {new} can be updated")
    for name, version, problem in result.waiting:
        typer.echo(f"{name} {version} waits: {problem}")
    for name, message in result.failed:
        typer.echo(f"{name} failed: {message}", err=True)
    if not result and not result.pending:
        typer.echo("The plugins are up to date.")
    if result.failed:
        raise typer.Exit(1)


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


# ---------------------------------------------------------------- assistant

KIND_HELP = "local (a server on this computer), claude or openai"
_LAST_SENT: list[str] = []  # the sent log of the last `ask` of this process


def _assistant_provider(kind: str, model: str, base_url: str):
    from .assistant.providers import ProviderError, make_provider

    try:
        return make_provider(kind, model, base_url)
    except ProviderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)


@assistant_app.command("ask")
def assistant_ask(project: Path = typer.Argument(..., help="the .ppg project the question is about"),
                  question: str = typer.Argument(..., help="the question, in quotes"),
                  kind: str = typer.Option("local", help=KIND_HELP),
                  model: str = typer.Option("", help="the model (default: the first of a local server, "
                                                     "claude-opus-5-5 for Claude)"),
                  base_url: str = typer.Option("", help="address of the server, e.g. http://127.0.0.1:8080/v1 (a "
                                                        "local one only on this computer; default: the usual ports)"),
                  lang: str = typer.Option("it", help="language of the conversation: it or en"),
                  show_sent: bool = typer.Option(False, "--show-sent", help="also print what was sent")):
    """Ask the assistant one question about a project and print the answer (and what it read). Here the assistant
    only reads: its actions (showing, choosing photos, stones, builds, SAM, exports, scripts) need the window and are
    not offered - in the Assistente pane of ppg-gui they are."""
    from .assistant import inventory
    from .assistant.knowledge import default_index
    from .assistant.session import Assistant

    doc = Document.open(project)
    provider = _assistant_provider(kind, model, base_url)
    assistant = Assistant(provider, lambda: doc, default_index("it" if lang.startswith("it") else "en"),
                          language=lang)
    turn = assistant.ask(question, inventory=inventory.snapshot(doc))  # the project alone: no window here
    _LAST_SENT[:] = assistant.sent_log
    if turn.note:
        typer.echo(turn.note)
    if turn.text:
        typer.echo(turn.text)
    if show_sent:
        typer.echo("\n--- sent ---\n" + "\n\n".join(_LAST_SENT))
    if turn.error:
        typer.echo(turn.error, err=True)
        raise typer.Exit(1)


@assistant_app.command("sent")
def assistant_sent():
    """What the last `ask` of this process sent, entry by entry (with a local model nothing left: marked so)."""
    if not _LAST_SENT:
        typer.echo("Nothing sent in this process: `ppg assistant ask --show-sent` prints it with the answer.")
        return
    typer.echo("\n\n".join(_LAST_SENT))


@assistant_app.command("models")
def assistant_models(kind: str = typer.Option("local", help=KIND_HELP),
                     base_url: str = typer.Option("", help="address of the server (local: default the usual ports)")):
    """The models of a local server (or of every local server that answers), of Claude or of the OpenAI account."""
    from .assistant.providers import OpenAICompatible, ProviderError, probe_local

    try:
        if kind == "local" and not base_url:
            found = probe_local()
            if not found:
                typer.echo("No local server answers (LM Studio, llama.cpp, Ollama, Unsloth).", err=True)
                raise typer.Exit(1)
            for name, url, models in found:
                typer.echo(f"{name} {url}")
                for m in models:
                    typer.echo(f"  {m}")
            return
        if kind == "local":
            models = OpenAICompatible(base_url, "", kind="local").list_models()
        else:
            models = _assistant_provider(kind, "", base_url).list_models()
    except ProviderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    for m in models:
        typer.echo(m)


@assistant_app.command("eval")
def assistant_eval(project: Path = typer.Argument(..., help="the .ppg project (the demo: ppg demo FOLDER --process)"),
                   kind: str = typer.Option("local", help=KIND_HELP + ", or reference (the answers of the set)"),
                   model: str = typer.Option("", help="the model"),
                   base_url: str = typer.Option("", help="address of the server"),
                   cases: Optional[Path] = typer.Option(None, help="the evaluation set (default: "
                                                                   "the set shipped with the program)"),
                   only: str = typer.Option("all", help="all, questions, scripts or actions"),
                   lang: str = typer.Option("it", help="language of the questions: it or en"),
                   out: Optional[Path] = typer.Option(None, help="write the results to this JSON file"),
                   record: bool = typer.Option(False, "--record", help="keep the score as the last evaluation of "
                                                                       "this kind in the settings of the interface")):
    """Evaluate a model: the questions, the script tasks and the actions of the set on a project (the scripts run on
    copies, the actions are only recorded: the project is never changed). Prints one line per case and the score."""
    from .assistant import evaluate

    path = cases or evaluate.CASES
    if kind == evaluate.REFERENCE:
        if record:
            typer.echo("The reference answers of the set are not a model: nothing to record.", err=True)
            raise typer.Exit(1)
        provider = evaluate.CasesProvider(path, language=lang)
    else:
        provider = _assistant_provider(kind, model, base_url)
    try:
        result = evaluate.run_eval(provider, project, path, kinds=only, progress=print_progress, language=lang)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo(evaluate.table(result))
    if out is not None:
        evaluate.write_results(result, out)
        typer.echo(f"results written to {out}")
    if record:
        try:
            from .gui import app_settings

            score = evaluate.record_score(app_settings(), kind, result)
        except (ImportError, ValueError) as exc:
            typer.echo(f"not recorded: {exc}", err=True)
            raise typer.Exit(1)
        typer.echo(f"score {score:g} recorded for {kind}")


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
    for updated, old, new in m.applied:  # downloaded by an earlier run, put in place by this start
        typer.echo(f"plugin {updated} updated from {old} to {new}", err=True)
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
                                                                     "csv, or flir (FLIR radiometric JPEG with a world file: one "
                                                                     "file when the orthomosaic fits in one, else tiles or --single)"),
            tile: Optional[int] = typer.Option(None, help="export flir: the side of the tiles, cells (default: the fewest "
                                                          "tiles a file can hold, as square as possible; a side given "
                                                          "makes exact tiles, at most 2895, even of an orthomosaic "
                                                          "that fits in one file)"),
            single: bool = typer.Option(False, "--single", help="export flir: when the orthomosaic does not fit in one file, "
                                                                "write one with larger cells (the temperatures averaged over "
                                                                "blocks of k x k cells) instead of tiles"),
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
        typer.echo(str(th.export(ch, target, fmt, tile=tile, palette=palette, single=single)))
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
    shared disk. Key points and PatchMatch depth maps come here in units of a few photos, the
    orthomosaic and the texture in units of tiles and of rows of cells."""
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
            # the chunk of a step (orthomosaic, texture) before the units that need it
            run(rsync + [str(files_dir) + "/nodes/state/", f"{host}:{remote_files}/nodes/state/"])
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


# ----------------------------------------------------------------------------- GNSS positions of the photos

def _metres(values) -> str:
    """Metres per axis as ``0.010/0.010/0.018``, ``-`` for none."""
    return "/".join(f"{v:.3f}" for v in values) if values else "-"


@gnss_app.command("status")
def gnss_status(project: Path, chunk: Optional[str] = None):
    """Where the positions of the photos come from and how good they are, flight by flight.

    A table per flight: the photos by RTK state (fix, float, single, none = a position and no RTK data), those
    without a position, the references switched off, the median accuracy, once aligned and georeferenced how far the
    cameras are from their positions, and the kinds of height of the positions (ellipsoidal, exif, unknown)."""
    from .gnss.apply import ONLY_FIX
    from .gnss.quality import mixed_heights, summary

    _doc, ch = _open(project, chunk)
    rows = summary(ch)
    photos = sum(r["photos"] for r in rows)
    positioned = photos - sum(r["no_position"] for r in rows)
    sources: dict[str, int] = {}
    for r in rows:
        for name, n in r["sources"].items():
            sources[name] = sources.get(name, 0) + n
    where = ", ".join(f"{name} {n}" for name, n in sorted(sources.items(), key=lambda p: -p[1]))
    typer.echo(f"{ch.label}: {photos} photos, {positioned} with a position" + (f" ({where})" if where else ""))
    if not rows:
        return
    typer.echo(f"{'flight':<8}{'photos':>7}{'fix':>6}{'float':>7}{'single':>8}{'none':>6}{'no position':>13}"
               f"{'disabled':>10}  {'accuracy (m)':<22}{'rms (m)':<22}{'max (m)':<10}heights")
    for r in rows:
        s = r["status"]
        biggest = "-" if r["residual_max"] is None else f"{r['residual_max']:.3f}"
        heights = ",".join(f"{k}:{n}" for k, n in sorted(r["heights"].items(), key=lambda p: -p[1])) or "-"
        typer.echo(f"{r['flight'] or 'undated':<8}{r['photos']:>7}{s['fix']:>6}{s['float']:>7}{s['single']:>8}"
                   f"{s['none']:>6}{r['no_position']:>13}{r['disabled']:>10}  {_metres(r['accuracy_median']):<22}"
                   f"{_metres(r['residual_rms']):<22}{biggest:<10}{heights}")
    mixed = mixed_heights(ch)
    if mixed:
        typer.echo(f"warning: the heights of these positions are of different kinds ({' / '.join(mixed)}): check "
                   "them before using them together")
    kept = ch.meta.get(ONLY_FIX)
    if kept is not None:
        typer.echo(f"only fix: the references of {len(kept.get('disabled') or [])} photos are switched off")


def _gnss_failed(exc: Exception) -> NoReturn:
    from .gnss import heights

    typer.echo(f"error: {heights.in_english(str(exc))}", err=True)  # the menu path of the window, in English
    raise typer.Exit(1)


def _unmatched(names: list[str], what: str) -> None:
    if names:
        typer.echo(f"{what} ({len(names)}): " + ", ".join(names[:8]) + (" ..." if len(names) > 8 else ""))


@gnss_app.command("mrk")
def gnss_mrk(project: Path, file: Path,
             photos: Optional[Path] = typer.Option(None, "--photos", help="folder of the photos (default: the folder "
                                                   "of the MRK)"),
             chunk: Optional[str] = None):
    """Give the photos the position of their shot in a DJI Timestamp.MRK.

    The camera, not the antenna, with its RTK state and accuracy. The photos are paired by the shot number in their
    names, else by order when the MRK has as many lines as the folder has photos, and each pair is checked against
    the time of the photo (a photo of another flight keeps its position). Doing it again changes nothing."""
    from .gnss import apply as gnss

    doc, ch = _open(project, chunk)
    try:
        result = gnss.apply_mrk(ch, file, photos)
    except (OSError, ValueError) as exc:
        _gnss_failed(exc)
    if not result["matched"]:
        _gnss_failed(ValueError("no photo of the chunk found its shot in the MRK: check the folder of the photos"))
    gnss.keep_only_fix(ch)
    doc.save()
    states = ", ".join(f"{k} {n}" for k, n in sorted(result["status"].items(), key=lambda p: -p[1]))
    typer.echo(f"{result['matched']} of {result['photos']} photos took the position of their shot ({states})")
    _unmatched(result["unmatched"], "without a position from the MRK")
    _unmatched([str(i) for i in result["shots_unmatched"]], "shots of the MRK without a photo")


SHOT_SOURCES = ("mrk", "events", "ubx", "log", "events-pos")  # as gnss.sources.KINDS
LOG_MESSAGES = ("auto", "cam", "trig", "capture", "trigger")


def _lever(text: str | None) -> tuple[float, float, float] | None:
    """``--lever``: forward, right, down of the camera from the antenna in the body of the drone, in mm."""
    if text is None:
        return None
    parts = text.split(",")
    try:
        if len(parts) != 3:
            raise ValueError
        return tuple(float(part.strip()) for part in parts)
    except ValueError:
        raise typer.BadParameter(f"--lever takes F,R,D: how far the camera is from the antenna forward, right and "
                                 f"down in the body of the drone (mm), three numbers with a point for the decimals: "
                                 f"{text!r}") from None


def _approx_time(text: str | None, like: Path | None):
    """``--approx-time`` (an ISO date or date and time, GPS time) or the first epoch of the RINEX ``--like``."""
    from datetime import datetime

    from .gnss import rinex

    if text is not None and like is not None:
        raise typer.BadParameter("give --approx-time or --like, not both")
    if text is not None:
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            raise typer.BadParameter(f"--approx-time takes a date, or a date and time (2025-05-18 or "
                                     f"2025-05-18T10:30), not {text!r}") from None
    if like is not None:
        try:
            span = rinex.time_span(like)
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(f"--like: {exc}") from None
        if span is None:
            raise typer.BadParameter(f"--like: {like.name} has no epochs")
        return span[0]
    return None


def _log_shots(path: Path, message: str = "auto", camera: Optional[int] = None):
    """The shots of an autopilot log: PX4 (ULog) or ArduPilot (DataFlash) (:func:`.gnss.sources.log_shots`), an
    option that does not fit the log a usage error."""
    from .gnss import sources

    try:
        sources.check_log_options(path, message, camera)
    except sources.OptionError as exc:
        raise typer.BadParameter(str(exc), param_hint=exc.option) from None
    return sources.log_shots(path, message, camera)


def _shot_source(kind: str, path: Path, edge: str = "falling", message: str = "auto",
                 camera: Optional[int] = None):
    """The shots of ``path`` read as ``kind`` (:data:`SHOT_SOURCES`, :func:`.gnss.sources.read`)."""
    from .gnss import sources

    if kind == "log":
        return _log_shots(path, message, camera)
    return sources.read(kind, path, edge)


def _pairing_words(result: dict) -> list[str]:
    """How the photos were paired, for the commands that apply shots."""
    from .gnss.shots import clock_text

    lines = []
    by = result.get("by")
    if by:
        clock = result.get("clock_s")
        lines.append(f"paired by {by}" + (f", the clock of the camera {clock_text(clock)} from UTC" if clock is not None
                                          else "") + ("" if result.get("confirmed") is not False else
                                                      " - not confirmed by the times: check the positions"))
    if result.get("doubles"):
        lines.append(f"{result['doubles']} double shots left out")
    if result.get("invalid"):
        lines.append(f"{result['invalid']} shots without a valid time never paired")
    return lines


@gnss_app.command("convert")
def gnss_convert(files: list[Path] = typer.Argument(..., exists=True, dir_okay=False,
                                                     help="raw receiver logs (u-blox, RTCM 3, Septentrio, Unicore, "
                                                     "Trimble RT-17, NovAtel with --kind, a DJI D-RTK .DAT) or RINEX"),
                 kind: Optional[str] = typer.Option(None, "--kind", help="the format of the logs when it cannot be "
                                                    "told from their content (novatel)"),
                 approx_time: Optional[str] = typer.Option(None, "--approx-time", help="the day (and time) of the "
                                                           "logs, GPS time: RTCM 3 and RT-17 carry the time of the "
                                                           "week only"),
                 like: Optional[Path] = typer.Option(None, "--like", exists=True, dir_okay=False, help="a RINEX file "
                                                     "of the same day: its first epoch is the approximate time"),
                 out: Optional[Path] = typer.Option(None, "--out", file_okay=False, help="a folder to copy the "
                                                    "RINEX into (default: they stay in the cache of the program)")):
    """Convert raw receiver logs into RINEX 3.04 observations and navigation, as the PPK does.

    The conversion is kept in the cache of the program and used again while the log is the same; --out copies the
    RINEX into a folder, named after each log (two logs of the same name are refused: the first is kept). Prints the RINEX written, the epochs and the events (the shots on the
    event pin), and what the conversion has to say."""
    import shutil

    from .gnss import convert

    when = _approx_time(approx_time, like)
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
    failed = False
    written: dict[str, Path] = {}  # the names written into --out in this run (case apart) and the log of each
    for path in files:
        try:
            done = convert.to_rinex(path, kind=kind, approx_time=when, progress=print_progress)
        except (OSError, ValueError, RuntimeError) as exc:
            typer.echo(f"error: {path.name}: {exc}", err=True)
            failed = True
            continue
        obs, nav = done.obs, done.nav
        if out is not None and done.kind != "rinex":
            first = written.get(path.stem.lower())
            if first is not None:
                typer.echo(f"error: {path}: --out: {path.stem}.obs is written from {first} in this run, not written "
                           "over: give the logs different names or convert them apart", err=True)
                failed = True
                continue
            written[path.stem.lower()] = path
            obs = Path(shutil.copyfile(done.obs, out / f"{path.stem}.obs"))
            if done.nav is not None:
                nav = Path(shutil.copyfile(done.nav, out / f"{path.stem}.nav"))
        counts = ("" if done.epochs is None else f": {done.epochs} epochs, {done.events} events")
        typer.echo(f"{path.name} ({done.kind}){counts}")
        typer.echo(f"  observations: {obs}")
        if nav is not None:
            typer.echo(f"  navigation: {nav}")
        for warning in done.warnings:
            typer.echo(f"warning: {warning}")
    if failed:
        raise typer.Exit(1)


@gnss_app.command("events")
def gnss_events(file: Path = typer.Argument(..., exists=True, dir_okay=False, help="RINEX observations with events "
                                            "(epoch flag 5), a u-blox log with TIM-TM2, or RTKLIB's _events.pos"),
                edge: Optional[str] = typer.Option(None, "--edge", help="u-blox: the edge of the signal at the event "
                                                   "input (falling, the default, or rising)")):
    """List the shots on the event pin of a receiver: their number, GPS time, UTC and whether their time is valid."""
    from .gnss import convert, events, shots

    if edge is not None and edge not in events.EDGES:
        raise typer.BadParameter(f"one of {', '.join(events.EDGES)}", param_hint="--edge")
    ubx = not file.name.endswith(".pos") and convert.raw_kind(file) == "ubx"
    if edge is not None and not ubx:
        raise typer.BadParameter("the edge is that of the time marks of a u-blox log: RINEX and RTKLIB's events "
                                 "carry no edge to choose", param_hint="--edge")
    edge = edge or "falling"
    try:
        if file.name.endswith(".pos"):
            shotset = events.events_pos(file)
        elif convert.raw_kind(file) == "ubx":
            shotset = events.ubx_time_marks(file, edge)
        else:
            shotset = events.rinex_events(file)
    except (OSError, ValueError) as exc:
        _gnss_failed(exc)
    valid = sum(s.valid for s in shotset.shots)
    typer.echo(f"{file.name}: {len(shotset.shots)} events ({shotset.kind}), {valid} with a valid time")
    typer.echo(f"{'index':>7}  {'GPS week':>8} {'seconds of week':>16}  {'UTC':<26}valid")
    for s in shotset.shots:
        week = int(s.gps_time // 604800)
        index = "-" if s.index is None else str(s.index)
        typer.echo(f"{index:>7}  {week:>8} {s.gps_time - week * 604800:>16.6f}  "
                   f"{shots.utc(s).isoformat(sep=' ', timespec='microseconds'):<26}{'yes' if s.valid else 'no'}")
    for warning in shotset.warnings:
        typer.echo(f"warning: {warning}")


@gnss_app.command("log")
def gnss_log(project: Path, log: Path = typer.Argument(..., exists=True, dir_okay=False,
                                                        help="an ArduPilot log (.bin, .log) or a PX4 log (.ulg)"),
             message: str = typer.Option("auto", "--message", help="ArduPilot: cam or trig; PX4: capture or "
                                         "trigger; auto: the confirmed shots when the log has them"),
             camera: Optional[int] = typer.Option(None, "--camera", help="ArduPilot: the camera (I) of a log with "
                                                  "several"),
             photos: Optional[Path] = typer.Option(None, "--photos", file_okay=False, help="folder of the photos "
                                                   "(default: the folder of the log)"),
             by: str = typer.Option("auto", "--by", help="how the photos are paired with the shots: auto, number, "
                                    "time or order"),
             lever: Optional[str] = typer.Option(None, "--lever", help="F,R,D: the camera from the antenna in the "
                                                 "body of the drone (mm forward, right, down), turned with the "
                                                 "attitude of each shot"),
             latency: float = typer.Option(0.0, "--latency", help="ms the camera exposes after the time of the shot "
                                           "(moves the times, not the positions)"),
             dry_run: bool = typer.Option(False, "--dry-run", help="print how the photos pair with the shots and "
                                          "change nothing"),
             chunk: Optional[str] = None):
    """Give the photos the position of their shot in the log of the autopilot (ArduPilot or PX4).

    The position is the autopilot's estimate at the shot (no PPK): its height above the sea level unless the GPS of
    the log gives the separation of its geoid (PX4), so mostly heights "msl", never taken for ellipsoidal ones
    (``ppg gnss status`` lists them). With PPK, ``ppg gnss ppk --shots log`` gives the antenna of the solution at the
    times of the log instead."""
    from .gnss import apply as gnss
    from .gnss import shots as gshots

    if by not in gshots.BY:
        raise typer.BadParameter(f"one of {', '.join(gshots.BY)}", param_hint="--by")
    arm = _lever(lever)
    doc, ch = _open(project, chunk)
    try:
        shotset = _log_shots(log, message, camera)
        typer.echo(f"{log.name}: {len(shotset.shots)} shots ({shotset.kind}), "
                   f"{sum(s.valid for s in shotset.shots)} with a valid time")
        if dry_run:
            pairing = gshots.pair(ch, shotset, photos, by=by, latency_s=latency / 1000.0)
            typer.echo(f"{len(pairing.kept)} of {len(pairing.files)} photos pair with a shot (by {pairing.by})"
                       + ("" if pairing.confirmed is not False else ", not confirmed by the times"))
            for f, shot, _cam in pairing.kept[:20]:
                typer.echo(f"  {f.name} <- shot {shot.index} at {gshots.utc(shot).isoformat(sep=' ')} UTC")
            if len(pairing.kept) > 20:
                typer.echo(f"  ... and {len(pairing.kept) - 20} more")
            for warning in shotset.warnings + pairing.warnings:
                typer.echo(f"warning: {warning}")
            _unmatched([f.stem for f in pairing.photos_without_shot], "photos without a shot")
            _unmatched([str(s.index) for s in pairing.extra_shots], "shots without a photo")
            typer.echo("dry run: nothing changed")
            return
        result = gnss.apply_shots(ch, shotset, photos, by=by, latency_s=latency / 1000.0, body_lever_mm=arm)
    except (OSError, ValueError) as exc:
        _gnss_failed(exc)
    if not result["matched"]:
        _gnss_failed(ValueError(f"no photo of the chunk found its shot in {log.name}: check the folder of the photos"))
    doc.save()
    states = ", ".join(f"{k} {n}" for k, n in sorted(result["status"].items(), key=lambda p: -p[1]))
    typer.echo(f"{result['matched']} of {result['photos']} photos took the position of their shot ({states})")
    for line in _pairing_words(result):
        typer.echo(line)
    for warning in shotset.warnings + result["warnings"]:
        typer.echo(f"warning: {warning}")
    _unmatched(result["unmatched"], "without a position from the log")
    _unmatched([str(i) for i in result["shots_unmatched"]], "shots of the log without a photo")


def _base_position(text: str) -> tuple[float, float, float]:
    """``--base-position``: latitude, longitude and height above the ellipsoid as three numbers with a point for the
    decimals and commas between them (spaces allowed)."""
    parts = text.split(",")
    try:
        if len(parts) != 3:
            raise ValueError
        lat, lon, h = (float(part.strip()) for part in parts)
    except ValueError:
        raise typer.BadParameter(f"--base-position takes LAT,LON,H: the latitude and longitude (degrees) and the "
                                 f"height above the ellipsoid (m) of the base, three numbers with a point for the "
                                 f"decimals: {text!r}") from None
    return lat, lon, h


def _frame_words(frame: str | None, epoch: float | None = None) -> str:
    """The reference frame of a position, and the epoch of its coordinates in one that moves with time, as the commands
    write them."""
    from .gnss import frames

    if frame in (None, frames.UNKNOWN, frames.WGS84):
        return "frame unknown: WGS 84"
    return f"{frame} at {epoch:.4f}" if epoch is not None and frame in frames.DYNAMIC else str(frame)


def _base_words(base: dict) -> str:
    """Where the position of a base comes from (``Solution.base``), its frame and the epoch of its coordinates."""
    source, detail = base.get("position_source"), base.get("detail") or {}
    station = detail.get("station") or base.get("name") or "?"
    said = {"header": "from its RINEX header", "typed": "typed",
            "station": f"the permanent station {station}, coordinates from "
                       f"{(detail.get('coordinates') or {}).get('source') or '?'}",
            "baseline": f"by a static baseline from the station {station}",
            "ppp": f"by a static PPP on the {detail.get('kind') or '?'} products of {detail.get('centre') or '?'}",
            }.get(source, "from where nobody knows")
    return f"{said}, {_frame_words(base.get('frame'), base.get('epoch'))}"


def _station_by_id(sid: str):
    """The permanent station ``sid`` of the lists of the EPN, the IGS and RING: its 9-character id, or its first four
    characters when they name one station only; a ``ValueError`` otherwise."""
    from .gnss import stations

    wanted = sid.strip().upper()
    listed = stations.station_list()
    same = [s for s in listed if s.id.upper() == wanted]
    if not same and len(wanted) == 4:
        same = [s for s in listed if s.id.upper().startswith(wanted)]
    if len(same) == 1:
        return same[0]
    if same:
        raise ValueError(f"{sid} names {len(same)} permanent stations ({', '.join(s.id for s in same)}): give the "
                         "9-character id")
    raise ValueError(f"no permanent station {sid} in the lists of the EPN, the IGS and RING: `ppg gnss stations "
                     "--near LAT,LON` lists those near a place")


@gnss_app.command("ppk")
def gnss_ppk(project: Path,
             mrk: Optional[Path] = typer.Option(None, "--mrk", exists=True, dir_okay=False,
                                                help="the Timestamp.MRK of the flight (as --shots mrk --shots-file)"),
             rover: Path = typer.Option(..., "--rover", exists=True, dir_okay=False,
                                        help="the observations of the drone: RINEX (DJI: *_Rinex.obs beside the MRK) "
                                        "or a raw log of its receiver, converted"),
             shots_kind: Optional[str] = typer.Option(None, "--shots", help="where the times of the shots are: mrk "
                                                      "(a DJI Timestamp.MRK), events (the events of the rover's "
                                                      "RINEX), ubx (the TIM-TM2 of a u-blox log), log (an ArduPilot "
                                                      "or PX4 log), events-pos (an _events.pos of RTKLIB)"),
             shots_file: Optional[Path] = typer.Option(None, "--shots-file", exists=True, dir_okay=False,
                                                       help="the file of the shots (default for events and ubx: the "
                                                       "rover)"),
             lever: Optional[str] = typer.Option(None, "--lever", help="F,R,D: the camera from the antenna in the "
                                                 "body of the drone (mm forward, right, down), turned with the "
                                                 "attitude of each shot when the source gives it"),
             latency: float = typer.Option(0.0, "--latency", help="ms the camera exposes after the time of the "
                                           "shot"),
             edge: Optional[str] = typer.Option(None, "--edge", help="--shots ubx: the edge of the signal at the "
                                                "event input (falling, the default, or rising)"),
             message: str = typer.Option("auto", "--message", help="--shots log: as for ppg gnss log - ArduPilot: "
                                         "cam or trig; PX4: capture or trigger; auto: the confirmed shots when the "
                                         "log has them"),
             camera: Optional[int] = typer.Option(None, "--camera", help="--shots log: the camera (I) of an "
                                                  "ArduPilot log with several"),
             by: str = typer.Option("auto", "--by", help="how the photos are paired with the shots: auto, number, "
                                    "time or order"),
             approx_time: Optional[str] = typer.Option(None, "--approx-time", help="the day of the flight (GPS time), "
                                                       "for a rover or base log of RTCM 3 or RT-17 (default: from "
                                                       "the other files)"),
             base: Optional[list[Path]] = typer.Option(None, "--base", exists=True, dir_okay=False,
                                                       help="the RINEX observations of a base that recorded during the "
                                                       "flight: your own, or a permanent station; repeat it for a "
                                                       "base in several files (pieces of an hour, two days)"),
             nav: Optional[list[Path]] = typer.Option(None, "--nav", exists=True, dir_okay=False,
                                                      help="a navigation file (repeat it for several; default: those "
                                                      "beside the rover, else those beside the base)"),
             base_position: Optional[str] = typer.Option(None, "--base-position", help="LAT,LON,H: the surveyed "
                                                         "position of the mark the base stands on, height above the "
                                                         "ellipsoid; default: the position of the base's RINEX header, "
                                                         "which can be metres off"),
             base_frame: Optional[str] = typer.Option(None, "--base-frame", help="the frame of --base-position: "
                                                      "unknown (WGS 84, the default), ITRF2020, ETRF2000 (RDN2008), "
                                                      "ETRF2014 or ETRF2020"),
             base_epoch: Optional[str] = typer.Option(None, "--base-epoch", help="the epoch of the coordinates of "
                                                      "--base-position, a decimal year or a date (default in "
                                                      "ITRF2020: the middle of the drone's observations)"),
             base_station: Optional[str] = typer.Option(None, "--base-station", help="a permanent station as the "
                                                        "base (its id): its observations for the hours of the flight "
                                                        "and its coordinates in ITRF2020 are downloaded"),
             base_baseline: Optional[str] = typer.Option(None, "--base-baseline", help="your base (--base) placed by "
                                                         "a static baseline from this permanent station (its id)"),
             base_ppp: bool = typer.Option(False, "--base-ppp", help="your base (--base) placed by a static PPP on the "
                                           "precise products of the IGS and its centres"),
             atx: str = typer.Option("auto", "--atx", help="the calibration of the antennas: auto (igs20.atx when "
                                     "the program has it; a station, a baseline and a PPP download it when missing), "
                                     "download (igs20.atx downloaded once, 62 MB, when missing) or off (none, nothing "
                                     "downloaded; not with --base-ppp, which needs it)"),
             photos: Optional[Path] = typer.Option(None, "--photos", help="folder of the photos (default: the folder "
                                                   "of the MRK)"),
             replace_fix: bool = typer.Option(False, "--replace-fix", help="give the PPK position also to the photos "
                                              "with an RTK fix of the drone itself (DJI XMP or MRK), which keep it by "
                                              "default"),
             chunk: Optional[str] = None):
    """Give the photos the PPK position of their shot: RTKLIB's solution of the drone's RINEX against a base.

    The base is your own receiver at the position of its RINEX header (metres off at times: a warning says so) or at
    --base-position in the frame of its coordinates (--base-frame, --base-epoch); a permanent station near the flight
    (--base-station: its observations and its coordinates in ITRF2020 are downloaded, and the navigation of the flight
    too when none is beside the drone's file); or your own base placed by a static baseline from a station
    (--base-baseline) or by a static PPP (--base-ppp). RTKLIB computes where the antenna of the drone was against the
    base (the antenna height of the base's RINEX header is added to its position, and the phase-centre offset of its
    antenna is applied from igs20.atx: --atx), then every photo takes the camera below the antenna at the time of its
    shot in the Timestamp.MRK (the lever arm of the shot), with the status and accuracy of the solution, in the frame
    of the base; a photo shot where the solution has no position keeps the one it had, and so does a photo with an RTK
    fix of the drone itself unless --replace-fix. The photos are paired with the shots as by ``ppg gnss mrk``. RTKLIB
    works in the folder of the chunk (gnss/ppk, a solution-<date>-<time>.pos per run); it needs the native core 0.5 or
    later, or PPG_RNX2RTKP.

    The shots can come from elsewhere than a DJI MRK (--shots, --shots-file): the events of the rover's receiver (its
    RINEX, or a u-blox log: --edge), an ArduPilot or PX4 log (--message, --camera, as for ``ppg gnss log``), an
    _events.pos; then --lever places the camera from the antenna in the body of the drone, --latency adds the delay of
    the camera to the times of the shots, and --by says how the photos pair with them. A raw rover or base (u-blox,
    RTCM 3, ...) is converted to RINEX first, as ``ppg gnss convert`` does."""
    from .gnss import apply as gnss
    from .gnss import base as gbase
    from .gnss import download, frames, ppk, sources
    from .gnss import shots as gshots

    if atx not in gbase.ATX_CHOICES:
        raise typer.BadParameter(f"one of {', '.join(gbase.ATX_CHOICES)}", param_hint="--atx")
    if base_frame is not None and base_frame not in frames.FRAMES and base_frame != frames.WGS84:
        raise typer.BadParameter(f"one of {', '.join(frames.FRAMES)}", param_hint="--base-frame")
    if base_position is None and (base_frame is not None or base_epoch is not None):
        raise typer.BadParameter("--base-frame and --base-epoch are those of --base-position: give it too",
                                 param_hint="--base-frame/--base-epoch")
    try:
        epoch = frames.parse_epoch(base_epoch) if base_epoch is not None else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--base-epoch") from None
    if base_ppp and atx == "off":
        raise typer.BadParameter("a static PPP needs the antenna calibrations of the IGS (igs20.atx): the precise "
                                 "orbits are of the centres of mass of the satellites, whose antennas are up to metres "
                                 "from them - give --atx auto or download", param_hint="--atx")
    kind = shots_kind or ("mrk" if mrk is not None else None)
    if kind is None:
        _gnss_failed(ValueError("give the shots: --mrk, or --shots with --shots-file"))
    if kind not in SHOT_SOURCES:
        raise typer.BadParameter(f"one of {', '.join(SHOT_SOURCES)}", param_hint="--shots")
    if mrk is not None and (kind != "mrk" or shots_file is not None):
        _gnss_failed(ValueError("--mrk is --shots mrk --shots-file MRK: give one of the two"))
    if by not in gshots.BY:
        raise typer.BadParameter(f"one of {', '.join(gshots.BY)}", param_hint="--by")
    arm = _lever(lever)
    if edge is not None and edge not in ("falling", "rising"):
        raise typer.BadParameter("falling or rising", param_hint="--edge")
    if edge is not None and kind != "ubx":
        raise typer.BadParameter("the edge of the time marks of a u-blox log, for --shots ubx only", param_hint="--edge")
    edge = edge or "falling"
    if kind != "log" and (message != "auto" or camera is not None):
        raise typer.BadParameter("the messages and the camera of an autopilot log, for --shots log only",
                                 param_hint="--message/--camera")
    if kind == "mrk" and (arm is not None or latency or by != "auto"):
        _gnss_failed(ValueError("a DJI MRK carries the lever arm of each shot and is paired as it always was: "
                                "--lever, --latency and --by are for the other sources of shots"))
    source_file = mrk if mrk is not None else shots_file
    if source_file is None and kind in ("events", "ubx"):
        source_file = rover
    if source_file is None:
        _gnss_failed(ValueError(f"--shots {kind} needs --shots-file"))
    when = _approx_time(approx_time, None)
    position = _base_position(base_position) if base_position is not None else None
    ways = [way for way, given in (("--base-station", base_station), ("--base-baseline", base_baseline),
                                   ("--base-ppp", base_ppp), ("--base-position", base_position is not None)) if given]
    if len(ways) > 1:
        _gnss_failed(ValueError(f"give one of --base-station, --base-baseline, --base-ppp and --base-position, not "
                                f"{' and '.join(ways)}"))
    bases = list(base or [])
    if base_station and bases:
        _gnss_failed(ValueError("--base-station downloads the observations of the station for the flight: give no "
                                "--base with it"))
    if not base_station and not bases:
        _gnss_failed(ValueError("give the observations of the base (--base), or a permanent station as the base "
                                "(--base-station)"))
    mode = ("station" if base_station else "baseline" if base_baseline else "ppp" if base_ppp else
            "typed" if position is not None else "header")
    frame = base_frame or frames.UNKNOWN
    doc, ch = _open(project, chunk)
    try:
        inputs = sources.ppk_inputs(kind, source_file, rover, bases, approx_time=when, edge=edge, message=message,
                                    camera=camera, progress=print_progress, reader=_shot_source)
        shotset, rover, bases, converted = inputs.shotset, inputs.rover, inputs.bases, inputs.converted
        for done in converted:
            typer.echo(f"{done.source.name} ({done.kind}) converted: {done.epochs} epochs, {done.events} events")
        station = _station_by_id(base_station or base_baseline) if mode in ("station", "baseline") else None
        prepared = gbase.prepare_base(mode, rover, bases or None, folder=ppk.work_folder(ch).parent, station=station,
                                      typed=position, frame=frame, epoch=epoch, navs=nav, atx=atx,
                                      progress=print_progress)
        if mode == "typed" and epoch is None and frame in frames.DYNAMIC:
            typer.echo(f"the epoch of --base-position: {prepared.position.epoch:.4f}, the middle of the drone's "
                       "observations (--base-epoch to give another)")
        if prepared.downloaded:
            typer.echo(f"navigation files downloaded for the flight: "
                       f"{', '.join(path.name for path in prepared.downloaded)}")
        solution = ppk.solve(rover, prepared.bases, prepared.navs or None, prepared.position,
                             folder=ppk.work_folder(ch), atx=prepared.atx)
        inputs.record(solution)  # remembered with the files as they were named, not with their conversions
        if kind == "mrk":
            result = gnss.apply_ppk(ch, source_file, solution, photos, replace_fix=replace_fix)
        else:
            result = gnss.apply_ppk_shots(ch, shotset, solution, photos, by=by, latency_s=latency / 1000.0,
                                          body_lever_mm=arm, replace_fix=replace_fix)
    except (OSError, ValueError, RuntimeError, download.DownloadError) as exc:
        _gnss_failed(exc)
    notes = [w for done in converted for w in done.warnings] + prepared.notes
    if kind != "mrk":
        notes += shotset.warnings
    states = ", ".join(f"{k} {n}" for k, n in sorted(result["status"].items(), key=lambda p: -p[1]))
    if result["matched"]:
        typer.echo(f"{result['matched']} of {result['photos']} photos took the PPK position of their shot ({states})")
    kept = result["kept_fix"]
    if kept:
        typer.echo(f"{kept} photo{'s' if kept > 1 else ''} with an RTK fix of the drone itself kept "
                   f"{'their positions' if kept > 1 else 'its position'} (--replace-fix gives them the PPK one)")
    where = solution.base
    lon, lat, h = where["position"]
    delta = where["antenna_delta_hen"]
    baseline = result["baseline_km"]
    typer.echo(f"base {where['name']}: latitude {lat:.8f}, longitude {lon:.8f}, {h:.3f} m above the ellipsoid "
               f"({_base_words(where)}), "
               + (f"antenna {delta[0]:.3f} m above it" if delta else "no antenna height in its RINEX header")
               + (f"; {baseline:.1f} km from the flight" if baseline is not None else ""))
    typer.echo(f"solution: {solution.path}")
    for warning in notes + result["warnings"]:
        typer.echo(f"warning: {warning}")
    if not result["matched"] and not kept:
        _gnss_failed(ValueError("no photo of the chunk took a PPK position: check the folder of the photos and that "
                                f"the {'MRK' if kind == 'mrk' else 'shots'} are of the flight of the drone's "
                                "observations"))
    if result["matched"]:
        gnss.keep_only_fix(ch)
        doc.save()
    else:  # nothing changed: some photos kept their onboard fix, the others (if any) have no PPK position
        many, left = kept > 1, len(result["unmatched"])
        text = (f"no photo took a PPK position: {kept} photo{'s' if many else ''} kept {'their' if many else 'its'} "
                f"onboard RTK fix (--replace-fix gives {'them' if many else 'it'} the PPK one)")
        if left:  # in a gap of the solution, not in the chunk, without a shot or not on time: listed below
            text += f" and {left} {'are' if left > 1 else 'is'} without a PPK position"
        typer.echo(text)
    _unmatched(result["unmatched"], "without a PPK position")
    _unmatched([str(i) for i in result["shots_unmatched"]],
               f"shots of the {'MRK' if kind == 'mrk' else source_file.name} without a PPK position")


def _deviations(sd) -> str:
    return " / ".join(f"{v:.4f}" for v in sd) if sd is not None else "-"


def _your_base(path: Path, position) -> None:
    """The mark found for the user's base: where, in which frame, and how to use it in the PPK."""
    from .gnss import ppk, rinex

    name = ppk.base_name(path, rinex.header(path))
    typer.echo(f"your base {name}: latitude {position.lat:.8f}, longitude {position.lon:.8f}, {position.h:.3f} m above "
               f"the ellipsoid, {_frame_words(position.frame, position.epoch)}")


def _in_the_ppk(position, other: str) -> str:
    epoch = f" --base-epoch {position.epoch:.4f}" if position.epoch is not None else ""
    return (f"in the PPK: --base-position {position.lat:.8f},{position.lon:.8f},{position.h:.4f} --base-frame "
            f"{position.frame}{epoch} (or {other})")


@gnss_app.command("stations")
def gnss_stations(project: Optional[Path] = typer.Argument(None, help="a project: the stations near its survey (the "
                                                           "positions of its photos, or its markers)"),
                  near: Optional[str] = typer.Option(None, "--near", help="LAT,LON: the stations near this point "
                                                     "(degrees)"),
                  limit: int = typer.Option(10, "--limit", help="how many stations at most"),
                  refresh: bool = typer.Option(False, "--refresh", help="download the lists of the stations again, "
                                               "whatever the age of the saved ones"),
                  chunk: Optional[str] = None):
    """The permanent GNSS stations (EPN, IGS, RING) nearest to a survey or to a point, within 200 km.

    Their id, the distance, their networks and their name; the files of a station for the hours of a flight are looked
    for when it is used (ppg gnss ppk --base-station, ppg gnss baseline)."""
    from .gnss import download, heights, stations

    if near is not None:
        try:
            lat, lon = (float(part.strip()) for part in near.split(","))
        except ValueError:
            raise typer.BadParameter(f"LAT,LON: two numbers in degrees, not {near!r}", param_hint="--near") from None
    elif project is not None:
        _doc, ch = _open(project, chunk)
        try:
            place = heights.survey_place(ch)
        except (ValueError, RuntimeError, OSError) as exc:  # PROJ's: a system it cannot read
            _gnss_failed(ValueError(f"where the survey of {ch.label} is could not be read ({exc}): give --near "
                                    "LAT,LON"))
        if place is None:
            _gnss_failed(ValueError(f"{ch.label} has no position on the earth (photos or markers): give --near "
                                    "LAT,LON"))
        lon, lat, _frame = place
    else:
        raise typer.BadParameter("give a project, or --near LAT,LON", param_hint="--near")
    try:
        found = stations.nearby(lat, lon, limit, stations=stations.station_list(refresh=refresh))
    except (download.DownloadError, ValueError, OSError) as exc:
        _gnss_failed(exc)
    if not found:
        typer.echo(f"no permanent station within 200 km of latitude {lat:.4f}, longitude {lon:.4f}")
        return
    typer.echo(f"{'station':<10} {'km':>8}  {'networks':<14} name")
    for station, km in found:
        typer.echo(f"{station.id:<10} {km:>8.1f}  {','.join(station.networks):<14} {station.name}")


@gnss_app.command("baseline")
def gnss_baseline(base: list[Path] = typer.Argument(..., exists=True, dir_okay=False,
                                                    help="the RINEX observations of your base: one file, or several "
                                                    "that make it"),
                  station: str = typer.Option(..., "--station", help="the permanent station (its id)"),
                  folder: Optional[Path] = typer.Option(None, "--folder", help="where the files of the station are "
                                                        "downloaded and RTKLIB works (default: baseline beside the "
                                                        "first file of the base)")):
    """The mark of your own base by a static baseline from a permanent station near it.

    RTKLIB solves your base, standing still for the session, against the station (its observations and its coordinates
    in ITRF2020 are downloaded): the mark is in the frame and at the epoch of the coordinates of the station. An hour
    or more of observations together, and a station within 20 km, make it fixed and centimetre-good."""
    from .gnss import base as gbase
    from .gnss import download, ppk

    try:
        chosen = _station_by_id(station)
        found = gbase.baseline(base, chosen, folder or base[0].parent / "baseline", progress=print_progress)
    except (OSError, ValueError, RuntimeError, download.DownloadError) as exc:
        _gnss_failed(exc)
    _your_base(base[0], found.position)
    typer.echo(f"{ppk.status(found.q)} (Q={found.q}), {found.baseline_km:.3f} km from the station {chosen.id}, "
               f"{found.hours:.2f} hours together; standard deviations {_deviations(found.sd)} m (east / north / up)")
    for warning in found.warnings:
        typer.echo(f"warning: {warning}")
    typer.echo(f"solution: {found.solution}")
    typer.echo(_in_the_ppk(found.position, f"--base-baseline {chosen.id}"))


@gnss_app.command("ppp")
def gnss_ppp(base: list[Path] = typer.Argument(..., exists=True, dir_okay=False,
                                               help="the RINEX observations of your base: one file, or several that "
                                               "make it"),
             folder: Optional[Path] = typer.Option(None, "--folder", help="where the precise products are put and "
                                                   "RTKLIB works (default: ppp beside the first file of the base)")):
    """The mark of your own base by a static PPP on the precise products of the IGS and its centres.

    The final orbits and clocks of ESA, CODE or the IGS, else the rapid ones of the IGS, and igs20.atx are downloaded;
    the mark is in ITRF2020 at the epoch of the middle of the session: 10-20 cm with less than 2 hours, a few
    centimetres with 4 hours or more (a static baseline from a station nearby is better under 4 hours)."""
    from .gnss import download
    from .gnss import ppp as gppp

    try:
        found = gppp.solve_static(list(base), folder or base[0].parent / "ppp", progress=print_progress)
    except (OSError, ValueError, RuntimeError, download.DownloadError) as exc:
        _gnss_failed(exc)
    _your_base(base[0], found.position)
    typer.echo(f"static PPP on the {found.products.kind} products of {found.products.centre}, {found.hours:.2f} "
               f"hours; standard deviations {_deviations(found.position.sd)} m (east / north / up)")
    for warning in found.warnings:
        typer.echo(f"warning: {warning}")
    typer.echo(f"solution: {found.solution}")
    typer.echo(_in_the_ppk(found.position, "--base-ppp"))


@gnss_app.command("atx")
def gnss_atx(download_it: bool = typer.Option(False, "--download", help="download igs20.atx (62 MB) when the "
                                              "program has none, or one older than 30 days")):
    """igs20.atx, the antenna calibrations of the IGS, which the PPK applies to the antenna of the base: where the
    program keeps it, or how to have it."""
    from datetime import datetime

    from .gnss import atx as antex
    from .gnss import download

    if download_it:
        try:
            antex.atx_file(progress=print_progress)
        except (download.DownloadError, ValueError, OSError) as exc:
            _gnss_failed(exc)
    kept = antex.cached_atx()
    if kept is None:
        typer.echo("igs20.atx: not in the program; `ppg gnss atx --download` downloads it (62 MB): with it the PPK "
                   "applies the phase-centre offset of the antenna of the base")
        return
    typer.echo(f"igs20.atx: {kept} (of {datetime.fromtimestamp(kept.stat().st_mtime):%Y-%m-%d})")


@gnss_app.command("geoid")
def gnss_geoid(download_it: bool = typer.Option(False, "--download", help="download the grid of the geoid EGM2008 "
                                                "(81 MB, public domain) into the folder of the program")):
    """The grid of the geoid EGM2008, which heights above the sea level (EPSG:...+3855) need: installed or not, and
    where PROJ finds the grids of the program."""
    from .gnss import download, heights

    if download_it:
        try:
            path = heights.download_geoid(print_progress)
        except (download.DownloadError, ValueError, OSError) as exc:
            _gnss_failed(exc)
        typer.echo(f"EGM2008 geoid downloaded: {path}")
        return
    if heights.geoid_available():
        typer.echo(f"EGM2008 geoid ({heights.GEOID_GRID}): installed")
    else:
        typer.echo(f"EGM2008 geoid ({heights.GEOID_GRID}): not installed; `{heights.DOWNLOAD_COMMAND}` downloads it "
                   f"({round(heights.GEOID_BYTES / 1e6)} MB, public domain), as does "
                   f"{heights.DOWNLOAD_IN_THE_PROGRAM_EN} in the window: without it heights above the geoid are refused, "
                   "never passed off as heights above the ellipsoid")
    typer.echo(f"grids of PROJ for the program: {heights.grid_folder()} (a grid of your own, such as ITALGEO2005, "
               "is copied there)")


# what PROJ raises for a system or a conversion it cannot make (pyproj's CRSError and ProjError are RuntimeErrors): the
# commands say it, never with a traceback
_PROJ_FAILURES = (ValueError, RuntimeError, OSError)


def _crs_status(ch) -> None:
    """The system of the chunk, the kind of its heights, the frames of the positions of its photos, the systems of a
    survey where it is, and what does not fit between the heights (heights.check)."""
    from collections import Counter

    from .core import crs as crs_mod
    from .gnss import frames, heights

    if crs_mod.is_local(ch.crs):
        typer.echo(f"{ch.label}: {ch.crs} (local coordinates)")
    else:
        try:
            kind = {heights.ELLIPSOIDAL: "above the ellipsoid", heights.ORTHOMETRIC: "above the geoid",
                    heights.NONE: "none of its own"}[heights.height_kind(ch.crs)]
            typer.echo(f"{ch.label}: {ch.crs} ({crs_mod.to_pyproj(ch.crs).name}), heights {kind}")
        except _PROJ_FAILURES as exc:
            typer.echo(f"{ch.label}: {ch.crs}, a system PROJ cannot read ({exc}): `ppg gnss crs --set CRS` puts the "
                       "chunk into another one")
    placed = [cam for cam in ch.cameras if cam.reference.location]
    counts = Counter(frames.position_frame(cam.meta.get("gnss")) for cam in placed)
    parts = []
    for frame, n in sorted(counts.items(), key=lambda pair: -pair[1]):
        epochs = sorted(float(e) for cam in placed if frames.position_frame(cam.meta.get("gnss")) == frame
                        and (e := (cam.meta.get("gnss") or {}).get("epoch")) is not None)
        when = (f" (epochs {epochs[0]:.4f} to {epochs[-1]:.4f})" if frame in frames.DYNAMIC and epochs else "")
        parts.append(f"{_frame_words(frame)} {n}{when}")
    typer.echo(f"positions of the photos: {', '.join(parts) if parts else 'none'}")
    try:
        place = heights.survey_place(ch)
        if place is not None:
            lon, lat, _frame = place
            proposed = heights.survey_crs(lon, lat, geoid=False)  # the place decides
            if heights.in_europe(lon, lat):
                options = ((proposed, "proposed in Europe: positions in ITRF2020 (permanent stations, PPP) and in the "
                                      "European frames (RDN2008, network RTK) arrive exactly, nothing drifts between "
                                      "flights"),
                           (heights.survey_crs(lon, lat, "WGS84", geoid=False),
                            "on WGS 84 (--frame WGS84): ITRF2020 as it is, the European frames about a metre off"))
            else:
                options = ((proposed, "proposed here, for positions in WGS 84 or ITRF2020"),
                           (heights.survey_crs(lon, lat, "ETRF2000", geoid=False),
                            "for positions in ETRF2000 (--frame ETRF2000)"))
            proposals = []
            for crs, says in options:
                if crs not in [c for c, _ in proposals]:
                    proposals.append((crs, f"{crs} ({crs_mod.to_pyproj(crs).name}) {says}"))
            typer.echo("a survey here: " + "; ".join(text for _crs, text in proposals))
            grid = ("the grid is installed" if heights.geoid_available() else
                    f"the grid is not installed: {heights.DOWNLOAD_COMMAND}")
            typer.echo(f"with heights above the sea level: +3855 (EGM2008; {grid})")
    except _PROJ_FAILURES as exc:
        typer.echo(f"warning: where the survey is could not be read, so no system is proposed: {exc}")
    try:
        found = heights.check(ch)
    except _PROJ_FAILURES as exc:
        found = [f"the heights of the photos and of the system could not be compared: {exc}"]
    for text in found:
        typer.echo(f"warning: {heights.in_english(text)}")


@gnss_app.command("crs")
def gnss_crs(project: Path,
             survey: bool = typer.Option(False, "--survey", help="put the chunk into the system of a survey where it "
                                         "is: in Europe a system of ETRF2000 (RDN2008 / UTM in Italy, ETRS89 / UTM "
                                         "elsewhere), where positions in ITRF2020 and in the European frames arrive "
                                         "exactly; beyond it UTM on WGS 84"),
             frame: Optional[str] = typer.Option(None, "--frame", help="with --survey: the system for positions in "
                                                 "this frame instead, WGS84 (UTM on WGS 84) or ETRF2000 (default: the "
                                                 "place decides)"),
             geoid: Optional[bool] = typer.Option(None, "--geoid/--no-geoid", help="with --survey: heights above the "
                                                  "geoid EGM2008 (+3855, its grid needed; the default), or a system "
                                                  "without heights of its own"),
             set_crs: Optional[str] = typer.Option(None, "--set", help="put the chunk into this projected system "
                                                   "(EPSG:7792, EPSG:32633+3855 ...)"),
             chunk: Optional[str] = None):
    """The coordinate system of the chunk and its heights, or the chunk put into another one.

    Without options: the system of the chunk and the kind of its heights, the frames of the positions of its photos,
    the systems of a survey where it is, and what does not fit between the heights. With --survey or --set the
    references of the markers are converted, the georeference is fitted again (or converted), what was drawn follows
    the model, and the DEM and the orthomosaic are to be built again; heights above EGM2008 need its grid
    (`ppg gnss geoid --download`)."""
    from .gnss import frames, heights

    if survey and set_crs is not None:
        raise typer.BadParameter("give --survey or --set, not both", param_hint="--set")
    if not survey and (frame is not None or geoid is not None):
        given = " and ".join(name for name, value in (("--frame", frame), ("--geoid/--no-geoid", geoid))
                             if value is not None)
        raise typer.BadParameter(f"{given} choose{'s' if ' and ' not in given else ''} the system of --survey: "
                                 + ("--set takes the system as it is written (EPSG:7792 without heights of its own, "
                                    "EPSG:7792+3855 with heights above EGM2008)" if set_crs is not None else
                                    "give --survey too"), param_hint=given)
    if frame is not None and frame not in (frames.WGS84, *frames.FRAMES):
        raise typer.BadParameter(f"WGS84 or one of {', '.join(frames.FRAMES)}", param_hint="--frame")
    doc, ch = _open(project, chunk)
    if not survey and set_crs is None:
        _crs_status(ch)
        return
    if survey:
        try:
            place = heights.survey_place(ch)
        except _PROJ_FAILURES as exc:
            _gnss_failed(ValueError(f"where the survey of {ch.label} is could not be read ({exc}): give --set CRS"))
        if place is None:
            _gnss_failed(ValueError(f"{ch.label} has no position on the earth (photos or markers): give --set CRS"))
        lon, lat, _found = place
        target = heights.survey_crs(lon, lat, frame, geoid=True if geoid is None else geoid)
    else:
        target = set_crs.strip()
    if heights.above_egm2008(target) and not heights.geoid_available():
        from .core import crs as crs_mod

        # --set takes the system as it is written: the same system without the heights; --survey has --no-geoid
        plain = f"--set {crs_mod.horizontal(target)}" if set_crs is not None else "--no-geoid"
        _gnss_failed(ValueError(f"{target} has heights above the geoid EGM2008, whose grid ({heights.GEOID_GRID}) is "
                                f"not installed: download it first with `{heights.DOWNLOAD_COMMAND}` "
                                f"({round(heights.GEOID_BYTES / 1e6)} MB), or choose a system without heights of its "
                                f"own ({plain})"))
    old = ch.crs
    try:
        result = heights.set_survey_crs(ch, target)
    except _PROJ_FAILURES as exc:
        _gnss_failed(exc)
    doc.save()
    typer.echo(f"{ch.label}: {old} -> {result['crs']}")
    if result["updated"]:
        typer.echo("the georeference was fitted again in the new system")
    if result.get("transform_rms") is not None:
        typer.echo("the georeference was converted by a similarity fitted on the extent of the chunk "
                   f"({result['transform_rms'] * 1000:.1f} mm off in plan)")
        if (result.get("transform_vertical") or 0.0) >= 0.001:
            vertical = result["transform_vertical"] * 1000
            typer.echo(f"over its {result['transform_relief']:.0f} m of relief the new scale of the plan changes the "
                       f"relief of the model, and of what was drawn with it, by {vertical:.1f} mm against the "
                       "conversion of the coordinates (at one point at most half of it), as for every georeference in "
                       "the new system")
    drawn = [kind for kind in ("units", "quote", "points", "stones", "sections", "patches", "texture_patches",
                               "colour_chart") if result["converted"].get(kind)]
    if result.get("drawings") == "follow" and result.get("drawings_lift") is not None:
        low, high = result["drawings_lift"]
        said = "what was drawn followed the model into the new system"
        if max(abs(low), abs(high)) >= 0.001:
            said += (f"; its heights changed by {(low + high) / 2:+.3f} m" if high - low < 0.001 else
                     f"; its heights changed by {low:+.3f} to {high:+.3f} m")
        if (result.get("drawings_shift") or 0.0) >= 0.001:
            said += (f"; in plan it is up to {result['drawings_shift']:.3f} m from the conversion of its coordinates: "
                     "the new georeference of the model is not only the conversion of the old one")
        typer.echo(said)
    elif result.get("drawings") == "convert" and drawn:
        typer.echo("the chunk has nothing in its internal frame to follow (model, clouds, aligned photos): what was "
                   "drawn was converted as coordinates, as the markers")
    elif result.get("drawings") == "follow" and (result.get("model_shift") or 0.0) >= 0.001:
        typer.echo(f"the model is up to {result['model_shift']:.3f} m in plan from the conversion of its old "
                   "georeference: the new georeference is not only that conversion")
    apart = result.get("markers_apart")
    if apart and apart[1] - apart[0] >= 0.01:
        typer.echo(f"the references of the markers, converted from system to system as coordinates measured on the "
                   f"ground, are up to {apart[1]:.3f} m in plan from the model in the new georeference (up to "
                   f"{apart[0]:.3f} m before)")
    follow = result.get("drawings") == "follow"  # what was drawn went with the model: only the markers were converted
    converted = [f"{kind} {n}" for kind, n in result["converted"].items() if n and (kind == "markers" or not follow)]
    followed = [f"{kind} {n}" for kind, n in result["converted"].items() if n and kind != "markers" and follow]
    if converted:
        typer.echo(f"converted: {', '.join(converted)}")
    if followed:
        typer.echo(f"with the model: {', '.join(followed)}")
    if result["rebuild"]:
        typer.echo(f"to build again in the new system: {', '.join(result['rebuild'])}")
    for kept in result["kept"]:
        typer.echo(f"kept in the old system: {kept['what']} ({', '.join(kept['names'])})")
    if result["heights_unchanged"]:
        typer.echo("warning: the old system has no heights of its own: the heights of the markers"
                   + (", spot heights and measuring points" if result.get("drawings") == "convert" else "")
                   + " pass into the new one as they are, right only if they were above the sea level")


@gnss_app.command("positions")
def gnss_positions(project: Path, file: Path,
                   crs: str = typer.Option(..., "--crs", help="coordinate system of the file: EPSG:4979 (longitude, "
                                           "latitude, ellipsoidal height), a projected system such as EPSG:32633, "
                                           "or LOCAL"),
                   height: str = typer.Option("ellipsoidal", "--height", help="ellipsoidal (GNSS) or exif (above the "
                                              "sea level)"),
                   epoch: Optional[str] = typer.Option(None, "--epoch", help="the epoch of the coordinates, a decimal "
                                                       "year or a date: positions in ITRF2020 (EPSG:9989 ...) move "
                                                       "with time (default: the EXIF date of each photo)"),
                   chunk: Optional[str] = None):
    """Give the photos named in a text file their position and accuracy.

    One line per photo: name, x, y, height and, when there are any, sx, sy, sz (standard deviations) or accuracy,
    and status; a header names the columns. Doing it again changes nothing."""
    from .gnss import frames, positions
    from .gnss.apply import keep_only_fix

    if height not in positions.HEIGHTS:
        raise typer.BadParameter(f"--height: one of {', '.join(positions.HEIGHTS)}")
    try:
        when = frames.parse_epoch(epoch) if epoch is not None else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--epoch") from None
    doc, ch = _open(project, chunk)
    try:
        result = positions.apply_positions(ch, positions.read_positions(file), crs, height=height, epoch=when)
    except (OSError, ValueError) as exc:
        _gnss_failed(exc)
    if not result["matched"]:
        _gnss_failed(ValueError("no line of the file found its photo in the chunk"))
    keep_only_fix(ch)
    doc.save()
    typer.echo(f"{result['matched']} of {result['rows']} rows found their photo")
    _unmatched(result["unmatched"], "not in the chunk")


# ------------------------------------------------------------------------------------------------ TIN
def _breaklines_file(path: Optional[Path]) -> list[dict]:
    """Break lines from a GeoJSON file: LineStrings (with z or not), property ``kind`` breakline (default),
    soft or boundary (a Polygon is a boundary)."""
    if path is None:
        return []
    from shapely.geometry import shape

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"{path}: not a GeoJSON file ({exc})") from None
    feats = data["features"] if data.get("type") == "FeatureCollection" else [data]
    out = []
    for k, f in enumerate(feats):
        geom = shape(f.get("geometry", f))
        props = f.get("properties") or {}
        kind = props.get("kind") or ("boundary" if geom.geom_type == "Polygon" else "breakline")
        parts = getattr(geom, "geoms", [geom])
        for j, g in enumerate(parts):
            coords = list(g.exterior.coords) if g.geom_type == "Polygon" else list(g.coords)
            out.append({"id": props.get("id", f"{k}.{j}"), "kind": kind, "coords": coords})
    return out


@tin_app.command("build")
def tin_build(project: Path,
              source: str = typer.Option("dem", help="dem (refinement of the DEM), cloud (ground of the dense "
                                                     "cloud) or spots (spot heights and break lines only)"),
              max_error: Optional[float] = typer.Option(None, help="dem: largest difference from the DEM (metres; "
                                                                   "default twice the DEM's noise, or 2 cm)"),
              breaklines: Optional[Path] = typer.Option(None, help="GeoJSON of the break lines: LineStrings with "
                                                                   "property kind = breakline, soft or boundary"),
              drawn: Optional[bool] = typer.Option(None, "--drawn/--no-drawn",
                                                   help="the break lines drawn on the plan of the chunk (default: "
                                                        "yes when no --breaklines file is given)"),
              boundary: Optional[list[str]] = typer.Option(None, help="the TIN stops at this polygon: WKT, GeoJSON "
                                                                      "or a GeoJSON file"),
              spots: bool = typer.Option(True, "--spots/--no-spots", help="the spot heights of the plan drawings "
                                                                          "as vertices"),
              classes: Optional[str] = typer.Option(None, help="cloud: the classes of the ground (e.g. 2,8); "
                                                               "default class 2 when the cloud has it"),
              chunk: Optional[str] = None):
    """Build the TIN of --chunk (default the active one) and keep it in the project."""
    from .engine import tin as tin_mod

    from .archaeo import breaklines as bl

    doc, ch = _open(project, chunk)
    cls = [int(c) for c in classes.split(",") if c.strip()] if classes else None
    lines = _breaklines_file(breaklines)
    if drawn or (drawn is None and breaklines is None):
        stored = bl.for_tin(bl.records(ch))
        if stored:
            typer.echo(f"{len(stored)} break lines drawn on the plan")
        lines += stored
    try:
        tin = tin_mod.build_tin(ch, source=source, max_error=max_error, breaklines=lines,
                                boundary=_polygons(boundary), spots=spots, classes=cls, progress=print_progress)
    except (RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from None
    doc.save()
    typer.echo(tin_mod.report(tin))


@tin_app.command("dem")
def tin_dem(project: Path,
            resolution: Optional[float] = typer.Option(None, help="cell size (metres; default: the grid of the DEM)"),
            name: Optional[str] = typer.Option(None, help="name of the DEM version (default: DEM dal TIN)"),
            chunk: Optional[str] = None):
    """The TIN rasterised as the active DEM; the DEM as it was stays among the versions."""
    from .engine import tin as tin_mod

    doc, ch = _open(project, chunk)
    try:
        path = tin_mod.tin_to_dem(ch, resolution=resolution, name=name, progress=print_progress)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from None
    doc.save()
    typer.echo(f"{ch.label}: DEM from the TIN -> {path} ({ch.meta['dem']['resolution']:.4g} m)")


@tin_app.command("contours")
def tin_contours(project: Path, out: Path = typer.Argument(..., help=".dxf, .gpkg or .geojson"),
                 interval: Optional[float] = typer.Option(None, help="metres (default: about 50 levels)"),
                 base: float = typer.Option(0.0, help="height of a level"),
                 index_every: int = typer.Option(5, help="an index contour every N levels"),
                 chunk: Optional[str] = None):
    """Contour lines of the TIN (exact on the triangles) written to a file."""
    from .archaeo.terrain import export_contours
    from .engine import tin as tin_mod

    _doc, ch = _open(project, chunk)
    try:
        lines = tin_mod.tin_contours(ch, interval=interval, base=base, index_every=index_every)
    except (RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from None
    export_contours(lines, out, ch.crs)
    typer.echo(f"{len(lines)} contour lines -> {out}")


@tin_app.command("volume")
def tin_volume(project: Path,
               reference: Optional[float] = typer.Option(None, help="height of the surface before the work "
                                                                    "(default: the lowest vertex as floor)"),
               plane: Optional[str] = typer.Option(None, help="a,b,c: the plane z = a x + b y + c as reference"),
               chunk: Optional[str] = None):
    """Exact cut and fill of the TIN against a height or a plane."""
    from .engine import tin as tin_mod

    _doc, ch = _open(project, chunk)
    ref = reference
    if plane:
        try:
            ref = tuple(float(v) for v in plane.split(","))
        except ValueError:
            raise typer.BadParameter("--plane: three numbers a,b,c") from None
        if len(ref) != 3:
            raise typer.BadParameter("--plane: three numbers a,b,c")
    try:
        v = tin_mod.tin_volume(ch, ref)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"area {v['area_m2']:.3f} m2, cut {v['cut_m3']:.4f} m3, fill {v['fill_m3']:.4f} m3, "
               f"net {v['net_m3']:.4f} m3 (heights {v['quota_min']:.3f} to {v['quota_max']:.3f})")


@tin_app.command("export")
def tin_export(project: Path, out: Path = typer.Argument(..., help=".dxf, .ply, .obj, .gpkg or .xml (LandXML 1.2)"),
               chunk: Optional[str] = None):
    """Write the TIN: DXF (3DFACE and break lines), PLY/OBJ mesh, GeoPackage or LandXML."""
    from .engine import tin as tin_mod

    _doc, ch = _open(project, chunk)
    tin = tin_mod.chunk_tin(ch)
    if tin is None:
        raise typer.BadParameter("build the TIN first: ppg tin build")
    try:
        path = tin_mod.export(tin, out, ch.crs)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"TIN ({tin.n_vertices} vertices, {tin.n_triangles} triangles) -> {path}")


# --------------------------------------------------------------------------- ties between chunks
ties_app = typer.Typer(help="Ties between chunks: the same point in two chunks (or a point of a chunk and its known "
                            "coordinates), then several chunks placed together in one least squares with chosen "
                            "unknowns, checked before anything is applied")
app.add_typer(ties_app, name="ties")

UNKNOWNS_HELP = ("unknowns of each chunk of the solve, e.g. A=plan,B=similarity,C=fixed (chunk labels or keys; "
                 "similarity 7, rigid 6, plan 4, shift 3, height 1, fixed 0; group: the chunk moves with its group of "
                 "chunks as one block; a group's name gives its first chunk the unknowns and the others group)")


def _group_chunks(doc, name: str) -> list:
    """The chunks of the group named ``name`` (none when no group has that name, or a chunk has that label)."""
    from .core import groups

    if any(c.label == name for c in doc.chunks):
        return []
    try:
        return groups.members(doc, name)
    except groups.GroupError:
        return []


def _ties_error(exc: Exception) -> NoReturn:
    """A ValueError/KeyError/TieError of engine.ties as ``error: ...`` and exit code 1."""
    text = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
    typer.echo(f"error: {text}", err=True)
    raise typer.Exit(1)


def _tie_chunk(doc, item: str):
    from .engine.chunks import resolve_chunk

    try:
        return resolve_chunk(doc, item)
    except (KeyError, ValueError) as exc:
        names = ", ".join(f"{c.label!r} (key {c.key})" for c in doc.chunks)
        raise typer.BadParameter(f"{exc.args[0] if exc.args else exc}; the chunks: {names}") from None


def _coordinates(text: str, what: str, need_z: bool = False) -> list[Optional[float]]:
    """``"X,Y[,Z]"`` -> ``[x, y, z or None]`` (a dot is the decimal separator: the comma separates)."""
    parts = [p.strip() for p in text.replace(";", ",").split(",")]
    if len(parts) not in ((3,) if need_z else (2, 3)) or not all(parts[:2]):
        form = "X,Y,Z" if need_z else "X,Y[,Z]"
        raise typer.BadParameter(f"{what}: coordinates as {form} (got {text!r})")
    try:
        values = [float(p) if p else None for p in parts] + [None] * (3 - len(parts))
    except ValueError:
        raise typer.BadParameter(f"{what}: coordinates are numbers with a dot as decimal separator "
                                 f"(got {text!r})") from None
    if need_z and values[2] is None:
        raise typer.BadParameter(f"{what}: the height is needed (got {text!r})")
    return values


def _internal_point(chunk, xyz: list[Optional[float]], what: str):
    """World coordinates of ``chunk`` (its system, its transform now) -> internal; a missing height read from the
    chunk's DEM, as a table imported does (:func:`engine.ties.world_to_internal`)."""
    from .engine.ties import world_to_internal

    try:
        return world_to_internal(chunk, xyz)
    except ValueError as exc:
        raise typer.BadParameter(f"{what}: {exc}") from None


def _tie_line(doc, t) -> str:
    """One line of a tie: id, name, its ends (chunk / marker), precision, state."""
    from .engine import ties as ties_mod

    def end(e, uid):
        found = ties_mod.resolve_end(doc, e, uid)
        return f"chunk {e[0]} marker {e[1]} (gone)" if found is None else f"{found[0].label} / {found[1].label}"

    a = end(t.a, t.a_uid)
    if t.b is None:
        known = ", ".join(f"{v:.3f}" for v in t.known) if t.known is not None else "-"
        b = f"known {known} ({t.crs or 'LOCAL'})"
    else:
        b = end(t.b, t.b_uid)
    state = "active" if t.active else ("inactive: " + t.broken if t.broken else "inactive")
    if t.stale:
        state += "; left out: " + "; ".join(
            f"marker {m!r} of chunk {c!r} to be measured again after the new alignment (ppg ties confirm)"
            for _code, m, c in t.stale_parts)
    return f"{t.id}  {t.name!r}: {a} <-> {b}, precision {t.precision:.3f} m, {state}"


def _find_tie(doc, ref: str, heights: bool = False):
    """A tie by id, else by its name when only one tie has it; ``heights``: the height ties count too (a tie or a
    :class:`engine.stable_height.HeightTie`)."""
    from .engine import stable_height
    from .engine import ties as ties_mod

    all_ties = ties_mod.ties_of(doc) + (stable_height.heights_of(doc) if heights else [])
    found = [t for t in all_ties if t.id == ref] or [t for t in all_ties if t.name == ref]
    if not found:
        where = "ppg ties list, ppg ties heights" if heights else "ppg ties list"
        raise typer.BadParameter(f"no tie {ref!r} (ids and names: {where})")
    if len(found) > 1:
        raise typer.BadParameter(f"{len(found)} ties are named {ref!r}: give the id ({', '.join(t.id for t in found)})")
    return found[0]


def _find_height(doc, ref: str):
    """A height tie by id, else by its name when only one has it."""
    from .engine import stable_height

    every = stable_height.heights_of(doc)
    found = [t for t in every if t.id == ref] or [t for t in every if t.name == ref]
    if not found:
        raise typer.BadParameter(f"no stable height {ref!r} (ids and names: ppg ties heights)")
    if len(found) > 1:
        raise typer.BadParameter(f"{len(found)} stable heights are named {ref!r}: give the id "
                                 f"({', '.join(t.id for t in found)})")
    return found[0]


def _height_labels(doc, h) -> tuple[str, str]:
    labels = {c.key: c.label for c in doc.chunks}
    return labels.get(h.b, f"chunk {h.b} (gone)"), labels.get(h.a, f"chunk {h.a} (gone)")


def _height_line(doc, h) -> str:
    """One line of a height tie: id, name, B on A, dz, NMAD, cells, precision, state."""
    b, a = _height_labels(doc, h)
    precision = min((x["precision"] for x in h.anchors), default=0.0)
    kind = "plane, 3 anchors" if h.plane else "offset"
    return (f"{h.id}  {h.name!r}: {b} on {a}, dz {h.dz:+.4f} m, NMAD {h.nmad:.4f} m, {h.cells} stable cells, "
            f"precision {precision:.4f} m ({kind}), {'active' if h.active else 'inactive'}")


def _parse_unknowns(doc, text: str) -> dict[int, str]:
    """``"A=plan,B=similarity"`` -> ``{chunk key: kind}``."""
    from .engine.ties import GROUP, KINDS

    out: dict[int, str] = {}
    for item in (p.strip() for p in text.split(",")):
        if not item:
            continue
        name, sep, kind = item.rpartition("=")
        name, kind = name.strip(), kind.strip().lower()
        if not sep or not name:
            raise typer.BadParameter(f"{item!r}: write CHUNK=KIND, e.g. A=plan", param_hint="--unknowns")
        if kind not in KINDS and kind != GROUP:
            raise typer.BadParameter(f"{item!r}: the unknowns are one of {', '.join((*KINDS, GROUP))}",
                                     param_hint="--unknowns")
        group_chunks = _group_chunks(doc, name)
        if group_chunks:  # a group: its first chunk takes the unknowns, the others move with it (one block)
            for i, ch in enumerate(group_chunks):
                if ch.key in out:
                    raise typer.BadParameter(f"chunk {ch.label!r} given twice", param_hint="--unknowns")
                out[ch.key] = kind if i == 0 else GROUP
            continue
        ch = _tie_chunk(doc, name)
        if ch.key in out:
            raise typer.BadParameter(f"chunk {ch.label!r} given twice", param_hint="--unknowns")
        out[ch.key] = kind
    if not out:
        raise typer.BadParameter("no chunk given", param_hint="--unknowns")
    return out


def _ties_report(doc, unknowns: dict[int, str], references: bool, robust: bool):
    """The solve and its checks: (solution, checks, leave one out, plausibility warnings)."""
    from .engine import ties as ties_mod

    solution = ties_mod.solve(doc, unknowns, references=references, robust=robust)
    checks = ties_mod.distance_checks(doc)
    loo = ties_mod.leave_one_out(doc, unknowns, references=references, robust=robust, solution=solution)
    return solution, checks, loo, ties_mod.plausibility(doc, solution)


@ties_app.command("add", context_settings={"ignore_unknown_options": True})  # "-12.5,3": a point
def ties_add(project: Path,
             chunk_a: str = typer.Argument(..., help="first chunk (label or key)"),
             point_a: str = typer.Argument(..., help="X,Y[,Z] in the world coordinates of the first chunk (no Z: "
                                                      "read from its DEM; negative values are accepted as they "
                                                      "are, or put -- before the arguments)"),
             chunk_b: str = typer.Argument(..., help="second chunk (label or key)"),
             point_b: str = typer.Argument(..., help="X,Y[,Z] of the same point in the world coordinates of the "
                                                      "second chunk"),
             precision: Optional[float] = typer.Option(None, help="precision of the tie in metres (default: two "
                                                                  "raster cells of each chunk, else 0.05)"),
             name: Optional[str] = typer.Option(None, help="name of the tie and of its markers (default: legame N)")):
    """A tie: the same point in two chunks, each in its own world coordinates (a new marker in each chunk)."""
    from .engine import ties as ties_mod

    doc = Document.open(project)
    ca, cb = _tie_chunk(doc, chunk_a), _tie_chunk(doc, chunk_b)
    pa = _internal_point(ca, _coordinates(point_a, "point of the first chunk"), "point of the first chunk")
    pb = _internal_point(cb, _coordinates(point_b, "point of the second chunk"), "point of the second chunk")
    try:
        tie = ties_mod.add_tie(doc, ca, pa, cb, pb, name=name, precision=precision)
    except (ValueError, KeyError) as exc:
        _ties_error(exc)
    doc.save()
    typer.echo(f"added {_tie_line(doc, tie)}")


@ties_app.command("known", context_settings={"ignore_unknown_options": True})  # "-12.5,3": a point
def ties_known(project: Path,
               chunk: str = typer.Argument(..., help="the chunk (label or key)"),
               point: str = typer.Argument(..., help="X,Y[,Z] in the world coordinates of the chunk (no Z: read "
                                                     "from its DEM; negative values are accepted as they are, or "
                                                     "put -- before the arguments)"),
               known: str = typer.Argument(..., help="E,N,H: the known coordinates of the point, in the chunk's "
                                                     "system"),
               precision: Optional[float] = typer.Option(None, help="precision of the known point in metres "
                                                                    "(default 0.05)"),
               name: Optional[str] = typer.Option(None, help="name of the tie and of its marker (default: legame N)")):
    """A tie towards known coordinates (a GNSS point, a benchmark): a new marker with them as its reference."""
    from .engine import ties as ties_mod

    doc = Document.open(project)
    ch = _tie_chunk(doc, chunk)
    p = _internal_point(ch, _coordinates(point, "point of the chunk"), "point of the chunk")
    xyz = _coordinates(known, "known coordinates", need_z=True)
    try:
        tie = ties_mod.add_known(doc, ch, p, xyz, name=name, precision=precision)
    except (ValueError, KeyError) as exc:
        _ties_error(exc)
    doc.save()
    typer.echo(f"added {_tie_line(doc, tie)}")


@ties_app.command("from-markers")
def ties_from_markers_cmd(project: Path,
                          chunk_a: str = typer.Argument(..., help="first chunk (label or key)"),
                          chunk_b: str = typer.Argument(..., help="second chunk (label or key)")):
    """A tie for every marker label found in both chunks (no new markers; pairs tied already are kept)."""
    from .engine import ties as ties_mod

    doc = Document.open(project)
    ca, cb = _tie_chunk(doc, chunk_a), _tie_chunk(doc, chunk_b)
    try:
        made = ties_mod.ties_from_markers(doc, ca, cb)
    except (ValueError, KeyError) as exc:
        _ties_error(exc)
    if made:
        doc.save()
    typer.echo(f"{len(made)} ties from the markers of {ca.label} and {cb.label}")
    for t in made:
        typer.echo(f"  {_tie_line(doc, t)}")


@ties_app.command("import")
def ties_import(project: Path,
                table: Path = typer.Argument(..., exists=True, dir_okay=False,
                                             help="CSV: chunk_a,x_a,y_a,z_a,chunk_b,x_b,y_b,z_b[,precision][,name] "
                                                  "(chunk_b empty: x_b,y_b,z_b are the known coordinates of the "
                                                  "point; z_a empty: read from the DEM)")):
    """Ties from a table, in the world coordinates of each chunk; nothing is added when a row is wrong."""
    from .engine import ties as ties_mod

    doc = Document.open(project)
    try:
        made = ties_mod.import_ties(doc, table)
    except (ValueError, KeyError, OSError) as exc:
        _ties_error(exc)
    if made:
        doc.save()
    typer.echo(f"{len(made)} ties imported from {table.name}")
    for t in made:
        typer.echo(f"  {_tie_line(doc, t)}")


@ties_app.command("list")
def ties_list(project: Path,
              as_json: bool = typer.Option(False, "--json", help="print the ties as JSON, with the world position of "
                                           "each end (an end measured on one photo is placed on the surface: the "
                                           "chunk's cloud is read once)")):
    """The ties of the project: ends, precision, active or why not."""
    from .engine import ties as ties_mod

    doc = Document.open(project)
    all_ties = ties_mod.ties_of(doc)
    if as_json:
        rows = []
        for t in all_ties:
            d = t.to_dict()
            d["broken"] = t.broken
            d["stale"] = t.stale or []
            for which, end, uid in (("a", t.a, t.a_uid), ("b", t.b, t.b_uid)):
                found = None if end is None else ties_mod.resolve_end(doc, end, uid)
                d[f"{which}_labels"] = None if found is None else [found[0].label, found[1].label]
                w = None if end is None else ties_mod.end_world(doc, end[0], end[1], uid)
                d[f"{which}_world"] = None if w is None else [float(v) for v in w]
            rows.append(d)
        typer.echo(json.dumps(rows, indent=1))
        return
    if not all_ties:
        typer.echo("no ties")
        return
    for t in all_ties:
        typer.echo(_tie_line(doc, t))


@ties_app.command("remove")
def ties_remove(project: Path,
                tie: str = typer.Argument(..., help="id of the tie or of the stable height (or its name when only "
                                                    "one has it)"),
                markers: bool = typer.Option(True, "--markers/--keep-markers",
                                             help="remove also the markers the tie made (never the markers that "
                                                  "were already there, nor those another tie still uses)")):
    """Remove a tie, or a stable height (ppg ties heights)."""
    from .engine import stable_height
    from .engine import ties as ties_mod

    doc = Document.open(project)
    t = _find_tie(doc, tie, heights=True)
    if isinstance(t, stable_height.HeightTie):
        stable_height.remove(doc, t.id)
        doc.save()
        typer.echo(f"removed stable height {t.id} {t.name!r}")
        return
    try:
        ties_mod.remove_tie(doc, t.id, keep_markers=not markers)
    except (ValueError, KeyError) as exc:
        _ties_error(exc)
    doc.save()
    typer.echo(f"removed {t.id} {t.name!r}" + ("" if markers and t.own_markers else ", markers kept"))


@ties_app.command("confirm")
def ties_confirm(project: Path,
                 tie: Optional[list[str]] = typer.Argument(None, help="ids (or names) of the ties; none: every tie with an end "
                                                            "to be measured again")):
    """Take the ends of ties placed without photos as still right after a new alignment of their chunk.

    A new alignment of the photos puts a chunk in a new internal frame: ends measured on photos are triangulated
    again, the others keep their old numbers and are left out of the solve until confirmed (or placed again). Confirm
    only an end you checked: in the new frame it must still be on its point."""
    from .engine import ties as ties_mod

    doc = Document.open(project)
    ids = None if not tie else [_find_tie(doc, ref).id for ref in tie]
    done = ties_mod.confirm_ends(doc, ids)
    if not done:
        typer.echo("no tie with an end to be measured again")
        return
    doc.save()
    typer.echo(f"confirmed {len(done)} ties: {', '.join(done)}")


@ties_app.command("height")
def ties_height(project: Path,
                chunk: str = typer.Argument(..., help="the chunk whose height is measured (B: label or key)"),
                reference: str = typer.Argument(..., help="the reference chunk (A): B is measured against it"),
                layer: Optional[str] = typer.Option(None, "--layer", help="the stable ground: a polygon layer of the "
                                                    "free drawings (of A, else of B)"),
                area: Optional[Path] = typer.Option(None, "--area", help="the stable ground: polygons of a GeoPackage, "
                                                    "GeoJSON or DXF file"),
                exclude: Optional[str] = typer.Option(None, "--exclude", help="ground left out: a polygon layer, or "
                                                      "a file (.gpkg, .geojson, .json, .dxf)"),
                plane: bool = typer.Option(False, "--plane", help="a plane (offset and tilt, three anchors) instead "
                                           "of an offset"),
                name: Optional[str] = typer.Option(None, help="name of the stable height (default: quota stabile N)")):
    """Measure how much higher CHUNK is than REFERENCE on stable ground (their DEMs as they are placed now: no
    horizontal shift) and keep it as a stable height, a z-only tie used by ppg ties check/solve. Without --layer or
    --area the stable ground is found by itself (gentle slopes, cells that agree)."""
    from .engine import stable_height
    from .engine import ties as ties_mod

    if layer is not None and area is not None:
        raise typer.BadParameter("give --layer or --area, not both")
    doc = Document.open(project)
    cb, ca = _tie_chunk(doc, chunk), _tie_chunk(doc, reference)
    where = {"layer": layer} if layer is not None else {"file": str(area)} if area is not None else None
    try:
        h = stable_height.measure(doc, cb, ca, area=where, exclude=exclude, plane=plane, name=name)
        h = stable_height.add(doc, h)
    except (ties_mod.TieError, ValueError, KeyError, FileNotFoundError) as exc:
        _ties_error(exc)
    doc.save()
    typer.echo(f"{cb.label} on {ca.label}: dz {h.dz:+.4f} m, NMAD {h.nmad:.4f} m, {h.cells} stable cells")
    typer.echo(f"added {_height_line(doc, h)}")


@ties_app.command("heights")
def ties_heights(project: Path,
                 as_json: bool = typer.Option(False, "--json", help="print the stable heights as JSON")):
    """The stable heights of the project: chunks, dz, NMAD, cells, precision, active or not."""
    from .engine import stable_height

    doc = Document.open(project)
    every = stable_height.heights_of(doc)
    if as_json:
        rows = []
        for h in every:
            d = h.to_dict()
            d["chunks"] = list(_height_labels(doc, h))
            rows.append(d)
        typer.echo(json.dumps(rows, indent=1))
        return
    if not every:
        typer.echo("no stable heights")
        return
    for h in every:
        typer.echo(_height_line(doc, h))


@ties_app.command("remeasure")
def ties_remeasure(project: Path,
                   height: str = typer.Argument(..., help="id of the stable height (or its name when only one has "
                                                          "it)")):
    """Measure a stable height again where its chunks are now (same chunks, areas and kind): needed when a solve
    moved them in plan against each other since it was measured."""
    from .engine import stable_height
    from .engine import ties as ties_mod

    doc = Document.open(project)
    h = _find_height(doc, height)
    try:
        new = stable_height.remeasure(doc, h.id)
    except (ties_mod.TieError, ValueError, KeyError, FileNotFoundError) as exc:
        _ties_error(exc)
    doc.save()
    typer.echo(f"measured again {_height_line(doc, new)} (before: dz {h.dz:+.4f} m)")


@ties_app.command("check")
def ties_check(project: Path,
               unknowns: str = typer.Option(..., "--unknowns", "-u", help=UNKNOWNS_HELP),
               robust: bool = typer.Option(False, help="Huber weights: a wrong tie weighs less"),
               references: bool = typer.Option(True, help="the control markers of the chunks count as known points"),
               as_json: bool = typer.Option(False, "--json", help="print the report as JSON")):
    """The solve and its checks (residuals, redundancy, distances between ties, leave one out, plausibility):
    a report only, nothing is changed. The active stable heights (ppg ties heights) between chunks of the solve take
    part as z-only ties."""
    from .engine import ties as ties_mod

    doc = Document.open(project)
    kinds = _parse_unknowns(doc, unknowns)
    try:
        solution, checks, loo, warnings = _ties_report(doc, kinds, references, robust)
    except (ties_mod.TieError, ValueError, KeyError) as exc:
        _ties_error(exc)
    if as_json:
        typer.echo(json.dumps(ties_mod.report(solution, checks, loo, warnings), indent=1))
        return
    typer.echo(ties_mod.report_text(solution, checks, loo, warnings))


@ties_app.command("solve")
def ties_solve(project: Path,
               unknowns: str = typer.Option(..., "--unknowns", "-u", help=UNKNOWNS_HELP),
               robust: bool = typer.Option(False, help="Huber weights: a wrong tie weighs less"),
               references: bool = typer.Option(True, help="the control markers of the chunks count as known points"),
               yes: bool = typer.Option(False, "--yes", "-y", help="apply without asking"),
               accept_no_check: bool = typer.Option(False, "--accept-no-check",
                                                    help="with --yes: apply even with redundancy 0 (no check at "
                                                         "all: a wrong tie cannot be seen)")):
    """The solve and its checks, then the new transforms applied to the chunks (with --yes, or once confirmed) and
    the project saved. A solve without redundancy (no check at all) asks its own confirmation, and with --yes it
    needs --accept-no-check too."""
    from .engine import ties as ties_mod

    doc = Document.open(project)
    kinds = _parse_unknowns(doc, unknowns)
    try:
        solution, checks, loo, warnings = _ties_report(doc, kinds, references, robust)
    except (ties_mod.TieError, ValueError, KeyError) as exc:
        _ties_error(exc)
    typer.echo(ties_mod.report_text(solution, checks, loo, warnings))
    moving = [solution.per_chunk[k]["label"] for k, kind in solution.unknowns.items() if kind != "fixed"]
    if not moving:
        typer.echo("Nothing to apply: every chunk is fixed.")
        return
    typer.echo("")
    unchecked = solution.redundancy <= 0
    if yes:
        if unchecked and not accept_no_check:
            typer.echo("error: no check at all (redundancy 0): a wrong tie cannot be seen. Nothing applied; add ties "
                       "or known points, or give --accept-no-check with --yes to apply anyway", err=True)
            raise typer.Exit(1)
    elif unchecked:
        if not typer.confirm(f"No check at all (redundancy 0): a wrong tie cannot be seen. Apply the solve to "
                             f"{', '.join(moving)} anyway?", default=False):
            typer.echo("Nothing applied.")
            return
    elif not typer.confirm(f"Apply the solve to {', '.join(moving)}?", default=False):
        typer.echo("Nothing applied.")
        return
    try:
        changed = ties_mod.apply(doc, solution)
    except (ties_mod.TieError, ValueError) as exc:
        _ties_error(exc)
    doc.save()
    labels = {c.key: c.label for c in doc.chunks}
    typer.echo(f"applied to {', '.join(moving)} (system {solution.crs or 'LOCAL'}); project saved")
    for key, stale in changed["stale_products"].items():
        typer.echo(f"  {labels.get(int(key), key)}: build again {', '.join(stale)}")


@ties_app.command("auto")
def ties_auto(project: Path,
              chunks: Optional[str] = typer.Option(None, "--chunks", "-c",
                                                   help="chunks to pair, the reference first (labels or keys, comma "
                                                        "separated; default every chunk with a DEM)"),
              pairs: Optional[str] = typer.Option(None, "--pairs", "-p",
                                                  help="pairs to match instead of the candidates, e.g. A:B,A:C (the "
                                                       "reference first)"),
              source: str = typer.Option("ortho", help="ortho (the orthophotos) or relief (the DEMs shaded: colours "
                                                       "that changed between the surveys)"),
              margin: float = typer.Option(5.0, help="metres: how far off the placement of a chunk may be"),
              whole: bool = typer.Option(False, "--whole", help="match the whole rasters, whatever the placement (two "
                                                                "unrelated local frames, a GPS far off)"),
              cell: Optional[float] = typer.Option(None, help="metres: the cell of the matching (default the coarser "
                                                              "raster)"),
              same_group: bool = typer.Option(False, "--same-group", help="pair chunks of one group too"),
              accept: bool = typer.Option(False, "--accept", help="make the pairs proposed into ties and save"),
              max_ties: int = typer.Option(12, "--max-ties", help="ties per pair accepted, spread over the overlap"),
              as_json: Optional[Path] = typer.Option(None, "--json", help="write the pairs and the results (with "
                                                                          "their points) to this JSON file")):
    """Automatic ties between chunks: the candidate pairs (overlap where the chunks are now), the matching of their
    orthophotos (or DEM relief), the robust estimate refined by correlation and the residual report of each pair. A
    pair that does not show the same ground is rejected. Nothing is stored without --accept; then ppg ties check /
    solve place the chunks."""
    from .engine import auto_ties

    doc = Document.open(project)
    if source not in auto_ties.SOURCES:
        raise typer.BadParameter(f"use one of {', '.join(auto_ties.SOURCES)}", param_hint="--source")
    found = []
    if pairs:
        todo = []
        for item in (p.strip() for p in pairs.split(",") if p.strip()):
            a, sep, b = item.partition(":")
            if not sep:
                raise typer.BadParameter(f"{item!r}: write A:B", param_hint="--pairs")
            todo.append((_tie_chunk(doc, a.strip()).key, _tie_chunk(doc, b.strip()).key))
    else:
        chosen = None if not chunks else [_tie_chunk(doc, c.strip()) for c in chunks.split(",") if c.strip()]
        try:
            found = auto_ties.find_pairs(doc, chosen, margin=margin, same_group=same_group)
        except (ValueError, KeyError) as exc:
            _ties_error(exc)
        typer.echo("Candidate pairs:")
        for p in found:
            state = "candidate" if p["candidate"] else "not a candidate: " + ties_render(p["reason"])
            typer.echo(f"  {p['b_label']} -> {p['a_label']}: overlap {p['share']:.0%} ({p['share_margin']:.0%} within "
                       f"{margin:g} m), {state}")
        todo = [(p["a"], p["b"]) for p in found
                if p["candidate"] or (whole and (p["reason"] or {}).get("code") == "small_overlap")]
        if not todo:
            typer.echo("No pair to match (try --whole, or --pairs)")
    results = auto_ties.run(doc, todo, source=source, margin=None if whole else margin, cell=cell,
                            progress=print_progress)
    for r in results:
        typer.echo(auto_ties.report_text(r))
    if as_json is not None:
        as_json.write_text(json.dumps({"pairs": found, "results": [r.to_dict() for r in results]}, indent=1))
        typer.echo(f"written {as_json}")
    if accept:
        made = 0
        for r in results:
            if r.ok:
                made += len(auto_ties.accept(doc, r, max_ties=max_ties))
        if made:
            doc.save()
        typer.echo(f"{made} ties made from {sum(r.ok for r in results)} pairs; project saved" if made
                   else "No pair proposed: nothing made")


def ties_render(m: Optional[dict]) -> str:
    from .engine.ties import render

    return render(m) if m else ""


stakes_app = typer.Typer(help="Stakes and benchmarks: dark round heads looked for on the orthophoto (and the photos) near "
                              "points of known coordinates, proposed as known ties; nothing is stored until accepted")
app.add_typer(stakes_app, name="stakes")


def _stake_line(p) -> str:
    """One line for a proposal of engine.stakes."""
    import numpy as np

    if p.best is None:
        why = ("no height for the point (no z and no DEM there)" if "no_height" in p.flags else
               "outside the orthophoto" if "outside_orthophoto" in p.flags else "no head found")
        return f"{p.point.name}: {why}"
    b = p.best
    where = (f"{b.agreeing} photos agree" if not b.ortho_only else
             "ortho only" + (" (photos unreadable)" if "photos_unreadable" in p.flags else
                             " (no aligned photos)" if "no_aligned_photos" in p.flags else ""))
    d = p.displacement
    shift = f"{float(np.hypot(d[0], d[1])):.3f} m from the known point" if d is not None else ""
    return (f"{p.point.name}: head at {b.world[0]:.3f}, {b.world[1]:.3f}, {b.world[2]:.3f}, {shift}, {where}, "
            f"precision {b.precision:.3f} m, diameter {b.diameter:.3f} m"
            + (f", {len(p.others)} other candidates" if p.others else ""))


@stakes_app.command("find")
def stakes_find(project: Path,
                chunk: str = typer.Argument(..., help="the chunk (label or key)"),
                points: str = typer.Argument(..., help="the known points: a CSV file (name,x,y[,z][,precision]), "
                                                       "'ties' (the known ties of the project) or a chunk (its markers "
                                                       "with coordinates)"),
                crs: Optional[str] = typer.Option(None, "--crs", help="system of the CSV coordinates (default: the "
                                                  "chunk's)"),
                radius: float = typer.Option(3.0, "--radius", help="metres around each point"),
                head: str = typer.Option("0.03,0.12", "--head", help="smallest,largest head diameter in metres"),
                min_contrast: float = typer.Option(25, "--min-contrast", help="grey levels darker than the "
                                                   "background"),
                min_photos: int = typer.Option(3, "--min-photos", help="photos that must agree, else ortho only"),
                out: Optional[Path] = typer.Option(None, "--out", help="the proposals (JSON; default proposals.json "
                                                   "next to the project); a picture <name>.png beside it for each "
                                                   "point")):
    """Look for stakes and benchmarks near known points: proposals written to a JSON file with a picture each
    (orthophoto and photo side by side); the project does not change. Accept them with ppg stakes accept."""
    import cv2

    from .engine import stakes
    from .engine import ties as ties_mod

    try:
        lo, hi = (float(v) for v in head.replace(";", ",").split(","))
    except ValueError:
        raise typer.BadParameter(f"--head is smallest,largest in metres (got {head!r})") from None
    doc = Document.open(project)
    ch = _tie_chunk(doc, chunk)
    try:
        if points == "ties":
            known = stakes.known_points("ties", doc=doc)
        elif Path(points).exists():
            known = stakes.known_points(Path(points), crs=crs)
        else:
            known = stakes.known_points(_tie_chunk(doc, points))
        if not known:
            raise ValueError(f"no known points in {points}")
        props = stakes.find(doc, ch, known, radius=radius, head=(lo, hi), min_contrast=min_contrast,
                            min_photos=min_photos)
    except (ties_mod.TieError, ValueError, KeyError, FileNotFoundError) as exc:
        _ties_error(exc)
    for p in props:
        typer.echo(_stake_line(p))
    result = stakes.check(props)
    med = result["displacement_median"]
    if med is not None:
        typer.echo(f"median displacement: {med[0]:+.3f}, {med[1]:+.3f}"
                   + (f", {med[2]:+.3f}" if med[2] is not None else "") + " m")
    if result["suspects"]:
        typer.echo(f"suspect (distances do not agree): {', '.join(result['suspects'])}")
    if result["outliers"]:
        typer.echo(f"displaced unlike the others: {', '.join(result['outliers'])}")
    out = out if out is not None else Path(project).parent / "proposals.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"project": str(Path(project).resolve()), "chunk": ch.key,
                               "proposals": [p.to_dict() for p in props], "check": result}, indent=1),
                   encoding="utf-8")
    for p in props:
        cv2.imwrite(str(out.parent / f"{stakes.file_name(p.point.name)}.png"),
                    stakes.preview(ch, p)[..., ::-1])
    typer.echo(f"proposals written to {out}; nothing changed in the project (ppg stakes accept)")


@stakes_app.command("accept")
def stakes_accept(project: Path,
                  chunk: str = typer.Argument(..., help="the chunk (label or key)"),
                  proposals: Path = typer.Argument(..., help="the JSON written by ppg stakes find"),
                  ids: Optional[str] = typer.Option(None, "--ids", help="the points accepted, by name: N1,N2"),
                  agreeing: bool = typer.Option(False, "--agreeing", help="every point confirmed by the photos "
                                                "(not ortho only)"),
                  yes: bool = typer.Option(False, "--yes", help="accept them (without it: only what would be done)")):
    """Accept proposals of ppg stakes find as known ties (markers with the agreeing photo measurements)."""
    from .engine import stakes
    from .engine import ties as ties_mod

    if (ids is None) == (not agreeing):
        raise typer.BadParameter("give --ids N1,N2 or --agreeing")
    doc = Document.open(project)
    ch = _tie_chunk(doc, chunk)
    try:
        data = json.loads(Path(proposals).read_text(encoding="utf-8"))
        props = [stakes.Proposal.from_dict(d) for d in data["proposals"]]
    except (OSError, ValueError, KeyError) as exc:
        _ties_error(ValueError(f"cannot read the proposals {proposals}: {exc}"))
    if ids is not None:
        names = [n.strip() for n in ids.split(",") if n.strip()]
    else:
        names = [p.point.name for p in props if p.best is not None and not p.best.ortho_only]
        if not names:
            _ties_error(ValueError("no proposal is confirmed by the photos"))
    chosen = [p for p in props if p.point.name in names]
    for p in chosen:
        typer.echo(("accept " if yes else "would accept ") + _stake_line(p))
    if not yes:
        typer.echo("Nothing changed: add --yes to accept them.")
        return
    try:
        made = stakes.accept(doc, ch, props, names=names)
    except (ties_mod.TieError, ValueError, KeyError) as exc:
        _ties_error(exc)
    doc.save()
    for t in made:
        typer.echo(f"added {_tie_line(doc, t)}")


mask_app = typer.Typer(help="Masks of the orthophoto written as layers of free drawings")
app.add_typer(mask_app, name="mask")


def _range_of(text: Optional[str], what: str, top: float) -> Optional[tuple[float, float]]:
    """``"A,B"`` -> ``(a, b)`` (None for None)."""
    if text is None:
        return None
    try:
        lo, hi = (float(v) for v in text.replace(";", ",").split(","))
    except ValueError:
        raise typer.BadParameter(f"{what} is low,high (got {text!r})") from None
    if not (0 <= lo <= top and 0 <= hi <= top):
        raise typer.BadParameter(f"{what}: from 0 to {top:g} (got {text!r})")
    return lo, hi


@mask_app.command("colour")
def mask_colour(project: Path,
                chunk: str = typer.Argument(..., help="the chunk (label or key)"),
                hue: Optional[str] = typer.Option(None, "--hue", help="hue low,high in degrees 0-360 (low above high "
                                                  "goes through 0: the reds, e.g. 350,10)"),
                sat: Optional[str] = typer.Option(None, "--sat", help="saturation low,high in % (default 0,100)"),
                val: Optional[str] = typer.Option(None, "--val", help="value (brightness) low,high in % (default "
                                                  "0,100)"),
                sample: Optional[str] = typer.Option(None, "--sample", help="X,Y: the range around the colour of the "
                                                     "orthophoto there (instead of --hue/--sat/--val)"),
                radius: float = typer.Option(0.1, "--radius", help="metres of the disc sampled"),
                opening: float = typer.Option(0.05, "--opening", help="specks smaller than this (metres) dropped"),
                margin: float = typer.Option(0.0, "--margin", help="metres added around the polygons (negative: "
                                             "taken away)"),
                keep_all: bool = typer.Option(False, "--all", help="every region of --min-area or more (default: the "
                                              "largest only)"),
                min_area: float = typer.Option(0.05, "--min-area", help="smallest region kept, m2"),
                holes: bool = typer.Option(False, "--holes", help="keep the holes of the mask (default: filled)"),
                layer: Optional[str] = typer.Option(None, "--layer", help="the layer of free drawings (default "
                                                    "'Maschera colore N'; an earlier mask in it is replaced)")):
    """The cells of the orthophoto within a colour range as polygons of a layer of free drawings (heights from the
    DEM): for the crop of the products, the stable height of ppg ties, every export of the free drawings."""
    from .engine import colour_mask
    from .engine import ties as ties_mod

    if sample is not None and (hue or sat or val):
        raise typer.BadParameter("give --sample or --hue/--sat/--val, not both")
    if sample is None and hue is None:
        raise typer.BadParameter("give --hue (with --sat, --val) or --sample X,Y")
    doc = Document.open(project)
    ch = _tie_chunk(doc, chunk)
    try:
        if sample is not None:
            x, y, _z = _coordinates(sample, "--sample")
            rng = colour_mask.sample(ch, x, y, radius=radius)
            typer.echo(f"sampled: {rng.text()}")
        else:
            rng = colour_mask.HsvRange(_range_of(hue, "--hue", 360), _range_of(sat, "--sat", 100) or (0.0, 100.0),
                                       _range_of(val, "--val", 100) or (0.0, 100.0))
        result = colour_mask.mask(ch, rng, opening=opening, fill_holes=not holes,
                                  keep="all" if keep_all else "largest", min_area=min_area, margin=margin,
                                  progress=print_progress)
        name = (layer or "").strip() or colour_mask.next_layer_name(ch)
        ids = colour_mask.to_layer(ch, result, layer=name)
    except (ties_mod.TieError, ValueError) as exc:
        _ties_error(exc)
    doc.save()
    typer.echo(f"layer {name!r}: {len(ids)} polygon(s), {result.area:.3f} m2 ({100 * result.share:.1f} % of the "
               f"orthophoto read), cells of {result.cell:.4f} m"
               + (f", {result.holes} hole(s) left out" if result.holes else ""))


water_app = typer.Typer(help="The water level of a chunk and the refraction correction of its dense cloud")
app.add_typer(water_app, name="water")


def _water_n(n: Optional[float], sea: bool, fresh: bool, default: Optional[float]) -> Optional[float]:
    from .engine import water

    if (n is not None) + bool(sea) + bool(fresh) > 1:
        raise typer.BadParameter("give one of --n, --sea, --fresh")
    if sea:
        return water.N_SEA
    if fresh:
        return water.N_FRESH
    return default if n is None else n


def _water_line(lv: dict) -> str:
    spread = lv.get("spread")
    return (f"water level {lv['level']:.3f} m ({lv.get('source')}), n {lv['n']:.2f}"
            + (f", spread (NMAD) {spread:.3f} m" if spread is not None else ""))


@water_app.command("level")
def water_level(project: Path,
                chunk: str = typer.Argument(..., help="the chunk (label or key)"),
                set_z: Optional[float] = typer.Option(None, "--set", help="the level by hand: a world height (m)"),
                layer: Optional[str] = typer.Option(None, "--layer", help="the water as the polygons of a layer of "
                                                    "free drawings (a colour mask, for instance)"),
                hue: Optional[str] = typer.Option(None, "--hue", help="the water as a colour range of the "
                                                  "orthophoto: hue low,high in degrees 0-360"),
                sat: Optional[str] = typer.Option(None, "--sat", help="saturation low,high in % (default 0,100)"),
                val: Optional[str] = typer.Option(None, "--val", help="value low,high in % (default 0,100)"),
                sample: Optional[str] = typer.Option(None, "--sample", help="X,Y: the water is the colour of the "
                                                     "orthophoto there"),
                n: Optional[float] = typer.Option(None, "--n", help="refractive index of the water (1.0-2.0)"),
                sea: bool = typer.Option(False, "--sea", help="sea water: n 1.34 (the default)"),
                fresh: bool = typer.Option(False, "--fresh", help="fresh water: n 1.33")):
    """The level of the water of a chunk, by hand (--set) or from the edge of the water (--layer, --hue/--sat/--val,
    --sample): the DEM read two cells on the land side of the outline, the median the level. Stored in the project."""
    from .engine import colour_mask, water

    if (sat or val) and hue is None:
        raise typer.BadParameter("--sat/--val need --hue")
    if [set_z, layer, hue, sample].count(None) != 3:
        raise typer.BadParameter("give one of --set Z, --layer NAME, --hue/--sat/--val, --sample X,Y")
    idx = _water_n(n, sea, fresh, water.N_SEA)
    doc = Document.open(project)
    ch = _tie_chunk(doc, chunk)
    try:
        if set_z is not None:
            rec = water.set_level(ch, set_z, source="hand", n=idx)
        else:
            if layer is not None:
                what = layer
            elif sample is not None:
                x, y, _z = _coordinates(sample, "--sample")
                what = colour_mask.sample(ch, x, y)
                typer.echo(f"sampled: {what.text()}")
            else:
                what = colour_mask.HsvRange(_range_of(hue, "--hue", 360), _range_of(sat, "--sat", 100) or (0.0, 100.0),
                                            _range_of(val, "--val", 100) or (0.0, 100.0))
            info = water.level_from_edge(ch, what, progress=print_progress)
            rec = water.set_level(ch, info.pop("level"), source="edge", n=idx, **info)
    except ValueError as exc:
        _ties_error(exc)
    doc.save()
    typer.echo(_water_line(rec))
    if rec.get("edge_cells"):
        typer.echo(f"edge: {rec['edge_cells']} DEM cells on the land side, 10-90 % {rec['p10']:.3f} - "
                   f"{rec['p90']:.3f} m; rise of the bank removed {rec.get('slope_bias') or 0.0:.3f} m")


@water_app.command("correct")
def water_correct(project: Path,
                  chunk: str = typer.Argument(..., help="the chunk (label or key)"),
                  n: Optional[float] = typer.Option(None, "--n", help="refractive index (default: the one stored "
                                                    "with the level)"),
                  dem: bool = typer.Option(False, "--dem", help="build the DEM again from the corrected cloud (a new "
                                           "version of the DEM)")):
    """The dense cloud corrected for the refraction under the water level (z' = L - n (L - z) below it): a new
    version of the dense cloud, the one before kept among the versions."""
    from .engine import water

    doc = Document.open(project)
    ch = _tie_chunk(doc, chunk)
    try:
        r = water.correct_cloud(ch, n=n, rebuild_dem=dem, progress=print_progress)
    except ValueError as exc:
        _ties_error(exc)
    doc.save()
    typer.echo(f"{r['below']} of {r['points']} points below the level {r['level']:.3f} m corrected with n "
               f"{r['n']:.2f}; the cloud before kept as version {r['version']}")
    typer.echo(f"note: {water.NOTE_EN} ({water.NOTE})")
    if r.get("dem"):
        typer.echo(f"DEM built again from the corrected cloud; the DEM before kept as version {r['dem_version']}")


@water_app.command("basin")
def water_basin(project: Path,
                chunk: str = typer.Argument(..., help="the chunk (label or key)"),
                name: Optional[str] = typer.Option(None, "--name", help="the name of the basin (a new one is added, "
                                                   "one with this name replaced)"),
                level: Optional[float] = typer.Option(None, "--level", help="its level: a world height (m)"),
                shore: Optional[str] = typer.Option(None, "--shore", help="points of its shoreline X,Y,Z;X,Y,Z;... "
                                                    "(three or more): a plane of the water fitted to them"),
                polygon: Optional[str] = typer.Option(None, "--polygon", help="where it is: a WKT polygon (world)"),
                layer: Optional[str] = typer.Option(None, "--layer", help="where it is: the polygons of a layer of "
                                                    "free drawings"),
                n: Optional[float] = typer.Option(None, "--n", help="its effective factor (default 1.34)"),
                clear: bool = typer.Option(False, "--clear", help="remove every basin: back to the single level")):
    """A basin of water with its own level (or shoreline plane), factor and polygon; several basins correct each
    their part of the cloud (the one without polygon the rest)."""
    from .engine import colour_mask, water

    doc = Document.open(project)
    ch = _tie_chunk(doc, chunk)
    try:
        if clear:
            water.set_basins(ch, [])
        else:
            if not name:
                raise typer.BadParameter("--name is needed (or --clear)")
            if (level is None) == (shore is None):
                raise typer.BadParameter("give one of --level Z, --shore X,Y,Z;X,Y,Z;...")
            pts = None
            if shore is not None:
                try:
                    pts = [[float(v) for v in p.split(",")] for p in shore.split(";") if p.strip()]
                except ValueError:
                    raise typer.BadParameter("--shore: X,Y,Z points separated by ;") from None
            where = polygon if polygon else (colour_mask.union(ch, layer) if layer else None)
            new = water.basin(name, level=level, n=water.N_SEA if n is None else n, polygon=where, shore=pts)
            kept = [b for b in (ch.meta.get("water") or {}).get("basins") or [] if b["name"] != name]
            water.set_basins(ch, kept + [new])
            if new.get("shore"):
                f = new["shore"]
                typer.echo(f"shoreline plane: tilt {f['tilt_deg']:.3f} degrees, rmse {f['rmse']:.4f} m on "
                           f"{f['points']} points")
    except ValueError as exc:
        _ties_error(exc)
    doc.save()
    for b in water.basins_of(ch):
        typer.echo(_basin_line(b))


def _basin_line(b: dict) -> str:
    plane = "" if not (b.get("a") or b.get("b")) else f", plane a {b['a']:.5f} b {b['b']:.5f}"
    return (f"basin {b['name']}: level {b['level']:.3f} m{plane}, n {b['n']:.3f}, "
            + ("everywhere else" if b.get("polygon") is None else "inside its polygon"))


@water_app.command("factor")
def water_factor(project: Path,
                 chunk: str = typer.Argument(..., help="the chunk (label or key)"),
                 points: Optional[Path] = typer.Option(None, "--points", help="CSV of control points: label,x,y,z "
                                                       "(true height) and optionally apparent"),
                 markers: Optional[str] = typer.Option(None, "--markers", help="markers with a reference under the "
                                                       "water, triangulated through it (comma separated)"),
                 source: str = typer.Option("dem", "--source", help="where the apparent heights of --points are "
                                            "read: dem or cloud"),
                 basin_name: Optional[str] = typer.Option(None, "--basin", help="only the points of this basin; "
                                                          "--apply stores the factor on it"),
                 fit_level: bool = typer.Option(False, "--fit-level", help="the level fitted together with n"),
                 apply: bool = typer.Option(False, "--apply", help="store the factor (and the fitted level)")):
    """The effective refraction factor from points of known true height under the water, by least squares on
    L - z_true = n (L - z_apparent), with the residual of every point."""
    import csv

    from .engine import water

    doc = Document.open(project)
    ch = _tie_chunk(doc, chunk)
    rows = []
    if points is not None:
        for r in csv.DictReader(points.read_text(encoding="utf-8-sig").splitlines()):
            r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
            rows.append({"label": r.get("label") or f"P{len(rows) + 1}", "x": float(r["x"]), "y": float(r["y"]),
                         "z": float(r["z"]), "apparent": float(r["apparent"]) if r.get("apparent") else None})
    labels = [m.strip() for m in markers.split(",") if m.strip()] if markers else []
    if not rows and not labels:
        raise typer.BadParameter("give --points and/or --markers")
    try:
        fit = water.estimate_factor(ch, water.control_points(ch, markers=labels, points=rows, source=source),
                                    fit_level=fit_level, basin_name=basin_name)
        if apply:
            water.apply_factor(ch, fit)
    except (ValueError, KeyError) as exc:
        _ties_error(exc)
    typer.echo(f"n {fit['n']:.4f} from {fit['points']} points" + (f", level {fit['level']:.3f} m" if fit["level"]
                                                                   is not None else "")
               + (f", rmse {fit['rmse']:.4f} m" if fit["rmse"] is not None else ""))
    if not fit["in_range"]:
        typer.echo(f"warning: n outside {water.N_RANGE[0]:g}-{water.N_RANGE[1]:g}: check the points")
    for r in fit["residuals"]:
        res = "left out" if r["residual"] is None else f"residual {r['residual']:+.4f} m"
        app_z = "-" if r["apparent"] is None else f"{r['apparent']:.3f}"
        typer.echo(f"  {r['label']}: true {r['z']:.3f}, apparent {app_z}, {res}")
    if apply:
        doc.save()
        typer.echo("stored")


@water_app.command("table")
def water_table(project: Path, table: Path = typer.Argument(..., help="CSV with chunk or date, level, n")):
    """A level and an effective factor for several chunks at once, by chunk (label or key) or by date of the survey
    (the chunks surveyed that day): one per year of a campaign, for instance."""
    from .engine import water

    doc = Document.open(project)
    try:
        done = water.apply_table(doc, water.read_table(table))
    except ValueError as exc:
        _ties_error(exc)
    doc.save()
    for d in done:
        typer.echo(f"{d['chunk']}: level {d['level']:.3f} m, n {d['n']:.3f}" + (f" ({d['date']})" if d["date"] else ""))


@water_app.command("show")
def water_show(project: Path, chunk: str = typer.Argument(..., help="the chunk (label or key)")):
    """The water level of a chunk in its current system, and the correction of its cloud if any."""
    from .engine import water

    doc = Document.open(project)
    ch = _tie_chunk(doc, chunk)
    lv = water.level_of(ch)
    if lv is None:
        typer.echo(f"{ch.label}: no water level")
        return
    typer.echo(f"{ch.label}: {_water_line(lv)}")
    for b in water.basins_of(ch):
        typer.echo("  " + _basin_line(b))
    corr = (ch.meta.get("dense") or {}).get("refraction")
    if corr:
        typer.echo(f"active dense cloud corrected for the refraction (n {corr['n']:.2f}, level {corr['level']:.3f} m, "
                   f"{corr['below']} points below); note: {water.NOTE_EN}")


def _axis_of(text: str) -> tuple:
    try:
        x1, y1, x2, y2 = (float(v) for v in text.replace(";", ",").split(","))
    except ValueError:
        raise typer.BadParameter(f"--axis is X1,Y1,X2,Y2 (got {text!r})") from None
    return (x1, y1), (x2, y2)


@app.command("classify-lines")
def classify_lines(project: Path,
                   chunk: str = typer.Argument(..., help="the chunk (label or key)"),
                   layer: Optional[str] = typer.Option(None, "--layer", help="the lines and polygons of a layer of "
                                                       "free drawings (plan x, y)"),
                   file: Optional[Path] = typer.Option(None, "--file", help="the lines and polygons of a GeoPackage, "
                                                       "GeoJSON or DXF"),
                   elevation: Optional[str] = typer.Option(None, "--elevation", help="the drawings of an elevation "
                                                           "(its id) in its (u, v)"),
                   axis: Optional[str] = typer.Option(None, "--axis", help="X1,Y1,X2,Y2: the axis the lines are "
                                                      "sorted by (default with --elevation: its u axis, 0,0,1,0)"),
                   axis_from: Optional[str] = typer.Option(None, "--axis-from", help="the axis of a plan drawing "
                                                           "along the horizontal of a flat elevation (its id)"),
                   param: Optional[list[str]] = typer.Option(None, "--param", help="NAME=VALUE over the defaults "
                                                             "(repeatable): simplify, min_length, along_deg, "
                                                             "across_deg, join_deg, join_offset, join_gap, "
                                                             "across_bin, pair_deg, pair_min, pair_max, "
                                                             "pair_overlap; pair_isolation (1.5; 1: the nearest "
                                                             "line only, 0: off)"),
                   plate: Optional[Path] = typer.Option(None, "--plate", help="the check plate (.png or .svg)"),
                   json_out: Optional[Path] = typer.Option(None, "--json", help="the classification as JSON"),
                   layers: bool = typer.Option(False, "--layers", help="write the elements as layers of free "
                                               "drawings, one per group (plan drawings only; heights from the DEM)")):
    """A drawing classified by its orientation along an axis: lines along it, across it, pairs of parallel lines
    (the two sides of one element) and the rest. The program does not name the elements: the plugins do."""
    import json as _json

    import numpy as np

    from .engine import orient_lines

    if [layer, file, elevation].count(None) != 2:
        raise typer.BadParameter("give one of --layer NAME, --file F, --elevation EID")
    if axis is not None and axis_from is not None:
        raise typer.BadParameter("give --axis or --axis-from, not both")
    if axis is None and axis_from is None and elevation is None:
        raise typer.BadParameter("give --axis X1,Y1,X2,Y2 (or --axis-from EID)")
    params = {}
    for item in param or []:
        if "=" not in item:
            raise typer.BadParameter(f"--param is NAME=VALUE (got {item!r})")
        k, v = item.split("=", 1)
        params[k.strip()] = v.strip()
    doc = Document.open(project)
    ch = _tie_chunk(doc, chunk)
    try:
        if layer is not None:
            lines = orient_lines.lines_from_layer(ch, layer)
        elif file is not None:
            lines = orient_lines.lines_from_file(file, chunk=ch)  # put in the chunk's system
        else:
            lines = orient_lines.lines_from_elevation(ch, elevation)
        if axis is not None:
            ax = _axis_of(axis)
        elif axis_from is not None:
            if elevation is not None:
                raise typer.BadParameter("--axis-from is for a plan drawing: an elevation's drawing is sorted along "
                                         "its own u axis")
            ax = orient_lines.elevation_axis(ch, axis_from)
        else:
            ax = ((0.0, 0.0), (1.0, 0.0))
        result = orient_lines.classify(lines, ax, params, frame=elevation)
        if plate is not None:
            result.plate(plate, language="en")
        if json_out is not None:
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(_json.dumps(result.to_dict(), indent=1), encoding="utf-8")
        written = orient_lines.to_layers(ch, result) if layers else {}
    except ValueError as exc:
        _ties_error(exc)
    if written:
        doc.save()
    counts = result.counts()
    typer.echo(f"{len(lines)} line(s), {len(result.segments)} straight piece(s), {len(result.elements)} element(s)")
    for g in orient_lines.GROUPS:
        typer.echo(f"  {orient_lines.GROUP_LABELS[g][1]}: {counts[g]}")
    widths = [e.width for e in result.elements if e.width is not None]
    if widths:
        typer.echo(f"pairs: width {min(widths):.3f} - {max(widths):.3f} m (median {float(np.median(widths)):.3f})")
    if plate is not None:
        typer.echo(f"plate: {plate}")
    if json_out is not None:
        typer.echo(f"json: {json_out}")
    for name, n in written.items():
        typer.echo(f"layer {name!r}: {n} drawing(s)")


mosaic_app = typer.Typer(help="The orthophoto or the DEM of several tied chunks merged into one raster")
app.add_typer(mosaic_app, name="mosaic")


def _mosaic_line(rec: dict, stale: list) -> str:
    what = "DEM" if rec.get("product") == "dem" else "orthophoto"
    w, h = rec.get("size") or (0, 0)
    labels = rec.get("labels") or {}
    names = ", ".join(str(labels.get(str(k), k)) for k in rec.get("chunks") or [])
    line = (f"{rec['id']}  {rec.get('name')}: {what} of {names}, {w} x {h} cells of {rec['resolution']:.4f} m "
            f"({rec.get('crs') or 'LOCAL'})")
    if rec.get("areas"):
        line += f", {len(rec['areas'])} area(s), background {rec.get('background')}"
    if stale:
        line += f"; to build again: {', '.join(stale)} moved since"
    return line


@mosaic_app.command("build")
def mosaic_build(project: Path,
                 chunks: str = typer.Option(..., "--chunks", help="the chunks in their order of priority (labels or "
                                            "keys, separated by commas): the first decides the system"),
                 dem: bool = typer.Option(False, "--dem", help="merge the DEMs (default: the orthophotos)"),
                 areas: Optional[Path] = typer.Option(None, "--areas", help="a GeoPackage of polygons with the "
                                                      "attribute 'chunk' (and 'priority'): inside an area its chunk "
                                                      "comes first"),
                 resolution: Optional[float] = typer.Option(None, "--resolution", help="the cell (m; default the "
                                                            "finest of the sources)"),
                 background: str = typer.Option("faded", "--background", help="outside the areas: faded (the list, "
                                                "the orthophoto blended toward white) or none (empty)"),
                 name: Optional[str] = typer.Option(None, "--name", help="the name of the merged product")):
    """The orthophotos (or DEMs) of several tied chunks merged into one raster, each placed where its chunk is now,
    with the source of every cell (sources.tif). Stored in the project."""
    from .engine import mosaic

    doc = Document.open(project)
    items = [c.strip() for c in chunks.split(",") if c.strip()]
    chs = [_tie_chunk(doc, c) for c in items]
    try:
        area_list = mosaic.read_areas(doc, areas, chs[0].crs if chs else None) if areas is not None else None
        rec = mosaic.build(doc, chs, product="dem" if dem else "orthomosaic", areas=area_list, resolution=resolution,
                           background=background, name=name, progress=print_progress)
    except (ValueError, OSError) as exc:  # OSError: a missing or unreadable file (RasterioIOError among them)
        _ties_error(exc)
    doc.save()
    typer.echo(_mosaic_line(rec, []))
    labels = rec.get("labels") or {}
    for key, n in (rec.get("counts") or {}).items():
        typer.echo(f"  {labels.get(key, key)}: {n} cells")
    if rec.get("moved"):
        typer.echo("placed with their chunk (built before the last change of its transform): "
                   + ", ".join(str(labels.get(str(k), k)) for k in rec["moved"]))
        for key, deg in (rec.get("moved_tilt_deg") or {}).items():
            if deg >= mosaic.TILT_NOTE_DEG:
                typer.echo(f"note: {labels.get(key, key)} is tilted {deg:.1f} degrees since its raster was built: "
                           "placed in plan only, the plan is approximate - build it again")
    typer.echo(f"files: {mosaic.folder(doc, rec['id'])}")


@mosaic_app.command("list")
def mosaic_list(project: Path, as_json: bool = typer.Option(False, "--json", help="the records as JSON")):
    """The merged products of the project."""
    import json as _json

    from .engine import mosaic

    doc = Document.open(project)
    recs = mosaic.mosaics_of(doc)
    if as_json:
        typer.echo(_json.dumps([dict(r, stale=mosaic.is_stale(doc, r)) for r in recs], indent=1))
        return
    if not recs:
        typer.echo("no merged products")
        return
    for r in recs:
        typer.echo(_mosaic_line(r, mosaic.is_stale(doc, r)))


@mosaic_app.command("remove")
def mosaic_remove(project: Path, mid: str = typer.Argument(..., help="the id of the merged product")):
    """A merged product taken out of the project, its files deleted."""
    from .engine import mosaic

    doc = Document.open(project)
    try:
        rec = mosaic.remove(doc, mid)
    except (ValueError, OSError) as exc:
        _ties_error(exc)
    doc.save()
    typer.echo(f"removed {rec['id']} ({rec.get('name')})")


web_app = typer.Typer(help="A survey as a web site: a folder with the orthophoto, the DEM, the units, the drawings "
                      "and a page that shows them, for any static server")
app.add_typer(web_app, name="web")


def _site_pairs(values, option: str) -> dict:
    """``CHUNK=VALUE`` options of ``ppg web build`` as a dict (the chunk by label or key)."""
    out = {}
    for text in values or []:
        name, sep, value = str(text).rpartition("=")
        if not sep or not name:
            typer.echo(f"{option} {text!r}: write it as CHUNK=VALUE", err=True)
            raise typer.Exit(1)
        out[name.strip()] = value.strip()
    return out


def _site_chunks(doc, names) -> list | None:
    """The chunks of ``--chunk``: a label or key of the project, or ``OTHER.ppg:LABEL`` of another project."""
    if not names:
        return None
    out, others = [], {}
    for name in names:
        head, sep, label = str(name).rpartition(".ppg:")
        if not sep:
            out.append(name)
            continue
        path = Path(head + ".ppg")
        if path not in others:
            if not path.exists():
                typer.echo(f"--chunk {name!r}: no project {path}", err=True)
                raise typer.Exit(1)
            others[path] = Document.open(path)
        found = next((c for c in others[path].chunks if c.label == label or str(c.key) == label), None)
        if found is None:
            typer.echo(f"--chunk {name!r}: no chunk {label!r} in {path.name}", err=True)
            raise typer.Exit(1)
        out.append(found)
    return out


@web_app.command("build")
def web_build(project: Path,
              folder: Path = typer.Argument(..., help="the folder of the site (new, empty, or a site built before)"),
              chunk: Optional[list[str]] = typer.Option(None, "--chunk", help="a chunk (label or key), repeatable; "
                                                        "OTHER.ppg:LABEL takes a chunk of another project (in the "
                                                        "same frame); default: every chunk with something to show"),
              frame: str = typer.Option("local", "--frame", help="local: coordinates shifted to the centre of the "
                                        "survey, no reference system written (the place of the site stays out of the "
                                        "folder); crs: the real coordinates and their system"),
              title: Optional[str] = typer.Option(None, "--title", help="the title of the page"),
              description: Optional[str] = typer.Option(None, "--description", help="a line under the title"),
              language: Optional[list[str]] = typer.Option(None, "--language", help="it or en, repeatable: the "
                                                           "languages of the page, the first shown first "
                                                           "(default it, en)"),
              product: Optional[list[str]] = typer.Option(None, "--product", help="orthomosaic, dem, units, "
                                                          "drawings, model, cloud or sections, repeatable (default: "
                                                          "all the chunk has but the cloud)"),
              quality: int = typer.Option(85, "--quality", help="quality of the WebP of the orthophoto"),
              model_texture: int = typer.Option(4096, "--model-texture", help="largest side in pixels of a texture "
                                                "page of the 3D model; 0: vertex colours alone"),
              budget: float = typer.Option(500.0, "--budget", help="size of the whole site in MB: the model and "
                                           "the cloud are thinned to keep to it; 0: no limit"),
              cloud_points: int = typer.Option(3_000_000, "--cloud-points", help="points of the cloud at most"),
              no_copc: bool = typer.Option(False, "--no-copc", help="the cloud without its COPC to download"),
              date: Optional[list[str]] = typer.Option(None, "--date", help="CHUNK=YYYY-MM-DD, repeatable: the "
                                                       "date of a survey; with dates the page shows one date at a "
                                                       "time"),
              label: Optional[list[str]] = typer.Option(None, "--label", help="CHUNK=TEXT, repeatable: the name of "
                                                        "a chunk on the page"),
              timeline: bool = typer.Option(False, "--timeline", help="one date at a time, the dates of the chunks "
                                            "(set on them or of their photos) where --date gives none")):
    """Write the site of a project: site.json, data/ and the page (see ppg web serve to look at it)."""
    doc = Document.open(project)
    dates, labels = _site_pairs(date, "--date"), _site_pairs(label, "--label")
    chunks = _site_chunks(doc, chunk)

    def by_chunk(pairs):  # a name of --date/--label that is the label of a chunk of another project
        out = []
        for name, value in pairs.items():
            found = next((c for c in chunks or [] if not isinstance(c, str) and c.label == name), None)
            out.append((found if found is not None else name, value))
        return out

    try:
        path = doc.export_site(folder, chunks=chunks, products=product or None, frame=frame, title=title,
                               description=description, language=language or ("it", "en"), quality=quality,
                               model_texture=model_texture or None, progress=print_progress,
                               budget_mb=budget or None, cloud_points=cloud_points, cloud_copc=not no_copc,
                               labels=by_chunk(labels) or None, dates=by_chunk(dates) or None,
                               timeline=True if timeline or dates else None)
    except (ValueError, FileExistsError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    from .io import site

    size = sum(f.stat().st_size for f in path.parent.rglob("*") if f.is_file())
    typer.echo(f"{path.parent}: {size / 1e6:.1f} MB, frame {frame}")
    for note in site.build_notes(path):
        typer.echo(f"  {note}")
    if frame == "crs":
        typer.echo("the site holds the real coordinates of the survey")
    typer.echo(f"look at it: ppg web serve {path.parent}")


@web_app.command("serve")
def web_serve(folder: Path,
              port: int = typer.Option(8765, "--port", help="0: a free port"),
              host: str = typer.Option("127.0.0.1", "--host", help="the address listened on; the default is this "
                                       "computer alone"),
              open_browser: bool = typer.Option(False, "--open", help="open the page in the browser"),
              no_ranges: bool = typer.Option(False, "--no-ranges", help="answer with whole files, as a host "
                                             "without range requests: to see what the page does there")):
    """Serve a site folder to look at it (HTTP range requests, which the page needs to read the rasters)."""
    from .io import site

    if not (folder / site.MANIFEST).exists():
        typer.echo(f"{folder} is not a site: no {site.MANIFEST} (ppg web build writes one)", err=True)
        raise typer.Exit(1)
    try:
        httpd = site.server(folder, host=host, port=port, ranges=not no_ranges)
    except OSError as exc:
        typer.echo(f"cannot listen on {host}:{port}: {exc}", err=True)
        raise typer.Exit(1) from None
    local = host in ("127.0.0.1", "localhost", "::1")
    url = f"http://{'127.0.0.1' if local else host}:{httpd.server_address[1]}/"
    typer.echo(f"{url}  (Ctrl+C to stop)")
    if not local:
        typer.echo(f"listening on {host}: other computers of the network can read this folder")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


publish_app = typer.Typer(help="Publish a site folder (ppg web build) on a hosting")
web_app.add_typer(publish_app, name="publish")


@publish_app.command("cloudflare")
def web_publish_cloudflare(
        folder: Path = typer.Argument(..., help="the site folder (ppg web build)"),
        project: Optional[str] = typer.Option(None, "--project", help="the Cloudflare Pages project (lowercase "
                                              "letters, digits, dashes): the site is at <project>.pages.dev; "
                                              "default: from the name of the folder"),
        bucket: Optional[str] = typer.Option(None, "--bucket", help="the R2 bucket for the data when they do not fit "
                                             "Pages (a bucket for this site alone: its CORS rule is replaced)"),
        data: str = typer.Option("auto", "--data", help="auto: Pages alone when every file fits its limits, else "
                                 "data/ to R2; pages; r2"),
        data_url: Optional[str] = typer.Option(None, "--data-url", help="https address of a custom domain "
                                               "connected to the bucket; default: its r2.dev address, turned on"),
        origin: Optional[list[str]] = typer.Option(None, "--origin", help="another address of the page (a custom "
                                                   "domain of the Pages project) allowed to read the bucket, "
                                                   "repeatable"),
        dry_run: bool = typer.Option(False, "--dry-run", help="say what would be sent where and stop (nothing is "
                                     "run, nothing is sent)"),
        yes_public: bool = typer.Option(False, "--yes-public", help="required: I know the site becomes public"),
        yes_true_coordinates: bool = typer.Option(False, "--yes-true-coordinates", help="required as well for a "
                                                  "site built with --frame crs: I know it tells where the survey "
                                                  "is"),
        record: Optional[Path] = typer.Option(None, "--record", help="a project (.ppg) that keeps the address of "
                                              "the published site")):
    """Publish a site on Cloudflare Pages (and R2 for large data) with your own wrangler: log in first with
    `wrangler login` in a terminal. The program never reads or keeps a token."""
    from .io import site_publish as sp

    if not (folder / "site.json").exists():
        typer.echo(f"{folder} is not a site: no site.json (ppg web build writes one)", err=True)
        raise typer.Exit(1)
    name = project or sp.default_project(folder.resolve().name)
    p = sp.plan(folder, name, bucket=bucket, data=data, data_url=data_url)
    for line in p.lines():
        typer.echo(line)
    typer.echo(f"(limits of the free plans as read on Cloudflare's documentation on {sp.CHECKED})")
    found = sp.find_wrangler()
    typer.echo(f"wrangler: {' '.join(found) if found else 'not found'}")
    if dry_run:
        typer.echo("dry run: nothing was sent")
        raise typer.Exit(0 if p.ok else 1)
    if not p.ok:
        raise typer.Exit(1)
    if p.frame != "local":
        typer.echo("WARNING: this site holds the TRUE coordinates of the survey", err=True)
    try:
        sp.check_consent(p.frame, yes_public, yes_true_coordinates)
    except sp.ConsentError as exc:
        typer.echo(f"not published: {exc}", err=True)
        raise typer.Exit(2) from None
    if found is None:
        typer.echo(sp.INSTALL_HINT, err=True)
        raise typer.Exit(1)
    w = sp.Wrangler(found)
    try:
        result = sp.publish(p, public=yes_public, coordinates=yes_true_coordinates, wrangler=w,
                            origins=list(origin or []), progress=print_progress)
    except (sp.WranglerMissing, sp.NotLoggedIn, sp.WranglerFailed, ValueError) as exc:
        typer.echo("\n".join(w.transcript[-12:]), err=True)
        typer.echo(f"not published: {exc}", err=True)
        raise typer.Exit(1) from None
    for note in result.unread:
        typer.echo(f"warning: not read in the answer of wrangler: {note}", err=True)
    for text in result.unread_output:  # whole and scrubbed: to send to the developers
        typer.echo(text, err=True)
    if result.address:
        typer.echo(f"published: {result.address}")
    else:
        typer.echo("published; the address is shown in the Cloudflare dashboard (Workers & Pages) or by "
                   "`wrangler pages project list` (it was not found in the answer of wrangler)")
    if result.data_address:
        typer.echo(f"data: {result.data_address} (bucket {result.bucket}: {result.sent} sent, {result.unchanged} "
                   f"unchanged, {result.removed} deleted)")
    typer.echo("to restrict who can open it: Cloudflare Access (Zero Trust) in the dashboard; see the guide")
    if record is not None:
        doc = Document.open(record)
        sp.record_published(doc, result)
        doc.save()
        typer.echo(f"recorded in {record}")


def main():  # pragma: no cover - console entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
