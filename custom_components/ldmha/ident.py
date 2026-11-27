"""Riconoscimento dei brani: impronta audio su AcoustID, poi i dettagli (album, anno, numero di traccia) da
MusicBrainz. Se l'impronta non trova niente (o manca la chiave) si cerca su MusicBrainz per nome e tag.
Si rispettano i limiti dei due servizi: AcoustID al massimo 3 richieste al secondo, MusicBrainz 1 al secondo."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import USER_AGENT
from .fpcalc import Fpcalc, FpcalcError
from .shazam import ShazamError

_LOGGER = logging.getLogger(__name__)

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


CAA = "https://coverartarchive.org"


def _cover_rel(rel_id: str) -> str:
    """Copertina dalla Cover Art Archive di MusicBrainz (piccola, per il pannello; per il file si usa la grande)."""
    return f"{CAA}/release/{rel_id}/front-250" if rel_id else ""


def _cover_rg(rg_id: str) -> str:
    return f"{CAA}/release-group/{rg_id}/front-250" if rg_id else ""


def _from_mb_recording(rec: dict, score: float, source: str) -> dict[str, Any]:
    rel = _pick_release(rec.get("releases"))
    rg = (rel or {}).get("release-group") or {}
    return {
        "cover": _cover_rel((rel or {}).get("id", "")) or _cover_rg(rg.get("id", "")),
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
    def __init__(self, hass: HomeAssistant, fpcalc: Fpcalc, key: str, shazam=None) -> None:
        self.hass = hass
        self.fpcalc = fpcalc
        self.key = key
        self.shazam = shazam  # ShazamRecognizer o None (disattivato nelle opzioni)
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

    async def by_fingerprint(self, path: str, log: list[str] | None = None) -> tuple[int, list[dict[str, Any]]]:
        log = log if log is not None else []
        t0 = time.monotonic()
        duration, fp = await self.fpcalc.fingerprint(path)
        log.append(f"Impronta audio calcolata in {time.monotonic() - t0:.1f} s (primi {min(duration, 120)} s di "
                   f"{duration // 60}:{duration % 60:02d})")
        if not self.key:
            log.append("Nessuna chiave AcoustID: l'impronta non si può cercare")
            return duration, []
        t0 = time.monotonic()
        data = await self._get(ACOUSTID_URL, {"client": self.key, "duration": duration, "fingerprint": fp,
                                              "meta": "recordings releasegroups compress", "format": "json"},
                               self._ac, method="POST")
        if data.get("status") != "ok":
            err = data.get("error") or {}
            log.append(f"AcoustID ha risposto con un errore: {err.get('message') or 'sconosciuto'}")
            raise IdentError(f"AcoustID: {err.get('message') or 'errore'}")
        res = data.get("results") or []
        best = max((float(r.get("score") or 0) for r in res), default=0)
        with_rec = sum(1 for r in res if r.get("recordings"))
        log.append(f"AcoustID ({time.monotonic() - t0:.1f} s): {len(res)} impronte simili"
                   + (f", la migliore al {round(best * 100)}%, {with_rec} con il brano collegato" if res else
                      " — brano sconosciuto ad AcoustID"))
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
                    "score": round(score, 3), "source": "impronta", "cover": _cover_rg(rg.get("id", "")),
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

    async def by_shazam(self, path: str, log: list[str]) -> list[dict[str, Any]]:
        """Seconda possibilità con Shazam; i dettagli (album, anno, numero) da MusicBrainz tramite il codice ISRC."""
        t0 = time.monotonic()
        try:
            hit = await self.shazam.recognize(path)
        except ShazamError as err:
            log.append(f"Shazam: {err}")
            return []
        if not hit:
            log.append(f"Shazam ({time.monotonic() - t0:.1f} s): brano non riconosciuto")
            return []
        log.append(f"Shazam ({time.monotonic() - t0:.1f} s): «{hit['artist']} — {hit['title']}»"
                   + (f", album «{hit['album']}»" if hit["album"] else "") + (f", ISRC {hit['isrc']}" if hit["isrc"] else ""))
        # Shazam non dà una percentuale: 85%, sotto la soglia di «Applica i sicuri» (si controlla prima di applicare)
        cand = {"mbid": "", "title": hit["title"], "artist": hit["artist"], "album": hit["album"], "albumartist": "",
                "year": hit["year"], "track": "", "duration": 0, "score": 0.85, "source": "shazam", "cover": hit["cover"]}
        if hit["isrc"]:
            try:
                data = await self._get(f"{MB_URL}/isrc/{hit['isrc']}",
                                       {"inc": "releases+release-groups+artist-credits+media", "fmt": "json"}, self._mb)
                recs = data.get("recordings") or []
                if recs:
                    mb = _from_mb_recording(recs[0], 0.85, "shazam")
                    cand = {**cand, **{k: v for k, v in mb.items() if v and k not in ("score", "source", "cover")},
                            "cover": cand["cover"] or mb.get("cover", "")}
                    log.append(f"MusicBrainz: ISRC trovato, aggiunti album, anno e numero di traccia")
                else:
                    log.append("MusicBrainz: ISRC non presente, restano i dati di Shazam")
            except IdentError as err:
                log.append(f"MusicBrainz non disponibile per l'ISRC: {err}")
        return [cand]

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
        duration, cands, note, log = 0, [], "", []
        t_all = time.monotonic()
        try:
            duration, cands = await self.by_fingerprint(path, log)
            if not self.key:
                note = "Senza chiave AcoustID il riconoscimento dall'audio non è attivo: cerco per nome e tag"
            elif not cands:
                note = "Impronta audio non trovata su AcoustID: provo " + ("Shazam, poi " if self.shazam else "") + "per nome e tag"
        except FpcalcError as err:
            log.append(f"Impronta audio non riuscita: {err}")
            note = f"Impronta audio non riuscita ({err}): cerco per nome e tag"
        except IdentError as err:
            log.append(f"AcoustID non disponibile: {err}")
            note = f"{err}: cerco per nome e tag"
        if cands:
            log.append(f"Proposte dall'audio: {len(cands)}; la prima è «{cands[0]['artist']} — {cands[0]['title']}»")
            t0 = time.monotonic()
            before = dict(cands[0])
            cands[0] = await self.enrich(cands[0])
            got = [k for k in ("album", "year", "track") if cands[0].get(k) and cands[0].get(k) != before.get(k)]
            log.append(f"MusicBrainz ({time.monotonic() - t0:.1f} s): "
                       + (f"aggiunti {', '.join({'album': 'album', 'year': 'anno', 'track': 'numero di traccia'}[k] for k in got)}"
                          if got else "nessun dettaglio in più"))
        elif self.shazam is not None:
            cands = await self.by_shazam(path, log)
            if cands:
                note = "Impronta non trovata su AcoustID: riconosciuto da Shazam (controlla prima di applicare)"
        if not cands:
            artist, title = guess_from_name(path, tags)
            log.append(f"Ricerca per nome su MusicBrainz: artista «{artist or '?'}», titolo «{title or '?'}»"
                       + (" (dai tag)" if tags.get("artist") and tags.get("title") else " (dal nome del file)"))
            t0 = time.monotonic()
            try:
                cands = await self.by_text(artist, title)
                if not cands and artist and title:
                    log.append("Nessun risultato esatto: riprovo con una ricerca libera")
                    cands = await self.by_text("", f"{artist} {title}", query=f"{_lucene(artist)} {_lucene(title)}")
                log.append(f"MusicBrainz ({time.monotonic() - t0:.1f} s): {len(cands)} risultati")
            except IdentError as err:
                log.append(f"MusicBrainz non disponibile: {err}")
                note = (note + " — " if note else "") + str(err)
                cands = []
            # a parità di nome, meglio la registrazione con la durata più vicina a quella del file
            if duration:
                far = 0
                for c in cands:
                    if c.get("duration") and abs(c["duration"] - duration) > 15:
                        c["score"] = round(c["score"] * 0.6, 3)
                        far += 1
                if far:
                    log.append(f"{far} risultati hanno una durata diversa dal file (oltre 15 s): sicurezza ridotta")
                cands.sort(key=lambda c: -c["score"])
        log.append(f"Fatto in {time.monotonic() - t_all:.1f} s: "
                   + (f"migliore «{cands[0]['artist']} — {cands[0]['title']}» al {round(cands[0]['score'] * 100)}%"
                      if cands else "nessuna proposta"))
        _LOGGER.debug("Riconoscimento di %s: %s", path, " | ".join(log))
        return {"duration": duration, "candidates": cands, "note": note, "log": log}
