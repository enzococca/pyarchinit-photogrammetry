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
    quantities: dict[str, float] = field(default_factory=dict)  # Qto: NetVolume, GrossArea, Height...
    material: str | None = None
    group: str | None = None  # IfcGroup (e.g. "Periodo 1 - Fase 2")
    key: str | None = None  # identifier used by ``relations``


def _require():
    try:
        import ifcopenshell  # noqa: F401
        import ifcopenshell.api  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise ImportError("IFC export needs ifcopenshell: pip install 'pyarchinit-photogrammetry[bim]'") from e


QTO_UNITS = {"NetVolume": "IfcQuantityVolume", "GrossVolume": "IfcQuantityVolume", "GrossArea": "IfcQuantityArea",
             "NetArea": "IfcQuantityArea", "Height": "IfcQuantityLength", "TopElevationMax": "IfcQuantityLength",
             "TopElevationMin": "IfcQuantityLength", "BottomElevationMin": "IfcQuantityLength"}


def export_ifc(path: str | Path, elements: list[IfcElement], crs: str | None = None, project: str = "Rilievo",
               site: str = "Sito", origin: np.ndarray | None = None, progress=None,
               relations: list[tuple[str, str, str]] | None = None) -> Path:
    """Write IFC4. ``relations``: ``(key_a, relation, key_b)`` between elements (stratigraphic
    relationships), written as ``IfcRelConnectsElements`` named after the relation."""
    from ..core.progress import as_progress

    progress = as_progress(progress)
    _require()
    progress(0.05, "IFC: progetto, unita' di misura e georeferenziazione")
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

    import ifcopenshell.api.group
    import ifcopenshell.api.material
    import ifcopenshell.guid

    products, groups, materials = {}, {}, {}
    for k, el in enumerate(elements):
        progress(0.1 + 0.85 * k / max(len(elements), 1), f"IFC: elemento {k + 1}/{len(elements)} - {el.name}")
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
        if el.quantities:
            qs = []
            for qname, val in el.quantities.items():
                cls = QTO_UNITS.get(qname, "IfcQuantityLength")
                attr = {"IfcQuantityVolume": "VolumeValue", "IfcQuantityArea": "AreaValue"}.get(cls, "LengthValue")
                qs.append(model.create_entity(cls, Name=qname, **{attr: float(val)}))
            qto = model.create_entity("IfcElementQuantity", GlobalId=ifcopenshell.guid.new(), Name="Qto_PyArchInit_US",
                                      MethodOfMeasurement="pyarchinit-photogrammetry: proxy solido dal DEM",
                                      Quantities=qs)
            model.create_entity("IfcRelDefinesByProperties", GlobalId=ifcopenshell.guid.new(), RelatedObjects=[prod],
                                RelatingPropertyDefinition=qto)
        if el.material:
            mat = materials.get(el.material)
            if mat is None:
                mat = materials[el.material] = ifcopenshell.api.material.add_material(model, name=el.material,
                                                                                         category="soil")
            ifcopenshell.api.material.assign_material(model, products=[prod], material=mat)
        if el.group:
            grp = groups.get(el.group)
            if grp is None:
                grp = groups[el.group] = ifcopenshell.api.group.add_group(model, name=el.group)
            ifcopenshell.api.group.assign_group(model, products=[prod], group=grp)
        if el.key:
            products[el.key] = prod
    for a, rel, b in relations or []:
        if a in products and b in products and a != b:
            model.create_entity("IfcRelConnectsElements", GlobalId=ifcopenshell.guid.new(), Name=rel,
                                Description="rapporto stratigrafico (PyArchInit)",
                                RelatingElement=products[a], RelatedElement=products[b])
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
                     site: str | None = None, include_survey: bool = True, progress=None) -> Path:
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
    return export_ifc(path, elements, crs=chunk.crs, project=chunk.label, site=site or chunk.label,
                      progress=progress)


