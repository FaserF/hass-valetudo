import logging
import re
from collections.abc import Callable
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceConnectionCollisionError

_LOGGER = logging.getLogger(__name__)
MAC_REGEX = re.compile(r"([0-9a-fA-F]{2}[:.\-]?){5}[0-9a-fA-F]{2}")


async def async_enrich_registry(
    hass: HomeAssistant, config_entry_id: str, device_id: str, vacuum_entity_id: str
) -> list[str]:
    """Merge the Valetudo device with its network identity."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get(device_id)
    if not device:
        return []

    try:
        ip, mac = await _resolve_network_identity(hass, device_id)
        if not ip and not mac:
            _LOGGER.info(
                "Valetudo: Could not resolve network identity for %s yet", device.name
            )
            return []

        formatted_mac = dr.format_mac(mac) if mac else None
        _LOGGER.info(
            "Valetudo: Identity for %s resolved to IP=%s, MAC=%s",
            device.name,
            ip,
            formatted_mac,
        )

        moved_entities: list[str] = []

        # 1. Proactively search for and merge any conflicting devices sharing the same MAC
        if formatted_mac:
            # Find any device (other than James) that has this MAC connection
            conflicting_device = next(
                (
                    d
                    for d in dev_reg.devices.values()
                    if d.id != device_id
                    and any(
                        c[0] == dr.CONNECTION_NETWORK_MAC
                        and dr.format_mac(c[1]) == formatted_mac
                        for c in d.connections
                    )
                ),
                None,
            )

            if conflicting_device:
                _LOGGER.info(
                    "Valetudo: Found conflicting device '%s' (%s) with same MAC %s. Merging manually.",
                    conflicting_device.name,
                    conflicting_device.id,
                    formatted_mac,
                )
                moved_entities.extend(
                    _move_all_entities(hass, conflicting_device.id, device_id)
                )

                # Try to add the conflicting device's identifiers to James
                if conflicting_device.identifiers:
                    _LOGGER.info(
                        "Valetudo: Adding identifiers %s to %s",
                        conflicting_device.identifiers,
                        device.name,
                    )
                    try:
                        dev_reg.async_get_or_create(
                            config_entry_id=config_entry_id,
                            identifiers=device.identifiers
                            | conflicting_device.identifiers,
                        )
                    except Exception as e:
                        _LOGGER.debug("Valetudo: Could not merge identifiers: %s", e)

                # CLEANUP: Remove the conflicting device
                try:
                    dev_reg.async_remove_device(conflicting_device.id)
                except Exception as e:
                    _LOGGER.debug("Valetudo: Could not remove old device: %s", e)

            # Ensure James himself has the connection
            new_conn = (dr.CONNECTION_NETWORK_MAC, formatted_mac)
            if new_conn not in device.connections:
                _LOGGER.info(
                    "Valetudo: Adding MAC connection %s to %s",
                    formatted_mac,
                    device.name,
                )
                try:
                    dev_reg.async_update_device(device_id, merge_connections={new_conn})
                except DeviceConnectionCollisionError:
                    pass
                except Exception as e:
                    _LOGGER.debug("Valetudo: Error updating device connections: %s", e)

        # 2. Also search for any trackers by IP and ensure they are on James
        if ip:
            trackers = _find_matching_trackers(hass, ip, formatted_mac)
            for entity_id in trackers:
                entry = ent_reg.async_get(entity_id)
                if entry and entry.device_id != device_id:
                    _LOGGER.info(
                        "Valetudo: Found tracker %s on wrong device. Moving to %s.",
                        entity_id,
                        device.name,
                    )
                    ent_reg.async_update_entity(entity_id, device_id=device_id)
                    if entity_id not in moved_entities:
                        moved_entities.append(entity_id)

        return moved_entities

    except Exception as e:
        _LOGGER.error(
            "Valetudo: Error during registry enrichment: %s", e, exc_info=True
        )
        return []


def _move_all_entities(
    hass: HomeAssistant, source_device_id: str, target_device_id: str
) -> list[str]:
    """Move all entities from one device to another."""
    ent_reg = er.async_get(hass)
    moved = []
    for entry in er.async_entries_for_device(ent_reg, source_device_id):
        _LOGGER.info(
            "Valetudo: Moving entity %s to device %s", entry.entity_id, target_device_id
        )
        ent_reg.async_update_entity(entry.entity_id, device_id=target_device_id)
        moved.append(entry.entity_id)
    return moved


def setup_merge_maintenance(
    hass: HomeAssistant,
    device_id: str,
    tracker_entity_ids: list[str],
) -> Callable:
    """Listen for entity registry changes and re-apply merge if a tracker is moved back."""
    ent_reg = er.async_get(hass)
    watched = set(tracker_entity_ids)

    @callback
    def _on_entity_updated(event):
        if event.data.get("action") != "update":
            return
        entity_id = event.data.get("entity_id")
        if entity_id not in watched:
            return
        if "device_id" not in event.data.get("changes", {}):
            return
        entry = ent_reg.async_get(entity_id)
        if entry and entry.device_id != device_id:
            _LOGGER.info("Valetudo: Re-applying entity move for %s", entity_id)
            ent_reg.async_update_entity(entity_id, device_id=device_id)

    return hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _on_entity_updated)


def _find_matching_trackers(
    hass: HomeAssistant, ip: str | None, formatted_mac: str | None
) -> list[str]:
    """Find all device_tracker entity_ids that match the given IP or MAC."""
    matches = []
    for state in hass.states.async_all("device_tracker"):
        state_ip = state.attributes.get("ip") or state.attributes.get("ip_address")
        state_mac_raw = state.attributes.get("mac") or state.attributes.get(
            "mac_address"
        )
        state_mac = dr.format_mac(state_mac_raw) if state_mac_raw else None
        if (ip and state_ip == ip) or (formatted_mac and state_mac == formatted_mac):
            matches.append(state.entity_id)
    return matches


async def _resolve_network_identity(
    hass: HomeAssistant, device_id: str
) -> tuple[str | None, str | None]:
    """Return (ip, mac) for the device by scanning its entity states."""
    ent_reg = er.async_get(hass)
    ip: str | None = None
    mac: str | None = None

    for entry in er.async_entries_for_device(ent_reg, device_id):
        state = hass.states.get(entry.entity_id)
        if not state:
            continue

        # Direct MAC
        m = state.attributes.get("mac") or state.attributes.get("mac_address")
        if m:
            mac = m

        # Direct IP
        candidate_ip = (
            state.attributes.get("ip")
            or state.attributes.get("ip_address")
            or state.attributes.get("local_ip")
        )
        if not candidate_ip:
            ips = state.attributes.get("ips")
            if ips and isinstance(ips, list):
                candidate_ip = next(
                    (x for x in ips if isinstance(x, str) and "." in x), None
                )
        if candidate_ip:
            ip = candidate_ip

        if ip and mac:
            break

    # Fallback: Find MAC via IP in other trackers
    if ip and not mac:
        for state in hass.states.async_all("device_tracker"):
            if (
                state.attributes.get("ip") == ip
                or state.attributes.get("ip_address") == ip
            ):
                m = state.attributes.get("mac") or state.attributes.get("mac_address")
                if m:
                    mac = m
                    break

    return ip, mac
