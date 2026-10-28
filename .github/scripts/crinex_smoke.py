"""Hatanaka smoke test of the native core: ppg_core.crx2rnx decodes the Compact RINEX files of tests/data/crinex
(CRINEX 1.0 with its event records, 3.0 and 3.1) byte for byte as RNXCMP's CRX2RNX decoded them (the .expected files
beside them), and refuses a file cut short without writing a decoded one. Every archive of the permanent stations is
Compact RINEX: without the decoder a station, a baseline or PPP cannot run. The native and the windows-arm workflows
run it after the wheel of the core is installed, and the desktop workflow on the core of the Windows ARM64 app:
python .github/scripts/crinex_smoke.py
"""
import pathlib
import tempfile

import ppg_core

data, tmp = pathlib.Path(__file__).resolve().parents[2] / "tests" / "data" / "crinex", pathlib.Path(tempfile.mkdtemp())
assert getattr(ppg_core, "API", 0) >= 9, f"the native core {ppg_core.__version__} has no Hatanaka decoder (API 9)"
for name in ("07590920.05d", "events.05d", "kraw299k.26d", "kraw_rinex402.crx", "KRAW00POL_R_20262991000_01H_30S_MO.crx"):
    out = tmp / f"{name}.rnx"
    ppg_core.crx2rnx(str(data / name), str(out))
    assert out.read_bytes() == (data / f"{name}.expected").read_bytes(), name
    print(f"{name}: decoded as CRX2RNX decodes it, {out.stat().st_size} bytes")
whole = (data / "kraw_rinex402.crx").read_bytes()
cut, decoded = tmp / "cut.crx", tmp / "cut.rnx"
cut.write_bytes(whole[: len(whole) // 2 + 7])  # cut in the middle of a line
try:
    ppg_core.crx2rnx(str(cut), str(decoded))
except ValueError as exc:
    print("a file cut short is refused:", exc)
else:
    raise AssertionError("a file cut short was decoded")
assert not decoded.exists() and not decoded.with_name(decoded.name + ".part").exists(), "a decoded file was left"
