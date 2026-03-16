import logging
import asyncio
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.components import camera, mqtt
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
import json

from .const import DOMAIN, CONF_ENTRY_TYPE, ENTRY_TYPE_AUGMENTATIONS
from .map_utils import extract_map_from_image

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Valetudo select entities."""
    if config_entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_AUGMENTATIONS:
        return

    manager = ValetudoSelectManager(hass, async_add_entities, config_entry.entry_id)
    await manager.async_setup()

    # Store manager reference if not already there
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    if config_entry.entry_id not in hass.data[DOMAIN]:
         # This shouldn't happen if sensor.py already ran, but just in case
         pass

class ValetudoSelectManager:
    def __init__(self, hass: HomeAssistant, async_add_entities: AddEntitiesCallback, config_entry_id: str):
        self.hass = hass
        self.async_add_entities = async_add_entities
        self.config_entry_id = config_entry_id
        self._selects: dict[str, list[SelectEntity]] = {}
        self._listeners = []

    async def async_setup(self):
        self._scan_existing_devices()
        self._listeners.append(self.hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            self._handle_device_registry_update
        ))

    @callback
    def _handle_device_registry_update(self, event: Event):
        action = event.data.get("action")
        device_id = event.data.get("device_id")
        if action in ("create", "update"):
            dev_reg = dr.async_get(self.hass)
            device = dev_reg.async_get(device_id)
            if device and device.manufacturer == "Valetudo":
                self._try_add_selects(device_id)

    def _scan_existing_devices(self):
        dev_reg = dr.async_get(self.hass)
        for device in dev_reg.devices.values():
            if device.manufacturer == "Valetudo":
                self._try_add_selects(device.id)

    def _try_add_selects(self, device_id: str):
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get(device_id)
        if not device or device.manufacturer != "Valetudo":
            return

        ent_reg = er.async_get(self.hass)
        device_entities = er.async_entries_for_device(ent_reg, device_id)

        map_entity = next(
            (e for e in device_entities
             if e.domain == "camera" and e.entity_id.endswith("_map_data")),
            None
        )
        if not map_entity:
            return

        if device_id not in self._selects:
            self._selects[device_id] = []

        if not any(isinstance(s, ValetudoRoomSelect) for s in self._selects[device_id]):
            _LOGGER.debug(f"Creating ValetudoRoomSelect for device {device.name}")
            select = ValetudoRoomSelect(self.hass, device, map_entity.entity_id)
            self._selects[device_id].append(select)
            self.async_add_entities([select])

class ValetudoRoomSelect(SelectEntity):
    _attr_has_entity_name = True
    _attr_name = "Room Selection"
    _attr_icon = "mdi:floor-plan"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, device: dr.DeviceEntry, map_entity_id: str):
        self.hass = hass
        self._map_entity_id = map_entity_id
        self._device_info = device
        self._attr_unique_id = f"{device.id}_room_select"
        self._attr_device_info = {
            "connections": device.connections,
            "identifiers": device.identifiers,
        }
        self._attr_current_option = None
        self._attr_options = []
        self._rooms: dict[str, str] = {} # Name -> ID
        self._attr_extra_state_attributes = {}
        self._attr_available = False
        
        # Get identifier for MQTT
        self._mqtt_identifier = None
        for identifier in device.identifiers:
            if identifier[0] == "mqtt":
                self._mqtt_identifier = identifier[1]
                break

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._map_entity_id], self._handle_map_update
            )
        )
        await self._update_from_map()

    @callback
    def _handle_map_update(self, event):
        self.hass.async_create_task(self._update_from_map())

    async def _update_from_map(self):
        try:
            image_obj = await camera.async_get_image(self.hass, self._map_entity_id)
            map_data = await self.hass.async_add_executor_job(
                extract_map_from_image,
                image_obj.content
            )
            if not map_data:
                return

            rooms = {}
            for layer in map_data.get("layers", []):
                if layer.get("type") == "segment":
                    meta = layer.get("metaData", {})
                    s_id = meta.get("segmentId")
                    s_name = meta.get("name") or f"Room {s_id}"
                    if s_id:
                        rooms[s_name] = str(s_id)

            if rooms != self._rooms:
                self._rooms = rooms
                self._attr_options = sorted(list(rooms.keys()))
                self._attr_available = len(self._attr_options) > 0
                if self._attr_current_option not in self._attr_options:
                    self._attr_current_option = self._attr_options[0] if self._attr_options else None
                
                # Expose IDs in attributes for automations
                self._attr_extra_state_attributes = {
                    "room_ids": self._rooms,
                    "selected_room_id": self._rooms.get(self._attr_current_option) if self._attr_current_option else None
                }
                self.async_write_ha_state()
        except Exception as e:
            _LOGGER.error(f"Error updating rooms from map: {e}")

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        self._attr_current_option = option
        self._attr_extra_state_attributes["selected_room_id"] = self._rooms.get(option)
        self.async_write_ha_state()
        
        # Trigger cleaning if possible
        if self._mqtt_identifier and option in self._rooms:
            room_id = self._rooms[option]
            topic = f"valetudo/{self._mqtt_identifier}/MapSegmentationCapability/clean/set"
            payload = json.dumps({"segment_ids": [room_id]})
            await mqtt.async_publish(self.hass, topic, payload)
            _LOGGER.info(f"Triggered cleaning for room {option} ({room_id})")
