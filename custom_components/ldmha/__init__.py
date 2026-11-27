"""LDMHA: pulizia della musica dalla barra laterale di Home Assistant (solo amministratori). Riconosce i brani
dall'audio (AcoustID + MusicBrainz), corregge i tag, rinomina i file e toglie i doppioni."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from . import ws_api
from .config_flow import parse_roots
from .const import (
    CONF_ACOUSTID_KEY,
    CONF_ROOTS,
    DATA_STATIC_REGISTERED,
    DEFAULT_ROOTS,
    DOMAIN,
    PANEL_ELEMENT,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL,
    STATIC_URL,
    VERSION,
)
from .fpcalc import Fpcalc
from .ident import Identifier
from .library import Library


@dataclass
class LdmhaData:
    library: Library
    fpcalc: Fpcalc
    identifier: Identifier


type LdmhaConfigEntry = ConfigEntry[LdmhaData]


async def async_setup_entry(hass: HomeAssistant, entry: LdmhaConfigEntry) -> bool:
    opts = entry.options
    library = Library(hass, parse_roots(opts.get(CONF_ROOTS) or DEFAULT_ROOTS))
    await library.async_load()
    fp = Fpcalc(hass)
    entry.runtime_data = LdmhaData(library, fp, Identifier(hass, fp, opts.get(CONF_ACOUSTID_KEY, "")))
    hass.data[DOMAIN] = entry

    # "servizio" nella scheda dell'integrazione: mostra la versione installata
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "ldmha")},
        name="LDMHA",
        manufacturer="LabirintoDigitale",
        model="Pulizia della musica (AcoustID + MusicBrainz)",
        sw_version=VERSION,
        entry_type=dr.DeviceEntryType.SERVICE,
        configuration_url="https://github.com/LabirintoDigitale/LDMHA",
    )

    if not hass.data.get(DATA_STATIC_REGISTERED):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), False)]
        )
        hass.data[DATA_STATIC_REGISTERED] = True

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=f"{STATIC_URL}/ldmha-panel.js?v={VERSION}",
        embed_iframe=False,
        require_admin=True,  # nella barra laterale solo per gli amministratori
        config={},
    )
    ws_api.async_setup(hass)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: LdmhaConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LdmhaConfigEntry) -> bool:
    frontend.async_remove_panel(hass, PANEL_URL)
    hass.data.pop(DOMAIN, None)
    return True
