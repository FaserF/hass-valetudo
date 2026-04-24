import logging
from typing import Any
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_state_change_event,
)
from homeassistant.components import mqtt

from .const import CONF_ENTRY_TYPE, ENTRY_TYPE_AUGMENTATIONS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Valetudo number entities."""
    if config_entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_AUGMENTATIONS:
        return

    manager = ValetudoNumberManager(hass, async_add_entities, config_entry.entry_id)
    await manager.async_setup()

    config_entry.async_on_unload(manager.async_unload)


class ValetudoNumberManager:
    def __init__(
        self,
        hass: HomeAssistant,
        async_add_entities: AddEntitiesCallback,
        config_entry_id: str,
    ):
        self.hass = hass
        self.async_add_entities = async_add_entities
        self.config_entry_id = config_entry_id
        self._numbers: dict[str, list[NumberEntity]] = {}
        self._listeners: list[Any] = []

    async def async_setup(self):
        self._scan_existing_devices()
        self._listeners.append(
            self.hass.bus.async_listen(
                dr.EVENT_DEVICE_REGISTRY_UPDATED, self._handle_device_registry_update
            )
        )

        self._listeners.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._handle_entity_registry_update
            )
        )

    @callback
    def async_unload(self):
        """Unregister listeners."""
        for unsub in self._listeners:
            unsub()
        self._listeners.clear()
        self._numbers.clear()

    @callback
    def _handle_device_registry_update(self, event: Event):
        action = event.data.get("action")
        device_id = event.data.get("device_id")
        if action in ("create", "update") and isinstance(device_id, str):
            dev_reg = dr.async_get(self.hass)
            device = dev_reg.async_get(device_id)
            if device and device.manufacturer == "Valetudo":
                self._try_add_numbers(device_id)

    @callback
    def _handle_entity_registry_update(self, event: Event):
        """Handle entity creation to catch when the base vacuum is added."""
        action = event.data.get("action")
        entity_id = event.data.get("entity_id")
        ent_reg = er.async_get(self.hass)

        if action == "create" and isinstance(entity_id, str):
            entry = ent_reg.async_get(entity_id)
            if entry and entry.device_id and entry.domain == "vacuum":
                self._try_add_numbers(entry.device_id)

    def _scan_existing_devices(self):
        dev_reg = dr.async_get(self.hass)
        for device in dev_reg.devices.values():
            if device.manufacturer == "Valetudo":
                self._try_add_numbers(device.id)

    def _try_add_numbers(self, device_id: str):
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get(device_id)
        if not device or device.manufacturer != "Valetudo":
            return

        ent_reg = er.async_get(self.hass)
        device_entities = er.async_entries_for_device(ent_reg, device_id)

        vacuum_entity = next((e for e in device_entities if e.domain == "vacuum"), None)
        if not vacuum_entity:
            return

        if device_id not in self._numbers:
            self._numbers[device_id] = []

        if not any(
            isinstance(n, ValetudoVolumeNumber) for n in self._numbers[device_id]
        ):
            _LOGGER.debug(f"Creating ValetudoVolumeNumber for device {device.name}")
            num = ValetudoVolumeNumber(self.hass, device, vacuum_entity.entity_id)
            self._numbers[device_id].append(num)
            self.async_add_entities([num])


class ValetudoVolumeNumber(NumberEntity):
    _attr_has_entity_name = True
    _attr_name = "Voice Volume"
    _attr_icon = "mdi:volume-source"
    _attr_should_poll = False
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_entity_registry_enabled_default = False  # As per user request
    _attr_entity_category = er.EntityCategory.CONFIG

    def __init__(
        self, hass: HomeAssistant, device: dr.DeviceEntry, vacuum_entity_id: str
    ):
        self.hass = hass
        self._vacuum_entity_id = vacuum_entity_id
        self._attr_unique_id = f"{device.id}_voice_volume"
        self._attr_device_info = {
            "connections": device.connections,
            "identifiers": device.identifiers,
        }
        self._attr_native_value: float | None = None
        self._mqtt_identifier = None
        for identifier in device.identifiers:
            if identifier[0] == "mqtt":
                self._mqtt_identifier = identifier[1]
                break

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._vacuum_entity_id], self._handle_vacuum_update
            )
        )
        state_obj = self.hass.states.get(self._vacuum_entity_id)
        if state_obj:
            self._update_from_state(state_obj)

    @callback
    def _handle_vacuum_update(self, event):
        new_state = event.data.get("new_state")
        if new_state:
            self._update_from_state(new_state)

    def _update_from_state(self, state):
        val = state.attributes.get("volume") or state.attributes.get("speaker_volume")
        if val is not None and val != self._attr_native_value:
            self._attr_native_value = float(val)
            self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        if not self._mqtt_identifier:
            return

        topic = (
            f"valetudo/{self._mqtt_identifier}/SpeakerVolumeControlCapability/value/set"
        )
        await mqtt.async_publish(self.hass, topic, str(int(value)))
        self._attr_native_value = value
        self.async_write_ha_state()
