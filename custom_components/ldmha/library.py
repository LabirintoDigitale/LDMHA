"""I file della musica: elenco con i tag, scrittura dei tag, nuovo nome, cestino e cronologia (ogni modifica si
può annullare). Si lavora solo dentro le cartelle scelte nella configurazione."""

from __future__ import annotations

import os
import re
import shutil
import time
import unicodedata
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import AUDIO_EXT, MAX_FILES, TRASH_DIR

TAG_KEYS = ("title", "artist", "album", "albumartist", "date", "tracknumber", "musicbrainz_trackid")
HISTORY_MAX = 1000


class LibraryError(Exception):
    """Errore mostrato nel pannello."""


# ---------------------------------------------------------------- tag (mutagen)

def _mutagen():
    try:
        import mutagen  # noqa: F401
        from mutagen import File
    except ImportError as err:  # pragma: no cover
        raise LibraryError("Manca la libreria mutagen (riavvia Home Assistant dopo l'installazione)") from err
    return File


def read_tags(path: str) -> dict[str, str]:
    File = _mutagen()
    try:
        f = File(path, easy=True)
    except Exception:  # file rovinato o formato strano: senza tag
        return {}
    if f is None:
        return {}
    out: dict[str, str] = {}
    for k in TAG_KEYS:
        try:
            v = f.get(k) if f.tags is not None else None
        except (KeyError, ValueError):
            v = None
        if v:
            out[k] = str(v[0] if isinstance(v, list) else v)
    try:
        out["_length"] = str(round(f.info.length)) if f.info and f.info.length else ""
    except AttributeError:
        pass
    return out


def write_tags(path: str, tags: dict[str, str], clear_missing: bool = False) -> None:
    """Scrive i tag indicati (vuoti = lasciati come sono, o tolti con clear_missing)."""
    File = _mutagen()
    try:
        f = File(path, easy=True)
    except Exception as err:
        raise LibraryError(f"File non leggibile: {err}") from err
    if f is None:
        raise LibraryError("Formato senza tag modificabili")
    if f.tags is None:
        try:
            f.add_tags()
        except Exception as err:
            raise LibraryError(f"Impossibile aggiungere i tag: {err}") from err
    for k in TAG_KEYS:
        v = str(tags.get(k) or "").strip()
        try:
            if v:
                f[k] = [v]
            elif clear_missing and k in f:
                del f[k]
        except (KeyError, ValueError, TypeError):
            continue  # questo formato non ha quel tag
    try:
        f.save()
    except Exception as err:
        raise LibraryError(f"Tag non salvati: {err}") from err


# ---------------------------------------------------------------- nomi

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def clean_part(s: str) -> str:
    s = unicodedata.normalize("NFC", str(s or ""))
    s = _BAD.sub(" ", s).replace("..", ".")
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:150]


def build_name(pattern: str, tags: dict[str, str], ext: str) -> str:
    """Nome nuovo (anche con sottocartelle, separate da /) dallo schema, es. «{artist} - {title}»."""
    track = re.sub(r"\D.*$", "", str(tags.get("tracknumber") or ""))
    vals = {
        "artist": tags.get("artist") or tags.get("albumartist") or "Sconosciuto",
        "albumartist": tags.get("albumartist") or tags.get("artist") or "Sconosciuto",
        "title": tags.get("title") or "Senza titolo",
        "album": tags.get("album") or "Senza album",
        "year": (tags.get("date") or "")[:4],
        "track": track.zfill(2) if track else "",
    }
    parts = []
    for seg in str(pattern or "{artist} - {title}").split("/"):
        txt = re.sub(r"\{(\w+)\}", lambda m: vals.get(m.group(1), ""), seg)
        # separatori rimasti senza niente intorno (es. «{track} - {title}» senza numero)
        txt = re.sub(r"^\s*[-–.]\s*|\s*[-–]\s*$", "", txt)
        txt = re.sub(r"\(\s*\)|\[\s*\]", "", txt)
        txt = clean_part(txt)
        if txt:
            parts.append(txt)
    if not parts:
        raise LibraryError("Lo schema del nome non produce niente")
    return "/".join(parts) + ext.lower()


# ---------------------------------------------------------------- libreria

