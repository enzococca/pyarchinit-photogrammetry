# Plugin guides

A plugin adds tools to pyarchinit-photogrammetry without changing it: a pane, menu actions, commands of
the command line, export formats. The plugins are collected in the
[plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins) and the program
installs them by itself. These guides explain how to use them - the data they need, the steps, how to
read the results - for archaeologists who do not write code. How plugins work, and how to write one, is
in [Plugins](../plugins.md).

## Installing a plugin { #install }

In the program:

1. *Tools > Plugins...* (*Strumenti > Plugin...* in the Italian interface).
2. *Refresh the list*: the lower table shows the plugins of the repository, with version, author,
   description and a note: whether it is installed or can be updated, or what the plugin needs - for
   example a newer version of the program.
3. Select the plugin and click *Install / update*: the program downloads it, checks it and puts it in its
   own plugin folder.
4. *Restart now*: the plugin is active from the next start, with its pane and its menu
   (*Plugins > name of the plugin*).

The upper table lists the plugins installed - active, disabled, or the error that kept one out - with
*Enable / disable* and *Remove*. To update a plugin, install it again when its note says it can be
updated. Plugins are Python code with the same rights as the program: install only those you trust.

From the command line:

```bash
ppg plugins available              # the plugins of the repository
ppg plugins install geoarchaeology
ppg plugins list                   # those installed, enabled or not
ppg plugin geoarchaeology help     # the commands of a plugin
```

The desktop application has no pip: the program installs plugins into a folder of its own. To install
them with pip in a Python environment, see [Plugins](../plugins.md).

## The guides

| Plugin | What it does | Guide |
|---|---|---|
| Geoarchaeology (`geoarchaeology`) | boreholes, cores and trench logs on the survey: stratigraphic columns, contact surfaces and thicknesses by kriging, sections, a 3D model of the units with their volumes | [Geoarchaeology](geoarchaeology.md) |

Guides for the other plugins of the repository - Terrain, Geophysics, GIS archive, terrio, Spirula,
Powerlines - will follow, with the same parts: what the plugin does and what it needs first, the data
and how to prepare them, the steps in the program and on the command line, how to read the results, the
files it writes.
