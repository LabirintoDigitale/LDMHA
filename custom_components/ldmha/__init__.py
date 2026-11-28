"""LDMHA: pulizia della musica dalla barra laterale di Home Assistant (solo amministratori). Riconosce i brani
dall'audio (AcoustID + MusicBrainz), corregge i tag, rinomina i file e toglie i doppioni."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from . import ws_api
from .config_flow import clean_key, parse_roots
from .const import (
    CONF_ACOUSTID_KEY,
    CONF_ROOTS,
    CONF_SHAZAM,
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
from .db import Db
from .fpcalc import Fpcalc
from .ident import Identifier
from .library import Library
from .audio import AudioView
from .players import Players, StreamProxyView
from .shazam import ShazamRecognizer

PLATFORMS = ["media_player"]


@dataclass
class LdmhaData:
    library: Library
    fpcalc: Fpcalc
    identifier: Identifier
    players: Players
    jobs: Any = None  # Jobs: riconoscimento di tanti brani dentro Home Assistant
    ratings: Any = None  # Ratings: stelle date da MusikHa


type LdmhaConfigEntry = ConfigEntry[LdmhaData]


async def async_setup_entry(hass: HomeAssistant, entry: LdmhaConfigEntry) -> bool:
    opts = entry.options
    db = await hass.async_add_executor_job(Db, hass.config.path("ldmha", "ldmha.db"))
    entry.async_on_unload(lambda: hass.async_add_executor_job(db.close))
    library = Library(hass, parse_roots(opts.get(CONF_ROOTS) or DEFAULT_ROOTS), db)
    await library.async_load()
    fp = Fpcalc(hass)
    players = Players(hass, entry.entry_id)  # lettori MusikHa (musica sul telefono)
    await players.async_load()
    entry.runtime_data = LdmhaData(library, fp, Identifier(
        hass, fp, clean_key(opts.get(CONF_ACOUSTID_KEY, "")), ShazamRecognizer(hass) if opts.get(CONF_SHAZAM, True) else None), players)
    from .jobs import Jobs

    entry.runtime_data.jobs = Jobs(hass, entry.runtime_data)
    from .ratings import Ratings

    entry.runtime_data.ratings = Ratings(library)
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
        hass.http.register_view(StreamProxyView())  # audio per i lettori MusikHa
        hass.http.register_view(AudioView())  # ascolto dal pannello
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
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: LdmhaConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LdmhaConfigEntry) -> bool:
    if entry.runtime_data.jobs:
        entry.runtime_data.jobs.stop()
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    frontend.async_remove_panel(hass, PANEL_URL)
    hass.data.pop(DOMAIN, None)
    return ok


async def async_remove_config_entry_device(hass: HomeAssistant, entry: LdmhaConfigEntry, device) -> bool:
    """Un lettore MusikHa (telefono) si può eliminare da Home Assistant: se il telefono si ricollega torna."""
    for dom, ident in device.identifiers:
        if dom == DOMAIN and ident.startswith("player_"):
            pid = ident[7:]
            await entry.runtime_data.players.forget(pid)
            entry.runtime_data.players.entities.pop(pid, None)
            return True
    return False
