"""Riconoscimento dei brani: impronta audio su AcoustID, poi i dettagli (album, anno, numero di traccia) da
MusicBrainz. Se l'impronta non trova niente (o manca la chiave) si cerca su MusicBrainz per nome e tag.
Si rispettano i limiti dei due servizi: AcoustID al massimo 3 richieste al secondo, MusicBrainz 1 al secondo."""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import USER_AGENT
from .fpcalc import Fpcalc, FpcalcError

ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"
MB_URL = "https://musicbrainz.org/ws/2"


class IdentError(Exception):
    """Errore mostrato nel pannello."""


class _Throttle:
    def __init__(self, gap: float) -> None:
        self.gap = gap
        self.last = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            d = self.last + self.gap - time.monotonic()
            if d > 0:
                await asyncio.sleep(d)
            self.last = time.monotonic()


def _artist(credits: list[dict] | None) -> str:
    """Nome dell'artista come lo scrive MusicBrainz (es. «Mina & Celentano»)."""
    out = ""
    for c in credits or []:
        out += (c.get("name") or c.get("artist", {}).get("name") or "") + (c.get("joinphrase") or "")
    return out.strip()


def _acoustid_artist(artists: list[dict] | None) -> str:
    out = ""
    for i, a in enumerate(artists or []):
        out += a.get("name", "") + (a.get("joinphrase") or ("" if i == len(artists) - 1 else ", "))
    return out.strip()


def _pick_release(releases: list[dict] | None) -> dict | None:
    """L'uscita più adatta: album ufficiale (non raccolta né dal vivo), poi la più vecchia."""
    def rank(r: dict) -> tuple:
        rg = r.get("release-group") or {}
        prim = (rg.get("primary-type") or "").lower()
        sec = rg.get("secondary-types") or []
        return (
            0 if (r.get("status") or "Official") == "Official" else 1,
            0 if prim == "album" and not sec else 1 if prim in ("single", "ep") and not sec else 2,
            r.get("date") or "9999",
        )
    rels = [r for r in releases or [] if isinstance(r, dict)]
    return min(rels, key=rank) if rels else None


def _track_no(rel: dict | None) -> str:
    if not rel:
        return ""
    for m in rel.get("media") or []:
        for t in m.get("track") or m.get("tracks") or []:
            n = t.get("number") or t.get("position")
            if n:
                return str(n)
        off = m.get("track-offset")
        if isinstance(off, int):
            return str(off + 1)
    return ""


def _from_mb_recording(rec: dict, score: float, source: str) -> dict[str, Any]:
    rel = _pick_release(rec.get("releases"))
    rg = (rel or {}).get("release-group") or {}
    return {
        "mbid": rec.get("id", ""),
        "title": rec.get("title", ""),
        "artist": _artist(rec.get("artist-credit")),
        "album": (rel or {}).get("title", "") or rg.get("title", ""),
        "albumartist": _artist((rel or {}).get("artist-credit")) or _artist(rec.get("artist-credit")),
        "year": ((rel or {}).get("date") or "")[:4],
        "track": _track_no(rel),
        "duration": round((rec.get("length") or 0) / 1000),
        "score": round(score, 3),
        "source": source,
    }


_NOISE = re.compile(r"\b(official|video|audio|lyrics?|hq|hd|remaster(ed)?|\d{3,4}kbps|320|mp3)\b", re.I)


def guess_from_name(path: str, tags: dict[str, str]) -> tuple[str, str]:
    """(artista, titolo) dai tag o, se mancano, dal nome del file («01 - Artista - Titolo (Official Video).mp3»)."""
    artist, title = tags.get("artist", "").strip(), tags.get("title", "").strip()
    if artist and title:
        return artist, title
    name = os.path.splitext(os.path.basename(path))[0]
    name = re.sub(r"[_]+", " ", name)
    name = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", name)
    name = _NOISE.sub(" ", name)
    name = re.sub(r"^\s*\d{1,3}\s*[-.)_ ]\s*", "", name)
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+|\s*[-–—]{2,}\s*", name) if p.strip()]
    if len(parts) >= 2:
        return artist or parts[0], title or " - ".join(parts[1:])
    folder = os.path.basename(os.path.dirname(path))
    return artist or (folder if not re.match(r"(?i)^(cd|disc)\s*\d", folder) else ""), title or name.strip()


def _lucene(s: str) -> str:
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", s)


