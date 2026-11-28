"""Archivio di LDMHA (SQLite nella configurazione di Home Assistant): i brani letti con i loro tag e i risultati
dei riconoscimenti, così aprendo una cartella già letta l'elenco compare subito. «Aggiorna» rilegge la cartella
aprendo solo i file cambiati (dimensione o data diverse) e toglie quelli che non ci sono più.
Si usa solo dai fili dell'executor (mai dal ciclo principale di Home Assistant)."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    dir TEXT NOT NULL,
    size INTEGER,
    mtime REAL,
    tags TEXT,
    ident TEXT,
    applied INTEGER DEFAULT 0,
    seen REAL
);
CREATE INDEX IF NOT EXISTS files_dir ON files(dir);
CREATE TABLE IF NOT EXISTS scans (
    path TEXT PRIMARY KEY,
    recursive INTEGER NOT NULL,
    t REAL NOT NULL,
    count INTEGER,
    folders INTEGER
);
CREATE TABLE IF NOT EXISTS ratings (
    path TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_name TEXT,
    stars INTEGER NOT NULL,
    t REAL,
    PRIMARY KEY (path, user_id)
);
"""


def _under(path: str) -> tuple[str, str]:
    """Intervallo di percorsi «dentro» path (con l'indice, senza LIKE): da path/ a path0 ('0' viene dopo '/')."""
    return path + "/", path + "0"


