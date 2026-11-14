"""What a plugin is and what it can add.

A plugin is a Python package that declares, in its ``pyproject.toml``::

    [project.entry-points."pyarchinit_photogrammetry.plugins"]
    chunk-summary = "ppg_chunk_summary:SummaryPlugin"

The object named there is a subclass of :class:`Plugin` (or an instance of one). When the
program starts, every enabled plugin is created and its :meth:`Plugin.activate` receives a
:class:`Context`, through which it adds menu actions to the window, commands to ``ppg plugin``,
export formats to *File > Esporta*, and runs long work in the background with a progress bar.
Everything it registers goes away with it when it is disabled.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

API_VERSION = 1  # raised when a change of this module breaks plugins written for the previous one


class Plugin:
    """Base of the plugins. Subclasses set the attributes and override :meth:`activate`."""

    name: str = ""  # identifier: lowercase letters, digits and dashes (the entry point's name when empty)
    title: str = ""  # shown in the interface (the name when empty)
    version: str = ""  # the version of the distribution when empty
    description: str = ""
    api: int = API_VERSION  # the plugin API it was written for

    def activate(self, context: Context) -> None:
        """Register actions, commands and exporters on ``context``."""

    def deactivate(self) -> None:
        """Release what :meth:`activate` took beyond its registrations (those are dropped by
        themselves): open files, threads, timers."""


@dataclass
class Action:
    """A menu action of a plugin: ``callback()`` when chosen."""

    title: str
    callback: Callable[[], object]
    menu: str = ""  # a submenu of *Plugin* ("" = the plugin's own submenu)
    tip: str = ""
    processing: bool = False  # disabled while a step runs, as the processing actions of the window
    plugin: str = ""
    # the QAction of the window for it (menu Plugin, rails); None in the command line or before the window made it
    qaction: object = field(default=None, compare=False, repr=False)


@dataclass
class Command:
    """A command of ``ppg plugin NAME ARGS...``: ``function(*args)`` with the arguments as
    strings; an int returned is the exit code."""

    name: str
    function: Callable[..., object]
    help: str = ""
    plugin: str = ""


@dataclass
class Exporter:
    """An export format of *File > Esporta*: ``function(chunk, path, progress)`` writes ``path``."""

    title: str
    suffix: str
    function: Callable[..., object]
    plugin: str = ""


@dataclass
class RailGroup:
    """Since 1.7.4: a group of a plugin in a tool rail of the window (:meth:`Context.add_rail_group`)."""

    key: str
    title: str
    icon: object  # a QIcon or the name of an icon of the program
    actions: list  # the plugin's Action (from add_action) or QAction objects
    view: str = "2d"  # "2d": the rail of the 2D view; "3d": that of the 3D view
    plugin: str = ""


def _rail_item(item) -> bool:
    """An item a rail group takes: an :class:`Action`, a QAction or None (a line)."""
    if item is None or isinstance(item, Action):
        return True
    try:
        from PySide6.QtGui import QAction
    except ImportError:  # no Qt: no QAction can exist
        return False
    return isinstance(item, QAction)


@dataclass
class Contributions:
    actions: list = field(default_factory=list)
    commands: dict = field(default_factory=dict)
    exporters: list = field(default_factory=list)
    connections: list = field(default_factory=list)  # (signal, slot) disconnected when the plugin goes away
    tree_items: list = field(default_factory=list)  # (plugin, provider) of the workspace, taken out with it
    rasters: list = field(default_factory=list)  # entries of engine.rastercalc.register, taken out with it
    rail_groups: list = field(default_factory=list)  # RailGroup of the tool rails, taken out with it
    site: list = field(default_factory=list)  # providers of io.site_plugins (the web site), taken out with it


class Context:
    """What a plugin receives in :meth:`Plugin.activate`: the open project, the window (None in
    the command line), a folder and settings of its own, a logger, and the registrations."""

    def __init__(self, name: str, folder: Path, window=None):
        self.name = name
        self.window = window
        self._folder = Path(folder)
        self._settings: dict | None = None
        self.contributions = Contributions()
        self.log = logging.getLogger(f"pyarchinit_photogrammetry.plugins.{name}")
        self._panes: list = []  # widgets given to add_pane: out of the window with release()
        self._stops: list = []  # the listeners of the transform (on_transform_changed): stopped with release()
        self.released = False  # release() called: what it gave the window is gone

    @property
    def app(self):
        """The application state shared with scripts: ``context.app.document.chunk``."""
        from ..app import app

        return app

    @property
    def data_dir(self) -> Path:
        """A folder for the plugin's own files (created when first asked for)."""
        self._folder.mkdir(parents=True, exist_ok=True)
        return self._folder

    @property
    def settings(self) -> dict:
        """The plugin's settings, kept between sessions with :meth:`save_settings`."""
        if self._settings is None:
            try:
                self._settings = json.loads((self._folder / "settings.json").read_text())
            except (OSError, ValueError):
                self._settings = {}
        return self._settings

    def save_settings(self) -> None:
        (self.data_dir / "settings.json").write_text(json.dumps(self.settings, indent=1))

    def add_action(self, title: str, callback: Callable[[], object], menu: str = "", tip: str = "",
                   processing: bool = False) -> Action:
        """A menu action under *Plugin* (in the plugin's submenu, or in ``menu``). Since 1.7.4 also when added
        after the plugin was activated: the window puts it in the menu then (and the search of the commands
        finds it) - only in the interface thread (``activate``, an action, ``done``), never from :meth:`run`."""
        action = Action(title, callback, menu, tip, processing, self.name)
        self.contributions.actions.append(action)
        self._tell_window()
        return action

    def add_rail_group(self, key: str, title: str, icon="plugin", actions=(), view: str = "2d") -> RailGroup:
        """Since 1.7.4: a group of the plugin in the tool rail of the 2D view (``view`` "2d") or of the 3D view
        ("3d"), after the program's own groups: its button with ``icon`` (a QIcon or the name of an icon of the
        program) opens a flyout with ``actions`` - the plugin's :class:`Action` objects returned by
        :meth:`add_action` (the same as in the menu *Plugin*) or QActions of its own. A plugin with a group of its
        own is not listed in the program's *Plugin* group of the rail. ``None`` among the actions draws a line
        between them; anything else raises TypeError at once. Called again with the same ``key`` and ``view`` it
        replaces the group. Taken away with the plugin (:meth:`release`); kept and shown nowhere in the command
        line. Only in the interface thread (``activate``, an action, ``done``), as :meth:`add_action`."""
        if view not in ("2d", "3d"):
            raise ValueError(f"view: '2d' or '3d', not {view!r}")
        actions = list(actions)
        bad = [a for a in actions if not _rail_item(a)]
        if bad:
            raise TypeError("add_rail_group: the actions are Action objects (from add_action), QActions or None "
                            f"(a line between them), not {', '.join(repr(b) for b in bad)}")
        group = RailGroup(str(key), str(title), icon, actions, view, self.name)
        groups = self.contributions.rail_groups
        groups[:] = [g for g in groups if not (g.key == group.key and g.view == view)]
        groups.append(group)
        self._tell_window()
        return group

    def _tell_window(self) -> None:
        """The window, once it has made the menu of the plugins, shows what was added now (menu, rails, search)."""
        method = getattr(self.window, "plugin_contributions_changed", None)
        if method is not None:
            method(self)

    def add_command(self, name: str, function: Callable[..., object], help: str = "") -> Command:
        """A command of ``ppg plugin NAME``; the name must not be taken by another plugin."""
        command = Command(name, function, help, self.name)
        self.contributions.commands[name] = command
        return command

    def add_exporter(self, title: str, suffix: str, function: Callable[..., object]) -> Exporter:
        """An export format in *File > Esporta* (``suffix`` like ".csv")."""
        exporter = Exporter(title, suffix if suffix.startswith(".") else "." + suffix, function, self.name)
        self.contributions.exporters.append(exporter)
        return exporter

    def store_drawings(self) -> None:
        """Write what the window shows (units and spot heights drawn, stones, sections) into the
        project, so that ``chunk.meta`` and the files beside the project hold it; nothing in the
        command line, where the project read from its file already holds everything. Only in the
        interface thread (an action, ``done``): the window does it itself before an exporter runs."""
        window = self.window
        if window is None:
            return
        import threading

        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("store_drawings() reads the window: call it in the interface thread, "
                               "not in the function given to run() or in an exporter")
        for name in ("_store_drawings", "_save_sections"):
            method = getattr(window, name, None)
            if method is not None:
                method()

    # ------------------------------------------------------------------ the window (since 1.3.2)
    def request_polygon(self, on_finished: Callable, on_cancelled: Callable | None = None, hint: str = "") -> bool:
        """Let the user draw a polygon on the 2D view with the program's own tool (drag a box, or click
        the vertices and double click or Enter; Backspace drops the last vertex, Esc cancels):
        ``on_finished(xy)`` gets the vertices, an ``(N, 2)`` array in the coordinates of the chunk (in the ``(u, v)``
        metres of the elevation or section the window shows, when it shows one), in the interface thread; ``on_cancelled()`` when the user picks another tool or another request comes.
        ``hint``: the text of the status bar. False (and ``on_cancelled``) when there is nothing to draw
        on; in the command line there is no drawing (``RuntimeError``)."""
        return self._request("polygon", on_finished, on_cancelled, hint)

    def request_line(self, on_finished: Callable, on_cancelled: Callable | None = None, hint: str = "") -> bool:
        """As :meth:`request_polygon` for a line of two or more vertices (double click or Enter ends)."""
        return self._request("line", on_finished, on_cancelled, hint)

    def request_point(self, on_finished: Callable, on_cancelled: Callable | None = None, hint: str = "") -> bool:
        """As :meth:`request_polygon` for one point: ``on_finished`` gets a ``(1, 2)`` array."""
        return self._request("point", on_finished, on_cancelled, hint)

    def _request(self, kind, on_finished, on_cancelled, hint) -> bool:
        if self.window is None:
            raise RuntimeError("drawing on the view needs the window: not in the command line")
        return self.window.request_geometry(kind, on_finished, on_cancelled, hint)

    def on_raster_loaded(self, callback: Callable[[], object]) -> None:
        """``callback()`` each time the 2D view shows another raster, an empty frame or nothing: the
        layers and overlays of a plugin are gone with the old one and are drawn again there.
        Disconnected by itself when the plugin is disabled. Nothing in the command line."""
        view = getattr(self.window, "ortho_view", None)
        if view is None:
            return
        view.raster_loaded.connect(callback)
        self.contributions.connections.append((view.raster_loaded, callback))

    def on_refresh(self, callback: Callable[[], object]) -> None:
        """Since 1.3.3: ``callback()`` each time the window shows the project again - another project
        opened, another active chunk, a processing step done, an undo: a pane of the plugin follows the
        active chunk there. Disconnected by itself when the plugin is disabled. Nothing in the command
        line."""
        signal = getattr(self.window, "refreshed", None)
        if signal is None:
            return
        signal.connect(callback)
        self.contributions.connections.append((signal, callback))

    def show_frame(self, bounds, resolution: float, text: str = "") -> None:
        """An empty frame on the 2D view (``bounds`` = xmin, ymin, xmax, ymax in the coordinates of the
        chunk, ``resolution`` metres per pixel of the view) when the chunk has no orthophoto or DEM to
        draw on: then layers and overlays can be added as usual."""
        view = getattr(self.window, "ortho_view", None)
        if view is None:
            raise RuntimeError("the 2D view needs the window: not in the command line")
        frame = getattr(self.window, "_set_drawings_frame", None)
        if frame is not None:  # a frame of the chunk's coordinates: the drawings of the plan, not of an elevation
            frame(None)
        view.show_frame(bounds, resolution, text)

    def mark_modified(self) -> None:
        """The project has changes to save (``chunk.meta`` or files of the plugin changed): the window
        shows it and asks to save at closing. Nothing in the command line, where the plugin saves."""
        method = getattr(self.window, "mark_modified", None)
        if method is not None:
            method()

    def call_in_gui(self, function: Callable, *args, **kwargs) -> None:
        """``function(*args, **kwargs)`` in the interface thread, soon: from the function given to
        :meth:`run` or from an exporter, for anything that touches the window. At once in the command
        line."""
        method = getattr(self.window, "call_in_gui", None)
        if method is None:
            function(*args, **kwargs)
        else:
            method(function, *args, **kwargs)

    # ------------------------------------------------------------------ chunks and views (since 1.3.4)
    def activate_chunk(self, chunk) -> None:
        """Since 1.3.4: ``chunk`` (of the open project) becomes the active one. In the window as when the
        user picks it in the workspace: the drawings of the chunk left stay with it, those of ``chunk``
        come back, the views show it and the ``on_refresh`` callbacks are called; nothing happens when it
        is the active one already. In the command line it becomes the active chunk of its project. Since 1.7.5 a
        RuntimeError while the window appends projects (*File > Aggiungi progetti*: no chunk changes then)."""
        window = self.window
        doc = getattr(window, "doc", None) if window is not None else getattr(chunk, "document", None)
        if doc is None or not any(c is chunk for c in doc.chunks):
            raise ValueError("the chunk does not belong to the open project")
        activate = getattr(window, "_activate_chunk", None)
        if activate is None:
            doc.chunk = chunk
        elif activate(chunk) is False:
            raise RuntimeError("the window is appending projects: the active chunk cannot change now")

    def show_raster(self, kind: str | None = None, chunk=None, path=None) -> bool:
        """Since 1.3.4: the orthophoto (``kind`` "ortho") or the DEM ("dem") of ``chunk`` in the 2D view,
        with the drawings of the window on top, as the window shows them; None: the orthophoto when the
        chunk has one, else the DEM. ``chunk``: the active one when None, else it becomes the active one
        first (:meth:`activate_chunk`). ``path``: another GeoTIFF of that kind instead of the product (a
        kept version, ``core.versions.version_file(chunk, "dem", id)``; a raster of the plugin). Returns
        whether it is shown: False when there is nothing of that kind (the view stays as it was), and in
        the command line."""
        if kind not in (None, "ortho", "dem"):
            raise ValueError("kind: 'ortho', 'dem' or None")
        if path is not None and kind is None:
            raise ValueError("a path is shown as 'ortho' or 'dem': give the kind")
        window = self.window
        view = getattr(window, "ortho_view", None)
        if view is None:
            return False
        if chunk is not None:
            self.activate_chunk(chunk)
        ch = window.chunk
        if ch is None:
            return False
        if kind is None:
            kind = "ortho" if ch.orthomosaic_path is not None and Path(ch.orthomosaic_path).exists() else "dem"
        file = path if path is not None else (ch.orthomosaic_path if kind == "ortho" else ch.dem_path)
        if file is None or not Path(file).exists():
            return False
        if path is None:
            window._show_raster(kind)
        else:
            window._load_raster(kind, path)
            window.show_view(view)
        return True

    def show_model(self, kind: str | None = None, chunk=None, path=None, title: str = "") -> bool:
        """Since 1.3.4: ``kind`` of ``chunk`` in the 3D view: "model", "model_textured", "point_cloud",
        "tie_points", "cameras", "proxies"; None: the best the chunk has (as after a processing step).
        ``chunk``: the active one when None, else it becomes the active one first (:meth:`activate_chunk`).
        ``path``: a mesh (``kind`` "model") or a point cloud ("point_cloud") of the plugin, a PLY in the
        internal frame of the chunk as its own products, shown instead of them and named ``title`` beside
        the list *Mostra* until the user chooses another view there. Returns whether it is shown: False
        when the chunk has nothing of that kind (the view stays as it was), and in the command line."""
        if path is not None and kind not in ("model", "point_cloud"):
            raise ValueError("a file of the plugin is shown as 'model' or 'point_cloud'")
        window = self.window
        view = getattr(window, "model_view", None)
        if view is None:
            return False
        from ..gui.model_view import VIEW_LABELS
        from ..gui.scene import available_views

        if kind is not None and kind not in VIEW_LABELS:
            raise ValueError(f"kind: one of {', '.join(VIEW_LABELS)} or None")
        if chunk is not None:
            self.activate_chunk(chunk)
        ch = window.chunk
        if ch is None:
            return False
        if path is not None:
            if not Path(path).exists():
                return False
            view.show_version(kind, path, title or Path(path).stem, version=False)
            window.show_view(view)
        elif kind is not None and kind not in ["cameras"] + available_views(ch):
            return False
        else:
            window._show_model(kind)
        return True

    def on_features_deleted(self, layer: str, callback: Callable[[list], object]) -> None:
        """Since 1.3.4: ``callback(features)`` when the user deletes features of ``layer`` on the 2D view
        (the tool *Seleziona* and Canc, *Elimina selezionati* in the menu of the selection or in the
        drawing bar): ``features`` are their attributes, a list of dicts as the layer held them (``_geom``
        is the type and the coordinates), for the plugin to drop its records of them. A layer taken out
        of the view (the pane *Layer*, another raster shown) is not a deletion. Disconnected by itself
        when the plugin is disabled. Nothing in the command line."""
        signal = getattr(self.window, "features_deleted", None)
        if signal is None:
            return

        def deleted(name, features):
            if name != layer:
                return
            try:
                callback(features)
            except Exception as exc:  # a plugin never takes the window down
                report = getattr(self.window, "_exc", None)
                if report is None:
                    self.log.exception("the plugin failed on the features deleted in %s", layer)
                else:
                    from ..gui.i18n import tr

                    report(tr("Plugin {0}", self.name), exc)

        signal.connect(deleted)
        self.contributions.connections.append((signal, deleted))

    def add_tree_items(self, provider: Callable[[object], list]) -> None:
        """Since 1.3.4: the products of the plugin in the workspace (*Area di lavoro*), under each chunk
        after those of the program. ``provider(chunk)`` returns the items of ``chunk``, each a dict:
        ``label``, ``icon`` (a name of ``gui.icons.icon`` such as "index", "zones", "multispectral", or
        a QIcon), ``tooltip``, ``children`` (items), ``activate`` (``activate()`` on a double click or
        Enter), ``actions`` (``[(text, callback), ...]``: the menu of the right click), ``expanded``;
        a key left out is empty. The callbacks run in the interface thread with the chunk of the item
        the active one. The provider is called for every chunk each time the tree is filled - every
        time the window shows the project again, and at :meth:`refresh_tree`; one that fails is written
        in the log and left out. A node keeps the expansion the user gave it. Taken out when the plugin
        is disabled. Nothing in the command line."""
        tree = getattr(self.window, "workspace", None)
        if tree is None or not hasattr(tree, "providers"):
            return
        entry = (self.name, provider)
        tree.providers.append(entry)
        self.contributions.tree_items.append(entry)
        if tree.document is not None:  # the tree is filled already (a plugin activated afterwards)
            self.refresh_tree()

    def add_rasters(self, provider: Callable[[object], list]) -> None:
        """Since 1.4.0: the rasters of the plugin by name in the raster calculator (*Strumenti >
        Calcolatore raster*, ``engine.rastercalc``) and wherever the program lists the rasters of a
        chunk. ``provider(chunk)`` returns a dict for each one: ``name`` (in the formulas: letters,
        digits and ``_``; made so if it is not), ``label`` and ``path`` (a GeoTIFF). Without it the
        calculator finds every GeoTIFF of the chunk folder anyway, named by its folder and file
        (``terrain_twi``), or by its ``ppg_name`` tag. Taken out when the plugin is disabled; also in
        the command line."""
        from ..engine import rastercalc

        self.contributions.rasters.append(rastercalc.register(provider, owner=self.name))

    def add_site_contributions(self, provider: Callable[[object], list]) -> None:
        """Since 1.10.0: what the plugin adds to the web site of a survey (``ppg web build``, the pane *Pubblica sul
        web*). ``provider(chunk)`` is called for every chunk that goes on a site and returns a list of dicts, each
        one of: a layer of the plan (``{"kind": "layer", "id", "title", "features": GeoJSON in the coordinates of
        the chunk, "colour", "legend": {"field", "items": [{"value", "title", "colour"}]}, "fields"}``), a tab of
        cards (``{"kind": "table", "id", "title", "columns", "rows", "image"}``, a row with the path of a picture
        under ``image``) or a tab with a page of text (``{"kind": "html", "id", "title", "html", "images"}``). The
        site moves the coordinates into its frame, cleans every text and the HTML, writes the pictures again
        without their metadata and puts the files in ``data/plugins/<plugin>/``; see
        :mod:`..io.site_plugins`. A provider that fails is left out of the site and said in its notes. Taken
        out when the plugin is disabled; also in the command line."""
        from ..io import site_plugins

        self.contributions.site.append(site_plugins.register(provider, owner=self.name))

    def rasters(self, chunk=None) -> list[dict]:
        """Since 1.4.0: every raster of ``chunk`` (the active one when None) the raster calculator reads,
        with its name in the formulas: dicts ``name``, ``label``, ``path``, ``source``, ``bands`` (their
        names, "" where none), ``width``, ``height``, ``resolution``. A formula of the calculator on
        them: ``engine.rastercalc.calculate(chunk, formula, name)``."""
        from ..engine import rastercalc

        chunk = chunk if chunk is not None else self.app.document.chunk
        return [] if chunk is None else [r.as_dict() for r in rastercalc.rasters(chunk)]

    def refresh_tree(self) -> None:
        """Since 1.3.4: the workspace filled again, the items of the plugins asked again (after the plugin
        made or deleted products). From another thread it is done in the interface thread, soon. Nothing
        in the command line."""
        tree = getattr(self.window, "workspace", None)
        if tree is None or getattr(tree, "document", None) is None:
            return
        import threading

        if threading.current_thread() is not threading.main_thread():
            self.call_in_gui(self.refresh_tree)
            return
        try:
            tree.populate(tree.document)
        except RuntimeError:  # the window is being closed
            pass

    # ------------------------------------------------------------------ units (since 1.5.0)
    def add_units(self, polygons, tipo: str = "US", attributes=None, chunk=None, elevation: str | None = None) -> int:
        """Since 1.5.0: stratigraphic units made by the plugin, as if drawn in the window: in the live
        QGIS link, in the US proposal (*Proponi US e rapporti* numbers those without a number), in the
        US forms of PyArchInit. ``polygons``: shapely (Multi)Polygons, or vertex lists ``(N, 2)`` in the
        coordinates of the chunk; an invalid outline is made valid, one that encloses no area is left
        out. ``attributes``: one dict for all, or a list with one per polygon - ``us``, ``area``,
        ``tipo``, ``sito`` and the columns of ``pyunitastratigrafiche``; any other key stays an attribute
        of the unit (kept with the project, a column in QGIS). The site is the project's when not given.
        ``chunk``: the active one when None. In the window, on the active chunk, the units are drawn and
        are one entry of *Modifica > Annulla*; on another chunk, and in the command line, they go into
        ``chunk.meta["drawings"]``, where the window finds them when that chunk becomes the active one
        (the command line saves the project). While the window shows an elevation or a section, the polygons are in
        its ``(u, v)`` metres and join its drawings. Since 1.10.0, ``elevation``: the id of an elevation or a section
        of the chunk the units go to, in its ``(u, v)`` metres, whatever the window shows (:meth:`elevation_drawings`
        reads them back). Only in the interface thread (an action, ``done``), as :meth:`store_drawings`. Returns how
        many units were added."""
        import threading

        from ..archaeo.stratigraphy import units_from_polygons

        window = self.window
        if window is not None and threading.current_thread() is not threading.main_thread():
            raise RuntimeError("add_units() changes the window: call it in the interface thread, "
                               "not in the function given to run() or in an exporter")
        doc = getattr(window, "doc", None) if window is not None else None
        if doc is None:
            doc = getattr(chunk, "document", None) or self.app.document
        chunk = chunk if chunk is not None else doc.chunk
        if chunk is None or not any(c is chunk for c in doc.chunks):
            raise ValueError("the chunk does not belong to the open project")
        units = units_from_polygons(polygons, tipo, attributes, sito=str(doc.meta.get("sito") or "") or None)
        if not units:
            return 0
        if elevation is not None:
            from ..engine import elevation_drawings

            if elevation_drawings.entry_of(chunk, elevation) is None:
                raise ValueError(f"no elevation {elevation!r} in the chunk")
            on_window = window is not None and chunk is getattr(window, "chunk", None)
            if on_window and getattr(window, "drawings_frame", None) == elevation and hasattr(window, "add_units"):
                window.add_units(units, source=self.name)
                return len(units)
            if on_window and hasattr(window, "_store_drawings"):
                window._store_drawings()  # the elevation the window shows, kept as it is before another one changes
            n = elevation_drawings.add_units(chunk, elevation, units)
            if on_window and hasattr(window, "_dirty"):
                window._dirty(True)  # a step of Modifica > Annulla
                schedule = getattr(window, "_schedule_elevation_drawings_3d", None)
                if schedule is not None:
                    schedule()
            else:
                self.mark_modified()
            return n
        if window is not None and chunk is getattr(window, "chunk", None) and hasattr(window, "add_units"):
            window.add_units(units, source=self.name)
            return len(units)
        drawings = chunk.meta.setdefault("drawings", {})
        drawings.setdefault("units", []).extend({**u.to_dict(), "layer": None} for u in units)
        drawings.setdefault("quote", [])
        self.mark_modified()
        return len(units)

    # ------------------------------------------------------------------ elevations (since 1.10.0)
    def shown_elevation(self) -> str | None:
        """Since 1.10.0: the id of the elevation or section the 2D view of the window shows (its drawings are in its
        ``(u, v)`` metres), None on the plan and in the command line - the stable name of the window's
        ``drawings_frame``."""
        return getattr(self.window, "drawings_frame", None) if self.window is not None else None

    def elevations(self, chunk=None) -> list[dict]:
        """Since 1.10.0: the entries of the elevations and sections of ``chunk`` (the active one when None): ``id``,
        ``name``, ``kind``, ``frame``, ``bounds``, ``v_is_height``, ``crs``... - copies."""
        import copy

        from ..engine import elevation_drawings

        chunk = self._chunk_or_active(chunk)
        return copy.deepcopy(elevation_drawings.entries(chunk)) if chunk is not None else []

    def elevation_drawings(self, elevation: str | None = None, chunk=None, world: bool = False) -> dict:
        """Since 1.10.0: the drawings kept with the elevation ``elevation`` (the one the window shows when None) of
        ``chunk`` (the active one when None), as :func:`..engine.elevation_drawings.drawings` gives them - units,
        spot heights, measuring points, lines and stones with shapely geometries in its ``(u, v)`` metres (``world``:
        each also placed in 3D, in the system of the elevation); copies, read only. In the window those being drawn
        are included (read in the interface thread). ``KeyError`` for an elevation the chunk does not have,
        ``ValueError`` when none is given and none is shown."""
        from ..engine import elevation_drawings

        chunk = self._chunk_or_active(chunk)
        if elevation is None:
            elevation = self.shown_elevation()
            if elevation is None:
                raise ValueError("no elevation shown: give its id")
        window = self.window
        if window is not None and chunk is getattr(window, "chunk", None) and \
                getattr(window, "drawings_frame", None) == elevation and hasattr(window, "_store_drawings"):
            import threading

            if threading.current_thread() is threading.main_thread():
                window._store_drawings()  # those being drawn on it, into the chunk first
        if chunk is None:
            raise KeyError(elevation)
        return elevation_drawings.drawings(chunk, elevation, world=world)

    def _chunk_or_active(self, chunk):
        if chunk is not None:
            return chunk
        window = self.window
        doc = getattr(window, "doc", None) if window is not None else None
        doc = doc if doc is not None else self.app.document
        return getattr(doc, "chunk", None)

    # ------------------------------------------------------------------ orientation (since 1.7.2)
    def orient(self, chunk=None, **kw) -> dict:
        """Since 1.7.2: the local system of ``chunk`` (the active one when None) oriented, as
        ``engine.orient.orient(chunk, **kw)``: ``points`` (A, B[, C] in the coordinates of the chunk), ``markers``
        (labels or keys), ``plane_xyz`` and ``toward``, ``x_along_ab``, ``quota``, ``origin_xy``, ``distance``,
        ``flip``, ``M`` (a 4x4 similarity given as it is), ``move_aligned``, ``dry_run``. Everything kept in the
        coordinates of the chunk follows; three orientation markers keep the frame through a new alignment. In the
        window it is a step of *Modifica > Annulla* and the views are shown again; in the command line the plugin
        saves the project. Refused with ``engine.orient.OrientRefused`` (``reason``, ``names``): a chunk in a
        system of the world, markers or photos that give the orientation, a chunk aligned to another. Returns the
        result of ``engine.orient.orient`` (``M``, ``describe``, ``moved``, ``stale``, ...)."""
        from ..engine import orient as orient_mod

        window = self.window
        if chunk is None:
            doc = getattr(window, "doc", None) if window is not None else None
            doc = doc if doc is not None else self.app.document
            chunk = getattr(doc, "chunk", None)
            if chunk is None:
                raise ValueError("no chunk to orient: open a project")
        method = getattr(window, "orient_chunk", None)
        if method is not None:
            return method(chunk, **kw)
        result = orient_mod.orient(chunk, **kw)
        if not kw.get("dry_run"):
            self.mark_modified()
        return result

    def free_drawings(self, chunk=None, layer: str | None = None) -> list[dict]:
        """Since 1.7.2: the free drawings in 3D of ``chunk`` (the active one when None), of ``layer`` only when given:
        records ``id``, ``layer``, ``kind`` (line, polygon, point), ``name``, ``xyz`` (the coordinates of the chunk,
        a polygon open), ``attributes``, ``created``, ``modified`` and an optional ``colour`` - copies: changing them
        changes nothing in the project (read only; their measures: ``archaeo.measures.feature_measures``)."""
        from ..core import free_drawings

        if chunk is None:
            window = self.window
            doc = getattr(window, "doc", None) if window is not None else None
            doc = doc if doc is not None else self.app.document
            chunk = getattr(doc, "chunk", None)
            if chunk is None:
                return []
        return free_drawings.features(chunk, layer)

    def on_transform_changed(self, callback: Callable, keys=()) -> None:
        """Since 1.7.2: ``callback(chunk, M)`` after the transform of a chunk was changed by the motion ``M`` (a 4x4
        array: an orientation of its local system, its undo): data of the plugin kept in the coordinates of the
        chunk is moved by ``M`` there. ``keys``: the keys of ``chunk.meta`` the plugin moves itself (the others of
        unknown plugins are said as not moved). Also in the command line; stopped when the plugin is disabled."""
        from ..engine import orient as orient_mod

        self._stops.append(orient_mod.on_transform_changed(callback, keys))

    # ------------------------------------------------------------------ panes (since 1.3.4)
    def add_pane(self, widget, title: str, icon=None, area: str = "right"):
        """Since 1.3.4: ``widget`` as a pane of the window, a dock of its own tabbed with the panes of
        ``area`` ("left", "bottom" or "right"): the user moves it, floats it as a window, closes it and
        opens it again from *Vista > Pannelli*, and the window remembers where it was. ``icon``: a QIcon
        or the name of an icon of the program ("plugin", "relief", ...). Returns the dock, None in the
        command line; the pane goes away with the plugin (:meth:`release`)."""
        method = getattr(self.window, "add_pane", None)
        if method is None:
            return None
        name = f"pane_plugin_{self.name}" + (f"_{len(self._panes) + 1}" if self._panes else "")
        dock = method(widget, title, icon, area=area, name=name)
        self._panes.append(widget)
        return dock

    def show_pane(self, widget) -> None:
        """Since 1.3.4: a pane of the plugin in front, also when the user closed it or floated it.
        Nothing in the command line."""
        method = getattr(self.window, "show_pane", None)
        if method is not None:
            method(widget)

    def release(self) -> None:
        """What the plugin connected through the context, disconnected, its items taken out of the workspace
        and its panes out of the window (the manager calls it after :meth:`Plugin.deactivate`); since 1.7.4 also its
        actions out of the menu *Plugin* and its groups out of the tool rails."""
        self.released = True
        for signal, slot in self.contributions.connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self.contributions.connections.clear()
        for stop in self._stops:
            stop()
        self._stops.clear()
        if self.contributions.rasters:
            from ..engine import rastercalc

            for entry in self.contributions.rasters:
                rastercalc.unregister(entry)
            self.contributions.rasters.clear()
        if self.contributions.site:
            from ..io import site_plugins

            for entry in self.contributions.site:
                site_plugins.unregister(entry)
            self.contributions.site.clear()
        tree = getattr(self.window, "workspace", None)
        if self.contributions.tree_items and tree is not None:
            tree.providers[:] = [p for p in tree.providers if not any(p is e for e in self.contributions.tree_items)]
            self.contributions.tree_items.clear()
            self.refresh_tree()
        remove = getattr(self.window, "remove_pane", None)
        for widget in self._panes:
            try:
                if remove is not None:
                    remove(widget)
            except RuntimeError:  # the window is already gone
                pass
        self._panes.clear()
        self.contributions.rail_groups.clear()
        method = getattr(self.window, "plugin_contributions_changed", None)
        if method is not None:
            try:
                method(self)
            except RuntimeError:  # the window is already gone
                pass

    def run(self, title: str, function: Callable, done: Callable | None = None) -> object:
        """``function(progress)`` with a progress bar: in the background in the window (``done(result)``
        afterwards, in the interface thread), at once in the command line (``done`` too)."""
        if self.window is not None:
            return self.window.run_task(title, function, done, refresh=False, save=False)
        from ..core.progress import print_progress

        result = function(print_progress)
        if done is not None:
            done(result)
        return result
