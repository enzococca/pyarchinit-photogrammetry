"""IFC4 export of photogrammetric models for BIM / HBIM.

The survey mesh becomes an ``IfcGeographicElement``; meshes split by
stratigraphic unit become one element per US with a ``Pset_PyArchInit_US``
property set (site, area, US, type, description, periods). Coordinates are
stored relative to a local origin and georeferenced with ``IfcMapConversion``
+ ``IfcProjectedCRS`` so that large map coordinates keep full precision.

A unit whose US form has a ``node_uuid`` (the id of its node in the Extended
Matrix / s3dgraphy, which StratiGraph shares) gets it as its ``GlobalId``
(:func:`global_id`): the same unit has the same identifier in the matrix, in
PyArchInit and in the BIM model. The stratigraphic relations of the forms are
``IfcRelConnectsElements``, one per fact (:func:`archaeo.stratigraphy.stratigraphic_edges`),
named after the relation and described by the edge of the Extended Matrix.
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
    global_id: str | None = None  # IFC GlobalId (default a new one)


def global_id(node_uuid) -> str | None:
    """The IFC ``GlobalId`` of a unit from the ``node_uuid`` of its US form (the 22 characters of
    IFC for the same UUID), None when it is not a UUID."""
    import uuid

    _require()
    import ifcopenshell.guid

    try:
        return ifcopenshell.guid.compress(uuid.UUID(str(node_uuid).strip()).hex)
    except (TypeError, ValueError, AttributeError):
        return None


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
               relations: list[tuple[str, str, str]] | None = None, spot_heights: list[dict] | None = None) -> Path:
    """Write IFC4. ``relations``: ``(key_a, relation, key_b[, edge])`` between elements
    (stratigraphic relationships), written as ``IfcRelConnectsElements`` named after the relation
    and described by ``edge`` (of the Extended Matrix) when given. Elements with a ``global_id``
    keep it (once: a second element with the same id gets a new one).
    ``spot_heights``: points ``{"x", "y", "z", "name", "props"}`` (world) written as
    ``IfcAnnotation`` (object type ``SpotHeight``) with ``props`` in ``Pset_PyArchInit_Quota``."""
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
    spots = [q for q in (spot_heights or []) if q.get("z") is not None]
    all_v = np.vstack([np.asarray(e.mesh.vertices) for e in elements] + [np.array([[q["x"], q["y"], q["z"]]
                                                                                     for q in spots]).reshape(-1, 3)])
    if not len(all_v):
        all_v = np.zeros((1, 3))
    if origin is None:
        origin = np.floor(all_v.min(0))
    origin = np.asarray(origin, float)
    if not crs_mod.is_local(crs):
        ifcopenshell.api.georeference.add_georeferencing(model)
        # a compound system (heights above a geoid, EPSG:7792+3855): the projected system of its plan and the vertical
        # datum of its heights, as IFC names them
        epsg, vertical = crs_mod.epsg_code(crs), crs_mod.vertical_part(crs)
        ifcopenshell.api.georeference.edit_georeferencing(
            model,
            projected_crs={"Name": f"EPSG:{epsg}" if epsg else str(crs_mod.horizontal(crs)),
                           **({"VerticalDatum": vertical} if vertical else {})},
            coordinate_operation={"Eastings": float(origin[0]), "Northings": float(origin[1]),
                                  "OrthogonalHeight": float(origin[2]), "XAxisAbscissa": 1.0,
                                  "XAxisOrdinate": 0.0, "Scale": 1.0})
    site_e = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSite", name=site)
    ifcopenshell.api.aggregate.assign_object(model, products=[site_e], relating_object=proj)

    import ifcopenshell.api.group
    import ifcopenshell.api.material
    import ifcopenshell.guid

    products, groups, materials, ids = {}, {}, {}, set()
    for k, el in enumerate(elements):
        progress(0.1 + 0.85 * k / max(len(elements), 1), f"IFC: elemento {k + 1}/{len(elements)} - {el.name}")
        kwargs = {"ifc_class": el.ifc_class, "name": el.name}
        if el.predefined_type:
            kwargs["predefined_type"] = el.predefined_type
        prod = ifcopenshell.api.root.create_entity(model, **kwargs)
        if el.global_id and el.global_id not in ids:
            prod.GlobalId = el.global_id
        ids.add(prod.GlobalId)
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
    if spots:
        annotation = ifcopenshell.api.context.add_context(model, context_type="Plan", context_identifier="Annotation",
                                                          target_view="PLAN_VIEW", parent=ctx)
    for q in spots:
        prod = model.create_entity("IfcAnnotation", GlobalId=ifcopenshell.guid.new(), Name=q.get("name") or "Quota",
                                   ObjectType="SpotHeight")
        ifcopenshell.api.spatial.assign_container(model, products=[prod], relating_structure=site_e)
        at = np.array([q["x"], q["y"], q["z"]], float) - origin
        prod.ObjectPlacement = model.createIfcLocalPlacement(None, model.createIfcAxis2Placement3D(
            model.createIfcCartesianPoint(tuple(map(float, at))), None, None))
        rep = model.createIfcShapeRepresentation(annotation, "Annotation", "Point",
                                                 [model.createIfcCartesianPoint((0.0, 0.0, 0.0))])
        prod.Representation = model.createIfcProductDefinitionShape(None, None, [rep])
        props = {k: _plain(v) for k, v in (q.get("props") or {}).items() if v not in (None, "")}
        if props:
            pset = ifcopenshell.api.pset.add_pset(model, product=prod, name="Pset_PyArchInit_Quota")
            ifcopenshell.api.pset.edit_pset(model, pset=pset, properties=props)
    for a, rel, b, *edge in relations or []:
        if a in products and b in products and a != b:
            edge = edge[0] if edge else None
            text = f"{edge} (Extended Matrix)" if edge else "rapporto stratigrafico (PyArchInit)"
            model.create_entity("IfcRelConnectsElements", GlobalId=ifcopenshell.guid.new(), Name=rel,
                                Description=text, RelatingElement=products[a], RelatedElement=products[b])
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


def spot_heights(quote: list[dict]) -> list[dict]:
    """Spot heights of the project (``x``, ``y`` and the columns of ``pyarchinit_quote``; with
    ``precisione_q``, the standard deviation of the height) as points for :func:`export_ifc`."""
    out = []
    for q in quote or []:
        z = q.get("quota_q")
        try:
            z = float(z)
        except (TypeError, ValueError):
            continue
        props = {"Sito": q.get("sito_q"), "Area": q.get("area_q"), "US": q.get("us_q"), "Quota": z,
                 "UnitaMisura": q.get("unita_misu_q"), "Tipo": q.get("unita_tipo_q"),
                 "Precisione": q.get("precisione_q"), "Data": q.get("data"), "Disegnatore": q.get("disegnatore"),
                 "RilievoOriginale": q.get("rilievo_originale")}
        name = f"Quota {z:.3f}" + (f" US {q['us_q']}" if q.get("us_q") else "")
        out.append({"x": float(q["x"]), "y": float(q["y"]), "z": z, "name": name, "props": props})
    return out


def export_chunk_ifc(chunk, path: str | Path, units=None, us_records: dict | None = None,
                     site: str | None = None, include_survey: bool = True, progress=None,
                     quote: list[dict] | None = None) -> Path:
    """Export the chunk model to IFC. ``units`` (list of :class:`UnitPolygon`) splits the
    mesh into one element per stratigraphic unit; ``us_records`` maps US numbers to the
    PyArchInit attributes to store in ``Pset_PyArchInit_US``. ``quote``: the spot heights of
    the project (default those saved in the chunk), with their precision when estimated."""
    import trimesh

    from ..archaeo.stratigraphy import stratigraphic_edges
    from ..pyarchinit.bridge import parse_relationships

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
    parts: dict[tuple, list] = {}  # the polygons of one unit make one element
    for u, sub in split_mesh_by_units(mesh, units or []):
        parts.setdefault((u.tipo, str(u.us)) if u.us else (u.tipo, None, id(u)), []).append((u, sub))
    declared = []
    for pieces in parts.values():
        u = pieces[0][0]
        sub = trimesh.util.concatenate([s for _, s in pieces]) if len(pieces) > 1 else pieces[0][1]
        rec = (us_records or {}).get(str(u.us), {}) if u.us else {}
        props = {"Sito": site, "Area": u.area, "US": u.us, "Tipo": u.tipo, **{k: _plain(v) for k, v in rec.items()}}
        elements.append(IfcElement(sub, f"{u.tipo} {u.us or '?'}", ifc_class="IfcGeographicElement",
                                   predefined_type="USERDEFINED", psets={"Pset_PyArchInit_US": props},
                                   color=tuple(rng.uniform(0.3, 0.9, 3)), key=str(u.us) if u.us else None,
                                   global_id=global_id(rec.get("node_uuid"))))
        if u.us:
            declared.append((str(u.us), parse_relationships(rec.get("rapporti"))))
    if quote is None:
        quote = ((chunk.meta or {}).get("drawings") or {}).get("quote") or []
    return export_ifc(path, elements, crs=chunk.crs, project=chunk.label, site=site or chunk.label,
                      progress=progress, spot_heights=spot_heights(quote), relations=stratigraphic_edges(declared))


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
        out.append({"name": e.Name, "ifc_class": e.is_a(), "global_id": e.GlobalId, "vertices": v, "faces": fc,
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
    period/phase, the ``node_uuid`` of the form as ``GlobalId`` and the stratigraphic
    relationships as ``IfcRelConnectsElements`` (see the module docstring).
    ``survey_mesh`` (world coordinates) adds the photogrammetric surface as context."""
    from ..archaeo.stratigraphy import stratigraphic_edges
    from ..pyarchinit.bridge import parse_relationships

    us_records = us_records or {}
    elements, declared = [], []
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
                                   key=str(p.us), global_id=global_id(rec.get("node_uuid"))))
        declared.append((str(p.us), parse_relationships(rec.get("rapporti"))))
    return export_ifc(path, elements, crs=crs, project=project, site=site or project, progress=progress,
                      relations=stratigraphic_edges(declared))
