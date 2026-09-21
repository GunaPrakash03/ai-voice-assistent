"""
PostgreSQL persistence for everything the service keeps.

The managers (agents, phone numbers, SIP trunks, post-call jobs, recordings) were written around
JSON files and WAVs on disk. Rather than rewrite each of them, this module makes Postgres the
durable copy behind those files. (Users, workspaces, API keys and sessions are the exception: the
auth manager reads and writes their collections here directly and keeps no JSON file.)

  * write-through  – every time a manager saves its JSON file it also upserts the same documents
                     into ``app_documents`` (one row per agent / number / trunk / job / user…),
                     and every recording written to disk is also stored as bytes in ``recordings``.
  * restore        – at start-up, ``bootstrap()`` rebuilds any missing or older JSON file from the
                     database and puts back any recording that is missing from the recordings folder.

So the on-disk files remain a working cache the rest of the code understands, and the database is
the source of truth that survives a wiped checkout, a new machine, or a container rebuild.

Configuration: ``DATABASE_URL`` (e.g. postgresql://voice:voice@127.0.0.1:5433/voice_agent). When it
is unset or the server is unreachable everything degrades to file-only behaviour with one warning.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("storage")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT_DIR, "config")
RECORDINGS_DIR = os.path.join(ROOT_DIR, "recordings")

_lock = threading.Lock()
_state: Dict[str, Any] = {"url": None, "ok": False, "checked": 0.0, "error": "", "schema": False}

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_documents (
    collection  TEXT        NOT NULL,
    doc_id      TEXT        NOT NULL,
    doc         JSONB       NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (collection, doc_id)
);
CREATE INDEX IF NOT EXISTS app_documents_collection_idx ON app_documents (collection, updated_at DESC);

CREATE TABLE IF NOT EXISTS recordings (
    filename    TEXT        PRIMARY KEY,
    call_id     TEXT        NOT NULL,
    size_bytes  BIGINT      NOT NULL,
    sha256      TEXT        NOT NULL,
    content     BYTEA       NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS recordings_call_idx ON recordings (call_id);

CREATE TABLE IF NOT EXISTS collection_meta (
    collection  TEXT        PRIMARY KEY,
    updated_at  DOUBLE PRECISION NOT NULL
);
"""

# Which JSON file feeds which collections, and how to split/merge it.
#   file -> list of (collection, json key holding a list, id field)
FILE_COLLECTIONS: Dict[str, List[Tuple[str, str, str]]] = {
    os.path.join(CONFIG_DIR, "agents.json"): [("agents", "agents", "agent_id")],
    os.path.join(CONFIG_DIR, "phone_numbers.json"): [("phone_numbers", "numbers", "phone_number")],
    os.path.join(CONFIG_DIR, "sip_trunks.json"): [("sip_trunks_unified", "trunks", "trunk_id"),
                                                  ("sip_trunks_inbound", "inbound", "trunk_id"),
                                                  ("sip_trunks_outbound", "outbound", "trunk_id"),
                                                  ("sip_dispatch_rules", "rules", "rule_id")],
    os.path.join(RECORDINGS_DIR, "pipeline_jobs.json"): [("call_jobs", "jobs", "job_id")],
}
# Keys of a file that are not lists of documents but must round-trip (e.g. agent revisions).
FILE_EXTRA_DOCS: Dict[str, List[str]] = {
    os.path.join(CONFIG_DIR, "agents.json"): ["revisions"],
}


def database_url() -> str:
    return (os.getenv("DATABASE_URL") or "").strip()


def _connect():
    import psycopg
    return psycopg.connect(database_url(), connect_timeout=4, autocommit=False)


