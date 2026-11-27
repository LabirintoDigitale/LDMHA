"""Comandi websocket del pannello: tutti solo per gli amministratori."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DATA_WS_REGISTERED, DOMAIN, VERSION
from .fpcalc import FpcalcError
from .ident import IdentError, guess_from_name
from .library import TAG_KEYS, LibraryError, build_name, read_cover, read_tags

_LOGGER = logging.getLogger(__name__)

TAGS = vol.Schema({vol.Optional(k): vol.Any(None, vol.Coerce(str)) for k in TAG_KEYS}, extra=vol.REMOVE_EXTRA)


@callback
def async_setup(hass: HomeAssistant) -> None:
    if hass.data.get(DATA_WS_REGISTERED):
        return
    for handler in (ws_player_register, ws_player_subscribe, ws_player_state, ws_info, ws_cover, ws_audio, ws_dirs, ws_list, ws_scan, ws_forget, ws_identify, ws_search, ws_preview, ws_apply, ws_trash, ws_history, ws_undo):
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
    except Exception as err:  # noqa: BLE001 - errore imprevisto: nome e messaggio al pannello, dettagli nel registro
        _LOGGER.exception("Errore imprevisto in LDMHA")
        import os
        import traceback

        # dove è successo (file e riga), così dal pannello si capisce subito cosa correggere
        where = " ← ".join(f"{os.path.basename(f.filename)}:{f.lineno}" for f in traceback.extract_tb(err.__traceback__)[-3:][::-1])
        connection.send_error(msg_id, "ldmha_error",
                              f"Errore imprevisto ({type(err).__name__}: {err}) in {where}. Dettagli nel registro di Home Assistant (cerca «ldmha»)")


# ---------------------------------------------------------------- lettori MusikHa (app sul telefono)
# Eccezione voluta (decisa dal proprietario): questi tre comandi NON sono solo per gli amministratori, perché l'app
# MusikHa può collegarsi con un utente qualsiasi. Fanno solo da lettore (registra sé stessa, riceve i comandi, manda
# lo stato) e non toccano i file della musica. Tutti gli altri comandi restano solo per gli admin.
_PID = vol.All(str, vol.Match(r"^[a-z0-9_]{6,40}$"))


@websocket_api.websocket_command({
    vol.Required("type"): "ldmha/player_register",
    vol.Required("player_id"): _PID,
    vol.Required("name"): vol.All(str, vol.Length(min=1, max=60)),
})
@websocket_api.async_response
async def ws_player_register(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    try:
        await _data(hass).players.register(msg["player_id"], msg["name"].strip())
    except LibraryError as err:
        connection.send_error(msg["id"], "ldmha_error", str(err))
        return
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "ldmha/player_subscribe", vol.Required("player_id"): _PID})
@callback
def ws_player_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """L'app resta in ascolto dei comandi per il suo lettore (play con l'indirizzo, pausa, stop, volume)."""
    try:
        players = _data(hass).players
    except LibraryError as err:
        connection.send_error(msg["id"], "ldmha_error", str(err))
        return
    mid = msg["id"]

    @callback
    def send(cmd: dict) -> None:
        connection.send_message(websocket_api.event_message(mid, cmd))

    connection.subscriptions[mid] = players.subscribe(msg["player_id"], send)
    connection.send_result(mid)


@websocket_api.websocket_command({
    vol.Required("type"): "ldmha/player_state",
    vol.Required("player_id"): _PID,
    vol.Optional("state"): vol.In(["idle", "buffering", "playing", "paused"]),
    vol.Optional("volume"): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
    vol.Optional("position"): vol.All(vol.Coerce(float), vol.Range(min=0)),
    vol.Optional("duration"): vol.All(vol.Coerce(float), vol.Range(min=0)),
})
@callback
def ws_player_state(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    st = {k: msg[k] for k in ("state", "volume", "position", "duration") if k in msg}
    try:
        _data(hass).players.set_state(msg["player_id"], st)
    except LibraryError as err:
        connection.send_error(msg["id"], "ldmha_error", str(err))
        return
    connection.send_result(msg["id"])


# ---------------------------------------------------------------- pannello (solo amministratori)
@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "ldmha/info"})
@websocket_api.async_response
async def ws_info(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    async def go() -> dict[str, Any]:
        d = _data(hass)
        roots = await hass.async_add_executor_job(d.library.real_roots)
        stats = await hass.async_add_executor_job(d.library.db.stats) if d.library.db else {}
        return {"version": VERSION, "roots": roots, "configured": d.library.roots, "db": stats,
                "acoustid": bool(d.identifier.key), "acoustid_error": d.identifier.key_error, "fpcalc": d.fpcalc.status(),
                "shazam": d.identifier.shazam.status() if d.identifier.shazam else "disattivato"}
    await _run(connection, msg["id"], go())


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "ldmha/dirs", vol.Required("path"): str})
@websocket_api.async_response
async def ws_dirs(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    async def go() -> dict[str, Any]:
        return await hass.async_add_executor_job(_data(hass).library.list_dirs, msg["path"])
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
@websocket_api.websocket_command({
    vol.Required("type"): "ldmha/scan",
    vol.Required("path"): str,
    vol.Optional("recursive", default=False): bool,
    vol.Optional("refresh", default=False): bool,
})
@websocket_api.async_response
async def ws_scan(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Lettura della cartella a blocchi: eventi start / files (a gruppi) / done / error; il pannello può fermarla."""
    try:
        lib = _data(hass).library
    except LibraryError as err:
        connection.send_error(msg["id"], "ldmha_error", str(err))
        return
    stopped = threading.Event()
    connection.subscriptions[msg["id"]] = stopped.set
    connection.send_result(msg["id"])

    def send(ev: dict[str, Any]) -> None:
        if not stopped.is_set():
            hass.loop.call_soon_threadsafe(connection.send_message, websocket_api.event_message(msg["id"], ev))

    try:
        await hass.async_add_executor_job(lib.scan, msg["path"], msg["recursive"], send, stopped.is_set,
                                          msg["refresh"])
    except LibraryError as err:
        send({"type": "error", "message": str(err)})
    except Exception as err:  # noqa: BLE001 - qualunque errore va mostrato nel pannello, non perso
        _LOGGER.exception("Lettura di %s non riuscita", msg["path"])
        send({"type": "error", "message": f"Lettura non riuscita: {err}"})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "ldmha/identify", vol.Required("path"): str})
