"""
Documents attached to cases: added by staff on the Cases page, or by callers through a private upload link
(web/documents-plan.html). Only the file is kept; nothing is read from it.

Storage follows agent/knowledge_manager.py: with DATABASE_URL the records live in ``app_documents``
(collection ``case_documents``) and the bytes in ``case_document_blobs``; without it, JSON and files under
config/case_documents/. Other processes see changes within REFRESH_SECONDS (collection_meta stamps).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from agent.knowledge_manager import KnowledgeError, clean_name, sniff as _sniff_known

log = logging.getLogger("case-documents")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(ROOT_DIR, "config", "case_documents")
COLLECTION = "case_documents"
BLOB_TABLE = "case_document_blobs"

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_CASE_FILES = 100
MAX_CASE_BYTES = 300 * 1024 * 1024
REFRESH_SECONDS = 5
SOURCES = ("staff", "caller")

KIND_LABELS = {"pdf": "PDF", "docx": "Word document", "xlsx": "Excel workbook", "csv": "CSV",
               "markdown": "Markdown", "text": "Text", "image": "Image"}


DocumentError = KnowledgeError     # same shape: a message for the person plus an HTTP status


@dataclass
class CaseDocument:
    doc_id: str
    case_id: str
    workspace_id: str
    name: str
    kind: str
    mime: str
    size_bytes: int
    sha256: str
    source: str = "staff"            # "staff" (Cases page) | "caller" (upload link)
    uploaded_by: str = ""            # staff email, or the caller's name / contact for links
    uploaded_by_id: str = ""         # staff user_id ("" for callers)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def public(self) -> Dict[str, Any]:
        d = self.to_dict()
        d.pop("uploaded_by_id", None)
        d["kind_label"] = KIND_LABELS.get(self.kind, self.kind)
        return d


def sniff(data: bytes, name: str):
    """(kind, mime): the knowledge base's types plus HEIC/HEIF photos from iPhones."""
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
        return "image", "image/heic"
    return _sniff_known(data, name)