def available(recheck: bool = False) -> bool:
    """True when DATABASE_URL is set and the server answers. Caches success for 30s, errors for 3s."""
    url = database_url()
    if not url:
        _state.update(url=None, ok=False, error="DATABASE_URL not set")
        return False
    with _lock:
        cache_ttl = 30 if _state.get("ok") else 3
        if not recheck and _state.get("url") == url and time.time() - float(_state.get("checked") or 0.0) < cache_ttl:
            return bool(_state["ok"])
        try:
            with _connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                if not _state.get("schema"):
                    with conn.cursor() as cur:
                        cur.execute(SCHEMA)
                    conn.commit()
                    _state["schema"] = True
            _state.update(url=url, ok=True, error="", checked=time.time())
        except Exception as e:  # driver missing, server down, bad credentials
            _state.update(url=url, ok=False, error=str(e).strip()[:300], checked=time.time())
            log.warning("PostgreSQL unavailable (%s); running on files only", _state["error"])
        return bool(_state["ok"])


def status() -> Dict[str, Any]:
    ok = available()
    out: Dict[str, Any] = {"configured": bool(database_url()), "connected": ok, "error": _state.get("error", ""),
                           "url": _redact(database_url())}
    if ok:
        try:
            with _connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT collection, count(*) FROM app_documents GROUP BY collection ORDER BY collection")
                out["documents"] = {c: n for c, n in cur.fetchall()}
                cur.execute("SELECT count(*), coalesce(sum(size_bytes),0) FROM recordings")
                n, size = cur.fetchone()
                out["recordings"] = {"count": int(n), "bytes": int(size)}
                cur.execute("SELECT pg_database_size(current_database())")
                out["database_bytes"] = int(cur.fetchone()[0])
        except Exception as e:
            out["error"] = str(e)[:200]
    return out


def _redact(url: str) -> str:
    if "@" not in url:
        return url
    head, tail = url.rsplit("@", 1)
    if "://" in head and ":" in head.split("://", 1)[1]:
        scheme, creds = head.split("://", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{tail}"
    return url


# ── write-through ────────────────────────────────────────────────────────────
def save_collection(collection: str, docs: Iterable[Dict[str, Any]], id_field: str, updated_at: Optional[float] = None) -> bool:
    """Makes the table's rows for ``collection`` equal to ``docs`` (upsert + delete the rest)."""
    if not available() and not available(recheck=True):
        return False
    rows = []
    stamp = float(updated_at or time.time())
    for d in docs:
        key = str(d.get(id_field, "") or "")
        if key:
            rows.append((collection, key, json.dumps(d, default=str), stamp))
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO app_documents (collection, doc_id, doc, updated_at) VALUES (%s, %s, %s::jsonb, to_timestamp(%s)) "
                "ON CONFLICT (collection, doc_id) DO UPDATE SET doc = EXCLUDED.doc, updated_at = EXCLUDED.updated_at",
                rows,
            )
            ids = [r[1] for r in rows]
            if ids:
                cur.execute("DELETE FROM app_documents WHERE collection = %s AND NOT (doc_id = ANY(%s))", (collection, ids))
            else:
                cur.execute("DELETE FROM app_documents WHERE collection = %s", (collection,))
            cur.execute(
                "INSERT INTO collection_meta (collection, updated_at) VALUES (%s, %s) "
                "ON CONFLICT (collection) DO UPDATE SET updated_at = EXCLUDED.updated_at",
                (collection, stamp),
            )
            conn.commit()
        return True
    except Exception as e:
        log.warning("Write-through to PostgreSQL failed for %s: %s", collection, e)
        return False


def save_document(collection: str, doc_id: str, doc: Dict[str, Any], updated_at: Optional[float] = None) -> bool:
    if not available() and not available(recheck=True):
        return False
    try:
        doc_stamp = float(updated_at if updated_at is not None else (doc.get("updated_at") or doc.get("created_at") or time.time()))
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_documents (collection, doc_id, doc, updated_at) VALUES (%s, %s, %s::jsonb, to_timestamp(%s)) "
                "ON CONFLICT (collection, doc_id) DO UPDATE SET doc = EXCLUDED.doc, updated_at = to_timestamp(%s)",
                (collection, doc_id, json.dumps(doc, default=str), doc_stamp, doc_stamp),
            )
            cur.execute(
                "INSERT INTO collection_meta (collection, updated_at) VALUES (%s, %s) "
                "ON CONFLICT (collection) DO UPDATE SET updated_at = GREATEST(collection_meta.updated_at, EXCLUDED.updated_at)",
                (collection, doc_stamp),
            )
            conn.commit()
        return True
    except Exception as e:
        log.warning("Write-through to PostgreSQL failed for %s/%s: %s", collection, doc_id, e)
        return False


