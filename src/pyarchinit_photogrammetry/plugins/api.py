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
class Contributions:
    actions: list = field(default_factory=list)
    commands: dict = field(default_factory=dict)
    exporters: list = field(default_factory=list)
    connections: list = field(default_factory=list)  # (signal, slot) disconnected when the plugin goes away


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
        """A menu action under *Plugin* (in the plugin's submenu, or in ``menu``)."""
        action = Action(title, callback, menu, tip, processing, self.name)
        self.contributions.actions.append(action)
        return action

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
        ``on_finished(xy)`` gets the vertices, an ``(N, 2)`` array in the coordinates of the chunk, in the
        interface thread; ``on_cancelled()`` when the user picks another tool or another request comes.
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

    def release(self) -> None:
        """What the plugin connected through the context, disconnected (the manager calls it after
        :meth:`Plugin.deactivate`)."""
        for signal, slot in self.contributions.connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self.contributions.connections.clear()

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
