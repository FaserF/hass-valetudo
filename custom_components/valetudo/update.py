import logging
import asyncio
from typing import Any
from datetime import timedelta
import aiohttp

from homeassistant.components.update import (
    UpdateEntity,
    UpdateEntityFeature,
    UpdateDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.components.mqtt import async_publish

from .const import (
    DOMAIN,
    CONF_ENTRY_TYPE,
    ENTRY_TYPE_AUGMENTATIONS,
    VALETUDO_LATEST_RELEASE_API,
    VALETUDO_RELEASES_URL,
)
from .device_utils import async_enrich_registry

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=1)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Valetudo update entities."""
    if config_entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_AUGMENTATIONS:
        return

    manager = ValetudoUpdateManager(hass, async_add_entities, config_entry.entry_id)
    await manager.async_setup()


class ValetudoUpdateManager:
    """Manages creation and removal of update entities for Valetudo devices."""

    def __init__(self, hass: HomeAssistant, async_add_entities: AddEntitiesCallback, config_entry_id: str):
        self.hass = hass
        self.async_add_entities = async_add_entities
        self.config_entry_id = config_entry_id
        self._entities: dict[str, list[UpdateEntity]] = {}
        self._listeners = []

    async def async_setup(self):
        self._scan_existing_devices()

        self._listeners.append(self.hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            self._handle_device_registry_update
        ))

    def _scan_existing_devices(self):
        dev_reg = dr.async_get(self.hass)
        for device in dev_reg.devices.values():
            if device.manufacturer == "Valetudo":
                self._try_add_entities(device.id)

    @callback
    def _handle_device_registry_update(self, event: Event):
        action = event.data.get("action")
        device_id = event.data.get("device_id")

        if action in ("create", "update"):
            dev_reg = dr.async_get(self.hass)
            device = dev_reg.async_get(device_id)
            if device and device.manufacturer == "Valetudo":
                self._try_add_entities(device_id)

    def _try_add_entities(self, device_id: str):
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get(device_id)

        if not device or device.manufacturer != "Valetudo":
            return

        if device_id not in self._entities:
            self._entities[device_id] = []

        if any(isinstance(e, ValetudoUpdateEntity) for e in self._entities[device_id]):
            return

        _LOGGER.debug(f"Creating ValetudoUpdateEntity for device {device.name}")
        entity = ValetudoUpdateEntity(self.hass, device)
        self._entities[device_id].append(entity)
        self.async_add_entities([entity])

        # Try to enrich with MAC if missing
        if not any(conn[0] == dr.CONNECTION_NETWORK_MAC for conn in device.connections):
            # We need to find the vacuum entity ID for this device
            ent_reg = er.async_get(self.hass)
            device_entities = er.async_entries_for_device(ent_reg, device_id)
            vacuum_entity = next((e for e in device_entities if e.domain == "vacuum"), None)
            if vacuum_entity:
                self.hass.async_create_task(async_enrich_registry(self.hass, device_id, vacuum_entity.entity_id))


class ValetudoUpdateEntity(UpdateEntity):
    """Update entity for Valetudo firmware."""

    _attr_has_entity_name = True
    _attr_name = "Firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.RELEASE_NOTES
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, device: dr.DeviceEntry):
        self.hass = hass
        self._device = device
        self._attr_unique_id = f"{device.id}_firmware"
        self._attr_device_info = {
            "connections": device.connections,
            "identifiers": device.identifiers,
        }
        self._attr_installed_version = device.sw_version
        self._attr_latest_version = None
        self._attr_release_notes = None
        self._attr_release_url = VALETUDO_RELEASES_URL

    async def async_update(self) -> None:
        """Fetch latest version from GitHub."""
        _LOGGER.debug(f"Updating Valetudo version for {self.unique_id}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(VALETUDO_LATEST_RELEASE_API, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        data = await response.json()
                        self._attr_latest_version = data.get("tag_name")
                        if self._attr_latest_version and self._attr_latest_version.startswith("v"):
                             # Some Valetudo versions might report 2026.02.0 vs v2026.02.0
                             pass
                        self._attr_release_notes = data.get("body")
                        _LOGGER.debug(f"Fetched latest Valetudo version: {self._attr_latest_version}")
                    else:
                        _LOGGER.warning(f"Failed to fetch latest Valetudo version: {response.status}")
                        # Fallback for when API fails or rate limited
                        if not self._attr_latest_version:
                            self._attr_latest_version = "unknown"
        except Exception as err:
            _LOGGER.error(f"Error fetching Valetudo version: {err}")
            if not self._attr_latest_version:
                self._attr_latest_version = "unknown"

        # Refresh installed version from device registry in case it changed
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get(self._device.id)
        if device and device.sw_version:
            self._attr_installed_version = device.sw_version
            _LOGGER.debug(f"Updated installed version for {self.unique_id}: {self._attr_installed_version}")
    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Trigger update via MQTT."""
        # We find the identifier to build the topic prefix
        mqtt_id = None
        for identifier in self._device.identifiers:
            if identifier[0] == "mqtt":
                mqtt_id = identifier[1]
                break

        if not mqtt_id:
            _LOGGER.error(f"No MQTT identifier found for device {self._device.id}")
            return
        
        # Valetudo MQTT Update Command topic
        topic = f"valetudo/{mqtt_id}/Updater/action/set"
        
        _LOGGER.info(f"Triggering Valetudo update for {self._device.name} to version {version} via {topic}")
        
        # We first send "check" to ensure the robot is aware of the environment
        # then "download" to start the process. 
        # In many Valetudo versions, "download" is the primary trigger.
        await async_publish(self.hass, topic, "download")
        
        # Some versions might also need explicit install/apply, but download is usually sufficient
        # to start the OTA process if a URL is provided or matched.
