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
import posixpath
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

# ``ioreg -r -c IOUSBHostDevice -l -w0``: the subtree of every USB device, children two columns deeper, the properties
# right after each ``+-o`` line; a disk behind a hub sits inside the hub's subtree; the media of a disk carry its BSD
# names (the whole disk, its partitions, the APFS disks synthesized on it)
IOREG_DEVICES = '''
+-o Travel Hub 4P@00100000  <class IOUSBHostDevice, id 0x100000300, registered, matched, active>
  | {
  |   "UsbLinkSpeed" = 5000000000
  |   "USB Serial Number" = "0000"
  |   "USB Product Name" = "Travel Hub 4P"
  | }
  |
  +-o Field Disk 9Z@00110000  <class IOUSBHostDevice, id 0x100000400, registered, matched, active>
    | {
    |   "USB Product Name" = "Field Disk 9Z"
    |   "USB Serial Number" = "0000"
    |   "UsbLinkSpeed" = 480000000
    | }
    |
    +-o Field Disk 9Z@00110000  <class IOUSBHostInterface, id 0x100000401, registered, matched, active>
      | {
      |   "bInterfaceNumber" = 0
      | }
      |
      +-o IOUSBMassStorageInterfaceNub@00110000  <class IOUSBMassStorageInterfaceNub, id 0x100000402, registered>
        +-o Field Disk 9Z Media  <class IOMedia, id 0x100000410, registered, matched, active>
          | {
          |   "BSD Name" = "disk7"
          |   "Whole" = Yes
          | }
          |
          +-o Field Disk 9Z Media  <class IOMedia, id 0x100000411, registered, matched, active>
            | {
            |   "BSD Name" = "disk7s1"
            |   "Whole" = No
            | }
            |
            +-o AppleAPFSMedia  <class AppleAPFSMedia, id 0x100000420, registered, matched, active>
                {
                  "BSD Name" = "disk8"
                }

+-o Zip Box Vega@00200000  <class IOUSBHostDevice, id 0x100000500, registered, matched, active>
  | {
  |   "USB Product Name" = "Zip Box Vega"
  |   "UsbLinkSpeed" = 10000000000
  | }
  |
  +-o Zip Box Vega Media  <class IOMedia, id 0x100000510, registered, matched, active>
      {
        "BSD Name" = "disk5"
      }

+-o Slow Hub 2P@00300000  <class IOUSBHostDevice, id 0x100000600, registered, matched, active>
  | {
  |   "UsbLinkSpeed" = 480000000
  |   "USB Product Name" = "Slow Hub 2P"
  | }
  |
  +-o Fast Stick@00310000  <class IOUSBHostDevice, id 0x100000700, registered, matched, active>
    | {
    |   "UsbLinkSpeed" = 5000000000
    |   "USB Product Name" = "Fast Stick"
    | }
    |
    +-o Fast Stick Media  <class IOMedia, id 0x100000710, registered, matched, active>
        {
          "BSD Name" = "disk9"
        }
'''

# ``diskutil info -plist`` of an APFS volume on an external disk: the media name is empty on the volume and on its
# physical store and sits on the whole disk, two steps up (the shapes of a real disk, the names made up)
VOLUME = {"BusProtocol": "USB", "SolidState": True, "MediaName": "",
          "APFSPhysicalStores": [{"APFSPhysicalStore": "disk7s1"}], "ParentWholeDisk": "disk8"}
STORE = {"BusProtocol": "USB", "MediaName": "", "ParentWholeDisk": "disk7"}
WHOLE = {"WholeDisk": True, "MediaName": "Field Disk 9Z", "ParentWholeDisk": "disk7"}
INTERNAL = {"BusProtocol": "Apple Fabric", "SolidState": True, "MediaName": ""}

NONE = {"bus": None, "link_mbps": None, "solid_state": None}


