"""Lavori su tanti brani dentro Home Assistant (non nel browser): riconoscimento e Applica. Continuano anche
cambiando scheda, chiudendo il pannello o spegnendo il PC. Un lavoro alla volta.

- Ferma: il lavoro va in pausa, i brani rimasti restano salvati e si può riprendere (o annullare).
- Riavvio di Home Assistant (o ricarica dell'integrazione) durante un lavoro: al ritorno è in pausa da solo,
  con il motivo «riavvio», e non riparte senza che lo si chieda.
I pannelli aperti ricevono stato, brano in corso e risultati, e si riagganciano quando si riaprono."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .fpcalc import FpcalcError
from .ident import IdentError
from .library import LibraryError, read_tags

_LOGGER = logging.getLogger(__name__)
WORKERS = {"identify": 2, "apply": 1}  # applica uno alla volta: scrive e rinomina file sul NAS
LABEL = {"identify": "riconosco", "apply": "applico"}
PER_TRACK = 240  # secondi massimi per un brano: oltre, errore e si passa al successivo


class Jobs:
    def __init__(self, hass: HomeAssistant, data) -> None:
        self.hass = hass
        self.data = data  # LdmhaData (libreria, riconoscitore, ...)
        self.task: asyncio.Task | None = None
        self._store = Store(hass, 1, "ldmha.job")
        self.kind = ""
        self.pending: list[dict[str, Any]] = []  # brani ancora da fare (salvati: servono per riprendere)
        self.inflight: dict[str, dict[str, Any]] = {}  # in lavorazione adesso (salvati anche loro: dopo un riavvio si rifanno)
        self.state: dict[str, Any] = {"running": False, "paused": False, "done": 0, "total": 0, "label": ""}
        self.subs: dict[int, Callable[[dict], None]] = {}
        self._n = 0
        self._stop = False
        self._reason = "fermato"

    async def async_load(self) -> None:
        """Lavoro rimasto dalla volta prima: se stava andando, è stato interrotto da un riavvio → in pausa."""
        data = await self._store.async_load() or {}
        items = data.get("pending") or []
        if not items:
            return
        self.kind = data.get("kind", "identify")
        self.pending = items
        self.state = {"running": False, "paused": True, "kind": self.kind, "label": LABEL.get(self.kind, ""),
                      "done": data.get("done", 0), "total": data.get("total", len(items)),
                      "reason": "riavvio" if data.get("running") else data.get("reason", "fermato")}
        self._save(running=False)

    def _save(self, running: bool) -> None:
        self._store.async_delay_save(lambda: {
            "kind": self.kind, "pending": [*self.inflight.values(), *self.pending], "done": self.state.get("done", 0),
            "total": self.state.get("total", 0), "running": running, "reason": self.state.get("reason", "")}, 2)

    @callback
    def subscribe(self, send: Callable[[dict], None]) -> Callable[[], None]:
        self._n += 1
        n = self._n
        self.subs[n] = send
        send({"type": "state", **self.public()})

        @callback
        def unsub() -> None:
            self.subs.pop(n, None)

        return unsub

    def public(self) -> dict[str, Any]:
        return {**self.state, "kind": self.kind, "remaining": len(self.pending)}

    @callback
    def _emit(self, ev: dict) -> None:
        for s in list(self.subs.values()):
            try:
                s(ev)
            except Exception:  # noqa: BLE001 - pannello chiuso nel frattempo
                pass

    @callback
    def _on_step(self, path: str, text: str) -> None:
        if not self.state.get("running"):
            return
        self.state.setdefault("steps", {})[path] = text
        self._emit({"type": "step", "path": path, "step": text})

    # ------------------------------------------------------------ comandi
    def busy(self) -> bool:
        return bool(self.task and not self.task.done())

    def start(self, kind: str, items: list[dict[str, Any]], resume: bool = False) -> bool:
        if self.busy() or kind not in WORKERS:
            return False
        self._stop = False
        self._reason = "fermato"
        self.kind = kind
        self.pending = list(items)
        done = self.state.get("done", 0) if resume else 0
        total = self.state.get("total", 0) if resume else len(items)
        self.state = {"running": True, "paused": False, "label": LABEL[kind], "done": done, "total": total,
                      "working": [], "steps": {}, "t": time.time()}
        self.data.identifier.on_step = self._on_step
        self._save(running=True)
        self._emit({"type": "state", **self.public()})
        self.task = self.hass.async_create_background_task(self._run(), f"ldmha_{kind}")
        return True

    def resume(self) -> bool:
        if not self.pending or self.busy():
            return False
        return self.start(self.kind, self.pending, resume=True)

    def stop(self, reason: str = "fermato") -> None:
        self._reason = reason
        self._stop = True

    def cancel(self) -> None:
        """Butta via il lavoro in pausa (i brani rimasti non verranno fatti)."""
        if self.busy():
            self._stop = True
        self.pending = []
        self.state = {"running": False, "paused": False, "done": 0, "total": 0, "label": ""}
        self._store.async_delay_save(lambda: {}, 0)
        self._emit({"type": "state", **self.public()})

    # ------------------------------------------------------------ lavoro
    async def _one(self, item: dict[str, Any]) -> dict[str, Any]:
        path = item["path"]
        if self.kind == "identify":
            real = self.data.library.check(path)
            tags = await self.hass.async_add_executor_job(read_tags, real)
            try:
                res = await asyncio.wait_for(self.data.identifier.identify(real, tags), PER_TRACK)
            except TimeoutError as err:
                raise IdentError(f"troppo lento (oltre {PER_TRACK // 60} minuti): saltato") from err
            if self.data.library.db:
                await self.hass.async_add_executor_job(self.data.library.db.set_ident, real, {**res, "t": time.time()})
            return res
        from .ws_api import apply_one

        self._on_step(path, "scrivo tag e nome")
        return await asyncio.wait_for(
            apply_one(self.hass, self.data, path, item.get("tags") or {}, item.get("pattern") or "",
                      item.get("cover") or "", bool(item.get("cover_if_missing", True))), PER_TRACK)

    async def _run(self) -> None:
        async def worker() -> None:
            while self.pending and not self._stop:
                item = self.pending.pop(0)
                path = item["path"]
                self.inflight[path] = item
                ev: dict[str, Any] = {"type": "result", "kind": self.kind, "path": path}
                self.state["working"] = [*self.state.get("working", []), path][-WORKERS[self.kind]:]
                self._emit({"type": "working", "working": self.state["working"]})
                try:
                    ev["res"] = await self._one(item)
                except (LibraryError, IdentError, FpcalcError) as err:
                    ev["error"] = str(err)
                except Exception as err:  # noqa: BLE001 - un brano non deve fermare gli altri
                    _LOGGER.exception("%s di %s non riuscito", LABEL[self.kind], path)
                    ev["error"] = f"Errore imprevisto ({type(err).__name__}: {err})"
                self.inflight.pop(path, None)
                self.state["working"] = [p for p in self.state.get("working", []) if p != path]
                self.state.get("steps", {}).pop(path, None)
                self.state["done"] += 1
                self._save(running=True)
                self._emit({**ev, "done": self.state["done"], "total": self.state["total"]})

        try:
            await asyncio.gather(*(worker() for _ in range(WORKERS[self.kind])))
        except asyncio.CancelledError:
            self._reason = "riavvio"  # Home Assistant che si spegne: al ritorno il lavoro è in pausa
            raise
        finally:
            left = bool(self.pending or self.inflight)
            self.pending = [*self.inflight.values(), *self.pending]
            self.inflight = {}
            self.state = {**self.state, "running": False, "paused": left, "working": [], "steps": {},
                          "reason": self._reason if left else ""}
            if left:
                self._save(running=False)
            else:
                self._store.async_delay_save(lambda: {}, 0)
            self._emit({"type": "state", **self.public(), "finished": not left})
            _LOGGER.info("Lavoro «%s» %s: %s su %s", LABEL[self.kind], "in pausa" if left else "finito",
                         self.state["done"], self.state["total"])
