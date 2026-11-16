"""The facts a measurement of the bench depends on: the machine, the link of the disk that holds the photos, and the
photos themselves (how many are online-only, the rate the disk reads them at), so that a record can say when a run is
not valid (a slow disk link, a busy machine, a Dropbox that downloads while the engine reads).

The facts go into bench records that may be shared: nothing here returns a host name, a user name, a path, a serial
number, a volume name or a photo name. A fact that cannot be read is None (an empty list for the adapters): a tool that
is missing, fails or answers something else never raises. Standard library only, beside three readers of the package
(:func:`.._env.physical_memory`, :func:`.._native.gpu_adapters`, :func:`..io.exif.read_exif`).
"""
from __future__ import annotations

import os
import platform
import plistlib
import re
import shutil
import subprocess
import time
from pathlib import Path

from .. import _env, _native
from ..io import exif

try:
    import fcntl  # POSIX: F_NOCACHE of macOS, for read_rate
except ImportError:  # pragma: no cover - Windows
    fcntl = None

# 20 s is a ceiling for system tools (diskutil, ioreg, sysctl, pmset, pgrep, tasklist) that answer in well under a
# second: an estimate, not a measurement
TOOL_TIMEOUT = 20
# what read_rate reads at most (256 MiB) and the least it accepts (8 MiB), estimates: a rate over a few kilobytes is
# noise, a few hundred megabytes tell the disk from its cache without reading a whole survey
READ_RATE_MAX_BYTES = 256 * 2**20
READ_RATE_MIN_BYTES = 8 * 2**20
_READ_CHUNK = 4 * 2**20
# the signs of a file whose bytes are not on the disk: SF_DATALESS of macOS (iCloud, Dropbox through the File
# Provider), FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS and FILE_ATTRIBUTE_OFFLINE of Windows (OneDrive, Dropbox)
_SF_DATALESS = 0x40000000
_WINDOWS_ONLINE_ONLY = 0x400000 | 0x1000
# the keys read from the blocks of ioreg, searched (the lines carry tree bars in front), in either order
_USB_PRODUCT = re.compile(r'"USB Product Name" = "([^"]*)"')
_USB_SPEED = re.compile(r'"UsbLinkSpeed" = (\d+)')
_BSD_NAME = re.compile(r'"BSD Name" = "([^"]*)"')
_NODE_CLASS = re.compile(r"<class (\w+)")
# the system tools of macOS at their fixed places: an absolute program, close_fds=False and no cwd get posix_spawn,
# not a fork (a child made with fork dies once SpatiaLite has loaded its PROJ: see pyarchinit/database.py)
_DARWIN_PROGRAMS = {"diskutil": "/usr/sbin/diskutil", "ioreg": "/usr/sbin/ioreg", "sysctl": "/usr/sbin/sysctl",
                    "pmset": "/usr/bin/pmset", "pgrep": "/usr/bin/pgrep"}


def _program(name: str, system: str) -> str | None:
    """The absolute program for ``name``: the fixed places of macOS, ``shutil.which`` on Linux and elsewhere (None
    when it finds nothing), the bare name on Windows (no fork there)."""
    if system == "Darwin":
        return _DARWIN_PROGRAMS.get(name)
    if system == "Windows":
        return name
    return shutil.which(name)


def _run(cmd: list[str], text: bool = True) -> subprocess.CompletedProcess:
    """One system tool, its output captured (text decoded with ``errors="replace"``: one stray byte must not cost
    the fact); the caller catches what goes wrong (a missing tool, a timeout)."""
    options = {"text": True, "errors": "replace"} if text else {}
    return subprocess.run(cmd, capture_output=True, close_fds=False, timeout=TOOL_TIMEOUT, **options)


def _tool(name: str, args: list[str], system: str, text: bool = True) -> subprocess.CompletedProcess | None:
    """``name`` started by its absolute program (:func:`_program`) with ``args``; None, and no call, when the
    program is missing."""
    program = _program(name, system)
    if program is None:
        return None
    return _run([program, *args], text=text)


# --- the machine ------------------------------------------------------------------------------------------------------