def _tools(monkeypatch, volume=VOLUME, ioreg=IOREG_TREE, devices=IOREG_DEVICES, **plists):
    """A Darwin whose ``subprocess.run`` answers ``diskutil`` by the identifier asked (anything that is not a known
    identifier is the volume), ``ioreg -r`` (the device tree) with ``devices`` and ``ioreg -p`` with ``ioreg``;
    every call is recorded (command, keyword arguments); any other tool is missing."""
    answers = {"disk7s1": STORE, "disk7": WHOLE, **plists}
    calls = []

    def run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        name = Path(cmd[0]).name
        if name == "diskutil":
            return subprocess.CompletedProcess(cmd, 0, plistlib.dumps(answers.get(cmd[-1], volume)), b"")
        if name == "ioreg":
            return subprocess.CompletedProcess(cmd, 0, devices if "-r" in cmd else ioreg, "")
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


def test_the_usb_link_is_read_per_bsd_name_from_the_device_tree():
    """The exact route: a BSD name gets the lowest ``UsbLinkSpeed`` of the USB devices its subtree is in (a disk
    behind a slow hub goes at the hub's speed); every medium of a disk is listed, the synthesized ones too."""
    assert machine.parse_ioreg_devices(IOREG_DEVICES) == {"disk7": 480_000_000, "disk7s1": 480_000_000,
                                                          "disk8": 480_000_000, "disk5": 10_000_000_000,
                                                          "disk9": 480_000_000}
    assert machine.parse_ioreg_devices("") == {}
    assert machine.parse_ioreg_devices(IOREG_TREE) == {}  # no medium in the plain list
    # a BSD name outside any USB device (a device without a speed) is not listed
    orphan = ('+-o Root  <class IORegistryEntry, id 0x1>\n  +-o Disk  <class IOMedia, id 0x2>\n'
              '    "BSD Name" = "disk1"\n')
    assert machine.parse_ioreg_devices(orphan) == {}


def test_diskutil_answers_are_read_into_five_keys():
    assert machine.parse_diskutil(VOLUME) == {"bus": "USB", "solid_state": True, "store": "disk7s1", "media": None,
                                              "parent": "disk8"}
    assert machine.parse_diskutil(WHOLE) == {"bus": None, "solid_state": None, "store": None,
                                             "media": "Field Disk 9Z", "parent": "disk7"}
    assert machine.parse_diskutil({"APFSPhysicalStores": []})["store"] is None
    assert machine.parse_diskutil({}) == {"bus": None, "solid_state": None, "store": None, "media": None,
                                          "parent": None}


# --- disk_facts -------------------------------------------------------------------------------------------------------

def _names(calls):
    return [Path(cmd[0]).name for cmd, _ in calls]


def test_the_link_of_an_external_disk_is_found_two_steps_up(monkeypatch, tmp_path):
    """The chain of diskutil reaches the whole disk; its BSD name in the device tree gives the link (the exact route:
    one ioreg call, the plain list is not asked)."""
    calls = _tools(monkeypatch)
    assert machine.disk_facts(tmp_path) == {"bus": "USB", "link_mbps": 480, "solid_state": True}
    assert _names(calls) == ["diskutil", "diskutil", "diskutil", "ioreg"]
    diskutil = [cmd for cmd, _ in calls if Path(cmd[0]).name == "diskutil"]
    assert [cmd[1:3] for cmd in diskutil] == [["info", "-plist"]] * 3
    assert [cmd[-1] for cmd in diskutil][1:] == ["disk7s1", "disk7"]  # the store first, never disk8
    assert calls[3][0][1:] == ["-r", "-c", "IOUSBHostDevice", "-l", "-w0"]
    for cmd, kwargs in calls:
        assert kwargs["capture_output"] is True and kwargs["close_fds"] is False and kwargs["timeout"] == 20
        assert "cwd" not in kwargs
        if Path(cmd[0]).name == "ioreg":
            assert kwargs["text"] is True and kwargs["errors"] == "replace"
        else:  # the plist of diskutil is read as bytes: neither text nor errors (which would turn text on)
            assert not kwargs.get("text") and "errors" not in kwargs


