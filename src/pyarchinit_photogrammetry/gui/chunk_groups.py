"""Groups of chunks in the window (:mod:`..core.groups`): the folder of a group in the Area di lavoro, its menu and
the operations on the whole group.

* **Tree**: a group is a node with its chunks under it (in the place of its first chunk); its box shows or hides all
  its chunks in the 3D view (partly ticked when some are shown); chunks are dragged onto a group (or onto a chunk of
  it, to be put before it) and out of it onto the project node.
* **Menu of a group**: *Mostra/Nascondi nella vista 3D*, *Rinomina...*, *Aggancia il gruppo con legami* (the window
  of the ties with the group as one block: its first chunk with its unknowns, the others *con il gruppo*), *Unisci i
  chunk del gruppo...*, *DEM e ortofoto del gruppo...* and *Esporta il gruppo...* (the batch with its steps on the
  chunks of the group), *Sciogli il gruppo* (the chunks stay).
* **Menu of a chunk** and of several chunks: *Nuovo gruppo...*, *Sposta nel gruppo*, *Togli dal gruppo*.

Every change of the groups is one step of *Modifica > Annulla* (:class:`GroupsStep`); showing and hiding is a choice of
the view (as the box of a chunk), not an edit.
"""
from __future__ import annotations

import copy

from ..core import groups
from .i18n import tr
from .icons import icon
from .undo import ScriptChanged

KIND = "chunk_group"  # the kind of the node of a group in the tree: ("chunk_group", None, group id)


class GroupsChanged(ScriptChanged):
    """The groups changed after the step outside the history: the step leaves it."""


def state(doc) -> list | None:
    return copy.deepcopy(doc.meta.get(groups.META_KEY))


class GroupsStep:
    """A change of the groups (made, renamed, filled, emptied, dissolved) as one step of Modifica > Annulla."""

    def __init__(self, window, before, after):
        self.window, self.before, self.after = window, before, after

    def _put(self, expected, value) -> None:
        doc = self.window.doc
        if state(doc) != expected:
            raise GroupsChanged("the groups of chunks changed after the step")
        if value:
            doc.meta[groups.META_KEY] = copy.deepcopy(value)
        else:
            doc.meta.pop(groups.META_KEY, None)
        self.window.workspace.populate(doc)
        self._kinds()

    def _kinds(self) -> None:
        dlg = self.window._live_ties_dialog()
        if dlg is not None:
            dlg.reload(kinds=dlg._kinds_with_new())

    def undo(self) -> None:
        self._put(self.after, self.before)

    def redo(self) -> None:
        self._put(self.before, self.after)


def edit(window, text: str, change) -> bool:
    """``change()`` (a function of :mod:`..core.groups`) run as one step of Modifica > Annulla; an error of the groups
    said in the window. Returns whether something changed."""
    doc = window.doc
    before = state(doc)
    try:
        change()
    except groups.GroupError as exc:
        window._warn(tr("Gruppi: {0}", str(exc)))
        return False
    after = state(doc)
    if after == before:
        return False
    window.setWindowModified(True)
    if window.history is not None:
        window.history.push(text, GroupsStep(window, before, after), document=True)
    window.workspace.populate(doc)
    GroupsStep(window, before, after)._kinds()
    return True


# --------------------------------------------------------------------------- the operations
def ask_name(window, title: str, name: str) -> str | None:
    from .dialogs import Field, ParamDialog

    v = ParamDialog.ask(window, title, [Field("name", "Nome del gruppo", name, kind="text")])
    name = (v or {}).get("name", "").strip()
    return name or None


def new_group(window, chunks, name: str | None = None) -> bool:
    """A new group with ``chunks`` (asked its name when ``name`` is None)."""
    if name is None:
        name = ask_name(window, tr("Nuovo gruppo"), groups.next_name(window.doc))
        if name is None:
            return False
    keys = [c.key for c in chunks]
    done = edit(window, tr("Nuovo gruppo"), lambda: groups.add_group(window.doc, name, keys))
    if done:
        window.log_pane.info(tr("Gruppo «{0}»: {1} chunk", name, len(keys)))
    return done