def machine_facts() -> dict:
    """The machine a measurement ran on: ``system``, ``release``, ``arch`` (``platform.system``, ``release``,
    ``machine``), ``cpu`` (the model name of the processor, or None), ``cores`` (``os.cpu_count()``), ``memory_gb``
    (``float | None``, from :func:`.._env.physical_memory`), ``adapters`` (the names of the GPUs wgpu lists, without
    the software rasterisers the program never uses unless asked by name; ``[]`` also when the native core is off
    or the GPU setting is ``off``), ``load`` (the load average of the last
    minute, None where the system has none), ``on_battery`` and ``dropbox_running`` (``bool | None``: None means not
    known and warns of nothing). Never the host name, the user name or a path; never raises."""
    system = platform.system()
    return {"system": system, "release": platform.release(), "arch": platform.machine(), "cpu": _cpu_name(system),
            "cores": os.cpu_count(), "memory_gb": _memory_gb(), "adapters": _gpu_names(), "load": _load(),
            "on_battery": _on_battery(system), "dropbox_running": _dropbox_running(system)}


def _memory_gb() -> float | None:
    try:
        memory = _env.physical_memory()
    except Exception:
        return None
    return None if memory is None else round(memory / 2**30, 1)


def _gpu_names() -> list[str]:
    """The adapters :func:`.._native.gpu_adapters` lists (in index order, as the native core gives them) whose type
    is not ``Cpu``: the rule of ``gpu_device("auto")``, a software rasteriser is no GPU. [] without the native
    module, and also when the native core is off or the GPU setting is ``off`` (that function lists nothing then)."""
    try:
        adapters = _native.gpu_adapters()
    except Exception:
        return []
    return [str(a.get("name", "")) for a in adapters if a.get("device_type") != "Cpu"]


def _cpu_name(system: str) -> str | None:
    """The model name of the processor: ``sysctl`` on macOS, ``/proc/cpuinfo`` on Linux, the registry on Windows;
    None elsewhere or when the reader fails (never ``platform.processor()``: it starts ``uname`` by a bare name and
    without a timeout)."""
    try:
        if system == "Darwin":
            return _cpu_darwin()
        if system == "Linux":
            return _cpu_linux()
        if system == "Windows":
            return _cpu_windows()
    except Exception:
        pass
    return None


def _cpu_darwin() -> str | None:
    done = _tool("sysctl", ["-n", "machdep.cpu.brand_string"], "Darwin")
    return (done.stdout.strip() or None) if done is not None and done.returncode == 0 else None


def _cpu_linux(cpuinfo: Path = Path("/proc/cpuinfo")) -> str | None:
    """The first ``model name`` line of ``/proc/cpuinfo``."""
    for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("model name"):
            return line.partition(":")[2].strip() or None
    return None


def _cpu_windows() -> str | None:
    """``ProcessorNameString`` of the first processor in the registry."""
    import winreg  # Windows only

    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
    try:
        value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
    finally:
        winreg.CloseKey(key)
    return str(value).strip() or None


def _load() -> float | None:
    """The load average of the last minute; None where the call is missing (Windows) or fails."""
    try:
        return float(os.getloadavg()[0])
    except Exception:
        return None


def _on_battery(system: str) -> bool | None:
    """Whether the computer runs on its battery (a laptop slows down on it): ``pmset`` on macOS, the power supplies
    of sysfs on Linux, ``GetSystemPowerStatus`` on Windows; None where this cannot be told."""
    try:
        if system == "Darwin":
            done = _tool("pmset", ["-g", "batt"], "Darwin")
            return _battery_from_pmset(done.stdout) if done is not None and done.returncode == 0 else None
        if system == "Linux":
            return _battery_from_power_supply()
        if system == "Windows":
            return _battery_windows()
    except Exception:
        return None
    return None


def _battery_from_pmset(text: str) -> bool | None:
    """``pmset -g batt`` says what the Mac draws from: ``Battery Power`` or ``AC Power``."""
    if "Battery Power" in text:
        return True
    if "AC Power" in text:
        return False
    return None


def _battery_from_power_supply(root: Path = Path("/sys/class/power_supply")) -> bool | None:
    """Linux: a supply of type ``Mains`` that is online means the mains (False); else a ``Battery`` that is
    discharging means the battery (True); else not known."""
    supplies = [(_sys_text(folder / "type"), folder) for folder in sorted(root.iterdir())] if root.is_dir() else []
    if any(kind == "Mains" and _sys_text(folder / "online") == "1" for kind, folder in supplies):
        return False
    if any(kind == "Battery" and _sys_text(folder / "status") == "Discharging" for kind, folder in supplies):
        return True
    return None


def _battery_windows() -> bool | None:
    """Windows: ``ACLineStatus`` of ``GetSystemPowerStatus`` (0 offline -> on battery, 1 online -> the mains,
    255 unknown -> None)."""
    import ctypes

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_uint32), ("BatteryFullLifeTime", ctypes.c_uint32)]

    status = SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return None
    return {0: True, 1: False}.get(int(status.ACLineStatus))


