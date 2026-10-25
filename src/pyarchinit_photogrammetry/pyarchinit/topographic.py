"""Topographic survey records in PyArchInit's own ``ut_table`` namespace."""
from __future__ import annotations

from numbers import Integral


def ut_number(value) -> int:
    """Canonical positive INTEGER identifier; descriptive prefixes belong to the name."""
    if isinstance(value, bool):
        raise ValueError("UT number must be a positive integer, without a prefix")
    if isinstance(value, Integral):
        number = int(value)
    elif isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        number = int(value.strip())
    else:
        raise ValueError("UT number must be a positive integer, without a prefix")
    if not 1 <= number <= 2_147_483_647:
        raise ValueError("UT number must be between 1 and 2147483647")
    return number


def _project(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A topographic unit needs a non-empty project (progetto)")
    return value.strip()


def _letter(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("UT descriptive name must be text")
    return value.strip()


def guard_stratigraphic_kind(value) -> None:
    if str(value or "").strip().upper() in {"UT", "TU"}:
        raise ValueError("UT/TU are topographic units: use create_ut() and ut_table, not the US form")


class TopographicMixin:
    """Record API shared by SQLite/SpatiaLite and PostgreSQL without changing their schema."""

    def _ut_table(self):
        if not self.db.has_table("ut_table"):
            raise RuntimeError("This PyArchInit database has no ut_table; use a database with topographic units")
        table = self.db.table("ut_table")
        required = {"id_ut", "progetto", "nr_ut", "ut_letterale"}
        if not required <= set(table.c.keys()):
            raise RuntimeError("The PyArchInit ut_table lacks required topographic identity columns")
        return table

    def topographic_units(self, progetto: str | None = None) -> list[dict]:
        """The topographic forms, optionally filtered by their own project namespace."""
        self._ut_table()
        return self.db.query("ut_table", progetto=_project(progetto) if progetto is not None else None)

    def next_ut(self, progetto: str) -> int:
        """The first number above every UT number already used by this project."""
        numbers = []
        for record in self.topographic_units(progetto):
            try:
                numbers.append(ut_number(record.get("nr_ut")))
            except ValueError:  # incomplete or legacy records do not block a new numeric sequence
                pass
        return max(numbers, default=0) + 1

    def ut_record(self, progetto: str, nr_ut, ut_letterale: str | None = None) -> dict | None:
        """One UT form; ``None`` name matches any literal suffix/name for that number."""
        self._ut_table()
        records = self.db.query("ut_table", progetto=_project(progetto), nr_ut=ut_number(nr_ut),
                                ut_letterale=_letter(ut_letterale) if ut_letterale is not None else None)
        if len(records) > 1:
            raise ValueError("Several UT records share this project and number; specify ut_letterale")
        return records[0] if records else None

    def create_ut(self, progetto: str, nr_ut, ut_letterale: str = "", **fields) -> int:
        """Create a topographic form, preserving PyArchInit's (project, number, name) identity.

        Unknown fields are ignored as for the US API. This creates no US record or site record,
        and does not invent geometry tables or change the schema.
        """
        table = self._ut_table()
        project, number, name = _project(progetto), ut_number(nr_ut), _letter(ut_letterale)
        if self.ut_record(project, number, name) is not None:
            raise FileExistsError(f"UT {number} {name} already exists in project {project}")
        values = {k: v for k, v in fields.items() if k in table.c and k != "id_ut"}
        values.update(progetto=project, nr_ut=number, ut_letterale=name)
        with self.db.begin() as conn:
            return int(self.db.insert(conn, "ut_table", values, id_column="id_ut"))

    def update_ut(self, id_ut: int, **fields) -> int:
        """Update one UT form by its database id; identifiers are checked before any write."""
        table = self._ut_table()
        rows = self.db.query("ut_table", id_ut=ut_number(id_ut))
        if not rows:
            return 0
        before = rows[0]
        values = {k: v for k, v in fields.items() if k in table.c and k != "id_ut"}
        if not values:
            return 0
        validators = {"progetto": _project, "nr_ut": ut_number, "ut_letterale": _letter}
        for key, validate in validators.items():
            if key in values:
                values[key] = validate(values[key])
        target = {k: values.get(k, before.get(k)) for k in validators}
        duplicate = self.ut_record(target["progetto"], target["nr_ut"], target["ut_letterale"])
        if duplicate is not None and int(duplicate["id_ut"]) != int(id_ut):
            raise FileExistsError("A UT with this project, number and name already exists")
        with self.db.begin() as conn:
            return conn.execute(table.update().where(table.c.id_ut == int(id_ut)).values(**values)).rowcount
