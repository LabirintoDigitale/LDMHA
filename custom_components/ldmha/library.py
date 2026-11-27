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

from .const import AUDIO_EXT, MAX_FILES, SCAN_BATCH, TRASH_DIR

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


# ---------------------------------------------------------------- copertina dentro il file

def read_cover(path: str) -> tuple[bytes, str] | None:
    """Copertina già dentro il file (immagine, tipo) o None."""
    from mutagen import File

    try:
        f = File(path)
    except Exception:
        return None
    if f is None:
        return None
    try:
        pics = getattr(f, "pictures", None)  # FLAC
        if pics:
            return pics[0].data, pics[0].mime or "image/jpeg"
        tags = f.tags
        if tags is None:
            return None
        if hasattr(tags, "getall"):  # MP3 (ID3)
            apic = tags.getall("APIC")
            if apic:
                return apic[0].data, apic[0].mime or "image/jpeg"
        if "covr" in tags:  # MP4 / M4A
            c = tags["covr"][0]
            return bytes(c), "image/png" if getattr(c, "imageformat", 0) == 14 else "image/jpeg"
        if "metadata_block_picture" in tags:  # OGG / Opus
            import base64
            from mutagen.flac import Picture

            p = Picture(base64.b64decode(tags["metadata_block_picture"][0]))
            return p.data, p.mime or "image/jpeg"
    except Exception:  # noqa: BLE001 - copertina rovinata: come se non ci fosse
        return None
    return None


def write_cover(path: str, data: bytes, mime: str) -> None:
    """Mette la copertina nel file (al posto di quella che c'era). MP3, FLAC, M4A, OGG e Opus."""
    from mutagen import File

    try:
        f = File(path)
    except Exception as err:
        raise LibraryError(f"File non leggibile: {err}") from err
    if f is None:
        raise LibraryError("Formato senza copertina modificabile")
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import APIC, ID3

            if f.tags is None:
                f.add_tags()
            tags: ID3 = f.tags
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        elif ext == ".flac":
            from mutagen.flac import Picture

            pic = Picture()
            pic.type, pic.mime, pic.data = 3, mime, data
            f.clear_pictures()
            f.add_picture(pic)
        elif ext in (".m4a", ".mp4", ".aac"):
            from mutagen.mp4 import MP4Cover

            if f.tags is None:
                f.add_tags()
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            f.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
        elif ext in (".ogg", ".oga", ".opus"):
            import base64

            from mutagen.flac import Picture

            pic = Picture()
            pic.type, pic.mime, pic.data = 3, mime, data
            f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        else:
            raise LibraryError(f"Copertina non supportata per i file {ext}")
        f.save()
    except LibraryError:
        raise
    except Exception as err:
        raise LibraryError(f"Copertina non salvata: {err}") from err