def _dropbox_running(system: str) -> bool | None:
    """Whether a Dropbox client runs (it downloads and reads the photos it syncs while the engine reads them):
    ``tasklist`` on Windows, ``pgrep`` elsewhere; None when the tool is missing or answers something else."""
    try:
        if system == "Windows":
            done = _run(["tasklist", "/FI", "IMAGENAME eq Dropbox.exe", "/NH"])
            if "dropbox.exe" in done.stdout.lower():
                return True
            return False if done.returncode == 0 else None
        done = _tool("pgrep", ["-ix", "dropbox"], system)
        return None if done is None else {0: True, 1: False}.get(done.returncode)
    except Exception:
        return None


# --- the disk ---------------------------------------------------------------------------------------------------------

def parse_ioreg_usb(text: str) -> dict[str, int]:
    """Product name -> link speed in bit/s of the USB devices ``ioreg -p IOUSB -l -w0`` lists: the text is cut into
    blocks at the lines that hold ``+-o ``; in each block ``"USB Product Name"`` and ``"UsbLinkSpeed"`` are searched
    in either order, a block without both is skipped, a name seen twice keeps the lowest speed. No other key is read:
    no serial number is kept."""
    speeds: dict[str, int] = {}
    for block in _ioreg_blocks(text):
        product, speed = _USB_PRODUCT.search(block), _USB_SPEED.search(block)
        if product is None or speed is None:
            continue
        name, bits = product.group(1), int(speed.group(1))
        speeds[name] = min(bits, speeds.get(name, bits))
    return speeds


def _ioreg_blocks(text: str) -> list[str]:
    """The lines of each device of the tree, without the ``+-o`` line that opens it."""
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if "+-o " in line:
            blocks.append([])
        elif blocks:
            blocks[-1].append(line)
    return ["\n".join(lines) for lines in blocks]


def parse_ioreg_devices(text: str) -> dict[str, int]:
    """BSD name -> link speed in bit/s of every medium that sits in the subtree of a USB device, from
    ``ioreg -r -c IOUSBHostDevice -l -w0``: a ``+-o`` line opens a node (its depth is the column of ``+-o``, its
    class is read from ``<class ...>``), the lines up to the next node are its properties, a child is deeper than
    its parent. A medium gets the lowest ``UsbLinkSpeed`` of the ``IOUSBHostDevice`` nodes above it (a disk behind
    a hub goes at the slower of the two links); a name seen twice keeps the lowest. No other key is read."""
    links: dict[str, int] = {}
    stack: list[list] = []  # [column, class, speed] of the open nodes, outermost first
    for line in text.splitlines():
        if "+-o " in line:
            column = line.index("+-o ")
            while stack and stack[-1][0] >= column:
                stack.pop()
            found = _NODE_CLASS.search(line)
            stack.append([column, found.group(1) if found else "", None])
            continue
        if not stack:
            continue
        speed = _USB_SPEED.search(line)
        if speed is not None:
            stack[-1][2] = int(speed.group(1))
            continue
        bsd = _BSD_NAME.search(line)
        if bsd is None or not bsd.group(1):
            continue
        speeds = [node[2] for node in stack if node[1] == "IOUSBHostDevice" and node[2] is not None]
        if speeds:
            lowest = min(speeds)
            links[bsd.group(1)] = min(lowest, links.get(bsd.group(1), lowest))
    return links


def parse_diskutil(plist: dict) -> dict:
    """The five facts of a ``diskutil info -plist`` answer: ``bus`` (``BusProtocol``), ``solid_state``
    (``SolidState``), ``store`` (the first of ``APFSPhysicalStores``), ``media`` (``MediaName``; an empty one is
    None), ``parent`` (``ParentWholeDisk``); None for what the answer lacks."""
    stores = plist.get("APFSPhysicalStores") or []
    first = stores[0] if stores else None
    store = first.get("APFSPhysicalStore") if isinstance(first, dict) else None
    return {"bus": plist.get("BusProtocol"), "solid_state": plist.get("SolidState"), "store": store or None,
            "media": plist.get("MediaName") or None, "parent": plist.get("ParentWholeDisk") or None}


def disk_facts(path: Path) -> dict:
    """The disk that holds ``path``: ``bus`` (``USB``, ``Apple Fabric``, ``PCI-Express``, ``SATA``... or None),
    ``link_mbps`` (the speed of the USB link in Mb/s, 480 for USB 2 and 5000 or more for USB 3; None for an internal
    disk or when it cannot be read), ``solid_state`` (True for an SSD, None when not known). macOS asks ``diskutil``
    about the volume the path is on (its mount point: ``diskutil info`` refuses a folder) and, for a USB disk,
    ``ioreg`` for the link; Linux reads sysfs; elsewhere everything is None. Never raises: a tool that is missing or
    fails gives None."""
    try:
        system = platform.system()
        if system == "Darwin":
            return _darwin_disk_facts(Path(path))
        if system == "Linux":
            device = os.stat(path).st_dev
            return _linux_block_facts(os.major(device), os.minor(device))
    except Exception:
        pass
    return {"bus": None, "link_mbps": None, "solid_state": None}


