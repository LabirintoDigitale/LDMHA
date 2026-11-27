"""Riconoscimento di tanti brani dentro Home Assistant (non nel browser): continua anche cambiando scheda,
chiudendo il pannello o spegnendo il PC. Un lavoro alla volta, due brani insieme; ogni risultato va subito
nell'archivio e ai pannelli aperti, che possono riagganciarsi in qualunque momento per vedere l'avanzamento."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .fpcalc import FpcalcError
from .ident import IdentError
from .library import LibraryError, read_tags

_LOGGER = logging.getLogger(__name__)
WORKERS = 2
PER_TRACK = 240  # secondi massimi per un brano: oltre, errore e si passa al successivo


class Jobs:
    def __init__(self, hass: HomeAssistant, data) -> None:
        self.hass = hass
        self.data = data  # LdmhaData (libreria e riconoscitore)
        self.task: asyncio.Task | None = None
        self.state: dict[str, Any] = {"running": False, "done": 0, "total": 0, "label": "", "t": 0}
        self.subs: dict[int, Callable[[dict], None]] = {}
        self._n = 0
        self._stop = False

    @callback
    def subscribe(self, send: Callable[[dict], None]) -> Callable[[], None]:
        self._n += 1
        n = self._n
        self.subs[n] = send
        send({"type": "state", **self.state})

        @callback
        def unsub() -> None:
            self.subs.pop(n, None)

        return unsub

    @callback
    def _emit(self, ev: dict) -> None:
        for s in list(self.subs.values()):
            try:
                s(ev)
            except Exception:  # noqa: BLE001 - pannello chiuso nel frattempo
                pass

    def start(self, paths: list[str]) -> bool:
        if self.task and not self.task.done():
            return False
        self._stop = False
        self.state = {"running": True, "done": 0, "total": len(paths), "label": "riconosco", "t": time.time()}
        self._emit({"type": "state", **self.state})
        self.task = self.hass.async_create_background_task(self._run(paths), "ldmha_riconoscimento")
        return True

    def stop(self) -> None:
        self._stop = True

    async def _run(self, paths: list[str]) -> None:
        queue = list(paths)

        async def worker() -> None:
            while queue and not self._stop:
                path = queue.pop(0)
                ev: dict[str, Any] = {"type": "result", "path": path}
                self.state.setdefault("working", [])
                self.state["working"] = [*self.state["working"], path][-WORKERS:]
                self._emit({"type": "working", "working": self.state["working"]})
                try:
                    real = self.data.library.check(path)
                    tags = await self.hass.async_add_executor_job(read_tags, real)
                    try:
                        res = await asyncio.wait_for(self.data.identifier.identify(real, tags), PER_TRACK)
                    except TimeoutError as err:
                        raise IdentError(f"troppo lento (oltre {PER_TRACK // 60} minuti): saltato") from err
                    if self.data.library.db:
                        await self.hass.async_add_executor_job(self.data.library.db.set_ident, real,
                                                               {**res, "t": time.time()})
                    ev["res"] = res
                except (LibraryError, IdentError, FpcalcError) as err:
                    ev["error"] = str(err)
                except Exception as err:  # noqa: BLE001 - un brano non deve fermare gli altri
                    _LOGGER.exception("Riconoscimento di %s non riuscito", path)
                    ev["error"] = f"Errore imprevisto ({type(err).__name__}: {err})"
                self.state["working"] = [p for p in self.state.get("working", []) if p != path]
                self.state["done"] += 1
                self._emit({**ev, "done": self.state["done"], "total": self.state["total"]})

        try:
            await asyncio.gather(*(worker() for _ in range(WORKERS)))
        finally:
            stopped = self._stop
            self.state = {**self.state, "running": False, "stopped": stopped}
            self._emit({"type": "state", **self.state})
            _LOGGER.info("Riconoscimento %s: %s brani su %s", "fermato" if stopped else "finito",
                         self.state["done"], self.state["total"])
