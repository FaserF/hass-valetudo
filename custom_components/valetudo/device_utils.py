import logging
import re
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

_LOGGER = logging.getLogger(__name__)

# Regular expression to match MAC addresses in various formats
MAC_REGEX = re.compile(r"([0-9a-fA-F]{2}[:.-]?){5}[0-9a-fA-F]{2}")


async def async_enrich_registry(
    hass: HomeAssistant, device_id: str, vacuum_entity_id: str
):
    """Try to find MAC address for the robot and add it to the device registry."""
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    if not device:
        return

    # 1. Try to extract MAC from identifiers (e.g. valetudo_1234567890ab)
    mac = _async_extract_mac_from_identifiers(device.identifiers)

    # 2. If not found in identifiers, try to find it via IP address from vacuum state
    if not mac:
        state = hass.states.get(vacuum_entity_id)
        if state:
            ip_address = (
                state.attributes.get("ip")
                or state.attributes.get("ip_address")
                or state.attributes.get("local_ip")
                or state.attributes.get("host")
            )

            if ip_address:
                mac = await _async_find_mac_by_ip(hass, ip_address)

    if mac:
        formatted_mac = dr.format_mac(mac).lower()
        # Check if already present to avoid redundant updates
        if not any(conn[1] == formatted_mac for conn in device.connections):
            dev_reg.async_update_device(
                device_id,
                merge_connections={(dr.CONNECTION_NETWORK_MAC, formatted_mac)},
            )
            _LOGGER.info(
                f"Enriched Valetudo device {device.name} ({device_id}) with MAC {formatted_mac}"
            )
    else:
        _LOGGER.debug(f"Could not find MAC for Valetudo device {device_id}")


def _async_extract_mac_from_identifiers(
    identifiers: set[tuple[str, str]],
) -> str | None:
    """Try to find something that looks like a MAC in the identifiers."""
    for _, value in identifiers:
        # Common Valetudo identifier: valetudo_1234567890ab
        if "_" in value:
            potential_mac = value.split("_")[-1]
            if len(potential_mac) == 12 and all(
                c in "0123456789abcdefABCDEF" for c in potential_mac
            ):
                return potential_mac

        # Check if the value itself is a MAC
        match = MAC_REGEX.search(value)
        if match:
            return match.group(0)

    return None


async def _async_find_mac_by_ip(hass: HomeAssistant, ip: str) -> str | None:
    """Search for a MAC address associated with the given IP in HA states."""
    # Search in device_tracker entities
    for state in hass.states.async_all("device_tracker"):
        if state.attributes.get("ip") == ip or state.attributes.get("ip_address") == ip:
            mac = state.attributes.get("mac")
            if mac:
                return str(mac)

    # Search in other entities that might have MAC as an attribute
    for domain in ["sensor", "binary_sensor", "vacuum"]:
        for state in hass.states.async_all(domain):
            if (
                state.attributes.get("ip") == ip
                or state.attributes.get("ip_address") == ip
            ) and state.attributes.get("mac"):
                return str(state.attributes.get("mac"))

    return None
