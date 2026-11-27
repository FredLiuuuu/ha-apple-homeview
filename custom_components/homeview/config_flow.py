"""Config flow for Apple HomeView."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import pyatv_compat
from .const import (
    CONF_CREDENTIALS,
    CONF_IDENTIFIER,
    CONF_SPEAKERS,
    DEFAULT_CREDENTIALS_FILE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CONF_METHOD = "method"
CONF_FILE = "credentials_file"
CONF_PIN = "pin"
METHOD_PAIR = "pair"
METHOD_FILE = "file"


def _main_id(conf) -> str:
    return conf.identifier or conf.all_identifiers[0]


async def _validate(loop, conf, credentials: str) -> str | None:
    """Connect once; return an error key or None."""
    try:
        atv = await pyatv_compat.connect(loop, conf, credentials)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Validation connect failed: %s", err)
        return "cannot_connect"
    try:
        await asyncio.sleep(2)
        from pyatv.const import FeatureName, FeatureState

        if atv.features.get_feature(FeatureName.OutputDevices).state != FeatureState.Available:
            return "no_remote_control"
    finally:
        atv.close()
    return None


class HomeViewConfigFlow(ConfigFlow, domain=DOMAIN):
    """Find a HomeView, then pair with a PIN or import existing credentials."""

    VERSION = 1

    def __init__(self) -> None:
        self._confs: dict[str, Any] = {}
        self._conf = None
        self._name = "HomeView"
        self._pairing: pyatv_compat.Pairing | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            loop = asyncio.get_running_loop()
            host = (user_input.get(CONF_HOST) or "").strip() or None
            confs = await pyatv_compat.scan_homeviews(loop, host)
            if not confs:
                errors["base"] = "not_found"
            else:
                self._confs = {_main_id(c): c for c in confs}
                if len(confs) == 1:
                    return await self._select(confs[0], user_input[CONF_METHOD])
                self._method = user_input[CONF_METHOD]
                return await self.async_step_pick()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Optional(CONF_HOST): str,
                vol.Required(CONF_METHOD, default=METHOD_PAIR): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[METHOD_PAIR, METHOD_FILE],
                                                  translation_key="method")),
            }),
            errors=errors,
        )

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._select(self._confs[user_input[CONF_IDENTIFIER]], self._method)
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema({vol.Required(CONF_IDENTIFIER): vol.In(
                {k: f"{c.name} ({c.address})" for k, c in self._confs.items()})}),
        )

    async def _select(self, conf, method: str) -> ConfigFlowResult:
        await self.async_set_unique_id(_main_id(conf))
        self._abort_if_unique_id_configured()
        self._conf = conf
        self._name = conf.name or "HomeView"
        self.context["title_placeholders"] = {"name": self._name}
        if method == METHOD_FILE:
            return await self.async_step_file()
        return await self.async_step_pair()

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Send the PIN request; the next form takes the PIN shown on HomeView."""
        errors: dict[str, str] = {}
        if user_input is None:
            loop = asyncio.get_running_loop()
            self._pairing = pyatv_compat.Pairing(loop, self._conf)
            try:
                await self._pairing.begin()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Could not start pairing: %s", err)
                return self.async_abort(reason="pairing_failed")
            return self.async_show_form(
                step_id="pair",
                data_schema=vol.Schema({vol.Required(CONF_PIN): str}),
                description_placeholders={"name": self._name},
            )
        pin = user_input[CONF_PIN].strip()
        if not pin.isdigit():
            errors["base"] = "invalid_pin"
            return self.async_show_form(step_id="pair", data_schema=vol.Schema(
                {vol.Required(CONF_PIN): str}), errors=errors,
                description_placeholders={"name": self._name})
        try:
            creds = await self._pairing.finish(int(pin))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Pairing failed: %s", err)
            creds = None
        if not creds:
            return self.async_abort(reason="invalid_pin")
        return await self._create(creds)

    async def async_step_file(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Import AirPlay credentials saved by an earlier pairing (file path)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            path = user_input[CONF_FILE]

            def _read():
                if not os.path.isfile(path):
                    return ""
                with open(path, encoding="utf-8") as f:
                    return f.read().strip()
            try:
                creds = await self.hass.async_add_executor_job(_read)
            except OSError:
                creds = ""
            if not creds:
                errors["base"] = "no_credentials"
            else:
                err = await _validate(asyncio.get_running_loop(), self._conf, creds)
                if err:
                    errors["base"] = err
                else:
                    return await self._create(creds)
        return self.async_show_form(
            step_id="file",
            data_schema=vol.Schema({vol.Required(CONF_FILE, default=DEFAULT_CREDENTIALS_FILE): str}),
            errors=errors,
        )

    async def _create(self, creds: str) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._name,
            data={
                CONF_IDENTIFIER: _main_id(self._conf),
                CONF_HOST: str(self._conf.address),
                CONF_NAME: self._name,
                CONF_CREDENTIALS: creds,
            },
        )

    def async_remove(self) -> None:
        if self._pairing is not None:
            self.hass.async_create_task(self._pairing.close())

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HomeViewOptionsFlow()


class HomeViewOptionsFlow(OptionsFlow):
    """Map each AirPlay output HomeView can send to onto a media_player entity."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        conn = getattr(self.config_entry, "runtime_data", None)
        outputs = []
        if conn is not None and conn.atv is not None:
            try:
                outputs = list(conn.atv.audio.output_devices)
            except Exception:  # noqa: BLE001
                outputs = []
        current: dict[str, str] = dict(self.config_entry.options.get(CONF_SPEAKERS, {}))
        known = {v: k for k, v in current.items()}
        # Offer the outputs HomeView reports now plus ones already mapped.
        ids: dict[str, str] = {d.identifier: d.name for d in outputs}
        for ent, oid in current.items():
            ids.setdefault(oid, oid)
        ids.pop(self.config_entry.unique_id or "", None)

        def key(oid: str) -> str:
            return f"{ids[oid]} [{oid}]"

        if user_input is not None:
            mapping = {}
            for oid in ids:
                ent = user_input.get(key(oid))
                if ent:
                    mapping[ent] = oid
            extra = (user_input.get("extra") or "").strip()
            for line in extra.splitlines():
                if "=" in line:
                    ent, oid = (p.strip() for p in line.split("=", 1))
                    if ent.startswith("media_player.") and oid:
                        mapping[ent] = oid
            return self.async_create_entry(data={CONF_SPEAKERS: mapping})

        schema: dict[Any, Any] = {}
        for oid in ids:
            default = known.get(oid)
            opt = vol.Optional(key(oid), description={"suggested_value": default}) if default \
                else vol.Optional(key(oid))
            schema[opt] = selector.EntitySelector(selector.EntitySelectorConfig(domain="media_player"))
        schema[vol.Optional("extra")] = selector.TextSelector(
            selector.TextSelectorConfig(multiline=True))
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
