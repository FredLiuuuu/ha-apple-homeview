"""Media player for Apple HomeView, including AirPlay group control."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import voluptuous as vol
from pyatv.const import DeviceState, FeatureName, FeatureState, MediaType
from pyatv.interface import AudioListener, PushListener

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType as HAMediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import homeassistant.util.dt as dt_util

from .const import (
    ATTR_OUTPUT_IDS,
    CONF_SPEAKERS,
    DOMAIN,
    SERVICE_REMOVE_MEMBERS,
    SERVICE_SET_OUTPUTS,
)

_LOGGER = logging.getLogger(__name__)

_STATE = {
    DeviceState.Idle: MediaPlayerState.IDLE,
    DeviceState.Loading: MediaPlayerState.BUFFERING,
    DeviceState.Paused: MediaPlayerState.PAUSED,
    DeviceState.Playing: MediaPlayerState.PLAYING,
    DeviceState.Seeking: MediaPlayerState.PLAYING,
    DeviceState.Stopped: MediaPlayerState.IDLE,
}

_FEATURES = [
    (FeatureName.Play, MediaPlayerEntityFeature.PLAY),
    (FeatureName.Pause, MediaPlayerEntityFeature.PAUSE),
    (FeatureName.Stop, MediaPlayerEntityFeature.STOP),
    (FeatureName.Next, MediaPlayerEntityFeature.NEXT_TRACK),
    (FeatureName.Previous, MediaPlayerEntityFeature.PREVIOUS_TRACK),
    (FeatureName.SetPosition, MediaPlayerEntityFeature.SEEK),
    (FeatureName.SetVolume, MediaPlayerEntityFeature.VOLUME_SET),
    (FeatureName.VolumeUp, MediaPlayerEntityFeature.VOLUME_STEP),
    (FeatureName.AddOutputDevices, MediaPlayerEntityFeature.GROUPING),
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([HomeViewMediaPlayer(entry)])
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_REMOVE_MEMBERS,
        {vol.Required("group_members"): cv.entity_ids},
        "async_remove_members",
    )
    platform.async_register_entity_service(
        SERVICE_SET_OUTPUTS,
        {vol.Required(ATTR_OUTPUT_IDS): vol.All(cv.ensure_list, [cv.string])},
        "async_set_outputs",
    )


class HomeViewMediaPlayer(MediaPlayerEntity, PushListener, AudioListener):
    """HomeView as a media player; group = HomeView's AirPlay outputs."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_media_content_type = HAMediaType.MUSIC

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._conn = entry.runtime_data
        self._playing = None
        self._attached = None
        self._attr_unique_id = self._conn.identifier
        info = self._conn.device_info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._conn.identifier)},
            manufacturer="Apple",
            model="HomeView",
            model_id=getattr(info, "raw_model", None),
            sw_version=getattr(info, "version", None),
            name=entry.title,
        )
        # options: {entity_id: output-device id}
        self._speakers: dict[str, str] = dict(entry.options.get(CONF_SPEAKERS, {}))

    # ------------------------------------------------------------ lifecycle
    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._conn.add_listener(self._connection_changed))
        self._attach()

    async def async_will_remove_from_hass(self) -> None:
        self._detach()

    @callback
    def _connection_changed(self) -> None:
        self._attach()
        self.async_write_ha_state()

    def _attach(self) -> None:
        atv = self._conn.atv
        if atv is None or atv is self._attached:
            if atv is None:
                self._attached = None
                self._playing = None
            return
        self._attached = atv
        atv.push_updater.listener = self
        atv.push_updater.start()
        atv.audio.listener = self

    def _detach(self) -> None:
        atv = self._attached
        self._attached = None
        if atv is not None:
            try:
                atv.push_updater.stop()
            except Exception:  # noqa: BLE001
                pass

    @property
    def _atv(self):
        atv = self._conn.atv
        if atv is None:
            raise HomeAssistantError("HomeView is not connected")
        return atv

    # ------------------------------------------------------------ pyatv callbacks
    def playstatus_update(self, updater, playstatus) -> None:
        self._playing = playstatus
        self._attr_media_position_updated_at = dt_util.utcnow()
        self.async_write_ha_state()

    def playstatus_error(self, updater, exception: Exception) -> None:
        _LOGGER.debug("Push update error: %s", exception)
        self._playing = None
        self.async_write_ha_state()

    def volume_update(self, old_level: float, new_level: float) -> None:
        self.async_write_ha_state()

    def volume_device_update(self, output_device, old_level: float, new_level: float) -> None:
        pass

    def outputdevices_update(self, old_devices, new_devices) -> None:
        # The pushed list can miss third-party speakers; re-read it fresh.
        self._conn.schedule_refresh(1)

    # ------------------------------------------------------------ state
    @property
    def available(self) -> bool:
        return self._conn.atv is not None

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        atv = self._conn.atv
        flags = MediaPlayerEntityFeature(0)
        if atv is None:
            return flags
        for name, flag in _FEATURES:
            try:
                if atv.features.get_feature(name).state == FeatureState.Available:
                    flags |= flag
            except Exception:  # noqa: BLE001
                pass
        return flags

    @property
    def state(self) -> MediaPlayerState | None:
        if self._conn.atv is None:
            return None
        if self._playing is None:
            return MediaPlayerState.IDLE
        return _STATE.get(self._playing.device_state, MediaPlayerState.IDLE)

    def _p(self, attr):
        if self._playing is None or self.state not in (
            MediaPlayerState.PLAYING, MediaPlayerState.PAUSED, MediaPlayerState.BUFFERING
        ):
            return None
        return getattr(self._playing, attr, None)

    @property
    def media_title(self):
        return self._p("title")

    @property
    def media_artist(self):
        return self._p("artist")

    @property
    def media_album_name(self):
        return self._p("album")

    @property
    def media_duration(self):
        return self._p("total_time")

    @property
    def media_position(self):
        return self._p("position")

    @property
    def app_name(self):
        atv = self._conn.atv
        try:
            return atv.metadata.app.name if atv and atv.metadata.app else None
        except Exception:  # noqa: BLE001
            return None

    @property
    def media_content_type(self):
        mt = self._p("media_type")
        if mt == MediaType.Video:
            return HAMediaType.VIDEO
        return HAMediaType.MUSIC

    @property
    def volume_level(self) -> float | None:
        atv = self._conn.atv
        try:
            return atv.audio.volume / 100.0 if atv else None
        except Exception:  # noqa: BLE001
            return None

    @property
    def media_image_hash(self) -> str | None:
        atv = self._conn.atv
        if atv is None or self._p("title") is None:
            return None
        try:
            return atv.metadata.artwork_id or self._playing.hash
        except Exception:  # noqa: BLE001
            return None

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        atv = self._conn.atv
        if atv is None:
            return None, None
        art = await atv.metadata.artwork(width=512)
        if art:
            return art.bytes, art.mimetype
        return None, None

    # ------------------------------------------------------------ group
    def _outputs(self) -> list[Any]:
        if self._conn.atv is None:
            return []
        if self._conn.outputs is not None:
            return [SimpleNamespace(name=n, identifier=i) for n, i in self._conn.outputs]
        try:
            return list(self._conn.atv.audio.output_devices)
        except Exception:  # noqa: BLE001
            return []

    @property
    def group_members(self) -> list[str] | None:
        rev = {v: k for k, v in self._speakers.items()}
        members = [self.entity_id]
        for dev in self._outputs():
            ent = rev.get(dev.identifier)
            if ent and ent not in members:
                members.append(ent)
        return members

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rev = {v: k for k, v in self._speakers.items()}
        return {
            "airplay_outputs": [
                {"name": d.name, "id": d.identifier, "entity_id": rev.get(d.identifier)}
                for d in self._outputs()
            ]
        }

    def _ids_for(self, entity_ids: list[str]) -> list[str]:
        ids, unknown = [], []
        for ent in entity_ids:
            if ent == self.entity_id:
                continue
            if ent in self._speakers:
                ids.append(self._speakers[ent])
            else:
                unknown.append(ent)
        if unknown:
            raise HomeAssistantError(
                f"No AirPlay speaker mapped for {', '.join(unknown)}; "
                "set it in the HomeView integration options"
            )
        return ids

    async def async_join_players(self, group_members: list[str]) -> None:
        ids = self._ids_for(group_members)
        current = {d.identifier for d in self._outputs()}
        new = [i for i in ids if i not in current]
        if new:
            await self._atv.audio.add_output_devices(*new)
            rev = {v: k for k, v in self._speakers.items()}
            self._conn.expect_change({i: rev.get(i, i) for i in new}, [])

    async def async_unjoin_player(self) -> None:
        """Unjoin on HomeView itself: keep only HomeView's own output."""
        raise HomeAssistantError(
            "Use homeview.remove_members on the HomeView entity to drop speakers"
        )

    async def async_remove_members(self, group_members: list[str]) -> None:
        ids = self._ids_for(group_members)
        current = {d.identifier for d in self._outputs()}
        gone = [i for i in ids if i in current]
        if gone:
            await self._atv.audio.remove_output_devices(*gone)
            self._conn.expect_change({}, gone)

    async def async_set_outputs(self, output_ids: list[str]) -> None:
        current = {d.identifier for d in self._outputs()}
        await self._atv.audio.set_output_devices(*output_ids)
        self._conn.expect_change({i: i for i in output_ids if i not in current},
                                 [i for i in current if i not in output_ids])

    # ------------------------------------------------------------ controls
    async def async_media_play(self) -> None:
        await self._atv.remote_control.play()

    async def async_media_pause(self) -> None:
        await self._atv.remote_control.pause()

    async def async_media_stop(self) -> None:
        await self._atv.remote_control.stop()

    async def async_media_next_track(self) -> None:
        await self._atv.remote_control.next()

    async def async_media_previous_track(self) -> None:
        await self._atv.remote_control.previous()

    async def async_media_seek(self, position: float) -> None:
        await self._atv.remote_control.set_position(position)

    async def async_set_volume_level(self, volume: float) -> None:
        await self._atv.audio.set_volume(volume * 100.0)

    async def async_volume_up(self) -> None:
        await self._atv.audio.volume_up()

    async def async_volume_down(self) -> None:
        await self._atv.audio.volume_down()
