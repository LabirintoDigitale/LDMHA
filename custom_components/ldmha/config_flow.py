"""Configurazione guidata: chiave AcoustID (per riconoscere i brani dall'audio) e cartelle della musica."""

from __future__ import annotations

import os
import re
from typing import Any

import aiohttp

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_ACOUSTID_KEY, CONF_ROOTS, CONF_SHAZAM, DEFAULT_ROOTS, DOMAIN

# pagina dove si crea la chiave AcoustID (gratuita), mostrata nella descrizione del modulo
PLACEHOLDERS = {"acoustid_url": "https://acoustid.org/new-application"}


def parse_roots(text: str) -> list[str]:
    """Cartelle separate da virgola o a capo, senza doppioni."""
    out: list[str] = []
    for r in str(text or "").replace("\n", ",").split(","):
        r = r.strip().rstrip("/")
        if r and r not in out:
            out.append(r)
    return out


def clean_key(v: Any) -> str:
    """Chiave AcoustID senza spazi o caratteri invisibili copiati per sbaglio (le chiavi sono lettere e cifre)."""
    return re.sub(r"[^A-Za-z0-9]", "", str(v or ""))


async def key_problem(hass: HomeAssistant, key: str) -> str | None:
    """Chiede ad AcoustID se la chiave va bene (con un'impronta finta): "invalid_key" se la rifiuta, None se va bene
    o se AcoustID non risponde (in quel caso si accetta e lo si vedrà riconoscendo i brani)."""
    if not key:
        return None
    try:
        async with async_get_clientsession(hass).post(
            "https://api.acoustid.org/v2/lookup",
            data={"client": key, "duration": "30", "fingerprint": "AQAAAA", "format": "json"},
            timeout=aiohttp.ClientTimeout(total=15)) as r:
            j = await r.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    err = (j or {}).get("error") or {} if isinstance(j, dict) else {}
    if err.get("code") == 4 or "api key" in str(err.get("message", "")).lower():
        return "invalid_key"
    return None


def _check(user_input: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    errors: dict[str, str] = {}
    roots = parse_roots(user_input.get(CONF_ROOTS) or DEFAULT_ROOTS)
    if not roots or any(not r.startswith("/") for r in roots):
        errors[CONF_ROOTS] = "invalid_roots"
    elif not any(os.path.isdir(r) for r in roots):
        errors[CONF_ROOTS] = "no_roots"
    data = {CONF_ACOUSTID_KEY: clean_key(user_input.get(CONF_ACOUSTID_KEY)), CONF_ROOTS: ", ".join(roots),
            CONF_SHAZAM: bool(user_input.get(CONF_SHAZAM, True))}
    return data, errors


def _schema(values: dict[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Optional(CONF_ACOUSTID_KEY, default=values.get(CONF_ACOUSTID_KEY, "")): str,
        vol.Required(CONF_ROOTS, default=values.get(CONF_ROOTS, DEFAULT_ROOTS)): str,
        vol.Optional(CONF_SHAZAM, default=values.get(CONF_SHAZAM, True)): bool,
    })


class LdmhaConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self.hass.async_add_executor_job(_check, user_input)
            if not errors and (bad := await key_problem(self.hass, data[CONF_ACOUSTID_KEY])):
                errors[CONF_ACOUSTID_KEY] = bad
            if not errors:
                return self.async_create_entry(title="LDMHA", data={}, options=data)
        return self.async_show_form(step_id="user", data_schema=_schema(user_input or {}), errors=errors,
                                    description_placeholders=PLACEHOLDERS)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return LdmhaOptionsFlow()


class LdmhaOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self.hass.async_add_executor_job(_check, user_input)
            if not errors and (bad := await key_problem(self.hass, data[CONF_ACOUSTID_KEY])):
                errors[CONF_ACOUSTID_KEY] = bad
            if not errors:
                return self.async_create_entry(data=data)
        values = {**self.config_entry.options, **(user_input or {})}
        return self.async_show_form(step_id="init", data_schema=_schema(values), errors=errors,
                                    description_placeholders=PLACEHOLDERS)
