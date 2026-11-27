"""Impronta audio dei brani con fpcalc (Chromaprint): se Home Assistant non ce l'ha, LDMHA lo scarica una volta
dalla pagina ufficiale di Chromaprint (versione statica per Linux x86_64 o arm64) nella cartella di configurazione."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import platform
import shutil
import tarfile

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import FPCALC_URL, FPCALC_VERSION

_LOGGER = logging.getLogger(__name__)

ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}


class FpcalcError(Exception):
    """fpcalc assente o non riuscito."""


class Fpcalc:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.local = hass.config.path("ldmha", "fpcalc")
        self.path: str | None = None
        self._lock = asyncio.Lock()
        # al massimo due brani alla volta: fpcalc legge e decodifica il file (sul NAS anche via rete)
        self._sem = asyncio.Semaphore(2)

    def status(self) -> str:
        if self.path:
            return "pronto"
        if shutil.which("fpcalc") or os.path.isfile(self.local):
            return "pronto"
        if platform.machine().lower() not in ARCH:
            return f"non disponibile per {platform.machine()}"
        return "da scaricare"

    async def _ensure(self) -> str:
        async with self._lock:
            if self.path:
                return self.path
            found = shutil.which("fpcalc")
            if not found and os.path.isfile(self.local) and os.access(self.local, os.X_OK):
                found = self.local
            if not found:
                found = await self._download()
            self.path = found
            return found

    async def _download(self) -> str:
        arch = ARCH.get(platform.machine().lower())
        if not arch:
            raise FpcalcError(f"fpcalc non è disponibile per questo processore ({platform.machine()})")
        url = FPCALC_URL.format(v=FPCALC_VERSION, arch=arch)
        _LOGGER.info("Scarico fpcalc da %s", url)
        try:
            async with async_get_clientsession(self.hass).get(url, timeout=aiohttp.ClientTimeout(total=120)) as r:
                if r.status != 200:
                    raise FpcalcError(f"Download di fpcalc non riuscito (HTTP {r.status})")
                data = await r.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise FpcalcError(f"Download di fpcalc non riuscito: {err}") from err

        def _extract() -> None:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                member = next((m for m in tar.getmembers() if m.isfile() and m.name.endswith("/fpcalc")), None)
                if member is None:
                    raise FpcalcError("fpcalc non trovato nell'archivio scaricato")
                src = tar.extractfile(member)
                os.makedirs(os.path.dirname(self.local), exist_ok=True)
                tmp = self.local + ".tmp"
                with open(tmp, "wb") as f:
                    shutil.copyfileobj(src, f)
                os.chmod(tmp, 0o755)
                os.replace(tmp, self.local)

        await self.hass.async_add_executor_job(_extract)
        return self.local

    async def fingerprint(self, path: str) -> tuple[int, str]:
        """(durata in secondi, impronta) dei primi 2 minuti del brano."""
        exe = await self._ensure()
        async with self._sem:
            try:
                proc = await asyncio.create_subprocess_exec(
                    exe, "-json", "-length", "120", path,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, err = await asyncio.wait_for(proc.communicate(), 120)
            except TimeoutError as e:
                proc.kill()
                raise FpcalcError("fpcalc non ha finito in tempo") from e
            except OSError as e:
                self.path = None
                raise FpcalcError(f"fpcalc non parte: {e}") from e
        if proc.returncode != 0 and not out:
            raise FpcalcError((err.decode(errors="replace").strip().splitlines() or ["fpcalc non riuscito"])[-1])
        try:
            j = json.loads(out.decode())
            return int(round(float(j["duration"]))), str(j["fingerprint"])
        except (ValueError, KeyError) as e:
            raise FpcalcError("Risposta di fpcalc non valida") from e
