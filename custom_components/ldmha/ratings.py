"""Valutazioni a stelle (1-5) date dall'app MusikHa al brano in ascolto, per ogni utente di Home Assistant.

- Nell'archivio: un voto per utente e brano.
- Nel file: prima le stelle «generali» (media arrotondata dei voti, quelle che leggono gli altri programmi), poi un
  campo per ogni utente. MP3: frame POPM (il primo senza nome, poi «ldmha:<utente>»); FLAC/OGG: RATING e
  RATING:<utente>; M4A: campi RATING liberi.
- Playlist .m3u sul NAS (cartella «Playlist LDMHA» nella cartella della musica): Music Assistant le legge come le
  altre playlist. Per ogni utente «5 stelle» e «4 stelle o più», e le stesse per la media di tutti.

Il voto è aperto a tutti gli utenti (anche non amministratori) ma tocca solo il campo valutazione del file che sta
suonando sul lettore indicato."""

from __future__ import annotations

import os
import re
import time
from typing import Any

from .library import LibraryError

POPM_STARS = {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}  # scala usata da Windows Media Player e da molti programmi
PLAYLIST_DIR = "Playlist LDMHA"


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-() ]+", "_", name or "utente").strip() or "utente"


def write_rating_tags(path: str, avg: int, per_user: dict[str, int]) -> None:
    """Stelle generali (avg, 0 = nessuna) e per utente nel file."""
    from mutagen import File

    try:
        f = File(path)
    except Exception as err:
        raise LibraryError(f"File non leggibile: {err}") from err
    if f is None:
        raise LibraryError("Formato senza tag")
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import POPM

            if f.tags is None:
                f.add_tags()
            f.tags.delall("POPM")
            if avg:
                f.tags.add(POPM(email="", rating=POPM_STARS[avg], count=0))  # la prima, sempre valida
            for user, stars in sorted(per_user.items()):
                f.tags.add(POPM(email=f"ldmha:{user}", rating=POPM_STARS[stars], count=0))
        elif ext in (".m4a", ".mp4", ".aac"):
            from mutagen.mp4 import MP4FreeForm

            if f.tags is None:
                f.add_tags()
            for k in [k for k in f.tags.keys() if k.startswith("----:com.apple.iTunes:RATING")]:
                del f.tags[k]
            if avg:
                f.tags["----:com.apple.iTunes:RATING"] = [MP4FreeForm(str(avg).encode())]
            for user, stars in per_user.items():
                f.tags[f"----:com.apple.iTunes:RATING:{user}"] = [MP4FreeForm(str(stars).encode())]
        elif hasattr(f, "tags") and f.tags is not None and hasattr(f.tags, "keys"):  # FLAC, OGG, Opus (Vorbis)
            for k in [k for k in f.tags.keys() if k.upper() == "RATING" or k.upper().startswith("RATING:")]:
                del f.tags[k]
            if avg:
                f.tags["RATING"] = str(avg)
            for user, stars in per_user.items():
                f.tags[f"RATING:{user}"] = str(stars)
        else:
            raise LibraryError(f"Valutazione non supportata per i file {ext}")
        f.save()
    except LibraryError:
        raise
    except Exception as err:
        raise LibraryError(f"Valutazione non salvata nel file: {err}") from err


class Ratings:
    def __init__(self, library) -> None:
        self.library = library  # Library (con l'archivio db)
        self._prefix: dict[str, str] = {}  # provider di Music Assistant -> cartella che corrisponde alla condivisione

    # ------------------------------------------------------------ brano in ascolto -> file
    def resolve(self, provider: str, item_id: str) -> str:
        """Percorso del file in Home Assistant per il brano che Music Assistant sta suonando (percorso nella
        condivisione del NAS): si cerca sotto le cartelle della musica, anche un livello più in basso
        (es. /media/Nas_Musica), e si ricorda dove l'ha trovato."""
        item_id = item_id.lstrip("/")
        if ".." in item_id.split("/"):
            raise LibraryError("Percorso non valido")
        tries = []
        if provider in self._prefix:
            tries.append(self._prefix[provider])
        for root in self.library.real_roots():
            tries.append(root)
            try:
                tries += [e.path for e in os.scandir(root) if e.is_dir() and not e.name.startswith(".")]
            except OSError:
                pass
        for base in tries:
            p = os.path.join(base, item_id)
            if os.path.isfile(p):
                self._prefix[provider] = base
                return self.library.check(p)
        raise LibraryError("Il brano in ascolto non è in una cartella della musica di LDMHA")

    # ------------------------------------------------------------ voti
    def votes(self, path: str) -> list[dict[str, Any]]:
        return self.library.db.ratings_of(path)

    def summary(self, path: str, user_id: str) -> dict[str, Any]:
        v = self.votes(path)
        mine = next((x["stars"] for x in v if x["user_id"] == user_id), 0)
        avg = round(sum(x["stars"] for x in v) / len(v), 1) if v else 0
        return {"path": path, "name": os.path.basename(path), "mine": mine, "avg": avg, "count": len(v),
                "votes": [{"user": x["user_name"], "stars": x["stars"]} for x in v]}

    def vote(self, path: str, user_id: str, user_name: str, stars: int) -> dict[str, Any]:
        db = self.library.db
        if stars:
            db.set_rating(path, user_id, user_name, stars)
        else:
            db.del_rating(path, user_id)
        v = self.votes(path)
        avg = int(round(sum(x["stars"] for x in v) / len(v) + 1e-9)) if v else 0
        write_rating_tags(path, avg, {_safe(x["user_name"]): x["stars"] for x in v})
        return self.summary(path, user_id)

    # ------------------------------------------------------------ playlist per Music Assistant
    def write_playlists(self) -> int:
        """Riscrive le playlist .m3u in ogni cartella della musica che ha brani votati. Ritorna quante."""
        rows = self.library.db.all_ratings()
        by_base: dict[str, dict[str, list]] = {}
        bases = sorted(set(self._prefix.values()), key=len, reverse=True)
        for r in rows:
            base = next((b for b in bases if r["path"].startswith(b + "/")), None)
            if base is None:
                try:
                    base = self.library.root_of(r["path"])
                except LibraryError:
                    continue
            by_base.setdefault(base, {}).setdefault(r["path"], []).append(r)
        n = 0
        for base, files in by_base.items():
            out_dir = os.path.join(base, PLAYLIST_DIR)
            lists: dict[str, list[str]] = {}
            for path, v in sorted(files.items()):
                avg = sum(x["stars"] for x in v) / len(v)
                if avg >= 4.5:
                    lists.setdefault("5 stelle - tutti (media)", []).append(path)
                if avg >= 3.5:
                    lists.setdefault("4 stelle o più - tutti (media)", []).append(path)
                for x in v:
                    if x["stars"] == 5:
                        lists.setdefault(f"5 stelle - {x['user_name']}", []).append(path)
                    if x["stars"] >= 4:
                        lists.setdefault(f"4 stelle o più - {x['user_name']}", []).append(path)
            os.makedirs(out_dir, exist_ok=True)
            wanted = set()
            for title, paths in lists.items():
                fn = os.path.join(out_dir, _safe(title) + ".m3u")
                wanted.add(fn)
                body = "#EXTM3U\n" + "".join(os.path.relpath(p, out_dir) + "\n" for p in paths)
                tmp = fn + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(body)
                os.replace(tmp, fn)
                n += 1
            for e in os.scandir(out_dir):  # playlist rimaste vuote: via
                if e.name.endswith(".m3u") and e.path not in wanted:
                    os.remove(e.path)
        return n