def load_collection(collection: str) -> List[Dict[str, Any]]:
    if not available():
        return []
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT doc FROM app_documents WHERE collection = %s ORDER BY updated_at ASC, doc_id ASC", (collection,))
            return [row[0] for row in cur.fetchall()]
    except Exception as e:
        log.warning("Load from PostgreSQL failed for %s: %s", collection, e)
        return []


def load_document(collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
    if not available():
        return None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT doc FROM app_documents WHERE collection = %s AND doc_id = %s", (collection, doc_id))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        log.warning("Load from PostgreSQL failed for %s/%s: %s", collection, doc_id, e)
        return None


def sync_file(path: str) -> bool:
    """Pushes one of the known JSON files into its collections. Called right after the file is written."""
    specs = FILE_COLLECTIONS.get(path)
    if not specs or not os.path.isfile(path) or not available():
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning("Cannot read %s for write-through: %s", path, e)
        return False
    ok = True
    stamp = float(data.get("updated_at") or time.time())
    for collection, key, id_field in specs:
        ok = save_collection(collection, data.get(key) or [], id_field, stamp) and ok
    for extra in FILE_EXTRA_DOCS.get(path, []):
        if extra in data:
            ok = save_document(f"{os.path.basename(path)}:{extra}", extra, {"value": data[extra]}) and ok
    return ok


def save_recording(path: str, call_id: str) -> bool:
    """Stores a WAV/MP3 on disk into the recordings table (idempotent by filename)."""
    if not available() or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            content = f.read()
        digest = hashlib.sha256(content).hexdigest()
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO recordings (filename, call_id, size_bytes, sha256, content) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (filename) DO UPDATE SET call_id = EXCLUDED.call_id, size_bytes = EXCLUDED.size_bytes, "
                "sha256 = EXCLUDED.sha256, content = EXCLUDED.content",
                (os.path.basename(path), call_id, len(content), digest, content),
            )
            conn.commit()
        return True
    except Exception as e:
        log.warning("Recording %s not stored in PostgreSQL: %s", os.path.basename(path), e)
        return False


def sync_recordings_dir(directory: str = RECORDINGS_DIR) -> int:
    """Uploads every recording on disk that the database does not have yet. Returns the count."""
    if not available() or not os.path.isdir(directory):
        return 0
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT filename FROM recordings")
            have = {r[0] for r in cur.fetchall()}
    except Exception as e:
        log.warning("Cannot list recordings in PostgreSQL: %s", e)
        return 0
    n = 0
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith((".wav", ".mp3")) or name in have:
            continue
        stem = name[:-4]
        stem = stem[4:] if stem.startswith("rec-") else stem
        call_id = __import__("re").sub(r"[-_]\d{10,}$", "", stem)
        if save_recording(os.path.join(directory, name), call_id):
            n += 1
    return n


# ── restore ──────────────────────────────────────────────────────────────────
def _file_stamp(path: str) -> float:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return float(json.load(f).get("updated_at") or 0.0)
    except Exception:
        return 0.0


def db_collections() -> Optional[set]:
    """Set of collection names that already exist in PostgreSQL (have a collection_meta row). Returns None on error."""
    if not available():
        return None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT collection FROM collection_meta")
            return {r[0] for r in cur.fetchall()}
    except Exception as e:
        log.warning("Cannot read collection list: %s", e)
        return None


