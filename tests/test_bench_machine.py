"""The bench reads the machine, the link of the disk and the photos. Its facts go into records that may be shared:
never a host name, a user name, a path, a serial number, a volume name or a photo name in what it returns. Every name
of a device below is made up; the tools of the system are stand-ins that record their calls."""
from __future__ import annotations

import ctypes
import getpass
import json
import os
import platform
import plistlib
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest

from pyarchinit_photogrammetry.bench import machine

# ``ioreg -p IOUSB -l -w0``: one device, and a tree with a hub (the lines carry tree bars, the keys come in any order)
IOREG = '''
+-o Field Disk 9Z@01100000  <class IOUSBHostDevice, id 0x1, registered, matched, active>
    {
      "USB Product Name" = "Field Disk 9Z"
      "USB Serial Number" = "0000"
      "UsbLinkSpeed" = 480000000
    }
'''

IOREG_TREE = '''
+-o Root  <class IORegistryEntry, id 0x100000100, retain 30>
  +-o HostController@00000000  <class HostController, id 0x100000200, registered, matched, active>
  | +-o Travel Hub 4P@00100000  <class IOUSBHostDevice, id 0x100000300, registered, matched, active>
  |   | {
  |   |   "UsbLinkSpeed" = 5000000000
  |   |   "USB Serial Number" = "0000"
  |   |   "USB Product Name" = "Travel Hub 4P"
  |   | }
  |   |
  |   +-o Field Disk 9Z@00110000  <class IOUSBHostDevice, id 0x100000400, registered, matched, active>
  |       {
  |         "USB Product Name" = "Field Disk 9Z"
  |         "USB Serial Number" = "0000"
  |         "UsbLinkSpeed" = 480000000
  |       }
'''

# ``diskutil info -plist`` of an APFS volume on an external disk: the media name is empty on the volume and on its
# physical store and sits on the whole disk, two steps up (the shapes of a real disk, the names made up)
VOLUME = {"BusProtocol": "USB", "SolidState": True, "MediaName": "",
          "APFSPhysicalStores": [{"APFSPhysicalStore": "disk7s1"}], "ParentWholeDisk": "disk8"}
STORE = {"BusProtocol": "USB", "MediaName": "", "ParentWholeDisk": "disk7"}
WHOLE = {"WholeDisk": True, "MediaName": "Field Disk 9Z", "ParentWholeDisk": "disk7"}
INTERNAL = {"BusProtocol": "Apple Fabric", "SolidState": True, "MediaName": ""}

NONE = {"bus": None, "link_mbps": None, "solid_state": None}