def read_ifc_elements(path: str | Path) -> list[dict]:
    """Elements of an IFC file with their mesh in world coordinates and property sets
    (``[{"name", "ifc_class", "vertices", "faces", "psets"}]``): for previews and checks."""
    _require()
    import ifcopenshell
    import ifcopenshell.geom
    import ifcopenshell.util.element as ue

    f = ifcopenshell.open(str(path))
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    offset = np.zeros(3)
    for mc in f.by_type("IfcMapConversion"):
        offset = np.array([mc.Eastings or 0.0, mc.Northings or 0.0, mc.OrthogonalHeight or 0.0], float)
    out = []
    for e in f.by_type("IfcProduct"):
        if e.Representation is None or e.is_a("IfcSite"):
            continue
        try:
            shp = ifcopenshell.geom.create_shape(settings, e)
        except Exception:
            continue
        v = np.array(shp.geometry.verts, float).reshape(-1, 3) + offset
        fc = np.array(shp.geometry.faces, int).reshape(-1, 3)
        out.append({"name": e.Name, "ifc_class": e.is_a(), "vertices": v, "faces": fc,
                    "psets": {k: {kk: vv for kk, vv in val.items() if kk != "id"}
                              for k, val in ue.get_psets(e).items()}})
    return out


def _plain(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def export_proxies_ifc(proxies, path: str | Path, crs: str | None = None, us_records: dict | None = None,
                       site: str | None = None, survey_mesh=None, project: str = "Rilievo", progress=None) -> Path:
    """IFC4 with one solid ``IfcGeographicElement`` per unit proxy (see :mod:`archaeo.proxies`):
    quantities for the bill of quantities (``Qto_PyArchInit_US``: NetVolume, GrossArea, Height,
    elevations), the US form in ``Pset_PyArchInit_US``, a material per formation, groups per
    period/phase and the stratigraphic relationships as ``IfcRelConnectsElements``.
    ``survey_mesh`` (world coordinates) adds the photogrammetric surface as context."""
    from ..pyarchinit.bridge import parse_relationships

    us_records = us_records or {}
    elements, relations = [], []
    palette = [(0.9, 0.1, 0.29), (0.24, 0.71, 0.29), (1.0, 0.88, 0.1), (0.26, 0.39, 0.85), (0.96, 0.51, 0.19),
               (0.57, 0.12, 0.71), (0.27, 0.94, 0.94), (0.94, 0.2, 0.9)]
    if survey_mesh is not None:
        elements.append(IfcElement(survey_mesh, f"Rilievo fotogrammetrico - {project}", color=(0.8, 0.8, 0.8)))
    for i, p in enumerate(proxies):
        rec = us_records.get(str(p.us), {})
        props = {"Sito": site, "Area": p.area, "US": p.us, "Tipo": p.tipo, **{k: _plain(v) for k, v in rec.items()}}
        per = rec.get("periodo_iniziale")
        fase = rec.get("fase_iniziale")
        group = f"Periodo {per} - Fase {fase}" if per else None
        material = rec.get("formazione") or rec.get("d_stratigrafica") or None
        qty = {"NetVolume": p.volume_m3, "GrossArea": p.area_m2, "Height": p.z_top[1] - p.z_bottom[0],
               "TopElevationMax": p.z_top[1], "TopElevationMin": p.z_top[0], "BottomElevationMin": p.z_bottom[0]}
        elements.append(IfcElement(p.mesh, p.name, ifc_class="IfcGeographicElement", predefined_type="USERDEFINED",
                                   psets={"Pset_PyArchInit_US": props}, color=palette[i % len(palette)],
                                   quantities=qty, material=str(material) if material else None, group=group,
                                   key=str(p.us)))
        for rel, other, _area, _site in parse_relationships(rec.get("rapporti")):
            relations.append((str(p.us), rel, str(other)))
    return export_ifc(path, elements, crs=crs, project=project, site=site or project, progress=progress,
                      relations=relations)