def test_every_tool_is_started_by_an_absolute_program(monkeypatch, tmp_path):
    """An absolute program with ``close_fds=False`` and no ``cwd`` gets posix_spawn, not a fork (a fork dies once
    SpatiaLite has loaded its PROJ: the convention of the program). macOS: fixed places; Linux: ``shutil.which``,
    and no call when it finds nothing."""
    calls = _tools(monkeypatch)
    machine.disk_facts(tmp_path)
    machine.machine_facts()
    assert calls and all(posixpath.isabs(cmd[0]) for cmd, _ in calls)  # POSIX programs, whatever the host
    assert set(_names(calls)) == {"diskutil", "ioreg", "sysctl", "pmset", "pgrep"}
    assert all("cwd" not in kwargs for _, kwargs in calls)
    calls.clear()
    monkeypatch.setattr(machine.platform, "system", lambda: "Linux")
    monkeypatch.setattr(machine.shutil, "which", lambda name: "/usr/bin/" + name)
    machine.machine_facts()
    assert calls and all(posixpath.isabs(cmd[0]) for cmd, _ in calls) and set(_names(calls)) == {"pgrep"}
    calls.clear()
    monkeypatch.setattr(machine.shutil, "which", lambda name: None)
    assert machine._dropbox_running("Linux") is None and calls == []


def test_an_internal_disk_has_no_link_speed_and_ioreg_is_not_asked(monkeypatch, tmp_path):
    calls = _tools(monkeypatch, volume=INTERNAL)
    assert machine.disk_facts(tmp_path) == {"bus": "Apple Fabric", "link_mbps": None, "solid_state": True}
    assert _names(calls) == ["diskutil"]


def test_the_link_falls_back_to_the_product_name_when_the_device_tree_has_no_such_disk(monkeypatch, tmp_path):
    """Without the whole disk in the device tree (or with a tree that cannot be read) the plain USB list is asked and
    the media name is matched to a product by containment either way, case ignored."""
    calls = _tools(monkeypatch, devices=IOREG_DEVICES.replace('"disk7"', '"disk2"'))
    assert machine.disk_facts(tmp_path) == {"bus": "USB", "link_mbps": 480, "solid_state": True}
    assert _names(calls) == ["diskutil"] * 3 + ["ioreg", "ioreg"]
    assert calls[3][0][1:] == ["-r", "-c", "IOUSBHostDevice", "-l", "-w0"]
    assert calls[4][0][1:] == ["-p", "IOUSB", "-l", "-w0"]
    # the device tree cannot be read: the same fallback
    calls = _tools(monkeypatch)
    real = machine.subprocess.run

    def run(cmd, **kwargs):
        if "-r" in cmd:
            calls.append((list(cmd), kwargs))  # recorded, as the stand-in would have, then the tool hangs
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
        return real(cmd, **kwargs)

    monkeypatch.setattr(machine.subprocess, "run", run)
    assert machine.disk_facts(tmp_path)["link_mbps"] == 480
    assert _names(calls) == ["diskutil"] * 3 + ["ioreg", "ioreg"]


def test_the_media_name_is_matched_to_the_product_by_containment_case_ignored(monkeypatch, tmp_path):
    # the case of the review: a media name of four characters inside a product name of three words
    _tools(monkeypatch, devices="", ioreg=IOREG_TREE.replace("Travel Hub 4P", "Zip Box Vega"),
           disk7=dict(WHOLE, MediaName="Vega"))
    assert machine.disk_facts(tmp_path)["link_mbps"] == 5000
    # the product name inside the media name
    _tools(monkeypatch, devices="", disk7=dict(WHOLE, MediaName="FIELD DISK 9Z MEDIA"))
    assert machine.disk_facts(tmp_path)["link_mbps"] == 480
    # the media name inside two product names: the lowest speed
    two = IOREG_TREE.replace("Travel Hub 4P", "Field Disk 9Z Reader")
    _tools(monkeypatch, devices="", ioreg=two, disk7=dict(WHOLE, MediaName="field disk"))
    assert machine.disk_facts(tmp_path)["link_mbps"] == 480
    # no match, and a product without a name matches nothing
    _tools(monkeypatch, devices="", disk7=dict(WHOLE, MediaName="Other Disk"))
    assert machine.disk_facts(tmp_path) == {"bus": "USB", "link_mbps": None, "solid_state": True}
    nameless = IOREG.replace('"Field Disk 9Z"', '""')
    _tools(monkeypatch, devices="", ioreg=nameless, disk7=dict(WHOLE, MediaName="Other Disk"))
    assert machine.disk_facts(tmp_path)["link_mbps"] is None