def rename(window, gid: str, name: str | None = None) -> bool:
    g = groups.group(window.doc, gid)
    if name is None:
        name = ask_name(window, tr("Rinomina il gruppo"), g["name"])
        if name is None or name == g["name"]:
            return False
    return edit(window, tr("Rinomina il gruppo"), lambda: groups.rename_group(window.doc, gid, name))


def dissolve(window, gid: str) -> bool:
    return edit(window, tr("Sciogli il gruppo"), lambda: groups.remove_group(window.doc, gid))


def move_chunks(window, keys, gid: str | None, before: int | None = None) -> bool:
    """Chunks ``keys`` into the group ``gid`` (None: out of their groups), before the chunk ``before`` of it."""
    text = tr("Sposta nel gruppo") if gid is not None else tr("Togli dal gruppo")
    return edit(window, text, lambda: groups.assign(window.doc, list(keys), gid, before))


def show(window, gid: str, shown: bool) -> None:
    """All the chunks of the group shown in (or taken out of) the 3D view with the active one."""
    from .chunk_views import set_shown_3d

    changed = False
    for ch in groups.members(window.doc, gid):
        if ch is not window.chunk:
            changed = set_shown_3d(window.doc, ch, shown) or changed
    if changed:
        window.setWindowModified(True)
        window.model_view.refresh(reset=False)
        if shown:
            window.show_view(window.model_view)
    window.workspace.populate(window.doc)


def shown_state(doc, gid: str, active) -> int:
    """0 none of its chunks in the 3D view, 1 some, 2 all (the active chunk is always shown)."""
    from .chunk_views import settings

    keys = set(settings(doc)["chunks_3d"])
    members = groups.group(doc, gid)["chunks"]
    n = sum(1 for k in members if k in keys or (active is not None and k == active.key))
    return 0 if not n else (2 if n == len(members) else 1)


def tie_as_block(window, gid: str) -> None:
    """The window of the ties with the group as one block: its first chunk keeps (or gets) its unknowns, the others
    move with it."""
    from ..engine.ties import GROUP

    window.tie_chunks()
    dlg = window._live_ties_dialog()
    if dlg is None:
        return
    kinds = dlg.kinds(include_out=True)
    keys = groups.group(window.doc, gid)["chunks"]
    if not keys:
        return
    lead = next((k for k in keys if kinds.get(k) == "fixed"), keys[0])
    if kinds.get(lead) in ("", None, GROUP):
        kinds[lead] = "similarity"
    for k in keys:
        if k != lead:
            kinds[k] = GROUP
    dlg.reload(kinds=kinds)


def batch(window, gid: str, ops) -> None:
    """The batch with the steps ``ops`` on the chunks of the group (its window opens to check and run them)."""
    from .. import batch as batch_mod

    labels = [c.label for c in groups.members(window.doc, gid)]
    window.batch_process(preset=[batch_mod.step(op, labels) for op in ops])


def merge(window, gid: str) -> None:
    window.merge_chunks(keys=groups.group(window.doc, gid)["chunks"])


# --------------------------------------------------------------------------- menus
def group_menu(window, menu, gid: str) -> None:
    """The entries of the menu of a group node."""
    doc = window.doc
    g = groups.group(doc, gid)
    n = len(g["chunks"])
    seen = shown_state(doc, gid, window.chunk)
    if seen < 2:
        menu.addAction(icon("view"), tr("Mostra nella vista 3D"), lambda: show(window, gid, True))
    if seen > 0:
        menu.addAction(icon("view"), tr("Nascondi dalla vista 3D"), lambda: show(window, gid, False))
    menu.addAction(icon("rename"), tr("Rinomina..."), lambda: rename(window, gid))
    menu.addSeparator()
    a = menu.addAction(icon("merge"), tr("Aggancia il gruppo con legami..."), lambda: tie_as_block(window, gid))
    a.setEnabled(n >= 1 and len(doc.chunks) > n)
    a = menu.addAction(icon("merge"), tr("Unisci i chunk del gruppo..."), lambda: merge(window, gid))
    a.setEnabled(n >= 2)
    a = menu.addAction(icon("dem"), tr("DEM e ortofoto del gruppo..."),
                       lambda: batch(window, gid, ["build_dem", "build_orthomosaic"]))
    a.setEnabled(n >= 1)
    a = menu.addAction(icon("export"), tr("Esporta il gruppo..."),
                       lambda: batch(window, gid, ["export_point_cloud", "export_dem", "export_orthomosaic"]))
    a.setEnabled(n >= 1)
    menu.addSeparator()
    menu.addAction(icon("remove"), tr("Sciogli il gruppo"), lambda: dissolve(window, gid))


