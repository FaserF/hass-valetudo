import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
)
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_ENTRY_TYPE,
    ENTRY_TYPE_AUGMENTATIONS,
    VALETUDO_LATEST_RELEASE_API,
    VALETUDO_RELEASES_URL,
)
from .device_utils import _resolve_network_identity

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

    def __init__(
        self,
        hass: HomeAssistant,
        async_add_entities: AddEntitiesCallback,
        config_entry_id: str,
    ):
        self.hass = hass
        self.async_add_entities = async_add_entities
        self.config_entry_id = config_entry_id
        self._entities: dict[str, list[UpdateEntity]] = {}
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
                # Notify existing entities of the registry update (e.g. name or connection changes)
                if device_id in self._entities:
                    for entity in self._entities[device_id]:
                        if isinstance(entity, ValetudoUpdateEntity):
                            entity.async_update_device(device)

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


class ValetudoUpdateEntity(UpdateEntity, RestoreEntity):
    """Update entity for Valetudo firmware."""

    _attr_has_entity_name = True
    _attr_name = "Valetudo Firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = UpdateEntityFeature.INSTALL
    _attr_should_poll = False
    _attr_update_percentage: int | float | None = None

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

    @callback
    def async_update_device(self, device: dr.DeviceEntry) -> None:
        """Update device reference and refresh HA state when Device Registry changes."""
        self._device = device
        self._attr_device_info = {
            "connections": device.connections,
            "identifiers": device.identifiers,
        }
        if device.sw_version and self._attr_installed_version != device.sw_version:
            self._attr_installed_version = device.sw_version
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()

        # Listen to direct device registry updates for this device (e.g. name or connection updates)
        @callback
        def _on_device_registry_update(event: Event):
            if event.data.get("device_id") == self._device.id:
                dev_reg = dr.async_get(self.hass)
                dev = dev_reg.async_get(self._device.id)
                if dev:
                    self.async_update_device(dev)

        self.async_on_remove(
            self.hass.bus.async_listen(
                dr.EVENT_DEVICE_REGISTRY_UPDATED, _on_device_registry_update
            )
        )

        # Restore last state
        last_state = await self.async_get_last_state()
        if last_state is not None:
            if not self._attr_installed_version:
                self._attr_installed_version = last_state.attributes.get(
                    "installed_version"
                )
            self._attr_latest_version = last_state.attributes.get("latest_version")
            self._attr_release_notes = last_state.attributes.get("release_notes")
            _LOGGER.debug(
                f"Restored state for {self.unique_id}: {self._attr_installed_version} -> {self._attr_latest_version}"
            )

        async def _initial_fetch(_now=None) -> None:
            await self.async_update()
            self.async_write_ha_state()

        async_call_later(self.hass, 10, _initial_fetch)

        self.async_on_remove(async_call_later(self.hass, 3600, _initial_fetch))

    async def async_update(self) -> None:
        """Fetch latest version from GitHub."""
        _LOGGER.debug(f"Updating Valetudo version for {self.unique_id}")
        try:
            session = async_get_clientsession(self.hass)
            headers = {
                "User-Agent": "HomeAssistant-Valetudo-Integration",
                "Accept": "application/vnd.github.v3+json",
            }
            async with session.get(
                VALETUDO_LATEST_RELEASE_API,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    new_version = data.get("tag_name")
                    if new_version:
                        self._attr_latest_version = new_version
                        self._attr_release_notes = data.get("body")
                        _LOGGER.debug(
                            f"Successfully fetched Valetudo version: {self._attr_latest_version}"
                        )
                    else:
                        _LOGGER.warning("GitHub API returned 200 but no tag_name found")
                elif response.status == 403:
                    _LOGGER.info(
                        "GitHub API rate limit hit (HTTP 403) while fetching Valetudo release info; cached version will be used."
                    )
                else:
                    _LOGGER.warning(
                        f"Failed to fetch Valetudo version from GitHub: {response.status}"
                    )
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning(
                f"Could not fetch Valetudo version from GitHub (network/timeout): {err}"
            )
        except Exception:
            _LOGGER.exception("Unexpected error fetching Valetudo version")

        # Refresh installed version from device registry in case it changed
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get(self._device.id)
        if device:
            if device.sw_version:
                if self._attr_installed_version != device.sw_version:
                    _LOGGER.info(
                        f"Refreshed installed version for {self.unique_id}: {device.sw_version}"
                    )
                    self._attr_installed_version = device.sw_version
            else:
                _LOGGER.debug(f"Device {self._device.id} has no sw_version in registry")
        else:
            _LOGGER.warning(
                f"Device {self._device.id} not found in registry during version refresh"
            )

        # Log final state for debugging
        _LOGGER.debug(
            "Final state for %s: installed=%s, latest=%s",
            self.unique_id,
            self._attr_installed_version,
            self._attr_latest_version,
        )
        self.async_write_ha_state()

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Trigger firmware update via Valetudo REST API.

        Valetudo's updater is a state machine. We must poll the state between
        each action because all operations are asynchronous on the robot side.

        State flow:
          idle/error → [check] → update_available → [download] → downloaded → [apply] → (reboot)

        Each action is only valid in specific states:
          'check'    : valid from idle, error
          'download' : valid from update_available
          'apply'    : valid from downloaded

        The 'busy' flag signals an async operation is in progress.
        We poll until busy=false before sending the next action.
        """
        # ── Valetudo updater state class names ───────────────────────────────
        S_IDLE = "ValetudoUpdaterIdleState"
        S_ERROR = "ValetudoUpdaterErrorState"
        S_AVAILABLE = "ValetudoUpdaterUpdateAvailableState"
        S_DOWNLOADING = "ValetudoUpdaterDownloadingState"
        S_DOWNLOADED = "ValetudoUpdaterDownloadedState"
        S_APPROVAL_PENDING = "ValetudoUpdaterApprovalPendingState"
        S_APPLY_PENDING = "ValetudoUpdaterApplyPendingState"
        S_FINALIZATION_PENDING = (
            "ValetudoUpdaterFinalizationPendingState"  # state right before apply
        )
        S_APPLYING = "ValetudoUpdaterApplyingState"

        # ── Timeouts ─────────────────────────────────────────────────────────
        POLL_INTERVAL = 3  # seconds between state polls
        TIMEOUT_CHECK = 45  # seconds to wait for 'check' to complete
        TIMEOUT_DOWNLOAD = 300  # seconds to wait for download (up to 5 min)
        TIMEOUT_APPLY = 30  # seconds for apply HTTP request

        # ── Resolve robot IP ─────────────────────────────────────────────────
        ip, _ = await _resolve_network_identity(self.hass, self._device.id)
        if not ip:
            _LOGGER.error(
                "Valetudo: Cannot trigger update for %s: No IP found. "
                "Make sure the Wi-Fi sensor is available.",
                self._device.name,
            )
            return

        base_url = f"http://{ip}/api/v2/updater"
        session = async_get_clientsession(self.hass)

        # ── Helper: GET state ─────────────────────────────────────────────────
        async def _get_state() -> dict | None:
            """GET /api/v2/updater/state. Returns parsed JSON or None on error."""
            try:
                async with session.get(
                    f"{base_url}/state",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    body = await resp.text()
                    _LOGGER.error(
                        "Valetudo: Could not read updater state for %s: HTTP %s – %s",
                        self._device.name,
                        resp.status,
                        body,
                    )
            except TimeoutError:
                _LOGGER.warning(
                    "Valetudo: Timeout reading updater state for %s",
                    self._device.name,
                )
            except aiohttp.ClientError as err:
                _LOGGER.error("Valetudo: Network error reading updater state: %s", err)
            except Exception:
                _LOGGER.exception("Valetudo: Unexpected error reading updater state")
            return None

        # ── Helper: PUT action ────────────────────────────────────────────────
        async def _put_action(action: str, req_timeout: int = 30) -> bool:
            """PUT {action} to the updater endpoint. Returns True on 200/202."""
            try:
                async with session.put(
                    base_url,
                    json={"action": action},
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=req_timeout),
                ) as resp:
                    if resp.status in (200, 202):
                        _LOGGER.info(
                            "Valetudo: updater action '%s' accepted for %s (HTTP %s)",
                            action,
                            self._device.name,
                            resp.status,
                        )
                        return True
                    body = await resp.text()
                    _LOGGER.error(
                        "Valetudo: updater action '%s' failed for %s: HTTP %s – %s",
                        action,
                        self._device.name,
                        resp.status,
                        body,
                    )
                    return False
            except TimeoutError:
                _LOGGER.error(
                    "Valetudo: Timeout sending action '%s' for %s",
                    action,
                    self._device.name,
                )
            except aiohttp.ClientError as err:
                _LOGGER.error(
                    "Valetudo: Network error sending action '%s': %s", action, err
                )
            except Exception:
                _LOGGER.exception(
                    "Valetudo: Unexpected error sending action '%s'", action
                )
            return False

        # ── Helper: poll until target state ───────────────────────────────────
        async def _poll_until(
            target_states: set,
            timeout: int,
            pct_start: int,
            pct_end: int,
        ) -> dict | None:
            """Poll state until it reaches one of target_states (busy=false), or error.

            Linearly interpolates _attr_update_percentage pct_start → pct_end.
            Returns the final state dict, or None on timeout / too many network errors.
            """
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout
            max_steps = max(timeout // POLL_INTERVAL, 1)
            step = 0
            consecutive_errors = 0

            while True:
                data = await _get_state()

                if data is None:
                    # Transient network error — tolerate several in a row
                    consecutive_errors += 1
                    if consecutive_errors >= 5:
                        _LOGGER.error(
                            "Valetudo: Too many consecutive network errors "
                            "waiting for state %s — aborting",
                            target_states,
                        )
                        return None
                    await asyncio.sleep(POLL_INTERVAL)
                    if loop.time() > deadline:
                        _LOGGER.error(
                            "Valetudo: Timed out (%ss) waiting for state %s "
                            "(network errors)",
                            timeout,
                            target_states,
                        )
                        return None
                    continue

                consecutive_errors = 0
                cls = data.get("__class", "")
                busy = data.get("busy", False)

                # Animate progress smoothly
                fraction = min(step / max_steps, 1.0)
                pct = int(pct_start + fraction * (pct_end - pct_start))
                if self._attr_update_percentage != pct:
                    self._attr_update_percentage = pct
                    self.async_write_ha_state()

                _LOGGER.debug("Valetudo: state=%s busy=%s pct=%s%%", cls, busy, pct)

                # Reached a desired state and no longer busy → done
                if cls in target_states and not busy:
                    return data

                # Error state reached (always settles to busy=false)
                if cls == S_ERROR and not busy:
                    msg = data.get("message", "unknown error")
                    _LOGGER.error(
                        "Valetudo: updater entered error state for %s: %s",
                        self._device.name,
                        msg,
                    )
                    return data  # caller checks __class to decide whether to abort

                # Timeout guard
                if loop.time() > deadline:
                    _LOGGER.error(
                        "Valetudo: Timed out after %ss waiting for %s "
                        "(current: %s busy=%s)",
                        timeout,
                        target_states,
                        cls,
                        busy,
                    )
                    return None

                step += 1
                await asyncio.sleep(POLL_INTERVAL)

        # ════════════════════════════════════════════════════════════════════
        #  Main update flow
        # ════════════════════════════════════════════════════════════════════
        initial = await _get_state()
        if initial is None:
            _LOGGER.error(
                "Valetudo: Cannot read updater state for %s — aborting",
                self._device.name,
            )
            return

        cls = initial.get("__class", "")
        busy = initial.get("busy", False)
        _LOGGER.info(
            "Valetudo: Starting update for %s (target: %s) — state: %s  busy: %s",
            self._device.name,
            version or "latest",
            cls,
            busy,
        )

        # Signal HA that the update has started
        self._attr_in_progress = True
        self._attr_update_percentage = 0
        self.async_write_ha_state()

        try:
            # ── Wait for any in-progress operation to settle ──────────────
            if busy:
                _LOGGER.info("Valetudo: Updater is busy (%s) — waiting to settle…", cls)
                settled = await _poll_until(
                    {S_IDLE, S_ERROR, S_AVAILABLE, S_DOWNLOADED, S_DOWNLOADING},
                    timeout=TIMEOUT_DOWNLOAD,
                    pct_start=0,
                    pct_end=5,
                )
                if settled is None:
                    return
                cls = settled.get("__class", cls)

            # ── Step 1: Check ─────────────────────────────────────────────
            if cls in (S_IDLE, S_ERROR):
                _LOGGER.info("Valetudo: Sending 'check' for %s…", self._device.name)
                if not await _put_action("check", req_timeout=15):
                    return
                # Poll until check completes (fetches release info from GitHub)
                settled = await _poll_until(
                    {S_AVAILABLE, S_APPROVAL_PENDING, S_IDLE},
                    timeout=TIMEOUT_CHECK,
                    pct_start=5,
                    pct_end=15,
                )
                if settled is None:
                    return
                cls = settled.get("__class", cls)
                if cls not in (S_AVAILABLE, S_APPROVAL_PENDING):
                    _LOGGER.warning(
                        "Valetudo: No update available after check for %s "
                        "(state: %s) — robot is already up to date.",
                        self._device.name,
                        cls,
                    )
                    return

            self._attr_update_percentage = 15
            self.async_write_ha_state()

            # ── Step 2: Download ──────────────────────────────────────────
            if cls in (S_AVAILABLE, S_APPROVAL_PENDING):
                _LOGGER.info(
                    "Valetudo: Update available/pending (%s) — sending 'download' for %s…",
                    cls,
                    self._device.name,
                )
                if not await _put_action("download", req_timeout=15):
                    return
                # Poll until download completes and reaches apply-pending/finalization/downloaded state
                settled = await _poll_until(
                    {S_APPLY_PENDING, S_FINALIZATION_PENDING, S_DOWNLOADED},
                    timeout=TIMEOUT_DOWNLOAD,
                    pct_start=20,
                    pct_end=85,
                )
                if settled is None:
                    return
                cls = settled.get("__class", cls)
                if cls == S_ERROR:
                    return

            elif cls == S_DOWNLOADING:
                # Resume polling an already in-progress download
                _LOGGER.info(
                    "Valetudo: Download in progress for %s — waiting for completion…",
                    self._device.name,
                )
                settled = await _poll_until(
                    {S_APPLY_PENDING, S_FINALIZATION_PENDING, S_DOWNLOADED},
                    timeout=TIMEOUT_DOWNLOAD,
                    pct_start=20,
                    pct_end=85,
                )
                if settled is None:
                    return
                cls = settled.get("__class", cls)
                if cls == S_ERROR:
                    return

            self._attr_update_percentage = 85
            self.async_write_ha_state()

            # ── Step 3: Apply ─────────────────────────────────────────────
            if cls in (
                S_APPLY_PENDING,
                S_FINALIZATION_PENDING,
                S_DOWNLOADED,
                S_APPLYING,
            ):
                if cls == S_APPLYING:
                    self._attr_update_percentage = 100
                    self.async_write_ha_state()
                    _LOGGER.info(
                        "Valetudo: %s is already applying the update.",
                        self._device.name,
                    )
                    return

                _LOGGER.info(
                    "Valetudo: Sending 'apply' for %s (current state: %s)…",
                    self._device.name,
                    cls,
                )
                success = await _put_action("apply", req_timeout=TIMEOUT_APPLY)

                if not success:
                    _LOGGER.error(
                        "Valetudo: 'apply' action was rejected for %s",
                        self._device.name,
                    )
                    return

                # Robot reboots after apply — connection drop is expected.
                self._attr_update_percentage = 100
                self.async_write_ha_state()
                _LOGGER.info(
                    "Valetudo: 'apply' accepted for %s — robot is rebooting into new firmware.",
                    self._device.name,
                )

            else:
                _LOGGER.warning(
                    "Valetudo: Cannot send 'apply' for %s while in state '%s' — aborting.",
                    self._device.name,
                    cls,
                )

        finally:
            # Always reset in_progress, regardless of how we exit
            self._attr_in_progress = False
            self.async_write_ha_state()
