"""PyArchInit settings (``config.cfg``) read without QGIS.

PyArchInit stores its settings as a Python dict literal in
``$PYARCHINIT_HOME/pyarchinit_DB_folder/config.cfg``. ``PYARCHINIT_HOME`` comes
from the environment, otherwise ``~/pyarchinit_5`` (5.x) or ``~/pyarchinit``.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

REMOTE_PREFIXES = ("gdrive://", "dropbox://", "s3://", "r2://", "webdav://", "http://", "https://", "sftp://",
                   "cloudinary://", "unibo://")


def pyarchinit_home() -> Path:
    env = os.environ.get("PYARCHINIT_HOME")
    if env:
        return Path(env).expanduser()
    for name in ("pyarchinit_5", "pyarchinit"):
        p = Path.home() / name
        if (p / "pyarchinit_DB_folder").exists():
            return p
    return Path.home() / "pyarchinit_5"


@dataclass
class PyArchInitConfig:
    server: str = "sqlite"
    database: str = "pyarchinit_db.sqlite"
    host: str = ""
    port: str = ""
    user: str = ""
    password: str = ""
    sslmode: str = "allow"
    thumb_path: str = ""
    thumb_resize: str = ""
    site_set: str = ""
    home: Path = field(default_factory=pyarchinit_home)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "PyArchInitConfig":
        path = Path(path) if path else pyarchinit_home() / "pyarchinit_DB_folder" / "config.cfg"
        data = ast.literal_eval(path.read_text(encoding="utf-8").strip())
        # the SQLite file name in DATABASE is relative to <home>/pyarchinit_DB_folder
        home = path.parent.parent if path.parent.name == "pyarchinit_DB_folder" else pyarchinit_home()
        return cls(server=str(data.get("SERVER", "sqlite")), database=str(data.get("DATABASE", "")),
                   host=str(data.get("HOST", "")), port=str(data.get("PORT", "")), user=str(data.get("USER", "")),
                   password=str(data.get("PASSWORD", "")), sslmode=str(data.get("SSLMODE", "allow")),
                   thumb_path=str(data.get("THUMB_PATH", "")), thumb_resize=str(data.get("THUMB_RESIZE", "")),
                   site_set=str(data.get("SITE_SET", "")), home=home)

    @property
    def is_postgres(self) -> bool:
        return self.server.lower().startswith("postgres")

    @property
    def sqlite_path(self) -> Path:
        p = Path(self.database)
        return p if p.is_absolute() else self.home / "pyarchinit_DB_folder" / p

    def url(self) -> str:
        """SQLAlchemy URL, same scheme PyArchInit builds in ``Connection.conn_str``."""
        if self.is_postgres:
            return (f"postgresql://{quote_plus(self.user)}:{quote_plus(self.password)}@{self.host}:{self.port}"
                    f"/{self.database}?sslmode={self.sslmode}")
        return f"sqlite:///{self.sqlite_path}"

    @property
    def thumb_dir(self) -> str:
        return _with_sep(self.thumb_path or str(Path.home() / "pyarchinit_Thumbnails_folder"))

    @property
    def resize_dir(self) -> str:
        return _with_sep(self.thumb_resize or str(Path.home() / "pyarchinit_Resize_folder"))

    @property
    def media_dir(self) -> str:
        """Default media repository: parent folder of the thumbnails (as the QField importer does)."""
        trimmed = self.thumb_dir.rstrip("/\\")
        return _with_sep(str(Path(trimmed).parent)) if not is_remote(trimmed) else trimmed.rsplit("/", 1)[0] + "/"


def _with_sep(p: str) -> str:
    return p if p.endswith(("/", "\\")) else p + "/"


def is_remote(path: str) -> bool:
    return bool(path) and path.lower().startswith(REMOTE_PREFIXES)
