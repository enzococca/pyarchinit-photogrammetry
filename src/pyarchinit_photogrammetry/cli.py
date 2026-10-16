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
app.add_typer(markers_app, name="markers")
app.add_typer(pa_app, name="pyarchinit")


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


@app.command()
def align(project: Path, quality: str = "high", pairs: str = "auto", keypoints: int = 40000,
          chunk: Optional[str] = None):
    """Match photos and align cameras."""
    doc, ch = _open(project, chunk)
    ch.match_photos(quality=quality, pair_selection=pairs, keypoint_limit=keypoints, progress=print_progress)
    n = ch.align_cameras(progress=print_progress)
    doc.save()
    typer.echo(f"aligned {n}/{len(ch.cameras)} photos")


@app.command()
def optimize(project: Path, markers: bool = typer.Option(True, help="control points (3 or more) take part in the "
                                                                   "adjustment; check points are reported"),
             marker_weight: int = typer.Option(4, help="a marker measurement counts as this many tie point observations"),
             principal_point: bool = False, chunk: Optional[str] = None):
    """Optimize the cameras (global bundle adjustment), on the control points when there are any."""
    doc, ch = _open(project, chunk)
    err = ch.optimize_cameras(fit_principal_point=principal_point, use_markers=markers, marker_weight=marker_weight,
                              progress=print_progress)
    if any(m.reference.location for m in ch.markers):
        ch.update_transform()
    doc.save()
    rep = ch.meta["sfm"]["optimize"]
    text = f"reprojection error {err:.3f} px"
    if rep.get("control_points"):
        b, a = rep["before"], rep["after"]
        text += (f", {rep['control_points']} control points: RMSE {b['control']} -> {a['control']} m"
                 + (f", check {b['check']} -> {a['check']} m" if a.get("check") is not None else ""))
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
def reference(project: Path, csv_file: Path, crs: Optional[str] = None, columns: str = "nxyz",
              delimiter: Optional[str] = None, skip_rows: int = 0, chunk: Optional[str] = None):
    """Import marker coordinates (GCP) and georeference the chunk."""
    doc, ch = _open(project, chunk)
    n = ch.import_reference(csv_file, crs=crs, columns=columns, delimiter=delimiter, skip_rows=skip_rows)
    report = ch.update_transform()
    doc.save()
    typer.echo(f"{n} reference points, method {report['method']}, CRS {report['crs']}, "
               f"control RMSE {report['control_rmse']}, check RMSE {report['check_rmse']}")


@app.command()
def dense(project: Path, quality: str = "medium", filter_mode: str = "moderate", backend: str = "auto",
          chunk: Optional[str] = None):
    """Depth maps and dense point cloud."""
    doc, ch = _open(project, chunk)
    ch.build_depth_maps(quality=quality, filter_mode=filter_mode, backend=backend, progress=print_progress)
    pc = ch.build_point_cloud(progress=print_progress)
    doc.save()
    typer.echo(f"{len(pc):,} points")


@app.command()
def mesh(project: Path,
         source: str = typer.Option("point_cloud", help="point_cloud, tie_points or depth_maps (TSDF fusion)"),
         surface: str = "arbitrary", faces: str = "medium",
         voxel_size: Optional[float] = typer.Option(None, help="depth_maps: voxel in the chunk units "
                                                               "(default: twice the depth-map pixel)"),
         classes: Optional[str] = typer.Option(None, help="dense cloud: only these " + CLASSES_HELP),
         texture: bool = True, texture_size: int = 4096, chunk: Optional[str] = None):
    """Mesh (Poisson, height field or fusion of the depth maps) and texture."""
    doc, ch = _open(project, chunk)
    m = ch.build_model(source=source, surface=surface, face_count=int(faces) if faces.isdigit() else faces,
                       voxel_size=voxel_size, classes=_classes(classes), progress=print_progress)
    if texture:
        ch.build_texture(texture_size=texture_size, progress=print_progress)
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
def ortho(project: Path, resolution: Optional[float] = None, surface: str = "dem", blending: str = "mosaic",
          hide_classes: Optional[str] = typer.Option(None, help="transparent where the surface is of these "
                                                                + CLASSES_HELP),
          chunk: Optional[str] = None):
    """Orthomosaic (GeoTIFF RGBA)."""
    doc, ch = _open(project, chunk)
    p = ch.build_orthomosaic(resolution=resolution, surface=surface, blending=blending,
                             hide_classes=_classes(hide_classes), progress=print_progress)
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
           classes: Optional[str] = typer.Option(None, help="--cloud: only these " + CLASSES_HELP),
           chunk: Optional[str] = None):
    """Export products in world coordinates."""
    _, ch = _open(project, chunk)
    for value, fn in ((ortho, ch.export_orthomosaic), (dem, ch.export_dem), (model, ch.export_model),
                      (cloud, lambda path: ch.export_point_cloud(path, classes=_classes(classes))),
                      (markers, ch.export_reference), (report, ch.generate_report)):
        if value:
            typer.echo(str(fn(value)))
    if cameras:
        typer.echo(str(ch.export_cameras(cameras, fmt="csv" if cameras.suffix == ".csv" else "colmap")))


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


@app.command()
def pottery(mesh_path: Path, out: Path = Path("profilo.svg"), scale: float = 1.0, title: Optional[str] = None,
            axis: Optional[str] = typer.Option(None, help="'x,y,z,dx,dy,dz' to impose the rotation axis")):
    """Profile drawing and measurements of a vessel/sherd model (SVG or DXF)."""
    import trimesh

    from .archaeo import pottery as pot

    m = trimesh.load(str(mesh_path), force="mesh")
    ax = None
    if axis:
        v = [float(x) for x in axis.split(",")]
        ax = pot.Axis(v[:3], v[3:])
    prof = pot.best_profile(m, ax)
    pot.draw(prof, out, scale=scale, title=title or mesh_path.stem)
    typer.echo(json.dumps(prof.measurements(), indent=1))


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
    """Project from a Metashape .psx: aligned cameras, calibration, markers, tie points, model, exported DEM/ortho."""
    from .integrations.metashape import import_psx

    doc = import_psx(psx, project, chunk_index=chunk, progress=print_progress)
    for ch in doc.chunks:
        typer.echo(f"{ch.label}: {len(ch.aligned_cameras)}/{len(ch.cameras)} cameras, CRS {ch.crs}, "
                   f"products {sorted(ch.products)}")


@app.command("import-webodm")
def import_webodm(source: Path, project: Path):
    """Project from a WebODM/ODM task folder or its all.zip (orthophoto, DEM, point cloud, mesh)."""
    from .integrations.webodm import import_task

    doc = import_task(source, project, progress=print_progress)
    ch = doc.chunk
    typer.echo(f"{ch.label}: CRS {ch.crs}, products {sorted(ch.products)}")


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


def main():  # pragma: no cover - console entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
