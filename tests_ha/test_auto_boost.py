"""Automatic boost: starts while the room is far below the target of the user
choice, ends once it has caught up, and waits for the send limit.

When it starts and ends is PresetLayers' decision (tests/test_preset_layers.py);
these tests check the wiring: the regulation cycle reports the room sensor's
own reading, the switch and the options apply live, and the dialog checks the
thresholds.
"""
from __future__ import annotations

from unittest.mock import patch

from homeassistant.components.climate import PRESET_BOOST, PRESET_COMFORT, PRESET_ECO
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.data_entry_flow import FlowResultType

from .helpers import (
    CONTROL_INTERVAL_S,
    ROOM_SENSOR,
    TEMPERATURE,
    Proxy,
    async_advance,
    async_set_state,
    async_setup_proxy,
    async_submit_options,
)

# The option the switch writes; set directly here for rooms that start with
# the automatic boost on.
AUTO_BOOST = {"auto_boost_enabled": True, "auto_boost_lockout_min": 60}


async def _async_room_reads(proxy: Proxy, freezer, room_c: float) -> None:
    """The room sensor reports *room_c*; two cycles pass, so the coordinator
    has read it before a cycle decides on it."""
    await async_set_state(proxy.hass, ROOM_SENSOR, str(room_c), TEMPERATURE)
    for _ in range(2):
        await async_advance(proxy.hass, freezer, CONTROL_INTERVAL_S)


def _setpoints(proxy: Proxy) -> list[float]:
    """Every setpoint the proxy sent to the TRV so far."""
    return [c["temperature"] for c in proxy.trv_commands("set_temperature")]


def _switch(proxy: Proxy) -> str:
    return proxy.entity_id("switch", "auto_boost")


async def test_off_by_default_and_shows_nothing(hass, freezer):
    """Also what an entry saved by v1.6.1 gets: it has no auto_boost_enabled."""
    proxy = await async_setup_proxy(hass, trv_adopts=True)
    assert "auto_boost_enabled" not in proxy.entry.options

    await _async_room_reads(proxy, freezer, 17.0)

    assert hass.states.get(_switch(proxy)).state == STATE_OFF
    assert proxy.preset == PRESET_COMFORT
    assert "auto_boost_active" not in proxy.state.attributes


async def test_switch_is_named_after_the_proxy(hass):
    proxy = await async_setup_proxy(hass)

    assert _switch(proxy) == "switch.living_auto_boost"
    state = hass.states.get(_switch(proxy))
    assert state is not None
    assert state.attributes["friendly_name"] == "Living Auto boost"


async def test_switch_shows_the_stored_option(hass):
    proxy = await async_setup_proxy(hass, options=AUTO_BOOST)

    assert hass.states.get(_switch(proxy)).state == STATE_ON


async def test_switch_applies_live_and_off_ends_a_running_boost(hass, freezer):
    proxy = await async_setup_proxy(hass, trv_adopts=True)
    await async_advance(hass, freezer, CONTROL_INTERVAL_S)

    with patch.object(hass.config_entries, "async_reload") as reload:
        await proxy.call("switch", "turn_on", _switch(proxy))
        await _async_room_reads(proxy, freezer, 19.0)

        assert proxy.entry.options["auto_boost_enabled"] is True
        assert hass.states.get(_switch(proxy)).state == STATE_ON
        assert proxy.preset == PRESET_BOOST
        assert proxy.state.attributes["auto_boost_active"] is True

        await proxy.call("switch", "turn_off", _switch(proxy))

    reload.assert_not_called()
    assert proxy.entry.options["auto_boost_enabled"] is False
    assert hass.states.get(_switch(proxy)).state == STATE_OFF
    assert proxy.preset == PRESET_COMFORT
    assert "auto_boost_active" not in proxy.state.attributes


async def test_starts_and_ends_on_the_room_sensor(hass, freezer):
    proxy = await async_setup_proxy(hass, options=AUTO_BOOST, trv_adopts=True)
    await async_advance(hass, freezer, CONTROL_INTERVAL_S)
    assert proxy.preset == PRESET_COMFORT
    assert proxy.state.attributes["auto_boost_active"] is False

    await _async_room_reads(proxy, freezer, 19.0)

    assert proxy.preset == PRESET_BOOST
    assert proxy.target == 25.0
    assert proxy.state.attributes["auto_boost_active"] is True
    assert proxy.state.attributes["user_preset"] == PRESET_COMFORT
    remaining = hass.states.get(proxy.entity_id("sensor", "boost_remaining"))
    assert remaining is not None and int(float(remaining.state)) > 0

    await _async_room_reads(proxy, freezer, 20.0)

    assert proxy.preset == PRESET_COMFORT
    assert proxy.target == 20.0
    assert proxy.state.attributes["auto_boost_active"] is False
    assert "auto_boost_lockout_until_ts" in proxy.state.attributes


async def test_waits_for_the_send_limit_like_every_other_increase(hass, freezer):
    """Only a person's boost may skip the send limit (CLAUDE.md)."""
    proxy = await async_setup_proxy(hass, options=AUTO_BOOST, trv_adopts=True)
    # The first command goes out on Comfort; the send limit runs from here.
    await async_advance(hass, freezer, CONTROL_INTERVAL_S)
    assert len(_setpoints(proxy)) == 1

    # 120 s later the boost runs, but its higher command still waits.
    await _async_room_reads(proxy, freezer, 19.0)
    assert proxy.preset == PRESET_BOOST
    assert proxy.state.attributes["regulation_reason"].startswith("rate_limited")
    assert len(_setpoints(proxy)) == 1

    await async_advance(hass, freezer, CONTROL_INTERVAL_S)
    assert proxy.state.attributes["regulation_reason"] == "sent(normal_update)"
    assert len(_setpoints(proxy)) == 2
    assert _setpoints(proxy)[-1] > _setpoints(proxy)[0]