class Db:
    def __init__(self, file: str) -> None:
        os.makedirs(os.path.dirname(file), exist_ok=True)
        self._lock = threading.Lock()
        self._c = sqlite3.connect(file, check_same_thread=False, timeout=30)
        self._c.execute("PRAGMA journal_mode=WAL")
        self._c.execute("PRAGMA synchronous=NORMAL")
        self._c.executescript(SCHEMA)
        self._c.commit()

    def close(self) -> None:
        with self._lock:
            self._c.close()

    # -------------------------------------------------------------- letture fatte
    def covered(self, path: str, recursive: bool) -> dict[str, Any] | None:
        """La lettura salvata che comprende questa cartella (la stessa, o una più in alto con le sottocartelle)."""
        with self._lock:
            rows = self._c.execute("SELECT path, recursive, t FROM scans").fetchall()
        best = None
        for p, rec, t in rows:
            ok = (p == path and (rec or not recursive)) or (rec and path.startswith(p + "/"))
            if ok and (best is None or t > best["t"]):
                best = {"path": p, "recursive": bool(rec), "t": t}
        return best

    def scanned(self, path: str, recursive: bool, count: int, folders: int) -> None:
        with self._lock:
            self._c.execute("INSERT OR REPLACE INTO scans(path, recursive, t, count, folders) VALUES (?,?,?,?,?)",
                            (path, int(recursive), time.time(), count, folders))
            self._c.commit()

    # -------------------------------------------------------------- brani
    @staticmethod
    def _row(r: tuple) -> dict[str, Any]:
        path, size, tags, ident, applied = r
        f: dict[str, Any] = {"path": path, "size": size, "tags": json.loads(tags) if tags else {}}
        if ident:
            f["ident"] = json.loads(ident)
        if applied:
            f["applied"] = True
        return f

    def files(self, path: str, recursive: bool, limit: int) -> Iterator[dict[str, Any]]:
        q = "SELECT path, size, tags, ident, applied FROM files WHERE "
        if recursive:
            a, b = _under(path)
            q += "path >= ? AND path < ? ORDER BY path LIMIT ?"
            args: tuple = (a, b, limit)
        else:
            q += "dir = ? ORDER BY path LIMIT ?"
            args = (path, limit)
        with self._lock:
            rows = self._c.execute(q, args).fetchall()
        for r in rows:
            yield self._row(r)

    def known(self, folder: str) -> dict[str, tuple[int, float, dict, bool]]:
        """Brani salvati di una cartella (solo quella): percorso -> (dimensione, data, tag, riconosciuto)."""
        with self._lock:
            rows = self._c.execute("SELECT path, size, mtime, tags, ident FROM files WHERE dir = ?", (folder,)).fetchall()
        return {p: (s, m, json.loads(t) if t else {}, bool(i)) for p, s, m, t, i in rows}

    def ident_of(self, paths: list[str]) -> dict[str, tuple[dict | None, bool]]:
        out: dict[str, tuple[dict | None, bool]] = {}
        with self._lock:
            for i in range(0, len(paths), 500):
                part = paths[i:i + 500]
                q = f"SELECT path, ident, applied FROM files WHERE path IN ({','.join('?' * len(part))})"
                for p, ident, applied in self._c.execute(q, part).fetchall():
                    out[p] = (json.loads(ident) if ident else None, bool(applied))
        return out

    def put_many(self, items: list[tuple[str, int, float, dict]], seen: float) -> None:
        """Brani letti (percorso, dimensione, data, tag): i risultati dei riconoscimenti restano."""
        with self._lock:
            self._c.executemany(
                "INSERT INTO files(path, dir, size, mtime, tags, seen) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, tags=excluded.tags, "
                "seen=excluded.seen",
                [(p, os.path.dirname(p), s, m, json.dumps(t, ensure_ascii=False), seen) for p, s, m, t in items])
            self._c.commit()

    def touch_seen(self, paths: list[str], seen: float) -> None:
        with self._lock:
            self._c.executemany("UPDATE files SET seen = ? WHERE path = ?", [(seen, p) for p in paths])
            self._c.commit()

    def drop_unseen(self, path: str, recursive: bool, seen: float) -> int:
        """Dopo una lettura completa: via i brani che non ci sono più."""
        with self._lock:
            if recursive:
                a, b = _under(path)
                cur = self._c.execute("DELETE FROM files WHERE path >= ? AND path < ? AND (seen IS NULL OR seen < ?)",
                                      (a, b, seen))
            else:
                cur = self._c.execute("DELETE FROM files WHERE dir = ? AND (seen IS NULL OR seen < ?)", (path, seen))
            self._c.commit()
            return cur.rowcount

    def set_ident(self, path: str, ident: dict[str, Any]) -> None:
        with self._lock:
            self._c.execute("UPDATE files SET ident = ?, applied = 0 WHERE path = ?",
                            (json.dumps(ident, ensure_ascii=False), path))
            self._c.commit()

    def moved(self, old: str, new: str, size: int, mtime: float, tags: dict, applied: bool) -> None:
        """Brano rinominato (o con tag nuovi): stessa riga, percorso e tag nuovi."""
        with self._lock:
            row = self._c.execute("SELECT ident FROM files WHERE path = ?", (old,)).fetchone()
            if old != new:
                self._c.execute("DELETE FROM files WHERE path = ?", (old,))
                self._c.execute("UPDATE OR REPLACE ratings SET path = ? WHERE path = ?", (new, old))
            self._c.execute(
                "INSERT OR REPLACE INTO files(path, dir, size, mtime, tags, ident, applied, seen) VALUES (?,?,?,?,?,?,?,?)",
                (new, os.path.dirname(new), size, mtime, json.dumps(tags, ensure_ascii=False), row[0] if row else None,
                 int(applied), time.time()))
            self._c.commit()

    def remove(self, path: str) -> None:
        with self._lock:
            self._c.execute("DELETE FROM files WHERE path = ?", (path,))
            self._c.commit()

    def clear(self) -> None:
        with self._lock:
            self._c.execute("DELETE FROM files")
            self._c.execute("DELETE FROM scans")
            self._c.commit()

    def dir_stats(self, paths: list[str]) -> dict[str, tuple[int, int]]:
        """Per ogni cartella (con le sottocartelle): (brani nell'archivio, brani sistemati)."""
        out: dict[str, tuple[int, int]] = {}
        with self._lock:
            for p in paths:
                a, b = _under(p)
                n, done = self._c.execute("SELECT COUNT(*), COALESCE(SUM(applied), 0) FROM files WHERE path >= ? AND path < ?",
                                          (a, b)).fetchone()
                out[p] = (n or 0, done or 0)
        return out

    # -------------------------------------------------------------- valutazioni (stelle per utente)
    def set_rating(self, path: str, user_id: str, user_name: str, stars: int) -> None:
        with self._lock:
            self._c.execute("INSERT OR REPLACE INTO ratings(path, user_id, user_name, stars, t) VALUES (?,?,?,?,?)",
                            (path, user_id, user_name, stars, time.time()))
            self._c.commit()

    def del_rating(self, path: str, user_id: str) -> None:
        with self._lock:
            self._c.execute("DELETE FROM ratings WHERE path = ? AND user_id = ?", (path, user_id))
            self._c.commit()

    def ratings_of(self, path: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._c.execute("SELECT user_id, user_name, stars FROM ratings WHERE path = ? ORDER BY user_name",
                                   (path,)).fetchall()
        return [{"user_id": u, "user_name": n or "utente", "stars": s} for u, n, s in rows]

    def all_ratings(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._c.execute("SELECT path, user_id, user_name, stars FROM ratings").fetchall()
        return [{"path": p, "user_id": u, "user_name": n or "utente", "stars": s} for p, u, n, s in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            n = self._c.execute("SELECT COUNT(*), SUM(ident IS NOT NULL), SUM(applied) FROM files").fetchone()
        return {"files": n[0] or 0, "identified": n[1] or 0, "applied": n[2] or 0}
