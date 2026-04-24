import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_call_later

from .const import (
    DOMAIN,
    CONF_ENTRY_TYPE,
    ENTRY_TYPE_ICONS,
    ENTRY_TYPE_AUGMENTATIONS,
    PLATFORMS,
)
from .custom_icons import async_setup_icons
from .services import async_setup_services
from .device_utils import async_enrich_registry, setup_merge_maintenance

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    entry_type = entry.data.get(CONF_ENTRY_TYPE)

    if entry_type == ENTRY_TYPE_ICONS:
        await async_setup_icons(hass)

    elif entry_type == ENTRY_TYPE_AUGMENTATIONS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        if DOMAIN not in hass.data:
            hass.data[DOMAIN] = {}
        if "unsubs" not in hass.data[DOMAIN]:
            hass.data[DOMAIN]["unsubs"] = {}

        async def trigger_enrichment(_=None):
            dev_reg = dr.async_get(hass)
            ent_reg = er.async_get(hass)

            for device in dev_reg.devices.values():
                if device.manufacturer != "Valetudo":
                    continue

                entries = er.async_entries_for_device(ent_reg, device.id)
                vacuum_entity_id = next(
                    (e.entity_id for e in entries if e.domain == "vacuum"), None
                )
                if not vacuum_entity_id:
                    continue

                moved = await async_enrich_registry(
                    hass, entry.entry_id, device.id, vacuum_entity_id
                )

                if moved:
                    # Clear existing listener for this device if any
                    if device.id in hass.data[DOMAIN]["unsubs"]:
                        hass.data[DOMAIN]["unsubs"][device.id]()

                    unsub = setup_merge_maintenance(hass, device.id, moved)
                    hass.data[DOMAIN]["unsubs"][device.id] = unsub
                    entry.async_on_unload(unsub)

        hass.async_create_task(trigger_enrichment())
        async_call_later(hass, 60, trigger_enrichment)

    await async_setup_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    entry_type = entry.data.get(CONF_ENTRY_TYPE)
    if entry_type == ENTRY_TYPE_AUGMENTATIONS:
        return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return True
