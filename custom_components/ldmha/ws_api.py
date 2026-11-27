"""Comandi websocket del pannello: tutti solo per gli amministratori."""

from __future__ import annotations

import os
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DATA_WS_REGISTERED, DOMAIN, VERSION
from .fpcalc import FpcalcError
from .ident import IdentError, guess_from_name
from .library import TAG_KEYS, LibraryError, build_name, read_tags

TAGS = vol.Schema({vol.Optional(k): vol.Any(None, vol.Coerce(str)) for k in TAG_KEYS}, extra=vol.REMOVE_EXTRA)


@callback
def async_setup(hass: HomeAssistant) -> None:
    if hass.data.get(DATA_WS_REGISTERED):
        return
    for handler in (ws_info, ws_list, ws_identify, ws_search, ws_preview, ws_apply, ws_trash, ws_history, ws_undo):
        websocket_api.async_register_command(hass, handler)
    hass.data[DATA_WS_REGISTERED] = True


def _data(hass: HomeAssistant):
    entry = hass.data.get(DOMAIN)
    if entry is None:
        raise LibraryError("LDMHA non è configurato")
    return entry.runtime_data


async def _run(connection, msg_id: int, coro) -> None:
    try:
        connection.send_result(msg_id, await coro)
    except (LibraryError, IdentError, FpcalcError) as err:
        connection.send_error(msg_id, "ldmha_error", str(err))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "ldmha/info"})
@websocket_api.async_response
async def ws_info(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    async def go() -> dict[str, Any]:
        d = _data(hass)
        roots = await hass.async_add_executor_job(d.library.real_roots)
        return {"version": VERSION, "roots": roots, "configured": d.library.roots,
                "acoustid": bool(d.identifier.key), "fpcalc": d.fpcalc.status()}
    await _run(connection, msg["id"], go())


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "ldmha/list",
    vol.Required("path"): str,
    vol.Optional("recursive", default=False): bool,
})
@websocket_api.async_response
async def ws_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    async def go() -> dict[str, Any]:
        return await hass.async_add_executor_job(_data(hass).library.list_dir, msg["path"], msg["recursive"])
    await _run(connection, msg["id"], go())


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "ldmha/identify", vol.Required("path"): str})
@websocket_api.async_response
async def ws_identify(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    async def go() -> dict[str, Any]:
        d = _data(hass)
        real = d.library.check(msg["path"])
        tags = await hass.async_add_executor_job(read_tags, real)
        return await d.identifier.identify(real, tags)
    await _run(connection, msg["id"], go())


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "ldmha/search",
    vol.Optional("path"): str,
    vol.Optional("artist", default=""): str,
    vol.Optional("title", default=""): str,
    vol.Optional("query", default=""): str,
})
@websocket_api.async_response
async def ws_search(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Ricerca a mano su MusicBrainz (artista e titolo, o testo libero)."""
    async def go() -> dict[str, Any]:
        d = _data(hass)
        artist, title, query = msg["artist"].strip(), msg["title"].strip(), msg["query"].strip()
        if not (artist or title or query) and msg.get("path"):
            real = d.library.check(msg["path"])
            artist, title = guess_from_name(real, await hass.async_add_executor_job(read_tags, real))
        if query:
            cands = await d.identifier.by_text("", "", query=query)
        else:
            cands = await d.identifier.by_text(artist, title)
        return {"candidates": cands}
    await _run(connection, msg["id"], go())


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "ldmha/preview",
    vol.Required("items"): [vol.Schema({vol.Required("path"): str, vol.Required("tags"): TAGS}, extra=vol.REMOVE_EXTRA)],
    vol.Required("pattern"): str,
})
@callback
def ws_preview(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Nomi nuovi dallo schema (senza toccare niente)."""
    out = []
    for it in msg["items"]:
        try:
            ext = os.path.splitext(it["path"])[1]
            out.append({"path": it["path"], "name": build_name(msg["pattern"], it["tags"], ext)})
        except LibraryError as err:
            out.append({"path": it["path"], "error": str(err)})
    connection.send_result(msg["id"], {"items": out})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "ldmha/apply",
    vol.Required("path"): str,
    vol.Optional("tags", default={}): TAGS,
    vol.Optional("pattern", default=""): str,
})
@websocket_api.async_response
async def ws_apply(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Scrive i tag e (con uno schema) dà il nome nuovo al file."""
    async def go() -> dict[str, Any]:
        d = _data(hass)
        tags = {k: v for k, v in msg["tags"].items() if v}
        name = None
        if msg["pattern"].strip():
            cur = await hass.async_add_executor_job(read_tags, d.library.check(msg["path"]))
            name = build_name(msg["pattern"], {**cur, **tags}, os.path.splitext(msg["path"])[1])
        res = await hass.async_add_executor_job(d.library.apply, msg["path"], tags, name)
        await d.library.async_commit()
        return res
    await _run(connection, msg["id"], go())


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "ldmha/trash", vol.Required("path"): str})
@websocket_api.async_response
async def ws_trash(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    async def go() -> dict[str, Any]:
        d = _data(hass)
        res = await hass.async_add_executor_job(d.library.trash, msg["path"])
        await d.library.async_commit()
        return res
    await _run(connection, msg["id"], go())


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "ldmha/history"})
@callback
def ws_history(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not hass.data.get(DOMAIN):
        connection.send_error(msg["id"], "ldmha_error", "LDMHA non è configurato")
        return
    connection.send_result(msg["id"], {"history": _data(hass).library.recent()})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "ldmha/undo", vol.Required("id"): str})
@websocket_api.async_response
async def ws_undo(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    async def go() -> dict[str, Any]:
        d = _data(hass)
        res = await hass.async_add_executor_job(d.library.undo, msg["id"])
        await d.library.async_commit()
        return res
    await _run(connection, msg["id"], go())