def test_the_chain_of_diskutil_stops_at_three_calls_or_at_an_identifier_already_asked(monkeypatch, tmp_path):
    # the whole disk has no media name either and names a further parent: three calls; the device tree is asked for
    # that parent, the plain list is not (no media name to match)
    calls = _tools(monkeypatch, devices="", disk7={"MediaName": "", "ParentWholeDisk": "disk6"})
    assert machine.disk_facts(tmp_path) == {"bus": "USB", "link_mbps": None, "solid_state": True}
    assert _names(calls) == ["diskutil"] * 3 + ["ioreg"]
    # the same whole disk in the device tree: the exact route needs no media name
    calls = _tools(monkeypatch, devices=IOREG_DEVICES.replace('"disk5"', '"disk6"'),
                   disk7={"MediaName": "", "ParentWholeDisk": "disk6"})
    assert machine.disk_facts(tmp_path)["link_mbps"] == 10000
    # a volume without a store whose parent names itself: two calls
    calls = _tools(monkeypatch, devices="", volume={"BusProtocol": "USB", "SolidState": False,
                                                    "ParentWholeDisk": "disk8"},
                   disk8={"MediaName": "", "ParentWholeDisk": "disk8"})
    assert machine.disk_facts(tmp_path) == {"bus": "USB", "link_mbps": None, "solid_state": False}
    assert [cmd[-1] for cmd, _ in calls][:2] == [calls[0][0][-1], "disk8"] and _names(calls)[2:] == ["ioreg"]
    # a diskutil that fails on the store ends the chain and keeps what the volume said; the exact route still runs
    # on the whole disk the volume named (its synthesized APFS disk, which sits in the device tree too)
    def store_fails(calls, tools):
        def run(cmd, **kwargs):
            if cmd[-1] == "disk7s1":
                calls.append((list(cmd), kwargs))
                return _completed(cmd, 1, b"", b"Could not find disk")
            return tools(cmd, **kwargs)

        return run

    calls = _tools(monkeypatch)
    monkeypatch.setattr(machine.subprocess, "run", store_fails(calls, machine.subprocess.run))
    assert machine.disk_facts(tmp_path) == {"bus": "USB", "link_mbps": 480, "solid_state": True}
    assert _names(calls) == ["diskutil", "diskutil", "ioreg"] and calls[2][0][1:3] == ["-r", "-c"]
    # the same failure with a tree that lacks that disk: the bus and the kind of disk, no link, no plain list
    calls = _tools(monkeypatch, devices="")
    monkeypatch.setattr(machine.subprocess, "run", store_fails(calls, machine.subprocess.run))
    assert machine.disk_facts(tmp_path) == {"bus": "USB", "link_mbps": None, "solid_state": True}
    assert _names(calls) == ["diskutil", "diskutil", "ioreg"]


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


@pytest.mark.skipif(not hasattr(os, "major"), reason="os.major and os.minor of a POSIX system")
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
    """The real tools, on the internal disk the test folder is on: the three keys, nothing raised; on macOS diskutil
    answers for the mount point of the folder, so the bus is known."""
    facts = machine.disk_facts(tmp_path)
    assert set(facts) == {"bus", "link_mbps", "solid_state"}
    assert facts["bus"] is None or isinstance(facts["bus"], str)
    assert facts["link_mbps"] is None or isinstance(facts["link_mbps"], int)
    assert facts["solid_state"] in (True, False, None)
    if platform.system() == "Darwin":
        assert isinstance(facts["bus"], str) and facts["bus"]


def test_the_mount_point_of_a_folder_is_a_mount_point_above_it(tmp_path):
    real = os.path.realpath(tmp_path)
    point = machine._mount_point(tmp_path)
    assert os.path.ismount(point)
    assert real == point or real.startswith(point.rstrip(os.sep) + os.sep)
    assert machine._mount_point(tmp_path / "missing" / "deeper") == point