def _mount_point(path: Path) -> str:
    """The mount point of the volume that holds ``path`` (the path itself when it is one)."""
    here = os.path.realpath(path)
    while not os.path.ismount(here):
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return here


def _darwin_disk_facts(path: Path) -> dict:
    """``diskutil info -plist`` of the volume gives the bus and the kind of disk. For a USB disk the chain of
    ``diskutil`` reaches the whole disk: on an APFS volume of an external disk the media name is empty on the volume
    and on its physical store and sits on the whole disk, so the store (else the parent) of each answer is asked in
    turn, at most three calls in all, never an identifier already asked; a call that fails after the first answer
    ends the chain. The link comes first by the exact route, the ``UsbLinkSpeed`` of the USB device whose subtree
    holds the BSD name of the whole disk known so far (``ioreg -r``; the synthesized APFS disk a volume names sits
    in that subtree too); when that gives nothing, by name: the media name matched to the products of the plain USB
    list (``ioreg -p``)."""
    facts = {"bus": None, "link_mbps": None, "solid_state": None}
    target, asked, media, whole = _mount_point(path), set(), None, None
    for step in range(3):
        plist = _diskutil_info(target)
        asked.add(target)
        if plist is None:
            if step == 0:
                return facts
            break
        info = parse_diskutil(plist)
        if step == 0:
            facts["bus"], facts["solid_state"] = info["bus"], info["solid_state"]
            if info["bus"] != "USB":
                return facts
        media, whole = info["media"], info["parent"] or target
        if media:
            break
        target = info["store"] or info["parent"]
        if target is None or target in asked:
            break
    bits = _ioreg_links().get(whole) if whole else None
    if bits is None and media:
        # a product matches when its name holds the media name or the media name holds it, case ignored (a disk
        # may be named by one word of its product name); of several matches (a reader and its disk) the lowest
        key = media.lower()
        matches = [speed for name, speed in _ioreg_usb_speeds().items()
                   if name and (name.lower() in key or key in name.lower())]
        bits = min(matches) if matches else None
    if bits is not None:
        facts["link_mbps"] = bits // 1_000_000
    return facts


def _diskutil_info(target: str) -> dict | None:
    """The plist ``diskutil info -plist`` gives for a mount point or a disk identifier, read as bytes; None when
    the tool is missing, fails or answers something that is not a plist."""
    try:
        done = _tool("diskutil", ["info", "-plist", target], "Darwin", text=False)
        if done is None or done.returncode != 0:
            return None
        plist = plistlib.loads(done.stdout)
    except Exception:
        return None
    return plist if isinstance(plist, dict) else None


def _ioreg_links() -> dict[str, int]:
    """BSD name -> link speed from the subtrees of the USB devices (the exact route)."""
    return _ioreg(["-r", "-c", "IOUSBHostDevice", "-l", "-w0"], parse_ioreg_devices)


def _ioreg_usb_speeds() -> dict[str, int]:
    """Product name -> link speed from the plain USB list (the route by name)."""
    return _ioreg(["-p", "IOUSB", "-l", "-w0"], parse_ioreg_usb)


def _ioreg(args: list[str], parse) -> dict[str, int]:
    """``ioreg`` with ``args`` through ``parse``; {} when the tool is missing, fails or cannot be parsed."""
    try:
        done = _tool("ioreg", args, "Darwin")
        return parse(done.stdout) if done is not None and done.returncode == 0 else {}
    except Exception:
        return {}


