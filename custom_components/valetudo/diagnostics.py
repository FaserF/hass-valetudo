"""Diagnostics support for Valetudo."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

REDACT_KEYS = {"api_key", "password", "token", "access_token"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    # Managed devices
    managed_devices = []
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        entities = er.async_entries_for_device(ent_reg, device.id)
        entity_data = []
        for entity in entities:
            state = hass.states.get(entity.entity_id)
            entity_data.append(
                {
                    "entity_id": entity.entity_id,
                    "state": state.state if state else None,
                    "attributes": dict(state.attributes) if state else None,
                }
            )
        managed_devices.append(
            {
                "name": device.name,
                "id": device.id,
                "identifiers": list(device.identifiers),
                "connections": list(device.connections),
                "entities": entity_data,
            }
        )

    # All Valetudo devices
    all_valetudo_devices = []
    for dev in dev_reg.devices.values():
        if dev.manufacturer == "Valetudo" or any(
            ident[0] == DOMAIN for ident in dev.identifiers
        ):
            all_valetudo_devices.append(
                {
                    "name": dev.name,
                    "id": dev.id,
                    "manufacturer": dev.manufacturer,
                    "config_entries": list(dev.config_entries),
                    "identifiers": list(dev.identifiers),
                    "connections": list(dev.connections),
                }
            )

    # Trackers for debugging
    device_trackers = []
    for state in hass.states.async_all("device_tracker"):
        ent_entry = ent_reg.async_get(state.entity_id)
        device_trackers.append(
            {
                "entity_id": state.entity_id,
                "state": state.state,
                "ip": state.attributes.get("ip"),
                "mac": state.attributes.get("mac"),
                "host_name": state.attributes.get("host_name"),
                "device_id": ent_entry.device_id if ent_entry else None,
            }
        )

    try:
        import homeassistant as _ha

        ha_version = _ha.__version__
    except Exception:
        ha_version = "unknown"

    return {
        "ha_version": ha_version,
        "config_entry": async_redact_data(entry.as_dict(), REDACT_KEYS),
        "managed_devices": managed_devices,
        "all_valetudo_devices": all_valetudo_devices,
        "device_trackers": device_trackers,
    }