def remove_cover(path: str) -> None:
    from mutagen import File

    try:
        f = File(path)
        if f is None:
            return
        if hasattr(f, "clear_pictures"):
            f.clear_pictures()
        elif f.tags is not None and hasattr(f.tags, "delall"):
            f.tags.delall("APIC")
        elif f.tags is not None:
            for k in ("covr", "metadata_block_picture"):
                if k in f.tags:
                    del f.tags[k]
        f.save()
    except Exception as err:  # noqa: BLE001
        raise LibraryError(f"Copertina non tolta: {err}") from err


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
    def __init__(self, hass: HomeAssistant, roots: list[str], db=None) -> None:
        self.hass = hass
        self.roots = roots
        self.db = db  # archivio dei brani letti (db.Db), facoltativo
        # copertine tolte dai file (per poterle rimettere annullando): file a parte, non nella cronologia
        self.covers_dir = hass.config.path("ldmha", "copertine_tolte") if hass is not None else "/tmp/ldmha_copertine"

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
    def list_dirs(self, path: str) -> dict[str, Any]:
        """Solo le sottocartelle (per l'albero del pannello): veloce anche sul NAS, senza leggere i brani."""
        real = self.check(path)
        try:
            names = sorted((e.name for e in os.scandir(real) if not e.name.startswith(".") and e.is_dir(follow_symlinks=False)),
                           key=str.lower)
        except OSError as err:
            raise LibraryError(f"Cartella non leggibile: {err}") from err
        return {"path": real, "dirs": names}

    def scan(self, path: str, recursive: bool, send, stop, refresh: bool = False) -> None:
        """Lettura a blocchi (nel filo dell'executor). Se la cartella è già nell'archivio e non si chiede di
        aggiornare, i brani arrivano subito da lì; altrimenti si legge il disco (o il NAS) aprendo solo i file nuovi
        o cambiati, si aggiorna l'archivio e si tolgono i brani spariti. stop() = il pannello ha smesso."""
        real = self.check(path)
        if not os.path.isdir(real):
            raise LibraryError("Non è una cartella")
        root = self.root_of(real)
        cov = self.db.covered(real, recursive) if self.db and not refresh else None
        if cov:
            send({"type": "start", "path": real, "root": root, "dirs": [], "cached": cov["t"]})
            batch: list[dict[str, Any]] = []
            count = 0
            for f in self.db.files(real, recursive, MAX_FILES):
                if stop():
                    return
                f["name"] = os.path.relpath(f["path"], real)
                batch.append(f)
                count += 1
                if len(batch) >= 1000:
                    send({"type": "files", "files": batch, "count": count, "folders": 0, "dir": ""})
                    batch = []
            send({"type": "files", "files": batch, "count": count, "folders": 0, "dir": ""})
            send({"type": "done", "count": count, "folders": 0, "truncated": count >= MAX_FILES, "cached": cov["t"]})
            return
        self._scan_disk(real, root, recursive, send, stop)

    def _scan_disk(self, real: str, root: str, recursive: bool, send, stop) -> None:
        seen_t = time.time()
        try:
            top = sorted((e for e in os.scandir(real) if not e.name.startswith(".")), key=lambda e: e.name.lower())
        except OSError as err:
            raise LibraryError(f"Cartella non leggibile: {err}") from err
        dirs = [e.name for e in top if e.is_dir(follow_symlinks=False)]
        send({"type": "start", "path": real, "root": root, "dirs": dirs})
        batch: list[dict[str, Any]] = []
        new_rows: list[tuple[str, int, float, dict]] = []
        same: list[str] = []
        count, folders, opened, last = 0, 0, 0, time.monotonic()

        def flush(cur: str) -> None:
            nonlocal batch, new_rows, same, last
            if self.db:
                if new_rows:
                    self.db.put_many(new_rows, seen_t)
                if same:
                    self.db.touch_seen(same, seen_t)
                # i risultati dei riconoscimenti già fatti tornano al pannello insieme ai brani
                if batch:
                    info = self.db.ident_of([f["path"] for f in batch])
                    for f in batch:
                        ident, applied = info.get(f["path"], (None, False))
                        if ident:
                            f["ident"] = ident
                        if applied:
                            f["applied"] = True
            send({"type": "files", "files": batch, "count": count, "folders": folders, "opened": opened,
                  "dir": os.path.relpath(cur, real)})
            batch, new_rows, same = [], [], []
            last = time.monotonic()

        def walk():
            yield real, [e.name for e in top if not e.is_dir(follow_symlinks=False)]
            if not recursive:
                return
            for d in dirs:
                for base, sub, names in os.walk(os.path.join(real, d)):
                    sub[:] = sorted(x for x in sub if not x.startswith("."))
                    yield base, names

        truncated = False
        for folder, names in walk():
            if stop():
                return
            folders += 1
            known = self.db.known(folder) if self.db else {}
            for n in sorted(names, key=str.lower):
                if os.path.splitext(n)[1].lower() not in AUDIO_EXT:
                    continue
                if count >= MAX_FILES:
                    truncated = True
                    break
                fp = os.path.join(folder, n)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                k = known.get(fp)
                if k and k[0] == st.st_size and abs((k[1] or 0) - st.st_mtime) < 1:
                    tags = k[2]  # file non cambiato: i tag salvati valgono ancora (non si apre il file)
                    same.append(fp)
                else:
                    tags = read_tags(fp)
                    opened += 1
                    new_rows.append((fp, st.st_size, st.st_mtime, tags))
                batch.append({"path": fp, "name": os.path.relpath(fp, real), "size": st.st_size, "tags": tags})
                count += 1
                if len(batch) >= SCAN_BATCH or time.monotonic() - last > 1:
                    if stop():
                        return
                    flush(folder)
            if truncated:
                break
            if time.monotonic() - last > 1:  # cartelle senza brani: almeno si vede dove si è arrivati
                flush(folder)
        flush(real)
        removed = 0
        if self.db and not truncated:
            removed = self.db.drop_unseen(real, recursive, seen_t)
            self.db.scanned(real, recursive, count, folders)
        send({"type": "done", "count": count, "folders": folders, "opened": opened, "removed": removed,
              "truncated": truncated})

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

    def apply(self, path: str, tags: dict[str, str], new_name: str | None,
              cover: tuple[bytes, str] | None = None, cover_if_missing: bool = False) -> dict[str, Any]:
        real = self.check(path)
        old_tags = read_tags(real)
        old_tags.pop("_length", None)
        if tags:
            write_tags(real, tags)
        old_cover = None
        if cover and cover_if_missing and read_cover(real):
            cover = None  # il file ha già una copertina e si è scelto di aggiungerla solo se manca
        if cover:
            prev = read_cover(real)
            if prev:  # la copertina di prima resta in un file a parte, per poterla rimettere annullando
                os.makedirs(self.covers_dir, exist_ok=True)
                keep = os.path.join(self.covers_dir, f"{time.time():.6f}.img")
                with open(keep, "wb") as fh:
                    fh.write(prev[0])
                old_cover = {"file": keep, "mime": prev[1]}
            write_cover(real, cover[0], cover[1])
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
        if cover:
            entry["cover"] = True
            entry["old_cover"] = old_cover
        self.history.append(entry)
        new_tags = read_tags(dst)
        self._remember(real, dst, new_tags, applied=True)
        return {"path": dst, "name": os.path.basename(dst), "tags": new_tags, "entry": entry}

    def _remember(self, old: str, new: str, tags: dict, applied: bool) -> None:
        if not self.db:
            return
        try:
            st = os.stat(new)
            self.db.moved(old, new, st.st_size, st.st_mtime, tags, applied)
        except OSError:
            pass

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
        if self.db:
            self.db.remove(real)
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
        if entry["kind"] == "apply" and entry.get("cover"):
            oc = entry.get("old_cover")
            if oc and os.path.isfile(oc.get("file", "")):
                with open(oc["file"], "rb") as fh:
                    write_cover(back, fh.read(), oc["mime"])
            elif not oc:
                remove_cover(back)
        entry["undone"] = time.time()
        self._remember(cur, back, read_tags(back), applied=False)
        return {"path": back}

    async def async_commit(self) -> None:
        await self._save()

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        return list(reversed(self.history[-limit:]))
