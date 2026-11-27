"""Riconoscimento con Shazam (libreria ShazamIO, servizio non ufficiale): seconda possibilità quando l'impronta di
AcoustID non trova niente. La libreria si installa da sola al primo uso (con le sue parti compilate per il
processore di Home Assistant); se non si installa o Shazam non risponde, si passa alla ricerca per nome."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

REQUIREMENT = "shazamio==0.9.0"
SEGMENT = 15  # secondi di ogni pezzo in più provato (Shazam ne ascolta circa 12)


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

    async def recognize(self, path: str, duration: int = 0, log: list[str] | None = None,
                        step=None) -> dict[str, Any] | None:
        """Brano riconosciuto da Shazam (titolo, artista, album, anno, ISRC, copertina) o None. Prima il pezzo che
        sceglie Shazam; se non lo riconosce si provano altri pezzi del brano (a 30 secondi, a metà, a tre quarti),
        ritagliati con ffmpeg, fino al primo riconosciuto."""
        log = log if log is not None else []
        step = step or (lambda _t: None)
        ffmpeg = shutil.which("ffmpeg")
        offsets = self._offsets(duration) if ffmpeg else []
        tries = 1 + len(offsets)
        step(f"Shazam tentativo 1 di {tries}")
        hit = await self._recognize_once(path)
        if hit:
            log.append("Shazam: riconosciuto al primo tentativo")
            return hit
        if not ffmpeg:
            log.append("Shazam: primo pezzo non riconosciuto; ffmpeg non c'è, niente altri pezzi")
            return None
        log.append(f"Shazam: primo pezzo non riconosciuto, provo altri {len(offsets)} pezzi")
        with tempfile.TemporaryDirectory(prefix="ldmha_shazam_") as tmp:
            for n, off in enumerate(offsets, 1):
                step(f"Shazam tentativo {n + 1} di {tries}")
                part = os.path.join(tmp, f"pezzo{n}.wav")
                if not await self._cut(ffmpeg, path, off, part):
                    log.append(f"Shazam: pezzo a {off // 60}:{off % 60:02d} non ritagliato")
                    continue
                hit = await self._recognize_once(part)
                if hit:
                    log.append(f"Shazam: riconosciuto con il pezzo a {off // 60}:{off % 60:02d}")
                    return hit
                log.append(f"Shazam: pezzo a {off // 60}:{off % 60:02d} non riconosciuto")
        return None

    @staticmethod
    def _offsets(duration: int) -> list[int]:
        """Punti da cui ritagliare: a 30 secondi, a metà e a tre quarti (senza uscire dal brano, senza doppioni)."""
        if duration and duration < SEGMENT + 5:
            return []
        pts = [30, duration // 2, duration * 3 // 4] if duration else [30, 60, 90]
        out: list[int] = []
        for p in pts:
            p = max(0, min(p, duration - SEGMENT) if duration else p)
            if all(abs(p - q) >= SEGMENT for q in out):
                out.append(p)
        return out

    @staticmethod
    async def _cut(ffmpeg: str, path: str, off: int, out: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg, "-loglevel", "error", "-y", "-ss", str(off), "-t", str(SEGMENT), "-i", path,
                "-ac", "1", "-ar", "16000", out, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), 60)
        except (OSError, TimeoutError):
            return False
        return proc.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 1000

    async def _recognize_once(self, path: str) -> dict[str, Any] | None:
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
