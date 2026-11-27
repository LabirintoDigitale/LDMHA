"""Configurazione guidata: chiave AcoustID (per riconoscere i brani dall'audio) e cartelle della musica."""

from __future__ import annotations

import os
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import CONF_ACOUSTID_KEY, CONF_ROOTS, DEFAULT_ROOTS, DOMAIN


def parse_roots(text: str) -> list[str]:
    """Cartelle separate da virgola o a capo, senza doppioni."""
    out: list[str] = []
    for r in str(text or "").replace("\n", ",").split(","):
        r = r.strip().rstrip("/")
        if r and r not in out:
            out.append(r)
    return out


def _check(user_input: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    errors: dict[str, str] = {}
    roots = parse_roots(user_input.get(CONF_ROOTS) or DEFAULT_ROOTS)
    if not roots or any(not r.startswith("/") for r in roots):
        errors[CONF_ROOTS] = "invalid_roots"
    elif not any(os.path.isdir(r) for r in roots):
        errors[CONF_ROOTS] = "no_roots"
    data = {CONF_ACOUSTID_KEY: str(user_input.get(CONF_ACOUSTID_KEY) or "").strip(), CONF_ROOTS: ", ".join(roots)}
    return data, errors


def _schema(values: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Optional(CONF_ACOUSTID_KEY, default=values.get(CONF_ACOUSTID_KEY, "")): str,
        vol.Required(CONF_ROOTS, default=values.get(CONF_ROOTS, DEFAULT_ROOTS)): str,
    })


class LdmhaConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self.hass.async_add_executor_job(_check, user_input)
            if not errors:
                return self.async_create_entry(title="LDMHA", data={}, options=data)
        return self.async_show_form(step_id="user", data_schema=_schema(user_input or {}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return LdmhaOptionsFlow()


class LdmhaOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self.hass.async_add_executor_job(_check, user_input)
            if not errors:
                return self.async_create_entry(data=data)
        values = {**self.config_entry.options, **(user_input or {})}
        return self.async_show_form(step_id="init", data_schema=_schema(values), errors=errors)
