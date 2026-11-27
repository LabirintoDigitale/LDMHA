"""Riconoscimento con Shazam (libreria ShazamIO, servizio non ufficiale): seconda possibilità quando l'impronta di
AcoustID non trova niente. La libreria si installa da sola al primo uso (con le sue parti compilate per il
processore di Home Assistant); se non si installa o Shazam non risponde, si passa alla ricerca per nome."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

REQUIREMENT = "shazamio==0.9.0"


class ShazamError(Exception):
    """Shazam non disponibile o non riuscito."""


class ShazamRecognizer:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._ready: bool | None = None  # None = da provare, False = non installabile
        self._err = ""
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(1)  # una richiesta alla volta: servizio non ufficiale, meglio non insistere

    def status(self) -> str:
        return "pronto" if self._ready else ("non disponibile: " + self._err) if self._ready is False else "da installare"

    async def _ensure(self) -> None:
        async with self._lock:
            if self._ready:
                return
            if self._ready is False:
                raise ShazamError(self._err)
            try:
                from homeassistant.requirements import async_process_requirements

                await async_process_requirements(self.hass, "ldmha_shazam", [REQUIREMENT])
                await self.hass.async_add_executor_job(__import__, "shazamio")
                self._ready = True
            except Exception as err:  # noqa: BLE001 - qualunque problema d'installazione: si fa a meno di Shazam
                self._ready = False
                self._err = f"libreria ShazamIO non installabile ({err})"
                _LOGGER.warning("Shazam non disponibile: %s", err)
                raise ShazamError(self._err) from err

    async def recognize(self, path: str) -> dict[str, Any] | None:
        """Brano riconosciuto da Shazam (titolo, artista, album, anno, ISRC, copertina) o None."""
        await self._ensure()
        try:
            from shazamio import Shazam
        except Exception as err:  # noqa: BLE001
            raise ShazamError(f"libreria ShazamIO non caricabile ({err})") from err

        async with self._sem:
            try:
                shazam = Shazam(language="it-IT", endpoint_country="IT")
            except Exception as err:  # noqa: BLE001
                raise ShazamError(f"Shazam non avviabile ({err})") from err
            try:
                data = await asyncio.wait_for(shazam.recognize(path), 60)
            except TimeoutError as err:
                raise ShazamError("Shazam non ha risposto in tempo") from err
            except Exception as err:  # noqa: BLE001 - errori di rete o del servizio
                raise ShazamError(f"Shazam non riuscito: {err}") from err
            finally:
                try:
                    await shazam.close()
                except Exception:  # noqa: BLE001
                    pass
        track = (data if isinstance(data, dict) else {}).get("track") or {}
        if not track.get("title"):
            return None
        meta: dict[str, str] = {}
        for sec in track.get("sections") or []:
            for m in sec.get("metadata") or []:
                if m.get("title") and m.get("text"):
                    meta[m["title"].lower()] = m["text"]
        images = track.get("images") or {}
        year = next((meta[k] for k in ("released", "pubblicazione", "anno") if k in meta), "")[:4]
        return {
            "title": track.get("title", ""),
            "artist": track.get("subtitle", ""),
            "album": next((meta[k] for k in ("album",) if k in meta), ""),
            "year": year if year.isdigit() else "",
            "isrc": track.get("isrc", ""),
            "cover": images.get("coverarthq") or images.get("coverart") or "",
        }
