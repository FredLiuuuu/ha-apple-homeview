"""Apple HomeView (AirPlay) integration.

Keeps one pyatv connection (AirPlay + MRP tunnel) to a HomeView and exposes it
as a media_player that can also add/remove AirPlay speakers in the group
HomeView is sending to.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from pyatv.interface import DeviceListener

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_interval

from . import pyatv_compat
from .const import (
    CONF_CREDENTIALS,
    CONF_IDENTIFIER,
    CONFIRM_DELAYS,
    OUTPUT_REFRESH_INTERVAL,
    OUTPUT_SETTLE,
    PENDING_HOLD,
    RECONNECT_DELAYS,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.MEDIA_PLAYER]


class HomeViewConnection(DeviceListener):
    """Owns the pyatv connection and reconnects when it drops."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.atv = None
        self.device_info = None
        # Group membership read on a fresh, short-lived connection. HomeView
        # does not reliably push DEVICE_INFO updates on the long-lived one when
        # a third-party AirPlay speaker joins or leaves (seen 2026-11-27), but
        # the list sent at connect time is always right.
        self.outputs: list[tuple[str, str]] | None = None
        # A fresh connection's list also lags a change by tens of seconds, so
        # changes we make are shown at once and held for PENDING_HOLD seconds
        # unless a refresh confirms them sooner: {output_id: (added, name, expiry)}
        self._pending: dict[str, tuple[bool, str, float]] = {}
        self._conf = None
        self._refresh_lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._reconnect_task: asyncio.Task | None = None
        self._closing = False

    @property
    def identifier(self) -> str:
        return self.entry.data[CONF_IDENTIFIER]

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    async def connect(self) -> bool:
        loop = asyncio.get_running_loop()
        conf = await pyatv_compat.find(loop, self.identifier, self.entry.data.get(CONF_HOST))
        if conf is None:
            _LOGGER.debug("HomeView %s not found", self.identifier)
            return False
        try:
            atv = await pyatv_compat.connect(loop, conf, self.entry.data[CONF_CREDENTIALS])
        except Exception as err:  # pyatv raises many types
            _LOGGER.warning("Could not connect to HomeView: %s", err)
            return False
        atv.listener = self
        self.atv = atv
        self._conf = conf
        self.device_info = atv.device_info
        if str(conf.address) != self.entry.data.get(CONF_HOST):
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_HOST: str(conf.address)}
            )
        _LOGGER.info("Connected to HomeView at %s", conf.address)
        self._notify()
        return True

    async def refresh_outputs(self, delay: float = 0) -> None:
        """Read HomeView's current outputs on a fresh connection."""
        if delay:
            await asyncio.sleep(delay)
        if self._closing or self._conf is None:
            return
        async with self._refresh_lock:
            loop = asyncio.get_running_loop()
            creds = self.entry.data[CONF_CREDENTIALS]
            try:
                atv = await pyatv_compat.connect(loop, self._conf, creds)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Output refresh connect failed (%s); rescanning", err)
                conf = await pyatv_compat.find(loop, self.identifier, self.entry.data.get(CONF_HOST))
                if conf is None:
                    return
                try:
                    atv = await pyatv_compat.connect(loop, conf, creds)
                except Exception as err2:  # noqa: BLE001
                    _LOGGER.debug("Output refresh connect failed: %s", err2)
                    return
            try:
                await asyncio.sleep(OUTPUT_SETTLE)
                seen = [(d.name, d.identifier) for d in atv.audio.output_devices]
                _LOGGER.debug("HomeView outputs (fresh): %s", seen)
                self.outputs = self._apply_pending(seen)
            finally:
                tasks = atv.close()
                if tasks:
                    await asyncio.wait(tasks, timeout=5)
        self._notify()

    def _apply_pending(self, seen: list[tuple[str, str]]) -> list[tuple[str, str]]:
        now = time.monotonic()
        result = list(seen)
        ids = {i for _, i in seen}
        for oid, (added, name, expiry) in list(self._pending.items()):
            if (oid in ids) == added or now > expiry:
                del self._pending[oid]  # confirmed, or give up and trust HomeView
            elif added:
                result.append((name, oid))
            else:
                result = [(n, i) for n, i in result if i != oid]
        return result

    @callback
    def expect_change(self, added: dict[str, str], removed: list[str]) -> None:
        """Show a change we just sent right away; confirm it in the background."""
        expiry = time.monotonic() + PENDING_HOLD
        for oid, name in added.items():
            self._pending[oid] = (True, name, expiry)
        for oid in removed:
            self._pending[oid] = (False, oid, expiry)
        base = self.outputs
        if base is None and self.atv is not None:
            base = [(d.name, d.identifier) for d in self.atv.audio.output_devices]
        self.outputs = self._apply_pending(base or [])
        self._notify()
        for delay in CONFIRM_DELAYS:
            self.schedule_refresh(delay)

    @callback
    def schedule_refresh(self, delay: float = 0) -> None:
        self.entry.async_create_background_task(
            self.hass, self.refresh_outputs(delay), "homeview_refresh_outputs"
        )

    # DeviceListener
    def connection_lost(self, exception: Exception) -> None:
        _LOGGER.warning("Connection to HomeView lost: %s", exception)
        self._dropped()

    def connection_closed(self) -> None:
        self._dropped()

    def _dropped(self) -> None:
        self.atv = None
        self.hass.loop.call_soon_threadsafe(self._notify)
        if not self._closing:
            self.hass.loop.call_soon_threadsafe(self.start_reconnect)

    @callback
    def start_reconnect(self) -> None:
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = self.entry.async_create_background_task(
                self.hass, self._reconnect_loop(), "homeview_reconnect"
            )

    async def _reconnect_loop(self) -> None:
        attempt = 0
        while not self._closing and self.atv is None:
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            await asyncio.sleep(delay)
            attempt += 1
            if await self.connect():
                return

    async def close(self) -> None:
        self._closing = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self.atv is not None:
            atv, self.atv = self.atv, None
            tasks = atv.close()
            if tasks:
                await asyncio.wait(tasks, timeout=5)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    conn = HomeViewConnection(hass, entry)
    if not await conn.connect():
        raise ConfigEntryNotReady("HomeView not reachable")
    entry.runtime_data = conn
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    conn.schedule_refresh()

    @callback
    def _periodic(_now) -> None:
        if conn.atv is not None:
            conn.schedule_refresh()

    entry.async_on_unload(async_track_time_interval(hass, _periodic, OUTPUT_REFRESH_INTERVAL))
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.close()
    return ok