async def test_a_degraded_room_sensor_ends_it(hass, freezer):
    proxy = await async_setup_proxy(hass, options=AUTO_BOOST, trv_adopts=True)
    await _async_room_reads(proxy, freezer, 19.0)
    assert proxy.preset == PRESET_BOOST

    await async_set_state(hass, ROOM_SENSOR, STATE_UNAVAILABLE)
    for _ in range(2):
        await async_advance(hass, freezer, CONTROL_INTERVAL_S)

    assert proxy.state.attributes["sensor_degraded"] is True
    assert proxy.preset == PRESET_COMFORT
    assert proxy.state.attributes["auto_boost_active"] is False


async def test_a_preset_picked_by_hand_ends_it(hass, freezer):
    proxy = await async_setup_proxy(hass, options=AUTO_BOOST, trv_adopts=True)
    await _async_room_reads(proxy, freezer, 19.0)
    assert proxy.preset == PRESET_BOOST

    await proxy.call("climate", "set_preset_mode", proxy.climate, preset_mode=PRESET_ECO)
    await async_advance(hass, freezer, CONTROL_INTERVAL_S)

    assert proxy.preset == PRESET_ECO
    assert proxy.state.attributes["auto_boost_active"] is False


async def test_an_automation_re_sending_the_same_target_does_not_end_it(hass, freezer):
    """Field case: a presence automation re-applies the Manual temperature the
    room already has while an automatic boost runs.  The boost keeps running
    and no lockout starts; so does the same HVAC mode sent again."""
    proxy = await async_setup_proxy(hass, options=AUTO_BOOST, trv_adopts=True)
    await proxy.call("climate", "set_temperature", proxy.climate, temperature=20.6)
    await _async_room_reads(proxy, freezer, 19.5)
    assert proxy.preset == PRESET_BOOST

    await proxy.call("climate", "set_temperature", proxy.climate, temperature=20.6)
    await proxy.call("climate", "set_hvac_mode", proxy.climate, hvac_mode="heat")
    await async_advance(hass, freezer, CONTROL_INTERVAL_S)

    assert proxy.preset == PRESET_BOOST
    assert proxy.state.attributes["auto_boost_active"] is True
    assert "auto_boost_lockout_until_ts" not in proxy.state.attributes
    assert proxy.state.attributes["manual_target_c"] == 20.6

    # A different temperature is a real change: the boost ends.
    await proxy.call("climate", "set_temperature", proxy.climate, temperature=20.5)
    assert proxy.preset == "none"
    assert "auto_boost_lockout_until_ts" in proxy.state.attributes


async def test_the_preset_that_already_is_the_choice_does_not_end_it(hass, freezer):
    proxy = await async_setup_proxy(hass, options=AUTO_BOOST, trv_adopts=True)
    await _async_room_reads(proxy, freezer, 19.0)
    assert proxy.preset == PRESET_BOOST

    await proxy.call("climate", "set_preset_mode", proxy.climate, preset_mode=PRESET_COMFORT)

    assert proxy.preset == PRESET_BOOST
    assert proxy.state.attributes["auto_boost_active"] is True
    assert "auto_boost_lockout_until_ts" not in proxy.state.attributes


async def test_options_apply_live_without_a_reload(hass, freezer):
    proxy = await async_setup_proxy(hass, options=AUTO_BOOST, trv_adopts=True)
    # The first save adds every option the dialog shows; let that reload pass.
    result = await async_submit_options(hass, proxy.entry)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await proxy.call("climate", "set_preset_mode", proxy.climate, preset_mode=PRESET_BOOST)

    with patch.object(hass.config_entries, "async_reload") as reload:
        result = await async_submit_options(
            hass, proxy.entry, auto_boost={"auto_boost_start_c": 0.5}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    reload.assert_not_called()
    assert proxy.entry.options["auto_boost_start_c"] == 0.5
    # Saving the dialog keeps what the switch stored.
    assert proxy.entry.options["auto_boost_enabled"] is True
    # A reload would have ended the boost.
    assert proxy.preset == PRESET_BOOST
    assert proxy.state.attributes["auto_boost_active"] is False


async def test_dialog_requires_the_start_gap_above_the_end_gap(hass):
    """Also while the switch is off: it can be turned on without the dialog."""
    proxy = await async_setup_proxy(hass)

    result = await async_submit_options(
        hass,
        proxy.entry,
        auto_boost={"auto_boost_start_c": 0.5, "auto_boost_stop_c": 0.5},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "auto_boost_thresholds"}

    result = await async_submit_options(
        hass,
        proxy.entry,
        auto_boost={"auto_boost_start_c": 0.6, "auto_boost_stop_c": 0.5},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_options_are_in_their_collapsed_section_without_the_switch(hass):
    proxy = await async_setup_proxy(hass)
    result = await hass.config_entries.options.async_init(proxy.entry.entry_id)
    schema = result["data_schema"].schema
    names = {str(key) for key in schema}
    section = next(value for key, value in schema.items() if str(key) == "auto_boost")
    fields = {str(key) for key in section.schema.schema}

    assert "auto_boost_start_c" not in names
    assert fields == {"auto_boost_start_c", "auto_boost_stop_c", "auto_boost_lockout_min"}
