"""A fake ``wrangler`` for the tests of the publication on Cloudflare: an executable put on the PATH that records its
calls and answers as the real one does (logged in or not, a failure, a file too large). No network, no account.

The behaviour comes from the environment: ``FAKE_WRANGLER_DIR`` (where it keeps its calls and its "cloud"),
``FAKE_WRANGLER_MODE`` (comma separated: ``logged_out``, ``fail_deploy``, ``too_large``, ``old``, ``taken``)."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

SECRET = "cfut_" + "Q" * 40  # what a token looks like: must never reach a log of the program
ACCOUNT = "0123456789abcdef0123456789abcdef"
EMAIL = "someone@example.org"
R2_DEV = "https://pub-0f1e2d3c4b5a69788796a5b4c3d2e1f0.r2.dev"

SCRIPT = r'''
import hashlib, json, os, sys
from pathlib import Path

home = Path(os.environ["FAKE_WRANGLER_DIR"])
modes = set(filter(None, os.environ.get("FAKE_WRANGLER_MODE", "").split(",")))
args = sys.argv[1:]
with open(home / "calls.jsonl", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\n")
state_path = home / "cloud.json"
cloud = json.loads(state_path.read_text()) if state_path.exists() else {"projects": {}, "buckets": {}}

def save():
    state_path.write_text(json.dumps(cloud))

def out(text, code=0):
    print(text)
    sys.exit(code)

if args[:1] == ["--version"]:
    out(" ⛅️ wrangler 4.51.0" if "old" not in modes else "3.0.0")
if args[:1] == ["whoami"]:
    if "old" in modes and "--json" in args:
        out("Unknown argument: json", 1)
    if "logged_out" in modes:
        out("You are not authenticated. Please run `wrangler login`.", 1)
    out(json.dumps({"loggedIn": True, "email": "%(email)s", "accounts": [{"id": "%(account)s"}]}))
if args[:3] == ["pages", "project", "list"]:
    rows = [{"Project Name": n, "Project Domains": d["domain"], "Git Provider": "No",
             "Last Modified": "1 day ago"} for n, d in cloud["projects"].items()]
    out("\n" + json.dumps(rows))
if args[:3] == ["pages", "project", "create"]:
    name = args[3]
    domain = name + ("-7xq" if "taken" in modes else "") + ".pages.dev"
    cloud["projects"][name] = {"domain": domain, "branch": args[args.index("--production-branch") + 1],
                               "deployments": []}
    save()
    out(f"Successfully created the '{name}' project. It will be available at https://{domain}/ once you create "
        "your first deployment.")
if args[:3] == ["r2", "bucket", "info"]:
    if args[3] not in cloud["buckets"]:
        out("The specified bucket does not exist.", 1)
    out(json.dumps({"name": args[3]}))
if args[:3] == ["r2", "bucket", "create"]:
    cloud["buckets"][args[3]] = {"objects": {}, "cors": None, "dev_url": False}
    save()
    out(f"Created bucket '{args[3]}' with default storage class of Standard.")
if args[:2] == ["r2", "object"]:
    bucket, key = args[3].split("/", 1)
    b = cloud["buckets"][bucket]
    if args[2] == "put":
        assert "--remote" in args
        path = Path(args[args.index("--file") + 1])
        if "too_large" in modes and path.stat().st_size > 1000:
            out("✘ [ERROR] Error: Wrangler only supports uploading files up to 300 MiB in size", 1)
        b["objects"][key] = {"sha": hashlib.sha256(path.read_bytes()).hexdigest(),
                             "type": args[args.index("--content-type") + 1],
                             "cache": args[args.index("--cache-control") + 1]}
        save()
        out(f"Creating object \"{key}\" in bucket \"{bucket}\".\nUsing token %(secret)s for account %(account)s\n"
            "Upload complete.")
    if args[2] == "delete":
        b["objects"].pop(key, None)
        save()
        out(f"Deleted object \"{key}\".")
if args[:4] == ["r2", "bucket", "dev-url", "enable"]:
    cloud["buckets"][args[4]]["dev_url"] = True
    save()
    out(f"Public access enabled at '%(r2dev)s'.")
if args[:4] == ["r2", "bucket", "dev-url", "get"]:
    out(f"Public access is enabled at '%(r2dev)s'.")
if args[:4] == ["r2", "bucket", "cors", "set"]:
    cloud["buckets"][args[4]]["cors"] = json.loads(Path(args[args.index("--file") + 1]).read_text())
    save()
    out(f"Set CORS configuration for bucket '{args[4]}'.")
if args[:2] == ["pages", "deploy"]:
    folder = Path(args[2])
    name = args[args.index("--project-name") + 1]
    if "fail_deploy" in modes:
        out("✘ [ERROR] A request to the Cloudflare API (/accounts/%(account)s/pages/projects) failed.\n"
            "  Authentication error [code: 10000] Bearer %(secret)s", 1)
    files = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
    manifest = json.loads((folder / "site.json").read_text()) if (folder / "site.json").exists() else None
    page = (folder / "index.html").read_text() if (folder / "index.html").exists() else ""
    cloud["projects"][name]["deployments"].append({"files": files, "manifest": manifest, "page": page,
                                                    "branch": args[args.index("--branch") + 1]})
    save()
    domain = cloud["projects"][name]["domain"]
    out(f"Uploaded {len(files)} files\n✨ Deployment complete! Take a peek over at https://a1b2c3d4.{domain}")
out("unknown command: " + " ".join(args), 1)
''' % {"email": EMAIL, "account": ACCOUNT, "secret": SECRET, "r2dev": R2_DEV}


def install(folder: Path) -> Path:
    """The fake ``wrangler`` in ``folder/bin`` (run by this Python), its records in ``folder``: the bin folder."""
    bin_dir = folder / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "wrangler"
    script.write_text(f"#!{sys.executable}\n" + SCRIPT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def calls(folder: Path) -> list[list[str]]:
    path = folder / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def cloud(folder: Path) -> dict:
    path = folder / "cloud.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"projects": {}, "buckets": {}}


def environment(monkeypatch, folder: Path, mode: str = "") -> Path:
    """The fake on the PATH alone (no real wrangler, no npx), its folder and mode in the environment."""
    from pyarchinit_photogrammetry.io import site_publish

    bin_dir = install(folder)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("FAKE_WRANGLER_DIR", str(folder))
    monkeypatch.setenv("FAKE_WRANGLER_MODE", mode)
    monkeypatch.delenv("PPG_WRANGLER", raising=False)
    monkeypatch.setattr(site_publish, "extra_dirs", lambda: [])
    return bin_dir


def no_wrangler(monkeypatch, folder: Path) -> None:
    from pyarchinit_photogrammetry.io import site_publish

    empty = folder / "empty_bin"
    empty.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("PPG_WRANGLER", raising=False)
    monkeypatch.setattr(site_publish, "extra_dirs", lambda: [])


def make_site(folder: Path, frame: str = "local", data_files: dict | None = None, title: str = "Prova") -> Path:
    """A synthetic site folder as ``ppg web build`` writes it: page, manifest, viewer, data (``{name: size}``,
    sparse files; default a small orthophoto and units)."""
    folder.mkdir(parents=True, exist_ok=True)
    data_files = {"ortho.tif": 2000, "units.geojson": 300} if data_files is None else data_files
    layers = [{"id": Path(n).stem, "url": f"data/{n}"} for n in data_files]
    manifest = {"format": "ppg-site", "version": 1, "frame": frame, "title": title,
                "chunks": [{"key": "0", "layers": layers, "report": "data/report.html"}]}
    (folder / "site.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "index.html").write_text(
        "<!doctype html><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'self'; script-src 'self'; "
        "img-src 'self' blob: data:; object-src 'none'\"><title>Prova</title>", encoding="utf-8")
    (folder / "app").mkdir(exist_ok=True)
    (folder / "app" / "main.js").write_text("export const a = 1;\n", encoding="utf-8")
    data = folder / "data"
    data.mkdir(exist_ok=True)
    for i, (name, size) in enumerate(data_files.items()):
        path = data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(bytes([i + 1]) * min(size, 64))
            fh.truncate(size)  # sparse: no disk used for the large ones
    return folder


__all__ = ["install", "calls", "cloud", "environment", "no_wrangler", "make_site", "SECRET", "ACCOUNT", "EMAIL",
           "R2_DEV"]

if os.environ.get("_FAKE_WRANGLER_SELFTEST"):  # pragma: no cover
    print(SCRIPT)
