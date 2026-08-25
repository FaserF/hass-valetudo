import logging

from homeassistant.components import camera
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .device_utils import _do_valetudo_put
from .map_utils import extract_and_parse_map

_LOGGER = logging.getLogger(__name__)


async def async_setup_services(hass: HomeAssistant):
    await async_register_extract_map_service(hass)
    await async_register_clean_room_service(hass)


async def async_register_extract_map_service(hass: HomeAssistant):
    service_name = "extract_map_data"

    if hass.services.has_service(DOMAIN, service_name):
        return

    async def async_handle_extract_map_data(call: ServiceCall) -> dict:
        input_device_id = call.data.get("device_id")
        input_entity_id = call.data.get("entity_id")

        target_entity_id = None

        if isinstance(input_device_id, str):
            dev_reg = dr.async_get(hass)
            target_device = dev_reg.async_get(input_device_id)

            if not target_device:
                raise ServiceValidationError(
                    f"Device {input_device_id} not found in registry."
                )

            if target_device.manufacturer != "Valetudo":
                raise ServiceValidationError(
                    f"Device '{target_device.name}' manufacturer is not 'Valetudo'."
                )

            ent_reg = er.async_get(hass)
            device_entities = er.async_entries_for_device(ent_reg, input_device_id)

            map_entity_entry = next(
                (
                    e
                    for e in device_entities
                    if e.domain == "camera" and e.entity_id.endswith("_map_data")
                ),
                None,
            )

            if not map_entity_entry:
                raise ServiceValidationError(
                    f"Could not find 'camera.valetudo_<system_id>_map_data' entity for device '{target_device.name}'"
                )

            target_entity_id = map_entity_entry.entity_id

        elif input_entity_id:
            target_entity_id = input_entity_id

        else:
            raise ServiceValidationError(
                "Please provide either a device_id or an entity_id."
            )

        try:
            image_obj = await camera.async_get_image(hass, target_entity_id)
            image_bytes = image_obj.content
        except Exception as e:  # noqa: BLE001
            raise ServiceValidationError(
                f"Failed to fetch image from entity '{target_entity_id}'. "
                f"Ensure it is a valid camera. Error: {e!s}"
            ) from e

        try:
            map_data = await hass.async_add_executor_job(
                extract_and_parse_map, image_bytes
            )
        except Exception as e:  # noqa: BLE001
            _LOGGER.error(f"Error parsing map data: {e}")
            raise ServiceValidationError(f"Error parsing map data: {e}") from e

        if not map_data:
            raise ServiceValidationError(
                f"No Valetudo map data found in image from '{target_entity_id}'."
            )

        return map_data

    hass.services.async_register(
        DOMAIN,
        service_name,
        async_handle_extract_map_data,
        supports_response=SupportsResponse.ONLY,
    )


async def async_register_clean_room_service(hass: HomeAssistant):
    """Register the clean_room service."""
    service_name = "clean_room"

    if hass.services.has_service(DOMAIN, service_name):
        return

    async def async_handle_clean_room(call: ServiceCall):
        device_id = call.data.get("device_id")
        room_id = call.data.get("room_id")
        room_name = call.data.get("room_name")
        iterations = call.data.get("iterations", 1)

        if not isinstance(device_id, str):
            raise ServiceValidationError("Device ID must be a string")

        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get(device_id)

        if not device:
            raise ServiceValidationError(f"Device {device_id} not found.")

        final_room_id = room_id

        if not final_room_id and room_name:
            # Try to resolve room_id from the select entity if it exists
            ent_reg = er.async_get(hass)
            state = None
            select_entity = next(
                (
                    e
                    for e in er.async_entries_for_device(ent_reg, device_id)
                    if e.domain == "select" and e.unique_id.endswith("_room_select")
                ),
                None,
            )
            if select_entity:
                state = hass.states.get(select_entity.entity_id)
                if state:
                    room_ids = state.attributes.get("room_ids", {})
                    final_room_id = room_ids.get(room_name)

        if not final_room_id:
            raise ServiceValidationError(
                "Please provide either a room_id or a valid room_name."
            )

        success = await _do_valetudo_put(
            hass,
            device_id,
            "/api/v2/robot/capabilities/MapSegmentationCapability",
            {
                "action": "start_segment_action",
                "segment_ids": [str(final_room_id)],
                "iterations": iterations,
            },
        )
        if success:
            _LOGGER.info(
                f"Service clean_room triggered for {device.name}, room {final_room_id}"
            )

    # For manual mapping in YAML
    hass.services.async_register(DOMAIN, service_name, async_handle_clean_room)