def _linux_block_facts(major: int, minor: int, sys_root: Path = Path("/sys")) -> dict:
    """Linux: the block device ``major:minor`` of sysfs, and up from its folder (itself included) to ``sys_root``:
    the nearest ``speed`` file (the link of the USB device that holds the disk, in Mb/s) gives ``link_mbps`` and the
    bus ``USB``; the nearest ``queue/rotational`` tells an SSD (``0``) from a spinning disk (``1``). What is not
    found is None."""
    facts = {"bus": None, "link_mbps": None, "solid_state": None}
    root = Path(os.path.realpath(sys_root))
    folder = Path(os.path.realpath(root / "dev" / "block" / f"{major}:{minor}"))
    if not folder.is_dir() or (folder != root and root not in folder.parents):
        return facts
    speed_seen = rotational_seen = False  # the nearest file of each kind counts, readable or not
    for here in (folder, *folder.parents):
        if not speed_seen and (here / "speed").is_file():
            speed_seen = True
            try:
                facts["link_mbps"], facts["bus"] = int(float(_sys_text(here / "speed") or "")), "USB"
            except ValueError:
                pass
        if not rotational_seen and (here / "queue" / "rotational").is_file():
            rotational_seen = True
            facts["solid_state"] = {"0": True, "1": False}.get(_sys_text(here / "queue" / "rotational"))
        if here == root or (speed_seen and rotational_seen):
            break
    return facts


def _sys_text(path: Path) -> str | None:
    """The stripped text of a sysfs file, None when it cannot be read."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


# --- the photos -------------------------------------------------------------------------------------------------------

def is_online_only(path: Path) -> bool:
    """Whether the bytes of ``path`` are not on the disk (Dropbox, iCloud and OneDrive keep a file online only):
    from one ``os.stat``, never opening the file (that would download it). True for a size of 0 (the placeholder
    :func:`..engine.visibility.unreadable_photos` already treats as online-only), for no block on the disk (POSIX:
    Windows has no ``st_blocks``), for the dataless flag of macOS and for the offline attributes of Windows. A
    missing file raises: that is the caller's error, not an online-only one."""
    st = os.stat(path)
    if st.st_size == 0:
        return True
    blocks = getattr(st, "st_blocks", None)
    if blocks is not None and blocks == 0:
        return True
    if getattr(st, "st_flags", 0) & _SF_DATALESS:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _WINDOWS_ONLINE_ONLY)


def read_rate(files: list[Path], max_bytes: int = READ_RATE_MAX_BYTES) -> float | None:
    """MB/s at which the disk gives the local files of the list, read past the system cache (``F_NOCACHE`` on
    macOS, ``posix_fadvise(POSIX_FADV_DONTNEED)`` on Linux) 4 MiB at a time, at most ``max_bytes`` in all, only
    the reads timed. The figure is an upper bound of what the disk gives: a file still in the system cache reads
    faster. None on Windows and on any system other than macOS and Linux, when the system could not be asked to
    read past the cache, when no file of the list is local (a file :func:`is_online_only` says is online-only is
    never opened), on any ``OSError``, when fewer than ``READ_RATE_MIN_BYTES`` were read, and when no time
    passed."""
    system = platform.system()
    if system not in ("Darwin", "Linux"):
        return None
    total, seconds = 0, 0.0
    try:
        for file in files:
            if total >= max_bytes:
                break
            if is_online_only(file):
                continue
            fd = os.open(file, os.O_RDONLY)
            try:
                if not _read_past_the_cache(fd, system):
                    return None
                while total < max_bytes:
                    started = time.perf_counter()
                    chunk = os.read(fd, min(_READ_CHUNK, max_bytes - total))
                    seconds += time.perf_counter() - started
                    if not chunk:
                        break
                    total += len(chunk)
            finally:
                os.close(fd)
    except OSError:
        return None
    if total < READ_RATE_MIN_BYTES or seconds <= 0:
        return None
    return total / 1e6 / seconds


def _read_past_the_cache(fd: int, system: str) -> bool:
    """Before the reads: ``F_NOCACHE`` on the open file (macOS), the cached pages dropped (Linux). False when the
    system cannot be asked: a figure read from the cache would say nothing about the disk."""
    if system == "Darwin" and fcntl is not None and hasattr(fcntl, "F_NOCACHE"):
        fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)
        return True
    if system == "Linux" and hasattr(os, "posix_fadvise"):
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        return True
    return False


def photo_set_facts(photos: list[Path]) -> dict:
    """The photos of a run: ``photos`` (how many), ``online_only`` (those :func:`is_online_only`), ``bytes`` (the
    size of the local ones), ``gps`` (the local ones whose EXIF holds a position). The EXIF is read from local files
    only, never from an online-only one (that would download it); a photo whose EXIF cannot be read counts as one
    without a position; a missing photo raises, as in :func:`is_online_only`. No photo name is returned."""
    online_only = total = gps = 0
    for photo in photos:
        if is_online_only(photo):
            online_only += 1
            continue
        total += os.stat(photo).st_size
        try:
            info = exif.read_exif(photo)
        except Exception:
            info = {}
        if info.get("gps") is not None:
            gps += 1
    return {"photos": len(photos), "bytes": total, "online_only": online_only, "gps": gps}