def _tools(monkeypatch, volume=VOLUME, ioreg=IOREG_TREE, **plists):
    """A Darwin whose ``subprocess.run`` answers ``diskutil`` by the identifier asked (anything that is not a known
    identifier is the volume) and ``ioreg`` with a text; every call is recorded (command, keyword arguments)."""
    answers = {"disk7s1": STORE, "disk7": WHOLE, **plists}
    calls = []

    def run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        if cmd[0] == "diskutil":
            return subprocess.CompletedProcess(cmd, 0, plistlib.dumps(answers.get(cmd[-1], volume)), b"")
        if cmd[0] == "ioreg":
            return subprocess.CompletedProcess(cmd, 0, ioreg, "")
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(machine.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(machine.subprocess, "run", run)
    return calls


def _write(path: Path, size: int) -> Path:
    """A local file of ``size`` bytes, on the disk (fsynced)."""
    with open(path, "wb") as fh:
        fh.write(b"x" * size)
        fh.flush()
        os.fsync(fh.fileno())
    return path


def _completed(cmd, code=0, out="", err=""):
    return subprocess.CompletedProcess(cmd, code, out, err)


def _raising(exc):
    """A stand-in that raises ``exc`` whatever it is called with."""
    def call(*args, **kwargs):
        raise exc

    return call


# --- ioreg and diskutil parsers ---------------------------------------------------------------------------------------

def test_the_usb_link_speed_is_read_per_product():
    assert machine.parse_ioreg_usb(IOREG) == {"Field Disk 9Z": 480_000_000}
    assert machine.parse_ioreg_usb(IOREG_TREE) == {"Travel Hub 4P": 5_000_000_000, "Field Disk 9Z": 480_000_000}
    assert machine.parse_ioreg_usb("") == {}


def test_a_product_seen_twice_keeps_the_lowest_speed_and_a_block_needs_both_keys():
    twice = IOREG + IOREG.replace("480000000", "5000000000")
    assert machine.parse_ioreg_usb(twice) == {"Field Disk 9Z": 480_000_000}
    halves = '+-o A@1\n  "USB Product Name" = "A"\n+-o B@2\n  "UsbLinkSpeed" = 12000000\n  "USB Serial Number" = "1"\n'
    assert machine.parse_ioreg_usb(halves) == {}
    # only names and speeds come out: no serial number, no address of the device
    assert all(isinstance(k, str) and isinstance(v, int) for k, v in machine.parse_ioreg_usb(IOREG_TREE).items())


def test_diskutil_answers_are_read_into_five_keys():
    assert machine.parse_diskutil(VOLUME) == {"bus": "USB", "solid_state": True, "store": "disk7s1", "media": None,
                                              "parent": "disk8"}
    assert machine.parse_diskutil(WHOLE) == {"bus": None, "solid_state": None, "store": None,
                                             "media": "Field Disk 9Z", "parent": "disk7"}
    assert machine.parse_diskutil({"APFSPhysicalStores": []})["store"] is None
    assert machine.parse_diskutil({}) == {"bus": None, "solid_state": None, "store": None, "media": None,
                                          "parent": None}


# --- disk_facts -------------------------------------------------------------------------------------------------------

def test_the_link_of_an_external_disk_is_found_two_steps_up(monkeypatch, tmp_path):
    calls = _tools(monkeypatch)
    assert machine.disk_facts(tmp_path) == {"bus": "USB", "link_mbps": 480, "solid_state": True}
    assert [cmd[0] for cmd, _ in calls] == ["diskutil", "diskutil", "diskutil", "ioreg"]
    diskutil = [cmd for cmd, _ in calls if cmd[0] == "diskutil"]
    assert [cmd[:3] for cmd in diskutil] == [["diskutil", "info", "-plist"]] * 3
    assert [cmd[-1] for cmd in diskutil][1:] == ["disk7s1", "disk7"]  # the store first, never disk8
    assert calls[3][0] == ["ioreg", "-p", "IOUSB", "-l", "-w0"]
    for cmd, kwargs in calls:
        assert kwargs["capture_output"] is True and kwargs["close_fds"] is False and kwargs["timeout"] == 20
        assert bool(kwargs.get("text")) is (cmd[0] == "ioreg")  # the plist of diskutil is read as bytes


def test_an_internal_disk_has_no_link_speed_and_ioreg_is_not_asked(monkeypatch, tmp_path):
    calls = _tools(monkeypatch, volume=INTERNAL)
    assert machine.disk_facts(tmp_path) == {"bus": "Apple Fabric", "link_mbps": None, "solid_state": True}
    assert [cmd[0] for cmd, _ in calls] == ["diskutil"]


def test_the_media_name_is_matched_to_the_product_by_prefix_case_ignored(monkeypatch, tmp_path):
    # the media name starts with the product name
    _tools(monkeypatch, disk7=dict(WHOLE, MediaName="FIELD DISK 9Z MEDIA"))
    assert machine.disk_facts(tmp_path)["link_mbps"] == 480
    # the product name starts with the media name; of several matches the lowest speed
    two = IOREG_TREE.replace("Travel Hub 4P", "Field Disk 9Z Reader")
    _tools(monkeypatch, ioreg=two, disk7=dict(WHOLE, MediaName="field disk"))
    assert machine.disk_facts(tmp_path)["link_mbps"] == 480
    # no match, and a product without a name matches nothing
    _tools(monkeypatch, disk7=dict(WHOLE, MediaName="Other Disk"))
    assert machine.disk_facts(tmp_path) == {"bus": "USB", "link_mbps": None, "solid_state": True}
    nameless = IOREG.replace('"Field Disk 9Z"', '""')
    _tools(monkeypatch, ioreg=nameless, disk7=dict(WHOLE, MediaName="Other Disk"))
    assert machine.disk_facts(tmp_path)["link_mbps"] is None


def test_the_chain_of_diskutil_stops_at_three_calls_or_at_an_identifier_already_asked(monkeypatch, tmp_path):
    # the whole disk has no media name either and names a further parent: three calls, no ioreg, no link
    calls = _tools(monkeypatch, disk7={"MediaName": "", "ParentWholeDisk": "disk6"})
    assert machine.disk_facts(tmp_path) == {"bus": "USB", "link_mbps": None, "solid_state": True}
    assert [cmd[0] for cmd, _ in calls] == ["diskutil"] * 3
    # a volume without a store whose parent names itself: two calls
    calls = _tools(monkeypatch, volume={"BusProtocol": "USB", "SolidState": False, "ParentWholeDisk": "disk8"},
                   disk8={"MediaName": "", "ParentWholeDisk": "disk8"})
    assert machine.disk_facts(tmp_path) == {"bus": "USB", "link_mbps": None, "solid_state": False}
    assert [cmd[-1] for cmd, _ in calls] == [calls[0][0][-1], "disk8"]
    # a diskutil that fails on the store keeps what the volume said
    calls = _tools(monkeypatch)
    real = machine.subprocess.run
    monkeypatch.setattr(machine.subprocess, "run",
                        lambda cmd, **k: _completed(cmd, 1, b"", b"") if cmd[-1] == "disk7s1" else real(cmd, **k))
    assert machine.disk_facts(tmp_path) == {"bus": "USB", "link_mbps": None, "solid_state": True}


def test_disk_facts_never_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(machine.platform, "system", lambda: "Plan9")
    assert machine.disk_facts(tmp_path) == NONE
    monkeypatch.setattr(machine.platform, "system", lambda: "Darwin")

    def missing(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(machine.subprocess, "run", missing)
    assert machine.disk_facts(tmp_path) == NONE
    monkeypatch.setattr(machine.subprocess, "run", lambda cmd, **k: _completed(cmd, 1, b"", b"Could not find disk"))
    assert machine.disk_facts(tmp_path) == NONE
    monkeypatch.setattr(machine.subprocess, "run", lambda cmd, **k: _completed(cmd, 0, b"not a plist", b""))
    assert machine.disk_facts(tmp_path) == NONE

    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

    monkeypatch.setattr(machine.subprocess, "run", slow)
    assert machine.disk_facts(tmp_path) == NONE
    monkeypatch.setattr(machine.platform, "system", lambda: "Windows")
    assert machine.disk_facts(tmp_path) == NONE


@pytest.mark.skipif(os.name != "posix", reason="a sysfs tree with a symbolic link")
def test_the_usb_link_of_a_linux_block_device_is_the_nearest_speed_file(tmp_path):
    sys_root = tmp_path / "sys"
    usb2 = sys_root / "devices" / "usb2"
    sdb = usb2 / "2-1" / "2-1:1.0" / "host6" / "block" / "sdb"
    (sdb / "queue").mkdir(parents=True)
    (sdb / "sdb1").mkdir()
    (usb2 / "speed").write_text("10000\n")  # the root hub, further up: the nearest speed wins
    (usb2 / "2-1" / "speed").write_text("480\n")
    (sdb / "queue" / "rotational").write_text("0\n")
    (sys_root / "dev" / "block").mkdir(parents=True)
    os.symlink(os.path.relpath(sdb / "sdb1", sys_root / "dev" / "block"), sys_root / "dev" / "block" / "8:17")
    assert machine._linux_block_facts(8, 17, sys_root) == {"bus": "USB", "link_mbps": 480, "solid_state": True}
    assert machine._linux_block_facts(8, 0, sys_root) == NONE
    (sdb / "queue" / "rotational").write_text("1\n")
    (usb2 / "2-1" / "speed").write_text("not a number\n")
    assert machine._linux_block_facts(8, 17, sys_root) == {"bus": None, "link_mbps": None, "solid_state": False}


def test_on_linux_the_block_device_is_the_one_of_the_path(monkeypatch, tmp_path):
    seen = []

    def block_facts(major, minor, sys_root=None):
        seen.append((major, minor))
        return dict(NONE, bus="USB")

    monkeypatch.setattr(machine.platform, "system", lambda: "Linux")
    monkeypatch.setattr(machine, "_linux_block_facts", block_facts)
    assert machine.disk_facts(tmp_path) == dict(NONE, bus="USB")
    dev = os.stat(tmp_path).st_dev
    assert seen == [(os.major(dev), os.minor(dev))]
    monkeypatch.setattr(machine.os, "stat", _raising(OSError("no stat")))
    assert machine.disk_facts(tmp_path) == NONE


def test_disk_facts_of_a_folder_of_this_computer(tmp_path):
    """The real tools, on the internal disk the test folder is on: the three keys, nothing raised."""
    facts = machine.disk_facts(tmp_path)
    assert set(facts) == {"bus", "link_mbps", "solid_state"}
    assert facts["bus"] is None or isinstance(facts["bus"], str)
    assert facts["link_mbps"] is None or isinstance(facts["link_mbps"], int)
    assert facts["solid_state"] in (True, False, None)


# --- online-only photos -----------------------------------------------------------------------------------------------

def test_a_file_without_blocks_on_disk_is_online_only(monkeypatch, tmp_path):
    # 64 KiB on the disk: some file systems keep a tiny file inside its inode and report no blocks for it
    f = _write(tmp_path / "a.jpg", 64 * 1024)
    assert machine.is_online_only(f) is False
    # a placeholder of size 0, as the program already treats it (engine/visibility.py)
    assert machine.is_online_only(_write(tmp_path / "empty.jpg", 0)) is True
    fake = types.SimpleNamespace(st_size=5_000_000, st_blocks=0, st_flags=0, st_file_attributes=0x400000)
    monkeypatch.setattr(machine.os, "stat", lambda *a, **k: fake)
    assert machine.is_online_only(f) is True


@pytest.mark.parametrize("fake, expected", [
    (types.SimpleNamespace(st_size=5, st_blocks=0), True),  # no block on the disk (POSIX)
    (types.SimpleNamespace(st_size=5, st_blocks=8, st_flags=0x40000000), True),  # dataless (macOS)
    (types.SimpleNamespace(st_size=5, st_file_attributes=0x1000), True),  # offline (Windows: no st_blocks there)
    (types.SimpleNamespace(st_size=5, st_file_attributes=0x400000), True),  # recall on data access (Windows)
    (types.SimpleNamespace(st_size=5, st_blocks=8, st_flags=0, st_file_attributes=0x20), False),  # archive only
    (types.SimpleNamespace(st_size=5), False),  # neither blocks nor flags nor attributes: local
])
def test_each_sign_of_an_online_only_file(monkeypatch, tmp_path, fake, expected):
    monkeypatch.setattr(machine.os, "stat", lambda *a, **k: fake)
    assert machine.is_online_only(tmp_path / "p.jpg") is expected


def test_a_missing_photo_is_the_callers_error_not_an_online_only_one(tmp_path):
    with pytest.raises(FileNotFoundError):
        machine.is_online_only(tmp_path / "missing.jpg")


# --- read_rate --------------------------------------------------------------------------------------------------------

def test_the_read_rate_needs_at_least_8_mib_of_local_files(tmp_path):
    assert machine.read_rate([]) is None
    assert machine.read_rate([_write(tmp_path / "small.bin", 64 * 1024)]) is None
    big = _write(tmp_path / "big.bin", 9 * 2**20)
    rate = machine.read_rate([big])
    if platform.system() in ("Darwin", "Linux"):
        assert isinstance(rate, float) and rate > 0
    else:
        assert rate is None
    assert machine.read_rate([big], max_bytes=4 * 2**20) is None  # capped below the least it accepts
    assert machine.read_rate([tmp_path / "missing.bin"]) is None  # an OSError
    assert machine.read_rate([tmp_path]) is None  # a folder: an OSError of os.read


def test_the_read_rate_never_opens_an_online_only_file(monkeypatch, tmp_path):
    big = _write(tmp_path / "big.bin", 9 * 2**20)
    cloud = tmp_path / "cloud.jpg"  # does not exist: opening it would fail and give None
    monkeypatch.setattr(machine, "is_online_only", lambda p: Path(p).name == "cloud.jpg")
    assert machine.read_rate([cloud]) is None
    rate = machine.read_rate([cloud, big])
    if platform.system() in ("Darwin", "Linux"):
        assert isinstance(rate, float) and rate > 0
    else:
        assert rate is None


def test_the_read_rate_is_none_on_windows_and_elsewhere(monkeypatch, tmp_path):
    big = _write(tmp_path / "big.bin", 9 * 2**20)
    for system in ("Windows", "Plan9"):
        monkeypatch.setattr(machine.platform, "system", lambda s=system: s)
        assert machine.read_rate([big]) is None


# --- photo_set_facts --------------------------------------------------------------------------------------------------

def test_the_facts_of_a_photo_set_skip_the_online_only_ones(monkeypatch, tmp_path):
    photos = [_write(tmp_path / f"p{i}.jpg", size) for i, size in enumerate((1000, 2000, 3000))]
    monkeypatch.setattr(machine, "is_online_only", lambda p: Path(p).name == "p2.jpg")
    read = []

    def read_exif(path):
        read.append(Path(path).name)
        if Path(path).name == "p2.jpg":
            raise AssertionError("the EXIF of an online-only photo was read")
        return {"gps": (1.0, 2.0, 3.0)} if Path(path).name == "p0.jpg" else {}

    monkeypatch.setattr(machine.exif, "read_exif", read_exif)
    facts = machine.photo_set_facts(photos)
    assert facts == {"photos": 3, "bytes": 3000, "online_only": 1, "gps": 1}
    assert read == ["p0.jpg", "p1.jpg"]
    assert machine.photo_set_facts([]) == {"photos": 0, "bytes": 0, "online_only": 0, "gps": 0}


def test_a_photo_whose_exif_cannot_be_read_has_no_position(monkeypatch, tmp_path):
    photos = [_write(tmp_path / f"p{i}.jpg", 100) for i in range(2)]

    def read_exif(path):
        if Path(path).name == "p1.jpg":
            raise OSError("not an image")
        return {"gps": (1.0, 2.0, 3.0)}

    monkeypatch.setattr(machine.exif, "read_exif", read_exif)
    assert machine.photo_set_facts(photos) == {"photos": 2, "bytes": 200, "online_only": 0, "gps": 1}
    monkeypatch.setattr(machine.exif, "read_exif", lambda path: {"gps": None})
    assert machine.photo_set_facts(photos)["gps"] == 0
    with pytest.raises(FileNotFoundError):  # a missing photo is the caller's error
        machine.photo_set_facts([tmp_path / "missing.jpg"])


# --- machine_facts ----------------------------------------------------------------------------------------------------

def _user_name() -> str:
    try:
        return getpass.getuser()
    except Exception:  # a system without a login name
        return ""


def test_the_machine_facts_name_no_host_no_user_no_path():
    facts = machine.machine_facts()
    assert set(facts) == {"system", "release", "arch", "cpu", "cores", "memory_gb", "adapters", "load",
                          "on_battery", "dropbox_running"}
    text = json.dumps(facts)
    assert str(Path.home()) not in text and socket.gethostname() not in text
    if _user_name():
        assert _user_name() not in text
    assert facts["system"] == platform.system() and facts["release"] == platform.release()
    assert facts["arch"] == platform.machine()
    assert facts["cpu"] is None or (isinstance(facts["cpu"], str) and facts["cpu"])
    assert facts["cores"] is None or facts["cores"] >= 1
    assert facts["memory_gb"] is None or (isinstance(facts["memory_gb"], float) and facts["memory_gb"] > 0)
    assert isinstance(facts["adapters"], list) and all(isinstance(n, str) for n in facts["adapters"])
    assert facts["load"] is None or isinstance(facts["load"], float)
    assert facts["on_battery"] in (True, False, None) and facts["dropbox_running"] in (True, False, None)


def test_the_adapters_are_the_gpus_not_the_software_rasterisers(monkeypatch):
    monkeypatch.setattr(machine._native, "gpu_adapters", lambda: [])
    assert machine.machine_facts()["adapters"] == []
    listed = [{"index": 0, "name": "Soft Rasteriser", "backend": "Vulkan", "device_type": "Cpu"},
              {"index": 1, "name": "Example GPU", "backend": "Metal", "device_type": "IntegratedGpu"}]
    monkeypatch.setattr(machine._native, "gpu_adapters", lambda: listed)
    assert machine.machine_facts()["adapters"] == ["Example GPU"]

    def broken():
        raise RuntimeError("driver")

    monkeypatch.setattr(machine._native, "gpu_adapters", broken)
    assert machine.machine_facts()["adapters"] == []


def test_the_memory_is_in_gb_or_none(monkeypatch):
    monkeypatch.setattr(machine._env, "physical_memory", lambda: 16 * 2**30)
    assert machine.machine_facts()["memory_gb"] == 16.0
    monkeypatch.setattr(machine._env, "physical_memory", lambda: None)
    assert machine.machine_facts()["memory_gb"] is None


def test_the_cpu_name_per_system(monkeypatch, tmp_path):
    monkeypatch.setattr(machine.subprocess, "run", lambda cmd, **k: _completed(cmd, 0, "Example CPU 12-Core\n"))
    assert machine._cpu_name("Darwin") == "Example CPU 12-Core"
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("processor\t: 0\nvendor_id\t: Example\nmodel name\t: Example CPU @ 3.0GHz\n"
                       "processor\t: 1\nmodel name\t: Example CPU @ 3.0GHz\n")
    assert machine._cpu_linux(cpuinfo) == "Example CPU @ 3.0GHz"
    (tmp_path / "no-model").write_text("vendor_id\t: Example\n")
    assert machine._cpu_linux(tmp_path / "no-model") is None
    if sys.platform == "win32":
        assert isinstance(machine._cpu_windows(), str)
    # elsewhere, or when the tool fails: platform.processor(), None when that is empty too
    monkeypatch.setattr(machine.platform, "processor", lambda: "Fallback CPU")
    assert machine._cpu_name("Plan9") == "Fallback CPU"
    monkeypatch.setattr(machine.subprocess, "run", lambda cmd, **k: _completed(cmd, 1, ""))
    assert machine._cpu_name("Darwin") == "Fallback CPU"
    monkeypatch.setattr(machine, "_cpu_linux", _raising(OSError("no cpuinfo")))
    assert machine._cpu_name("Linux") == "Fallback CPU"
    monkeypatch.setattr(machine.platform, "processor", lambda: "")
    assert machine._cpu_name("Plan9") is None


def test_the_load_is_none_where_the_system_has_none(monkeypatch):
    monkeypatch.setattr(machine.os, "getloadavg", lambda: (1.5, 1.0, 0.5), raising=False)
    assert machine._load() == 1.5

    def failing():
        raise OSError("not available")

    monkeypatch.setattr(machine.os, "getloadavg", failing, raising=False)
    assert machine._load() is None
    monkeypatch.delattr(machine.os, "getloadavg", raising=False)
    assert machine._load() is None


def test_on_battery_per_system(monkeypatch, tmp_path):
    assert machine._battery_from_pmset("Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t57%") is True
    assert machine._battery_from_pmset("Now drawing from 'AC Power'\n") is False
    assert machine._battery_from_pmset("") is None
    monkeypatch.setattr(machine.subprocess, "run", lambda cmd, **k: _completed(cmd, 0, "Now drawing from 'AC Power'"))
    assert machine._on_battery("Darwin") is False
    monkeypatch.setattr(machine.subprocess, "run", _raising(FileNotFoundError("pmset")))
    assert machine._on_battery("Darwin") is None
    assert machine._on_battery("Plan9") is None
    # Linux: the supplies of sysfs
    supplies = tmp_path / "power_supply"
    (supplies / "AC").mkdir(parents=True)
    (supplies / "BAT0").mkdir()
    (supplies / "AC" / "type").write_text("Mains\n")
    (supplies / "AC" / "online").write_text("1\n")
    (supplies / "BAT0" / "type").write_text("Battery\n")
    (supplies / "BAT0" / "status").write_text("Discharging\n")
    assert machine._battery_from_power_supply(supplies) is False  # mains online wins
    (supplies / "AC" / "online").write_text("0\n")
    assert machine._battery_from_power_supply(supplies) is True
    (supplies / "BAT0" / "status").write_text("Charging\n")
    assert machine._battery_from_power_supply(supplies) is None
    assert machine._battery_from_power_supply(tmp_path / "missing") is None


def test_on_battery_on_windows_from_kernel32(monkeypatch):
    if sys.platform == "win32":  # the real call
        assert machine._battery_windows() in (True, False, None)
        return

    def kernel32(ac_line_status, result=1):
        def status(ref):
            ref._obj.ACLineStatus = ac_line_status
            return result

        return types.SimpleNamespace(kernel32=types.SimpleNamespace(GetSystemPowerStatus=status))

    monkeypatch.setattr(ctypes, "windll", kernel32(0), raising=False)
    assert machine._battery_windows() is True
    monkeypatch.setattr(ctypes, "windll", kernel32(1), raising=False)
    assert machine._battery_windows() is False
    monkeypatch.setattr(ctypes, "windll", kernel32(255), raising=False)
    assert machine._battery_windows() is None
    monkeypatch.setattr(ctypes, "windll", kernel32(0, result=0), raising=False)
    assert machine._battery_windows() is None
    monkeypatch.delattr(ctypes, "windll", raising=False)
    assert machine._on_battery("Windows") is None


def test_dropbox_running_per_system(monkeypatch):
    seen = []

    def pgrep(code):
        def run(cmd, **kwargs):
            seen.append(cmd)
            return _completed(cmd, code)

        return run

    for code, expected in ((0, True), (1, False), (2, None), (3, None)):
        monkeypatch.setattr(machine.subprocess, "run", pgrep(code))
        assert machine._dropbox_running("Darwin") is expected
    assert seen == [["pgrep", "-ix", "dropbox"]] * 4
    monkeypatch.setattr(machine.subprocess, "run", _raising(FileNotFoundError("pgrep")))
    assert machine._dropbox_running("Linux") is None
    # Windows: tasklist
    seen.clear()

    def tasklist(code, out):
        def run(cmd, **kwargs):
            seen.append(cmd)
            return _completed(cmd, code, out)

        return run

    monkeypatch.setattr(machine.subprocess, "run", tasklist(0, "Dropbox.exe    1234 Console    1    120,000 K\n"))
    assert machine._dropbox_running("Windows") is True
    monkeypatch.setattr(machine.subprocess, "run", tasklist(0, "INFO: No tasks are running which match the criteria."))
    assert machine._dropbox_running("Windows") is False
    monkeypatch.setattr(machine.subprocess, "run", tasklist(1, ""))
    assert machine._dropbox_running("Windows") is None
    assert seen == [["tasklist", "/FI", "IMAGENAME eq Dropbox.exe", "/NH"]] * 3


def test_the_machine_facts_never_raise(monkeypatch):
    def failing(*args, **kwargs):
        raise RuntimeError("every tool fails")

    monkeypatch.setattr(machine.subprocess, "run", failing)
    monkeypatch.setattr(machine._native, "gpu_adapters", failing)
    monkeypatch.setattr(machine._env, "physical_memory", failing)
    monkeypatch.setattr(machine.os, "getloadavg", failing, raising=False)
    monkeypatch.setattr(machine.platform, "processor", lambda: "")
    facts = machine.machine_facts()
    assert facts["adapters"] == [] and facts["memory_gb"] is None and facts["load"] is None
    assert facts["cpu"] is None and facts["on_battery"] is None and facts["dropbox_running"] is None
    assert facts["system"] == platform.system()