@pytest.mark.skipif(os.name != "posix", reason="a symbolic link")
def test_the_mount_point_is_found_through_a_symlink(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    os.symlink(target, tmp_path / "link")
    assert machine._mount_point(tmp_path / "link") == machine._mount_point(target)
    assert machine._mount_point(tmp_path / "link" / "deeper") == machine._mount_point(target)


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
    # a folder: on APFS it reports st_blocks == 0, so is_online_only skips it; on Linux os.read raises an OSError
    assert machine.read_rate([tmp_path]) is None


def test_the_read_rate_is_none_when_the_cache_cannot_be_bypassed(monkeypatch, tmp_path):
    """A figure read from the cache would say nothing about the disk: None rather than a wrong figure."""
    big = _write(tmp_path / "big.bin", 9 * 2**20)
    monkeypatch.setattr(machine.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(machine, "fcntl", None)
    assert machine.read_rate([big]) is None
    monkeypatch.setattr(machine.platform, "system", lambda: "Linux")
    monkeypatch.delattr(machine.os, "posix_fadvise", raising=False)
    assert machine.read_rate([big]) is None


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
    assert json.dumps(str(Path.home()))[1:-1] not in text  # as JSON writes it (Windows doubles the backslashes)
    assert socket.gethostname() not in text
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
    seen = []

    def sysctl(cmd, **kwargs):
        seen.append(cmd)
        return _completed(cmd, 0, "Example CPU 12-Core\n")

    monkeypatch.setattr(machine.subprocess, "run", sysctl)
    assert machine._cpu_name("Darwin") == "Example CPU 12-Core"
    assert len(seen) == 1 and Path(seen[0][0]).name == "sysctl" and posixpath.isabs(seen[0][0])
    assert seen[0][1:] == ["-n", "machdep.cpu.brand_string"]
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("processor\t: 0\nvendor_id\t: Example\nmodel name\t: Example CPU @ 3.0GHz\n"
                       "processor\t: 1\nmodel name\t: Example CPU @ 3.0GHz\n")
    assert machine._cpu_linux(cpuinfo) == "Example CPU @ 3.0GHz"
    (tmp_path / "no-model").write_text("vendor_id\t: Example\n")
    assert machine._cpu_linux(tmp_path / "no-model") is None
    if sys.platform == "win32":
        assert isinstance(machine._cpu_windows(), str)
    # elsewhere, or when the reader fails: None (no platform.processor(), which starts uname by a bare name)
    assert machine._cpu_name("Plan9") is None
    monkeypatch.setattr(machine.subprocess, "run", lambda cmd, **k: _completed(cmd, 1, ""))
    assert machine._cpu_name("Darwin") is None
    monkeypatch.setattr(machine, "_cpu_linux", _raising(OSError("no cpuinfo")))
    assert machine._cpu_name("Linux") is None
    assert machine._cpu_name("Windows") is None or sys.platform == "win32"


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
    assert [cmd[1:] for cmd in seen] == [["-ix", "dropbox"]] * 4
    assert all(Path(cmd[0]).name == "pgrep" and posixpath.isabs(cmd[0]) for cmd in seen)
    monkeypatch.setattr(machine.shutil, "which", lambda name: "/usr/bin/" + name)
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
    """Every reader fails: every fact is None. As a Darwin, whatever the host: there the cpu, the battery and Dropbox
    are read through subprocess.run (Linux reads /proc and sysfs, Windows the registry and kernel32, which the
    stand-in below would not reach)."""
    def failing(*args, **kwargs):
        raise RuntimeError("every tool fails")

    monkeypatch.setattr(machine.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(machine.subprocess, "run", failing)
    monkeypatch.setattr(machine._native, "gpu_adapters", failing)
    monkeypatch.setattr(machine._env, "physical_memory", failing)
    monkeypatch.setattr(machine.os, "getloadavg", failing, raising=False)
    facts = machine.machine_facts()
    assert facts["adapters"] == [] and facts["memory_gb"] is None and facts["load"] is None
    assert facts["cpu"] is None and facts["on_battery"] is None and facts["dropbox_running"] is None
    assert facts["system"] == platform.system()
