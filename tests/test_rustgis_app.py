"""RustGIS itself, the app the user has open. The link talks to it over HTTP only while the user ticks *Let your Agents
Control RustGIS* in its AI Assistant settings - consent that nothing here gives for them - so this file is opt-in:
PPG_RUSTGIS_LIVE=1 (the tests then read the user's real RustGIS settings folder, which the other tests never do), run
with RustGIS open and the box ticked:

    PPG_RUSTGIS_LIVE=1 pytest tests/test_rustgis_app.py -s

It prints the version and what the app answered. The second test also needs RustGIS minimized (or hidden: an app that
does not draw cannot run a request) and says so with PPG_RUSTGIS_MINIMIZED=1; it records how long the transport waits
and that the call is reported as busy, not as a failure."""
from __future__ import annotations

import os
import time

import pytest

from pyarchinit_photogrammetry.io import rustgis_link as link

pytestmark = pytest.mark.skipif(os.environ.get("PPG_RUSTGIS_LIVE") != "1",
                                reason="the open RustGIS answers only while its user ticks \"Let your Agents Control "
                                       "RustGIS\": set PPG_RUSTGIS_LIVE=1 to try it")


def _transport(timeout: float = 30.0) -> link.HttpTransport:
    where = link.endpoint()
    if where is None:
        pytest.fail("RustGIS has made no token yet: open it and tick \"Let your Agents Control RustGIS\" in its AI "
                    f"Assistant settings (it looks for {link.TOKEN_FILE} in {link.settings_dir()})")
    return link.HttpTransport(where, timeout=timeout)


def test_the_open_app_answers():
    transport = _transport()
    try:
        version = transport.initialize()  # RustGisTooOld below 0.4.0
    except link.RustGisOff as exc:
        pytest.fail(f"nothing answers: open RustGIS with \"Let your Agents Control RustGIS\" ticked ({exc})")
    print(f"\nRustGIS {version} answers at {link.endpoint().url}")
    layers = transport.call("list_layers", {})
    assert isinstance(layers["layers"], list)
    print(f"the map {layers.get('activeMap')!r} has {len(layers['layers'])} layer(s)")


@pytest.mark.skipif(os.environ.get("PPG_RUSTGIS_MINIMIZED") != "1",
                    reason="minimize RustGIS, then set PPG_RUSTGIS_MINIMIZED=1")
def test_a_minimized_app_is_busy_not_a_failure():
    transport = _transport(timeout=15)
    started = time.monotonic()
    with pytest.raises(link.RustGisBusy) as busy:
        transport.call("list_layers", {})  # a read: it may run when the window is back, which does no harm
    print(f"\nminimized: RustGisBusy after {time.monotonic() - started:.1f} s, outcome_unknown="
          f"{busy.value.outcome_unknown}: {busy.value}")