def chunk_entries(window, menu, chunks) -> None:
    """*Gruppo* entries for one or more chunks: a new group with them, moved into a group, out of their group."""
    doc = window.doc
    keys = [c.key for c in chunks]
    sub = menu.addMenu(icon("folder"), tr("Gruppo"))
    sub.addAction(tr("Nuovo gruppo..."), lambda: new_group(window, chunks))
    others = groups.groups_of(doc)
    for g in others:
        if not all(k in g["chunks"] for k in keys):
            sub.addAction(tr("Sposta in «{0}»", g["name"]), lambda gid=g["id"]: move_chunks(window, keys, gid))
    if any(groups.group_of(doc, k) is not None for k in keys):
        sub.addSeparator()
        sub.addAction(tr("Togli dal gruppo"), lambda: move_chunks(window, keys, None))


# --------------------------------------------------------------------------- the tree
def tree_nodes(tree, document, root):
    """The nodes of the groups for :meth:`.panels.WorkspaceTree._fill`: returns ``parent(chunk)`` (the node a chunk
    goes under: its group's, made when its first chunk comes, else ``root``) and ``finish()`` (the empty groups added
    at the end, the boxes set)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItem

    every = groups.groups_of(document)
    by_chunk = {k: g for g in every for k in g["chunks"]}
    nodes: dict[str, QTreeWidgetItem] = {}

    def node(g):
        if g["id"] not in nodes:
            it = QTreeWidgetItem([tr("{0}  ({1} chunk)", g["name"], len(g["chunks"]))])
            it.setIcon(0, icon("folder"))
            it.setData(0, Qt.ItemDataRole.UserRole, (KIND, None, g["id"]))
            it.setToolTip(0, tr("Gruppo di chunk: si muovono insieme nei legami. Trascina qui i chunk; la spunta li "
                                "mostra tutti nella vista 3D; tasto destro: operazioni sul gruppo"))
            it.setFlags((it.flags() | Qt.ItemFlag.ItemIsDropEnabled) & ~Qt.ItemFlag.ItemIsDragEnabled)
            if len(document.chunks) > 1 and g["chunks"]:
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                seen = shown_state(document, g["id"], document.chunk)
                it.setCheckState(0, (Qt.CheckState.Unchecked, Qt.CheckState.PartiallyChecked,
                                     Qt.CheckState.Checked)[seen])
            root.addChild(it)
            it.setExpanded(True)
            nodes[g["id"]] = it
        return nodes[g["id"]]

    def parent(chunk):
        g = by_chunk.get(chunk.key)
        return root if g is None else node(g)

    def finish():
        for g in every:
            it = node(g)
            kids = [it.takeChild(0) for _ in range(it.childCount())]  # in the order of the group, not the project's
            order = {k: i for i, k in enumerate(g["chunks"])}
            kids.sort(key=lambda k: order.get(getattr(k.data(0, Qt.ItemDataRole.UserRole)[1], "key", None), 1 << 30))
            it.addChildren(kids)

    return parent, finish


def drop_target(tree, event):
    """Where chunks dropped at the position of ``event`` go: ``(group id or None, before chunk key or None)``, or
    ``False`` when not there (a product, a photo...)."""
    from PySide6.QtCore import Qt

    item = tree.itemAt(event.position().toPoint())
    if item is None:
        return False
    data = item.data(0, Qt.ItemDataRole.UserRole)
    if not data:
        return False
    if data[0] == KIND:
        return data[2], None
    if data[0] == "document":
        return None, None
    if data[0] == "chunk" and data[1] is not None:
        g = groups.group_of(tree.document, data[1])
        return (g["id"], data[1].key) if g is not None else (None, None)
    return False


def dragged_chunks(tree) -> list:
    return [d[1] for d in tree.selected_items() if d[0] == "chunk" and d[1] is not None]