def restore_files() -> List[str]:
    """Rebuilds JSON files from the database copy. Returns what it restored."""
    if not available():
        return []
    restored: List[str] = []
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT collection, updated_at FROM collection_meta")
            db_stamps = {c: float(t) for c, t in cur.fetchall()}
    except Exception as e:
        log.warning("Cannot read collection stamps: %s", e)
        return []
    for path, specs in FILE_COLLECTIONS.items():
        stamps = [db_stamps.get(c, 0.0) for c, _, _ in specs]
        db_stamp = max(stamps) if stamps else 0.0
        if db_stamp <= 0.0:
            continue  # nothing in the database for this file
        data: Dict[str, Any] = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        for collection, key, _ in specs:
            data[key] = load_collection(collection)
        for extra in FILE_EXTRA_DOCS.get(path, []):
            doc = load_document(f"{os.path.basename(path)}:{extra}", extra)
            if doc is not None:
                data[extra] = doc.get("value")
        data["updated_at"] = db_stamp
        try:
            import tempfile
            dir_name = os.path.dirname(path)
            os.makedirs(dir_name, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
                json.dump(data, tf, indent=2)
                temp_name = tf.name
            os.replace(temp_name, path)
            restored.append(os.path.relpath(path, ROOT_DIR))
        except Exception as e:
            log.warning("Could not restore %s from PostgreSQL: %s", path, e)
    return restored


def restore_recordings(directory: str = RECORDINGS_DIR) -> int:
    """Writes back recordings the database has but the folder does not. Returns the count."""
    if not available():
        return 0
    os.makedirs(directory, exist_ok=True)
    n = 0
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT filename FROM recordings")
            names = [r[0] for r in cur.fetchall()]
            for name in names:
                path = os.path.join(directory, name)
                if os.path.isfile(path):
                    continue
                cur.execute("SELECT content FROM recordings WHERE filename = %s", (name,))
                row = cur.fetchone()
                if row:
                    with open(path, "wb") as f:
                        f.write(bytes(row[0]))
                    n += 1
    except Exception as e:
        log.warning("Restoring recordings from PostgreSQL failed: %s", e)
    return n


def bootstrap(label: str = "") -> Dict[str, Any]:
    """Run once at process start, before the managers load their files."""
    if not database_url():
        log.info("DATABASE_URL not set; %s runs on JSON files and the recordings folder only", label or "process")
        return {"connected": False}
    if not available(recheck=True):
        return {"connected": False, "error": _state.get("error", "")}
    files = restore_files()
    recs = restore_recordings()
    # PostgreSQL is authoritative once seeded. Only push a file to the DB to SEED a collection the DB
    # has never held — never to overwrite one it already has. Otherwise a stale config/*.json baked
    # into the container image would clobber real data (deleted numbers reappear, re-routes revert,
    # trunk edits undone) on every boot. Restore above already pulled DB -> file for the running
    # process; runtime writes (user actions) still push through sync_file() as normal.
    existing = db_collections()
    if existing is None:
        log.warning("Cannot query PostgreSQL collections; aborting seed push to protect database")
        return {"connected": True, "restored_files": files, "restored_recordings": recs, "pushed_files": 0, "uploaded_recordings": 0}
    pushed = 0
    for path, specs in FILE_COLLECTIONS.items():
        path_cols = {c for c, _, _ in specs}
        if path_cols & existing:
            continue  # DB already owns this data — do not overwrite from a (possibly stale) file
        if os.path.isfile(path) and sync_file(path):
            pushed += 1
    uploaded = sync_recordings_dir()
    log.info("PostgreSQL storage ready (%s): restored %d file(s) %s, %d recording(s); pushed %d file(s), uploaded %d recording(s)",
             label or "process", len(files), files, recs, pushed, uploaded)
    return {"connected": True, "restored_files": files, "restored_recordings": recs,
            "pushed_files": pushed, "uploaded_recordings": uploaded}
