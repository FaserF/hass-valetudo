"""Regression tests for safe device_registry iteration across HA versions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.valetudo.const import DOMAIN
from custom_components.valetudo.device_utils import iter_devices
from custom_components.valetudo.number import ValetudoNumberManager
from custom_components.valetudo.select import ValetudoSelectManager
from custom_components.valetudo.sensor import ValetudoSensorManager
from custom_components.valetudo.switch import ValetudoSwitchManager
from custom_components.valetudo.update import ValetudoUpdateManager


class _NoValuesDeviceCollection:
    """Mimic HA 2026.11+ Collection[DeviceEntry] where .values() is deprecated/forbidden."""

    def __init__(
        self, entries: list[dr.DeviceEntry], mutate_on_iter: bool = False
    ) -> None:
        self._entries = entries
        self._mutate_on_iter = mutate_on_iter

    def values(self) -> None:
        raise AssertionError("dev_reg.devices.values() must not be called")

    def __iter__(self):
        for idx, entry in enumerate(self._entries):
            if self._mutate_on_iter and idx == 0:
                self._entries.append(
                    SimpleNamespace(id="dev_extra", manufacturer="Other")
                )
            yield entry


def test_no_dev_reg_devices_values_in_component() -> None:
    """Ensure no module accesses dev_reg.devices.values()."""
    root = Path(__file__).resolve().parent.parent / "custom_components" / "valetudo"
    for py_file in root.glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        assert "devices.values()" not in content, (
            f"{py_file.name} still calls .values()"
        )


def test_iter_devices_handles_collection_without_values() -> None:
    """iter_devices works on HA 2026.11 Collection[DeviceEntry] without calling .values()."""
    dev1 = SimpleNamespace(id="dev_1", manufacturer="Valetudo")
    dev2 = SimpleNamespace(id="dev_2", manufacturer="Other")
    fake_reg = SimpleNamespace(
        devices=_NoValuesDeviceCollection([dev1, dev2], mutate_on_iter=True),
        async_get=lambda _id: None,
    )

    result = iter_devices(fake_reg)  # type: ignore[arg-type]
    assert [d.id for d in result[:2]] == ["dev_1", "dev_2"]


def test_iter_devices_handles_real_ha_registry(hass: HomeAssistant) -> None:
    """iter_devices returns DeviceEntry objects with real HA DeviceRegistry."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    created = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "robot_1")},
        manufacturer="Valetudo",
        name="Roborock",
    )

    devices = iter_devices(dev_reg)
    assert any(d.id == created.id for d in devices)


def test_managers_scan_existing_devices_concurrent_mutation(
    hass: HomeAssistant,
) -> None:
    """Scanning existing devices must tolerate concurrent device additions without RuntimeError."""
    dev1 = SimpleNamespace(id="valetudo_1", manufacturer="Valetudo")
    dev2 = SimpleNamespace(id="valetudo_2", manufacturer="Valetudo")

    devices_map: dict[str, SimpleNamespace] = {"valetudo_1": dev1, "valetudo_2": dev2}
    fake_reg = MagicMock()
    fake_reg.devices = devices_map
    fake_reg.async_get.side_effect = lambda did: devices_map.get(did)

    add_entities = MagicMock()
    managers = [
        (ValetudoUpdateManager(hass, add_entities, "cfg_1"), "_try_add_entities"),
        (ValetudoSensorManager(hass, add_entities, "cfg_1"), "_try_add_sensors"),
        (ValetudoNumberManager(hass, add_entities, "cfg_1"), "_try_add_numbers"),
        (ValetudoSelectManager(hass, add_entities, "cfg_1"), "_try_add_selects"),
        (ValetudoSwitchManager(hass, add_entities, "cfg_1"), "_try_add_switches"),
    ]

    with patch(
        "homeassistant.helpers.device_registry.async_get", return_value=fake_reg
    ):
        for manager, method_name in managers:
            seen: list[str] = []

            def _record(device_id: str, _seen: list[str] = seen) -> None:
                _seen.append(device_id)
                devices_map[f"dyn_{device_id}"] = SimpleNamespace(
                    id=f"dyn_{device_id}", manufacturer="Valetudo"
                )

            with patch.object(manager, method_name, side_effect=_record):
                manager._scan_existing_devices()

            assert "valetudo_1" in seen
            assert "valetudo_2" in seen
