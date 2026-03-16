import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.components import mqtt

from .const import DOMAIN, CONF_ENTRY_TYPE, ENTRY_TYPE_AUGMENTATIONS

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Valetudo button entities."""
    if config_entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_AUGMENTATIONS:
        return

    manager = ValetudoButtonManager(hass, async_add_entities, config_entry.entry_id)
    await manager.async_setup()

class ValetudoButtonManager:
    def __init__(self, hass: HomeAssistant, async_add_entities: AddEntitiesCallback, config_entry_id: str):
        self.hass = hass
        self.async_add_entities = async_add_entities
        self.config_entry_id = config_entry_id
        self._buttons: dict[str, list[ButtonEntity]] = {}
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
                self._try_add_buttons(device_id)

    def _scan_existing_devices(self):
        dev_reg = dr.async_get(self.hass)
        for device in dev_reg.devices.values():
            if device.manufacturer == "Valetudo":
                self._try_add_buttons(device.id)

    def _try_add_buttons(self, device_id: str):
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get(device_id)
        if not device or device.manufacturer != "Valetudo":
            return

        ent_reg = er.async_get(self.hass)
        device_entities = er.async_entries_for_device(ent_reg, device_id)

        vacuum_entity = next(
            (e for e in device_entities if e.domain == "vacuum"),
            None
        )
        if not vacuum_entity:
            return

        if device_id not in self._buttons:
            self._buttons[device_id] = []

        # Add Locate button
        if not any(isinstance(b, ValetudoLocateButton) for b in self._buttons[device_id]):
            _LOGGER.debug(f"Creating ValetudoLocateButton for device {device.name}")
            btn = ValetudoLocateButton(self.hass, device)
            self._buttons[device_id].append(btn)
            self.async_add_entities([btn])

class ValetudoLocateButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Locate Robot"
    _attr_icon = "mdi:map-marker-question"
    _attr_entity_category = er.EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, device: dr.DeviceEntry):
        self.hass = hass
        self._attr_unique_id = f"{device.id}_locate"
        self._attr_device_info = {
            "connections": device.connections,
            "identifiers": device.identifiers,
        }
        
        # Get identifier for MQTT
        self._mqtt_identifier = None
        for identifier in device.identifiers:
            if identifier[0] == "mqtt":
                self._mqtt_identifier = identifier[1]
                break

    async def async_press(self) -> None:
        """Handle the button press."""
        if not self._mqtt_identifier:
            _LOGGER.error(f"No MQTT identifier found for device {self.unique_id}")
            return

        topic = f"valetudo/{self._mqtt_identifier}/LocateCapability/locate/set"
        await mqtt.async_publish(self.hass, topic, "PERFORM")
        _LOGGER.info(f"Triggered locate for Valetudo robot {self._mqtt_identifier}")
