import logging
from typing import Any
from datetime import timedelta
import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from homeassistant.components.update import (
    UpdateEntity,
    UpdateEntityFeature,
    UpdateDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.helpers.event import async_track_state_change_event, EventStateChangedData
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.components.mqtt import async_publish

from .const import (
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

    config_entry.async_on_unload(manager.async_unload)


class ValetudoUpdateManager:
    """Manages creation and removal of update entities for Valetudo devices."""

    def __init__(self, hass: HomeAssistant, async_add_entities: AddEntitiesCallback, config_entry_id: str):
        self.hass = hass
        self.async_add_entities = async_add_entities
        self.config_entry_id = config_entry_id
        self._entities: dict[str, list[UpdateEntity]] = {}
        self._listeners: list[Any] = []

    async def async_setup(self):
        self._scan_existing_devices()

        self._listeners.append(self.hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            self._handle_device_registry_update
        ))

        self._listeners.append(self.hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            self._handle_entity_registry_update
        ))

    @callback
    def async_unload(self):
        """Unregister listeners."""
        for listener in self._listeners:
            if isinstance(listener, tuple):
                listener[0]()
            else:
                listener()
        self._listeners.clear()
        self._entities.clear()

    def _scan_existing_devices(self):
        dev_reg = dr.async_get(self.hass)
        for device in dev_reg.devices.values():
            if device.manufacturer == "Valetudo":
                self._try_add_entities(device.id)

    @callback
    def _handle_device_registry_update(self, event: Event):
        action = event.data.get("action")
        device_id = event.data.get("device_id")

        if action in ("create", "update") and isinstance(device_id, str):
            dev_reg = dr.async_get(self.hass)
            device = dev_reg.async_get(device_id)
            if device and device.manufacturer == "Valetudo":
                self._try_add_entities(device_id)

    @callback
    def _handle_entity_registry_update(self, event: Event):
        """Handle entity creation to catch when the base vacuum is added."""
        action = event.data.get("action")
        entity_id = event.data.get("entity_id")
        ent_reg = er.async_get(self.hass)

        if action == "create" and isinstance(entity_id, str):
            entry = ent_reg.async_get(entity_id)
            if entry and entry.device_id and entry.domain == "vacuum":
                self._try_add_entities(entry.device_id)

    def _try_add_entities(self, device_id: str):
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get(device_id)

        if not device or device.manufacturer != "Valetudo":
            return

        # Ensure the base vacuum entity exists before adding our augmentation
        ent_reg = er.async_get(self.hass)
        device_entities = er.async_entries_for_device(ent_reg, device_id)
        vacuum_entity = next((e for e in device_entities if e.domain == "vacuum"), None)
        if not vacuum_entity:
            return

        if device_id not in self._entities:
            self._entities[device_id] = []

        if any(isinstance(e, ValetudoUpdateEntity) for e in self._entities[device_id]):
            return

        _LOGGER.debug(f"Creating ValetudoUpdateEntity for device {device.name}")
        entity = ValetudoUpdateEntity(self.hass, device)
        self._entities[device_id].append(entity)
        self.async_add_entities([entity])

        # Try enrichment immediately - use async_add_job for extra safety in registry callback
        self.hass.async_create_task(async_enrich_registry(self.hass, device_id, vacuum_entity.entity_id))
        
        # Also listen for first state change to retry enrichment when IP/MAC might appear
        # Store as a tuple (unsub_function, entity_id) to easily check if already listening
        if not any(isinstance(listener, tuple) and listener[1] == vacuum_entity.entity_id for listener in self._listeners): # Check if we are already listening for this entity
             async def _async_handle_enrich(event: Event[EventStateChangedData]) -> None:
                 await async_enrich_registry(self.hass, device_id, vacuum_entity.entity_id)

             unsub = async_track_state_change_event(
                 self.hass, 
                 [vacuum_entity.entity_id], 
                 _async_handle_enrich
             )
             self._listeners.append((unsub, vacuum_entity.entity_id))


class ValetudoUpdateEntity(UpdateEntity, RestoreEntity):
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
        self._attr_latest_version: str | None = None
        self._attr_release_notes = None
        self._attr_release_url = VALETUDO_RELEASES_URL

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()

        # Restore last state
        last_state = await self.async_get_last_state()
        if last_state is not None:
            if not self._attr_installed_version:
                self._attr_installed_version = last_state.attributes.get("installed_version")
            self._attr_latest_version = last_state.attributes.get("latest_version")
            self._attr_release_notes = last_state.attributes.get("release_notes")
            _LOGGER.debug(f"Restored state for {self.unique_id}: {self._attr_installed_version} -> {self._attr_latest_version}")

        # Trigger an immediate update to fetch latest version and refresh installed
        _LOGGER.debug(f"Entity {self.unique_id} added to Hass, triggering initial update")
        self.hass.async_create_task(self.async_update())

    async def async_update(self) -> None:
        """Fetch latest version from GitHub."""
        _LOGGER.debug(f"Updating Valetudo version for {self.unique_id}")
        try:
            session = async_get_clientsession(self.hass)
            headers = {"User-Agent": "HomeAssistant-Valetudo-Integration"}
            async with session.get(
                VALETUDO_LATEST_RELEASE_API,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    new_version = data.get("tag_name")
                    if new_version:
                        self._attr_latest_version = new_version
                        if new_version.startswith("v"):
                             # Consistent handling of 'v' prefix if needed
                             pass
                        self._attr_release_notes = data.get("body")
                        _LOGGER.debug(f"Successfully fetched Valetudo version: {self._attr_latest_version}")
                    else:
                        _LOGGER.warning("GitHub API returned 200 but no tag_name found")
                else:
                    _LOGGER.warning(f"Failed to fetch Valetudo version from GitHub: {response.status}")
        except Exception as err:
            _LOGGER.error(f"Unexpected error fetching Valetudo version: {err}", exc_info=True)

        # Refresh installed version from device registry in case it changed
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get(self._device.id)
        if device:
            if device.sw_version:
                if self._attr_installed_version != device.sw_version:
                    _LOGGER.info(f"Refreshed installed version for {self.unique_id}: {device.sw_version}")
                    self._attr_installed_version = device.sw_version
            else:
                _LOGGER.debug(f"Device {self._device.id} has no sw_version in registry")
        else:
            _LOGGER.warning(f"Device {self._device.id} not found in registry during version refresh")

        # Log final state for debugging
        _LOGGER.debug(f"Final state for {self.unique_id}: installed={self._attr_installed_version}, latest={self._attr_latest_version}")
        self.async_write_ha_state()
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

        await async_publish(self.hass, topic, "download")
