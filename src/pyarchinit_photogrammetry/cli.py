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
def mesh(project: Path, surface: str = "arbitrary", faces: str = "medium", texture: bool = True,
         texture_size: int = 4096, chunk: Optional[str] = None):
    """Mesh (Poisson or height field) and texture."""
    doc, ch = _open(project, chunk)
    m = ch.build_model(surface=surface, face_count=int(faces) if faces.isdigit() else faces, progress=print_progress)
    if texture:
        ch.build_texture(texture_size=texture_size, progress=print_progress)
    doc.save()
    typer.echo(f"{len(m.faces):,} faces")


@app.command()
def dem(project: Path, resolution: Optional[float] = None, source: str = "point_cloud", chunk: Optional[str] = None):
    """Digital elevation model (GeoTIFF)."""
    doc, ch = _open(project, chunk)
    p = ch.build_dem(resolution=resolution, source=source, progress=print_progress)
    doc.save()
    typer.echo(str(p))


@app.command()
def ortho(project: Path, resolution: Optional[float] = None, surface: str = "dem", blending: str = "mosaic",
          chunk: Optional[str] = None):
    """Orthomosaic (GeoTIFF RGBA)."""
    doc, ch = _open(project, chunk)
    p = ch.build_orthomosaic(resolution=resolution, surface=surface, blending=blending, progress=print_progress)
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


@app.command()
def export(project: Path, ortho: Optional[Path] = None, dem: Optional[Path] = None, model: Optional[Path] = None,
           cloud: Optional[Path] = None, cameras: Optional[Path] = None, markers: Optional[Path] = None,
           report: Optional[Path] = None, chunk: Optional[str] = None):
    """Export products in world coordinates."""
    _, ch = _open(project, chunk)
    for value, fn in ((ortho, ch.export_orthomosaic), (dem, ch.export_dem), (model, ch.export_model),
                      (cloud, ch.export_point_cloud), (markers, ch.export_reference), (report, ch.generate_report)):
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
            source: str = "model", label: str = "Sezione", scale: float = 20.0, chunk: Optional[str] = None):
    """Vertical section along a trace (world coordinates) to DXF/SVG."""
    from .archaeo import sections as sec_mod

    _, ch = _open(project, chunk)
    s = sec_mod.section(ch, (x1, y1), (x2, y2), source=source, label=label)
    p = s.to_svg(out, scale=scale) if out.suffix.lower() == ".svg" else s.to_dxf(out)
    typer.echo(f"{p} ({len(s.polylines)} polylines, length {s.length:.3f} m)")


@app.command()
def contours(project: Path, interval: float = 0.05, out: Path = Path("curve.dxf"), chunk: Optional[str] = None):
    """Contour lines from the DEM (DXF or GeoJSON)."""
    from .archaeo import terrain

    _, ch = _open(project, chunk)
    lines = terrain.contours(ch.dem_path, interval)
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
        units, records = pa.us_polygons(sito), pa.us_records(sito)
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
         process: bool = typer.Option(False, help="also run the whole pipeline (about two minutes)"),
         pyarchinit: bool = typer.Option(True, help="create the demo PyArchInit database")):
    """Write demo data: trench photos + GCPs, a ready project, pottery meshes, a PyArchInit database."""
    from .demo import create_demo

    d = create_demo(folder, process=process, with_pyarchinit=pyarchinit, progress=print_progress)
    typer.echo(f"Project:    {d.project}")
    typer.echo(f"Pottery:    {d.vessel}  {d.sherd}")
    if d.pyarchinit_home:
        typer.echo(f"PyArchInit: export PYARCHINIT_HOME={d.pyarchinit_home}")
    typer.echo(f"Read {d.folder / 'LEGGIMI.txt'}; open it in the interface with: ppg-gui {d.project}")


@app.command()
def gui(project: Optional[Path] = typer.Argument(None)):
    """Open the desktop interface (needs the [gui] extra)."""
    from .gui import main as gui_main

    raise SystemExit(gui_main([str(project)] if project else []))


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
