"""pyatv glue for Apple HomeView, kept inside this integration.

HomeView (model HomeAccessory17,1) needs two pyatv changes that were not in a
pyatv release when this was written (pyatv 0.18.0, Home Assistant 2026.11).
Both are backports of open upstream pull requests; the credit for finding and
fixing these belongs to their authors:

* postlund/pyatv#2894 "Fix AirPlay PIN pairing on tvOS 27" by @JaviSoto
  https://github.com/postlund/pyatv/pull/2894
  Send X-Apple-Client-Name on the AirPlay /pair-pin-start and /pair-setup
  requests. Without it HomeView never shows a PIN. Applied below only while
  *this integration* pairs (`_client_name_header`), then removed, so other
  pyatv users in HA (the core apple_tv integration) are unaffected outside
  that window. Sending the header is harmless for other devices (the upstream
  PR sends it for every device). If a future pyatv already sends the header,
  or no longer has the private table this patches, the patch is skipped.
* postlund/pyatv#2952 "Support Apple HomeView remote control and device
  model" by @zackwag
  https://github.com/postlund/pyatv/pull/2952
  Allow the MRP remote-control tunnel over AirPlay on HomeView. Done per
  connection with the documented setting MrpTunnel.Force on this
  integration's own storage object, so nothing global is patched.

TODO: remove these backports once pyatv ships both PRs in a release and Home
Assistant bundles that version: delete `_client_name_header` (pass `name=` to
pyatv.pair only) and drop the MrpTunnel.Force line. The core apple_tv
integration should then support HomeView by itself.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pyatv
from pyatv.const import Protocol
from pyatv.settings import MrpTunnel
from pyatv.storage.memory_storage import MemoryStorage

from .const import CLIENT_NAME, HOMEVIEW_MODEL_PREFIX


@contextlib.contextmanager
def _client_name_header():
    from pyatv.protocols.airplay.auth import hap

    headers = getattr(hap, "_AIRPLAY_HEADERS", None)  # private pyatv table
    if not isinstance(headers, dict) or headers.get("X-Apple-Client-Name"):
        # pyatv changed, or already sends the header (#2894 merged): no patch.
        yield
        return
    had = "X-Apple-Client-Name" in headers
    old = headers.get("X-Apple-Client-Name")
    headers["X-Apple-Client-Name"] = CLIENT_NAME
    try:
        yield
    finally:
        if had:
            headers["X-Apple-Client-Name"] = old
        else:
            headers.pop("X-Apple-Client-Name", None)


async def scan_homeviews(loop: asyncio.AbstractEventLoop, host: str | None = None,
                         timeout: int = 5) -> list[Any]:
    """Return pyatv configs for HomeView devices (by model)."""
    kwargs: dict[str, Any] = {"timeout": timeout}
    if host:
        kwargs["hosts"] = [host]
    confs = await pyatv.scan(loop, **kwargs)
    result = []
    for conf in confs:
        service = conf.get_service(Protocol.AirPlay)
        model = service.properties.get("model", "") if service else ""
        if model.startswith(HOMEVIEW_MODEL_PREFIX):
            result.append(conf)
    return result


async def find(loop, identifier: str, host: str | None):
    """Find one HomeView: unicast to the last known host, then multicast."""
    if host:
        confs = await pyatv.scan(loop, hosts=[host], timeout=4)
        for conf in confs:
            if identifier in conf.all_identifiers:
                return conf
    confs = await pyatv.scan(loop, identifier=identifier, timeout=6)
    return confs[0] if confs else None


async def connect(loop, conf, credentials: str):
    """Connect with stored AirPlay credentials and the MRP tunnel forced on."""
    conf.set_credentials(Protocol.AirPlay, credentials)
    storage = MemoryStorage()
    settings = await storage.get_settings(conf)
    settings.protocols.airplay.mrp_tunnel = MrpTunnel.Force  # pyatv PR #2952
    return await pyatv.connect(conf, loop, storage=storage)


class Pairing:
    """AirPlay PIN pairing with HomeView (two steps: begin, then finish)."""

    def __init__(self, loop, conf) -> None:
        self._loop = loop
        self._conf = conf
        self._handler = None

    async def begin(self) -> None:
        self._handler = await pyatv.pair(self._conf, Protocol.AirPlay, self._loop,
                                         name=CLIENT_NAME)
        with _client_name_header():
            await self._handler.begin()

    async def finish(self, pin: int) -> str | None:
        """Return credentials, or None if the PIN was rejected."""
        try:
            self._handler.pin(pin)
            with _client_name_header():
                await self._handler.finish()
            if not self._handler.has_paired:
                return None
            return self._handler.service.credentials
        finally:
            await self.close()

    async def close(self) -> None:
        if self._handler is not None:
            await self._handler.close()
            self._handler = None
