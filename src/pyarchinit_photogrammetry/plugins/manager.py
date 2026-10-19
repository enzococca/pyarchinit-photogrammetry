"""Finding, enabling and activating the plugins.

Plugins come from two places: the Python environment (packages installed with pip that declare
the entry point group ``pyarchinit_photogrammetry.plugins``) and the plugin folder of the program
(``<data folder>/plugins/<name>/``, where *Strumenti > Plugin* and ``ppg plugins install`` unpack
them: the packaged application has no pip). A plugin of the folder wins over one of the same name
in the environment. Each plugin is activated on its own: one that fails is reported and left out,
the program goes on. ``PPG_NO_PLUGINS=1`` starts without plugins, ``PPG_PLUGINS_DIR`` moves the
folder (the tests use one of their own), ``PPG_PLUGINS_ENVIRONMENT=0`` leaves out those of the
environment (only the folder: the tests, a plugin checked on its own).
"""
from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .api import API_VERSION, Context, Plugin

GROUP = "pyarchinit_photogrammetry.plugins"
log = logging.getLogger(__name__)
_ON_PATH: set[str] = set()  # folders of plugins put on sys.path: their plugins are not of the environment


def _norm(path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def plugins_dir() -> Path:
    """The plugin folder of the program (``PPG_PLUGINS_DIR`` moves it)."""
    if os.environ.get("PPG_PLUGINS_DIR"):
        return Path(os.environ["PPG_PLUGINS_DIR"])
    from ..ai.sam_env import data_dir

    return data_dir() / "plugins"


class _FunctionPlugin(Plugin):
    """An entry point that names a function ``activate(context)`` instead of a class."""

    def __init__(self, function):
        self._function = function

    def activate(self, context: Context) -> None:
        self._function(context)


@dataclass
class PluginInfo:
    """A plugin found, active or not."""

    name: str
    title: str
    version: str
    description: str
    source: str  # "folder" (installed by the program) or "environment" (pip)
    distribution: str
    location: Path | None  # the folder of a plugin installed by the program
    entry_point: object = field(repr=False, default=None)
    enabled: bool = True
    active: bool = False
    error: str = ""
    plugin: Plugin | None = field(repr=False, default=None)
    context: Context | None = field(repr=False, default=None)


def _info(ep, dist, source: str, location: Path | None) -> PluginInfo:
    meta = dist.metadata if dist is not None else {}
    dist_name = (meta.get("Name") if meta else None) or ep.name
    return PluginInfo(name=ep.name, title=ep.name, version=(dist.version if dist is not None else ""),
                      description=(meta.get("Summary") if meta else "") or "", source=source,
                      distribution=dist_name, location=location, entry_point=ep)


class Manager:
    """The plugins of a session: :meth:`load` finds and activates them; the window and the
    command line read what they registered from :attr:`actions`, :attr:`commands` and
    :attr:`exporters`."""

    def __init__(self, folder: str | os.PathLike | None = None, window=None):
        self.folder = Path(folder) if folder is not None else plugins_dir()
        self.window = window
        self.plugins: list[PluginInfo] = []

    # ------------------------------------------------------------------ state
    def _state_path(self) -> Path:
        return self.folder / "state.json"

    def disabled(self) -> set[str]:
        try:
            return set(json.loads(self._state_path().read_text()).get("disabled", []))
        except (OSError, ValueError):
            return set()

    def set_enabled(self, name: str, enabled: bool) -> None:
        """Enable or disable a plugin from the next start (kept in ``state.json``)."""
        off = self.disabled()
        (off.discard if enabled else off.add)(name)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._state_path().write_text(json.dumps({"disabled": sorted(off)}, indent=1))
        for info in self.plugins:
            if info.name == name:
                info.enabled = enabled

    # ------------------------------------------------------------------ discovery
    def discover(self) -> list[PluginInfo]:
        """The plugins of the folder and of the environment, without importing them."""
        found: dict[str, PluginInfo] = {}
        if self.folder.is_dir():
            for sub in sorted(p for p in self.folder.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
                for dist in importlib.metadata.distributions(path=[str(sub)]):
                    for ep in dist.entry_points.select(group=GROUP):
                        found.setdefault(ep.name, _info(ep, dist, "folder", sub))
        environment = os.environ.get("PPG_PLUGINS_ENVIRONMENT", "1") not in ("0", "no", "false")
        for ep in importlib.metadata.entry_points(group=GROUP) if environment else ():
            dist = getattr(ep, "dist", None)
            if dist is not None and _norm(dist.locate_file("")) in _ON_PATH:
                continue  # a plugin folder this session put on sys.path
            if ep.name not in found:
                found[ep.name] = _info(ep, dist, "environment", None)
        off = self.disabled()
        known = {i.name: i for i in self.plugins}
        for name, info in list(found.items()):
            old = known.get(name)
            if old is not None and (old.active or old.error):  # loaded in this session: its state stays
                found[name] = old
            found[name].enabled = name not in off
        self.plugins = sorted(found.values(), key=lambda i: i.name)
        return self.plugins

    # ------------------------------------------------------------------ activation
    def load(self, window=None) -> list[PluginInfo]:
        """Find the plugins and activate the enabled ones (none with ``PPG_NO_PLUGINS=1``)."""
        if window is not None:
            self.window = window
        self.discover()
        if os.environ.get("PPG_NO_PLUGINS", "") not in ("", "0"):
            return self.plugins
        for info in self.plugins:
            if info.enabled:
                self._activate(info)
        return self.plugins

    def _activate(self, info: PluginInfo) -> None:
        try:
            if info.location is not None and str(info.location) not in sys.path:
                sys.path.insert(0, str(info.location))
                _ON_PATH.add(_norm(info.location))
            obj = info.entry_point.load()
            plugin = obj() if isinstance(obj, type) else obj
            if not isinstance(plugin, Plugin):
                if not callable(plugin):
                    raise TypeError("the entry point names neither a Plugin nor a function activate(context)")
                plugin = _FunctionPlugin(plugin)
            api = int(getattr(plugin, "api", API_VERSION) or API_VERSION)
            if api > API_VERSION:
                raise RuntimeError(f"written for the plugin API {api}, this program has {API_VERSION}: "
                                   "update pyarchinit-photogrammetry")
            info.title = plugin.title or info.title
            info.version = plugin.version or info.version
            info.description = plugin.description or info.description
            context = Context(info.name, self.folder / "_data" / info.name, self.window)
            plugin.activate(context)
            taken = self.commands
            for name in list(context.contributions.commands):
                if name in taken:
                    log.warning("plugin %s: the command %s is already given by %s", info.name, name,
                                taken[name].plugin)
                    del context.contributions.commands[name]
            info.plugin, info.context, info.active, info.error = plugin, context, True, ""
            log.info("plugin %s %s active", info.name, info.version)
        except Exception as exc:  # a plugin never stops the program
            info.plugin, info.context, info.active = None, None, False
            info.error = f"{type(exc).__name__}: {exc}"
            log.warning("plugin %s not loaded: %s", info.name, info.error)

    def unload(self) -> None:
        """Deactivate every active plugin (what they registered goes away)."""
        for info in self.plugins:
            if info.active and info.plugin is not None:
                try:
                    info.plugin.deactivate()
                except Exception as exc:
                    log.warning("plugin %s: deactivate failed: %s", info.name, exc)
            info.active, info.plugin, info.context = False, None, None

    def remove(self, name: str) -> None:
        """Delete a plugin installed by the program (one installed with pip: ``pip uninstall``)."""
        info = next((i for i in self.plugins if i.name == name), None)
        if info is None or info.source != "folder" or info.location is None:
            raise ValueError(f"{name}: not a plugin installed by the program (with pip: pip uninstall it)")
        if info.active and info.plugin is not None:
            try:
                info.plugin.deactivate()
            except Exception:
                pass
        shutil.rmtree(info.location)
        self.plugins = [i for i in self.plugins if i.name != name]

    # ------------------------------------------------------------------ what the plugins added
    def _active_contexts(self):
        return [i.context for i in self.plugins if i.active and i.context is not None]

    @property
    def actions(self) -> list:
        return [a for c in self._active_contexts() for a in c.contributions.actions]

    @property
    def commands(self) -> dict:
        out: dict = {}
        for c in self._active_contexts():
            for name, command in c.contributions.commands.items():
                out.setdefault(name, command)
        return out

    @property
    def exporters(self) -> list:
        return [e for c in self._active_contexts() for e in c.contributions.exporters]

    def get(self, name: str) -> PluginInfo | None:
        return next((i for i in self.plugins if i.name == name), None)