class Library:
    def __init__(self, hass: HomeAssistant, roots: list[str]) -> None:
        self.hass = hass
        self.roots = roots
        self._store = Store(hass, 1, "ldmha.history")
        self.history: list[dict[str, Any]] = []

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.history = [h for h in data.get("history") or [] if isinstance(h, dict)]

    async def _save(self) -> None:
        self.history = self.history[-HISTORY_MAX:]
        await self._store.async_save({"history": self.history})

    def real_roots(self) -> list[str]:
        return [os.path.realpath(r) for r in self.roots if os.path.isdir(r)]

    def check(self, path: str, must_exist: bool = True) -> str:
        """Percorso vero, solo se è dentro una delle cartelle della musica."""
        if not path or not path.startswith("/"):
            raise LibraryError("Percorso non valido")
        real = os.path.realpath(path)
        for r in self.real_roots():
            if real == r or real.startswith(r + os.sep):
                if must_exist and not os.path.exists(real):
                    raise LibraryError("Il file non c'è più (aggiorna l'elenco)")
                return real
        raise LibraryError("Percorso fuori dalle cartelle della musica")

    def root_of(self, real: str) -> str:
        for r in sorted(self.real_roots(), key=len, reverse=True):
            if real == r or real.startswith(r + os.sep):
                return r
        raise LibraryError("Percorso fuori dalle cartelle della musica")

    # -- elenco
    def list_dir(self, path: str, recursive: bool) -> dict[str, Any]:
        real = self.check(path)
        if not os.path.isdir(real):
            raise LibraryError("Non è una cartella")
        dirs: list[str] = []
        files: list[dict[str, Any]] = []
        truncated = False

        def add(fp: str) -> bool:
            if len(files) >= MAX_FILES:
                return False
            try:
                st = os.stat(fp)
            except OSError:
                return True
            files.append({"path": fp, "name": os.path.relpath(fp, real), "size": st.st_size, "tags": read_tags(fp)})
            return True

        try:
            entries = sorted(os.scandir(real), key=lambda e: e.name.lower())
        except OSError as err:
            raise LibraryError(f"Cartella non leggibile: {err}") from err
        for e in entries:
            if e.name.startswith("."):
                continue
            if e.is_dir(follow_symlinks=False):
                dirs.append(e.name)
            elif os.path.splitext(e.name)[1].lower() in AUDIO_EXT and not add(e.path):
                truncated = True
                break
        if recursive and not truncated:
            for d in dirs:
                for base, sub, names in os.walk(os.path.join(real, d)):
                    sub[:] = sorted(s for s in sub if not s.startswith("."))
                    for n in sorted(names, key=str.lower):
                        if os.path.splitext(n)[1].lower() in AUDIO_EXT and not add(os.path.join(base, n)):
                            truncated = True
                            break
                    if truncated:
                        break
                if truncated:
                    break
        return {"path": real, "dirs": dirs, "files": files, "truncated": truncated,
                "roots": self.real_roots(), "root": self.root_of(real)}

    # -- modifiche
    def _free(self, target: str, own: str) -> str:
        """Nome libero: se c'è già un altro file con quel nome si aggiunge « (2)», « (3)»…"""
        base, ext = os.path.splitext(target)
        cand, n = target, 2
        while os.path.exists(cand) and os.path.realpath(cand) != own:
            cand = f"{base} ({n}){ext}"
            n += 1
        return cand

    def _move(self, src: str, dst: str) -> None:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.normcase(src) == os.path.normcase(dst) and src != dst:
            # cambia solo maiuscole/minuscole (su alcune condivisioni conta come lo stesso file): passo intermedio
            tmp = dst + ".ldmha_tmp"
            shutil.move(src, tmp)
            shutil.move(tmp, dst)
        else:
            shutil.move(src, dst)
        self._prune(os.path.dirname(src))

    def _prune(self, folder: str) -> None:
        """Toglie le cartelle rimaste vuote (non le cartelle principali)."""
        roots = set(self.real_roots())
        while folder not in roots and os.path.isdir(folder):
            try:
                left = [n for n in os.listdir(folder) if n not in (".DS_Store", "Thumbs.db", "desktop.ini")]
                if left:
                    return
                for n in os.listdir(folder):
                    os.remove(os.path.join(folder, n))
                os.rmdir(folder)
            except OSError:
                return
            folder = os.path.dirname(folder)

    def apply(self, path: str, tags: dict[str, str], new_name: str | None) -> dict[str, Any]:
        real = self.check(path)
        old_tags = read_tags(real)
        old_tags.pop("_length", None)
        if tags:
            write_tags(real, tags)
        dst = real
        if new_name:
            rel = "/".join(clean_part(p) for p in new_name.split("/") if clean_part(p))
            if not rel:
                raise LibraryError("Nome nuovo vuoto")
            # le parti del nome sono ripulite (niente «..» né «/»): si resta sotto la cartella del file
            want = os.path.normpath(os.path.join(os.path.dirname(real), rel))
            if not want.startswith(self.root_of(real) + os.sep):
                raise LibraryError("Il nome nuovo esce dalla cartella della musica")
            dst = self._free(want, real)
            if dst != real:
                try:
                    self._move(real, dst)
                except OSError as err:
                    if tags:  # nome non cambiato: si rimettono anche i tag di prima
                        write_tags(real, old_tags, clear_missing=True)
                    raise LibraryError(f"Rinomina non riuscita: {err}") from err
        entry = {"id": f"{time.time():.6f}", "t": time.time(), "kind": "apply", "old": real, "new": dst,
                 "old_tags": old_tags, "tags": tags}
        self.history.append(entry)
        return {"path": dst, "name": os.path.basename(dst), "tags": read_tags(dst), "entry": entry}

    def trash(self, path: str) -> dict[str, Any]:
        real = self.check(path)
        root = self.root_of(real)
        dst = self._free(os.path.join(root, TRASH_DIR, os.path.relpath(real, root)), "")
        try:
            self._move(real, dst)
        except OSError as err:
            raise LibraryError(f"Spostamento nel cestino non riuscito: {err}") from err
        entry = {"id": f"{time.time():.6f}", "t": time.time(), "kind": "trash", "old": real, "new": dst}
        self.history.append(entry)
        return {"entry": entry}

    def undo(self, entry_id: str) -> dict[str, Any]:
        entry = next((h for h in self.history if h.get("id") == entry_id), None)
        if entry is None:
            raise LibraryError("Modifica non trovata nella cronologia")
        if entry.get("undone"):
            raise LibraryError("Modifica già annullata")
        cur = self.check(entry["new"])
        back = entry["old"]
        if cur != back:
            if os.path.exists(back):
                raise LibraryError("Al posto del nome vecchio c'è già un altro file")
            try:
                self._move(cur, back)
            except OSError as err:
                raise LibraryError(f"Ripristino non riuscito: {err}") from err
        if entry["kind"] == "apply" and entry.get("tags"):
            write_tags(back, entry.get("old_tags") or {}, clear_missing=True)
        entry["undone"] = time.time()
        return {"path": back}

    async def async_commit(self) -> None:
        await self._save()

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        return list(reversed(self.history[-limit:]))
