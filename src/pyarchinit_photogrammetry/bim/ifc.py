"""IFC4 export of photogrammetric models for BIM / HBIM.

The survey mesh becomes an ``IfcGeographicElement``; meshes split by
stratigraphic unit become one element per US with a ``Pset_PyArchInit_US``
property set (site, area, US, type, description, periods). Coordinates are
stored relative to a local origin and georeferenced with ``IfcMapConversion``
+ ``IfcProjectedCRS`` so that large map coordinates keep full precision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..core import crs as crs_mod


@dataclass
class IfcElement:
    mesh: object  # trimesh.Trimesh in world coordinates
    name: str
    ifc_class: str = "IfcGeographicElement"
    predefined_type: str | None = "TERRAIN"
    psets: dict[str, dict] = field(default_factory=dict)
    color: tuple[float, float, float] | None = None  # 0..1 RGB


def _require():
    try:
        import ifcopenshell  # noqa: F401
        import ifcopenshell.api  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise ImportError("IFC export needs ifcopenshell: pip install 'pyarchinit-photogrammetry[bim]'") from e


def export_ifc(path: str | Path, elements: list[IfcElement], crs: str | None = None, project: str = "Rilievo",
               site: str = "Sito", origin: np.ndarray | None = None) -> Path:
    _require()
    import ifcopenshell
    import ifcopenshell.api.aggregate
    import ifcopenshell.api.context
    import ifcopenshell.api.geometry
    import ifcopenshell.api.georeference
    import ifcopenshell.api.pset
    import ifcopenshell.api.root
    import ifcopenshell.api.spatial
    import ifcopenshell.api.style
    import ifcopenshell.api.unit

    model = ifcopenshell.file(schema="IFC4")
    proj = ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name=project)
    ifcopenshell.api.unit.assign_unit(model, length={"is_metric": True, "raw": "METERS"},
                                      area={"is_metric": True, "raw": "METERS"},
                                      volume={"is_metric": True, "raw": "METERS"})  # coordinates are metres
    ctx = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(model, context_type="Model", context_identifier="Body",
                                                target_view="MODEL_VIEW", parent=ctx)
    all_v = np.vstack([np.asarray(e.mesh.vertices) for e in elements]) if elements else np.zeros((1, 3))
    if origin is None:
        origin = np.floor(all_v.min(0))
    origin = np.asarray(origin, float)
    if not crs_mod.is_local(crs):
        ifcopenshell.api.georeference.add_georeferencing(model)
        epsg = crs_mod.epsg_code(crs)
        ifcopenshell.api.georeference.edit_georeferencing(
            model,
            projected_crs={"Name": f"EPSG:{epsg}" if epsg else str(crs)},
            coordinate_operation={"Eastings": float(origin[0]), "Northings": float(origin[1]),
                                  "OrthogonalHeight": float(origin[2]), "XAxisAbscissa": 1.0,
                                  "XAxisOrdinate": 0.0, "Scale": 1.0})
    site_e = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSite", name=site)
    ifcopenshell.api.aggregate.assign_object(model, products=[site_e], relating_object=proj)

    for el in elements:
        kwargs = {"ifc_class": el.ifc_class, "name": el.name}
        if el.predefined_type:
            kwargs["predefined_type"] = el.predefined_type
        prod = ifcopenshell.api.root.create_entity(model, **kwargs)
        ifcopenshell.api.spatial.assign_container(model, products=[prod], relating_structure=site_e)
        v = np.asarray(el.mesh.vertices, float) - origin
        f = np.asarray(el.mesh.faces, int)
        pts = model.createIfcCartesianPointList3D([tuple(map(float, p)) for p in v])
        faces = model.createIfcTriangulatedFaceSet(pts, None, None, [tuple(int(i) + 1 for i in t) for t in f], None)
        rep = model.createIfcShapeRepresentation(body, "Body", "Tessellation", [faces])
        ifcopenshell.api.geometry.assign_representation(model, product=prod, representation=rep)
        placement = model.createIfcLocalPlacement(None, model.createIfcAxis2Placement3D(
            model.createIfcCartesianPoint((0.0, 0.0, 0.0)), None, None))
        prod.ObjectPlacement = placement
        if el.color is not None:
            style = ifcopenshell.api.style.add_style(model, name=f"{el.name}_style")
            ifcopenshell.api.style.add_surface_style(model, style=style, ifc_class="IfcSurfaceStyleShading",
                                                     attributes={"SurfaceColour": {"Name": None, "Red": el.color[0],
                                                                                   "Green": el.color[1],
                                                                                   "Blue": el.color[2]}})
            ifcopenshell.api.style.assign_representation_styles(model, shape_representation=rep, styles=[style])
        for pset_name, props in el.psets.items():
            pset = ifcopenshell.api.pset.add_pset(model, product=prod, name=pset_name)
            ifcopenshell.api.pset.edit_pset(model, pset=pset, properties={k: v for k, v in props.items() if v is not None})
    path = Path(path)
    model.write(str(path))
    return path


def split_mesh_by_units(mesh_world, units) -> list[tuple[object, object]]:
    """Faces whose centroid falls inside each unit polygon (plan view). Returns ``[(unit, submesh)]``."""
    from shapely import contains_xy

    centers = mesh_world.triangles_center
    out = []
    for u in units:
        inside = contains_xy(u.geometry, centers[:, 0], centers[:, 1])
        if inside.any():
            out.append((u, mesh_world.submesh([np.nonzero(inside)[0]], append=True)))
    return out


def export_chunk_ifc(chunk, path: str | Path, units=None, us_records: dict | None = None,
                     site: str | None = None, include_survey: bool = True) -> Path:
    """Export the chunk model to IFC. ``units`` (list of :class:`UnitPolygon`) splits the
    mesh into one element per stratigraphic unit; ``us_records`` maps US numbers to the
    PyArchInit attributes to store in ``Pset_PyArchInit_US``."""
    mesh = chunk.model
    if mesh is None:
        raise RuntimeError("build the model first")
    mesh.apply_transform(chunk.transform)
    elements = []
    if include_survey:
        elements.append(IfcElement(mesh, f"Rilievo fotogrammetrico - {chunk.label}",
                                   psets={"Pset_Photogrammetry": {"Chunk": chunk.label, "CRS": chunk.crs,
                                                                  "Faces": int(len(mesh.faces))}}))
    rng = np.random.default_rng(1)
    for u, sub in split_mesh_by_units(mesh, units or []):
        rec = (us_records or {}).get(str(u.us), {})
        props = {"Sito": site, "Area": u.area, "US": u.us, "Tipo": u.tipo, **{k: _plain(v) for k, v in rec.items()}}
        elements.append(IfcElement(sub, f"{u.tipo} {u.us}", ifc_class="IfcGeographicElement",
                                   predefined_type="USERDEFINED", psets={"Pset_PyArchInit_US": props},
                                   color=tuple(rng.uniform(0.3, 0.9, 3))))
    return export_ifc(path, elements, crs=chunk.crs, project=chunk.label, site=site or chunk.label)


def _plain(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)
