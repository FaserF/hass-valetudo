import logging
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

async def async_enrich_registry(hass: HomeAssistant, device_id: str, vacuum_entity_id: str):
    """Try to find MAC address for the robot and add it to the device registry."""
    state = hass.states.get(vacuum_entity_id)
    if not state:
        return

    ip_address = state.attributes.get("ip")
    if not ip_address:
        # Try other common attributes
        ip_address = (
            state.attributes.get("ip_address") 
            or state.attributes.get("local_ip")
            or state.attributes.get("host")
        )

    if not ip_address:
        _LOGGER.debug(f"Could not find IP for vacuum {vacuum_entity_id}")
        return

    # Attempt to find MAC in device tracker states
    mac = await _async_find_mac_by_ip(hass, ip_address)
    if mac:
        formatted_mac = dr.format_mac(mac)
        dev_reg = dr.async_get(hass)
        dev_reg.async_update_device(
            device_id,
            merge_connections={(dr.CONNECTION_NETWORK_MAC, formatted_mac)}
        )
        _LOGGER.info(f"Enriched Valetudo device {device_id} with MAC {formatted_mac} via IP {ip_address}")

async def _async_find_mac_by_ip(hass: HomeAssistant, ip: str) -> str | None:
    """Search for a MAC address associated with the given IP in HA states."""
    # Search in device_tracker entities
    for state in hass.states.async_all("device_tracker"):
        if state.attributes.get("ip") == ip or state.attributes.get("ip_address") == ip:
            mac = state.attributes.get("mac")
            if mac:
                return str(mac)

    # Search in other entities that might have MAC as an attribute
    for domain in ["sensor", "binary_sensor"]:
        for state in hass.states.async_all(domain):
            if (state.attributes.get("ip") == ip or state.attributes.get("ip_address") == ip) and state.attributes.get("mac"):
                return str(state.attributes.get("mac"))

    return None