class CaseDocumentStore:
    def __init__(self, local_dir: str = LOCAL_DIR):
        self.local_dir = local_dir
        self._lock = threading.RLock()
        self._docs: Dict[str, CaseDocument] = {}
        self._checked_at = 0.0
        self._stamp: Optional[float] = None
        self._local_mtime = 0.0
        self._load()

    # storage ------------------------------------------------------------------------------------------
    @property
    def _index_path(self) -> str:
        return os.path.join(self.local_dir, "documents.json")

    def _load(self) -> None:
        docs: List[Dict[str, Any]] = []
        if os.getenv("DATABASE_URL"):
            from agent import storage
            if not storage.available():
                return
            docs = storage.load_collection(COLLECTION)
            stamps = storage.collection_stamps((COLLECTION,)) or {}
            self._stamp = stamps.get(COLLECTION)
        elif os.path.isfile(self._index_path):
            try:
                self._local_mtime = os.path.getmtime(self._index_path)
                with open(self._index_path, "r", encoding="utf-8") as f:
                    docs = json.load(f).get("documents") or []
            except Exception as e:
                log.warning("Failed to load case documents from %s: %s", self._index_path, e)
                return
        known = set(CaseDocument.__dataclass_fields__)
        loaded: Dict[str, CaseDocument] = {}
        for d in docs:
            try:
                cd = CaseDocument(**{k: v for k, v in d.items() if k in known})
                loaded[cd.doc_id] = cd
            except TypeError as e:
                log.warning("Skipping malformed case document record: %s", e)
        with self._lock:
            self._docs = loaded
        self._checked_at = time.time()

    def _refresh(self) -> None:
        if os.getenv("DATABASE_URL"):
            if time.time() - self._checked_at < REFRESH_SECONDS:
                return
            self._checked_at = time.time()
            from agent import storage
            stamps = storage.collection_stamps((COLLECTION,))
            if stamps is not None and stamps.get(COLLECTION) != self._stamp:
                self._load()
        elif os.path.isfile(self._index_path) and os.path.getmtime(self._index_path) != self._local_mtime:
            self._load()

    def _write_local_index(self) -> None:
        os.makedirs(self.local_dir, exist_ok=True)
        tmp = self._index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"documents": [d.to_dict() for d in self._docs.values()], "updated_at": time.time()}, f, indent=2)
        os.replace(tmp, self._index_path)
        self._local_mtime = os.path.getmtime(self._index_path)

    def _blob_path(self, doc_id: str) -> str:
        return os.path.join(self.local_dir, "blobs", doc_id + ".bin")

    def load_blob(self, doc_id: str) -> Optional[bytes]:
        if os.getenv("DATABASE_URL"):
            from agent import storage
            return storage.load_blob(doc_id, table=BLOB_TABLE)
        p = self._blob_path(doc_id)
        if not os.path.isfile(p):
            return None
        with open(p, "rb") as f:
            return f.read()

    # reads --------------------------------------------------------------------------------------------
    def get(self, doc_id: str) -> Optional[CaseDocument]:
        self._refresh()
        return self._docs.get(doc_id)

    def list_for_case(self, case_id: str) -> List[CaseDocument]:
        self._refresh()
        return sorted((d for d in self._docs.values() if d.case_id == case_id), key=lambda d: -d.created_at)

    def counts(self, case_ids: List[str]) -> Dict[str, int]:
        """Number of documents per case, for the case list."""
        self._refresh()
        wanted, out = set(case_ids), {}
        for d in self._docs.values():
            if d.case_id in wanted:
                out[d.case_id] = out.get(d.case_id, 0) + 1
        return out

    # writes -------------------------------------------------------------------------------------------
    def add(self, case_id: str, workspace_id: str, name: str, data: bytes, source: str = "staff",
            uploaded_by: str = "", uploaded_by_id: str = "") -> CaseDocument:
        if not case_id or not workspace_id:
            raise DocumentError("No case selected for this file.")
        if source not in SOURCES:
            raise DocumentError(f"Unknown source '{source}'.")
        if len(data) > MAX_FILE_BYTES:
            raise DocumentError(f"The file is {len(data) / 1048576:.1f} MB; the limit is {MAX_FILE_BYTES // 1048576} MB per file.", 413)
        name = clean_name(name)
        kind, mime = sniff(data, name)
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            self._refresh()
            existing = [d for d in self._docs.values() if d.case_id == case_id]
            dup = next((d for d in existing if d.sha256 == digest), None)
            if dup:
                raise DocumentError(f"This file is already on the case as \"{dup.name}\".", 409)
            if len(existing) >= MAX_CASE_FILES:
                raise DocumentError(f"The case already has {MAX_CASE_FILES} documents.", 413)
            if sum(d.size_bytes for d in existing) + len(data) > MAX_CASE_BYTES:
                raise DocumentError(f"Not enough space on this case (limit {MAX_CASE_BYTES // 1048576} MB).", 413)
            doc = CaseDocument(doc_id="cd-" + uuid.uuid4().hex[:12], case_id=case_id, workspace_id=workspace_id,
                               name=name, kind=kind, mime=mime, size_bytes=len(data), sha256=digest,
                               source=source, uploaded_by=uploaded_by, uploaded_by_id=uploaded_by_id)
            if os.getenv("DATABASE_URL"):
                from agent import storage
                if not storage.save_blob(doc.doc_id, workspace_id, data, table=BLOB_TABLE):
                    raise DocumentError("The file couldn't be saved: the database is unavailable.", 503)
                if not storage.save_document(COLLECTION, doc.doc_id, doc.to_dict(), doc.created_at):
                    storage.delete_blob(doc.doc_id, table=BLOB_TABLE)
                    raise DocumentError("The file couldn't be saved: the database is unavailable.", 503)
                self._docs[doc.doc_id] = doc
            else:
                os.makedirs(os.path.dirname(self._blob_path(doc.doc_id)), exist_ok=True)
                with open(self._blob_path(doc.doc_id), "wb") as f:
                    f.write(data)
                self._docs[doc.doc_id] = doc
                self._write_local_index()
        log.info("Document %s (%s, %d bytes) added to case %s by %s", doc.doc_id, doc.kind, doc.size_bytes, case_id, source)
        return doc

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            doc = self._docs.pop(doc_id, None)
            if not doc:
                return False
            if os.getenv("DATABASE_URL"):
                from agent import storage
                if not storage.delete_document(COLLECTION, doc_id):
                    self._docs[doc_id] = doc
                    raise DocumentError("The file couldn't be deleted: the database is unavailable.", 503)
                storage.delete_blob(doc_id, table=BLOB_TABLE)
            else:
                self._write_local_index()
                p = self._blob_path(doc_id)
                if os.path.isfile(p):
                    os.remove(p)
        return True

    def delete_for_case(self, case_id: str) -> int:
        n = 0
        for d in list(self.list_for_case(case_id)):
            n += 1 if self.delete(d.doc_id) else 0
        return n


case_documents = CaseDocumentStore()