@websocket_api.async_response
async def ws_identify(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    async def go() -> dict[str, Any]:
        d = _data(hass)
        real = d.library.check(msg["path"])
        tags = await hass.async_add_executor_job(read_tags, real)
        try:
            res = await d.identifier.identify(real, tags)
        except (IdentError, FpcalcError) as err:
            _LOGGER.warning("Riconoscimento di %s non riuscito: %s", real, err)
            raise
        if d.library.db:  # resta nell'archivio: riaprendo la cartella le proposte ci sono ancora
            await hass.async_add_executor_job(d.library.db.set_ident, real, {**res, "t": time.time()})
        return res
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
            artist, title, _hints = guess_from_name(real, await hass.async_add_executor_job(read_tags, real))
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
    vol.Optional("cover", default=""): str,  # indirizzo della copertina da mettere nel file (vuoto = non toccarla)
    vol.Optional("cover_if_missing", default=True): bool,  # solo se il file non ne ha già una
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
        url = msg["cover"]
        if url and msg["cover_if_missing"]:
            real = d.library.check(msg["path"])
            if await hass.async_add_executor_job(read_cover, real):
                url = ""  # il file ha già una copertina: non serve nemmeno scaricarla
        cover, warn = None, ""
        if url:
            try:
                cover = await _download_cover(hass, url)
            except LibraryError as err:  # copertina mancante: tag e nome si applicano lo stesso
                warn = f"copertina saltata: {err}"
        res = await hass.async_add_executor_job(d.library.apply, msg["path"], tags, name, cover, msg["cover_if_missing"])
        await d.library.async_commit()
        if warn:
            res["warning"] = "; ".join(x for x in (warn, res.get("warning", "")) if x)
        return res
    await _run(connection, msg["id"], go())


async def _download_cover(hass: HomeAssistant, url: str) -> tuple[bytes, str]:
    """Scarica la copertina scelta (versione grande se è della Cover Art Archive). Solo https, solo immagini."""
    import aiohttp

    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    if not url.startswith("https://"):
        raise LibraryError("Indirizzo della copertina non valido")
    url = url.replace("/front-250", "/front-500")
    try:
        async with async_get_clientsession(hass).get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                raise LibraryError(f"Copertina non scaricata (HTTP {r.status})")
            mime = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            if not mime.startswith("image/"):
                raise LibraryError("L'indirizzo della copertina non è un'immagine")
            data = await r.content.read(8 * 1024 * 1024 + 1)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise LibraryError(f"Copertina non scaricata: {err}") from err
    if len(data) > 8 * 1024 * 1024:
        raise LibraryError("Copertina troppo grande")
    return data, mime


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "ldmha/audio", vol.Required("path"): str})
@websocket_api.async_response
async def ws_audio(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Indirizzo firmato (valido 2 ore) per ascoltare il brano nel browser."""
    async def go() -> dict[str, Any]:
        from datetime import timedelta

        from homeassistant.components.http.auth import async_sign_path

        from .audio import audio_key

        real = await hass.async_add_executor_job(_data(hass).library.check, msg["path"])
        url = async_sign_path(hass, f"/api/ldmha/audio/{audio_key(real)}", timedelta(hours=2),
                              refresh_token_id=connection.refresh_token_id)
        return {"url": url}
    await _run(connection, msg["id"], go())


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "ldmha/cover", vol.Required("path"): str})
@websocket_api.async_response
async def ws_cover(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Copertina già dentro il file (per vederla nella scheda del brano), come immagine incorporata."""
    async def go() -> dict[str, Any]:
        import base64

        real = _data(hass).library.check(msg["path"])
        c = await hass.async_add_executor_job(read_cover, real)
        if not c:
            return {"cover": None}
        if len(c[0]) > 3 * 1024 * 1024:
            return {"cover": None, "size": len(c[0])}
        return {"cover": f"data:{c[1]};base64,{base64.b64encode(c[0]).decode('ascii')}", "size": len(c[0])}
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
@websocket_api.websocket_command({vol.Required("type"): "ldmha/forget"})
@websocket_api.async_response
async def ws_forget(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Svuota l'archivio (brani letti e riconoscimenti): la prossima apertura rilegge tutto dal disco."""
    async def go() -> dict[str, Any]:
        db = _data(hass).library.db
        if db:
            await hass.async_add_executor_job(db.clear)
        return {}
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
