"""Plugins: packages that add actions, commands and export formats to pyarchinit-photogrammetry.

* :mod:`.api` - :class:`Plugin` (subclassed by a plugin) and :class:`Context` (what it can add);
* :mod:`.manager` - finding, enabling and activating them (:class:`Manager`);
* :mod:`.index` - the online index of the plugin repository and installing from it;
* :mod:`.updates` - the plugins installed by the program checked against the index, newer versions staged
  and put in place at the next start.

Writing one: see ``docs/plugins.md``.
"""
from .api import API_VERSION, Action, Command, Context, Exporter, Plugin
from .manager import GROUP, Manager, PluginInfo, plugins_dir

__all__ = ["API_VERSION", "Action", "Command", "Context", "Exporter", "GROUP", "Manager", "Plugin", "PluginInfo",
           "plugins_dir"]