class Identifier:
    def __init__(self, hass: HomeAssistant, fpcalc: Fpcalc, key: str) -> None:
        self.hass = hass
        self.fpcalc = fpcalc
        self.key = key
        self._ac = _Throttle(0.35)
        self._mb = _Throttle(1.1)
        self._mb_cache: dict[str, dict] = {}

    async def _get(self, url: str, params: dict[str, Any], throttle: _Throttle, method: str = "GET") -> dict:
        session = async_get_clientsession(self.hass)
        for attempt in range(3):
            await throttle.wait()
            try:
                kw = {"data": params} if method == "POST" else {"params": params}
                async with session.request(method, url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                                           timeout=aiohttp.ClientTimeout(total=30), **kw) as r:
                    if r.status in (429, 503) and attempt < 2:
                        await asyncio.sleep(2 + attempt * 3)
                        continue
                    data = await r.json(content_type=None)
                    if r.status >= 400:
                        msg = (data or {}).get("error", {})
                        msg = msg.get("message") if isinstance(msg, dict) else msg
                        raise IdentError(f"{url.split('/')[2]}: {msg or f'HTTP {r.status}'}")
                    return data or {}
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                if attempt == 2:
                    raise IdentError(f"{url.split('/')[2]} non risponde ({err.__class__.__name__})") from err
                await asyncio.sleep(2)
        return {}

    async def mb_recording(self, mbid: str) -> dict:
        if mbid not in self._mb_cache:
            self._mb_cache[mbid] = await self._get(
                f"{MB_URL}/recording/{mbid}", {"inc": "releases+release-groups+artist-credits+media", "fmt": "json"},
                self._mb)
            if len(self._mb_cache) > 500:
                self._mb_cache.pop(next(iter(self._mb_cache)))
        return self._mb_cache[mbid]

    async def by_fingerprint(self, path: str) -> tuple[int, list[dict[str, Any]]]:
        duration, fp = await self.fpcalc.fingerprint(path)
        if not self.key:
            return duration, []
        data = await self._get(ACOUSTID_URL, {"client": self.key, "duration": duration, "fingerprint": fp,
                                              "meta": "recordings releasegroups compress", "format": "json"},
                               self._ac, method="POST")
        if data.get("status") != "ok":
            err = data.get("error") or {}
            raise IdentError(f"AcoustID: {err.get('message') or 'errore'}")
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for res in sorted(data.get("results") or [], key=lambda r: -float(r.get("score") or 0)):
            score = float(res.get("score") or 0)
            for rec in res.get("recordings") or []:
                if not rec.get("title") or rec.get("id") in seen:
                    continue
                seen.add(rec["id"])
                rgs = rec.get("releasegroups") or []
                rg = next((g for g in rgs if (g.get("type") or "").lower() == "album" and not g.get("secondarytypes")),
                          rgs[0] if rgs else {})
                out.append({
                    "mbid": rec["id"], "title": rec.get("title", ""), "artist": _acoustid_artist(rec.get("artists")),
                    "album": rg.get("title", ""), "albumartist": _acoustid_artist(rg.get("artists")),
                    "year": "", "track": "", "duration": rec.get("duration") or 0,
                    "score": round(score, 3), "source": "impronta",
                })
        # stesso titolo e artista registrati più volte: resta il primo
        uniq: list[dict[str, Any]] = []
        keys: set[tuple] = set()
        for c in out:
            k = (c["title"].lower(), c["artist"].lower())
            if k not in keys:
                keys.add(k)
                uniq.append(c)
        return duration, uniq[:8]

    async def by_text(self, artist: str, title: str, query: str = "") -> list[dict[str, Any]]:
        if query:
            q = query
        elif artist and title:
            q = f'recording:"{_lucene(title)}" AND artist:"{_lucene(artist)}"'
        elif title:
            q = f'recording:"{_lucene(title)}"'
        else:
            return []
        data = await self._get(f"{MB_URL}/recording", {"query": q, "limit": 8, "fmt": "json"}, self._mb)
        return [_from_mb_recording(r, (r.get("score") or 0) / 100, "nome") for r in data.get("recordings") or []]

    async def enrich(self, cand: dict[str, Any]) -> dict[str, Any]:
        """Album, anno e numero di traccia da MusicBrainz per un candidato trovato con l'impronta."""
        if not cand.get("mbid"):
            return cand
        try:
            rec = await self.mb_recording(cand["mbid"])
        except IdentError:
            return cand
        full = _from_mb_recording(rec, cand.get("score", 0), cand.get("source", ""))
        return {**cand, **{k: v for k, v in full.items() if v and k not in ("score", "source")}}

    async def identify(self, path: str, tags: dict[str, str]) -> dict[str, Any]:
        duration, cands, note = 0, [], ""
        try:
            duration, cands = await self.by_fingerprint(path)
            if not self.key:
                note = "Senza chiave AcoustID il riconoscimento dall'audio non è attivo: cerco per nome e tag"
            elif not cands:
                note = "Impronta audio non trovata su AcoustID: cerco per nome e tag"
        except FpcalcError as err:
            note = f"Impronta audio non riuscita ({err}): cerco per nome e tag"
        if cands:
            cands[0] = await self.enrich(cands[0])
        else:
            artist, title = guess_from_name(path, tags)
            cands = await self.by_text(artist, title)
            if not cands and artist and title:
                cands = await self.by_text("", f"{artist} {title}", query=f"{_lucene(artist)} {_lucene(title)}")
            # a parità di nome, meglio la registrazione con la durata più vicina a quella del file
            if duration:
                for c in cands:
                    if c.get("duration") and abs(c["duration"] - duration) > 15:
                        c["score"] = round(c["score"] * 0.6, 3)
                cands.sort(key=lambda c: -c["score"])
        return {"duration": duration, "candidates": cands, "note": note}
