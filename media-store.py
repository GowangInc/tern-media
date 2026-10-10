#!/usr/bin/env python3
"""Tern Session Media backend.

One JSON request on stdin, one JSON response on stdout. Ops: touch, register,
list, tag, watch, backfill, discover. Python standard library only; retained
originals are immutable and content addressed, so a rewritten screenshot keeps
its older version instead of replacing it.

`discover` resolves the pane's own live conversation from this host's process
table and imports the transcript that process holds open, so media an open
conversation already carries appears without a manual import. A resumed
conversation idling between turns holds no transcript open; it is resolved
through the terminal breadcrumb OMP stamps on every create/switch/resume,
proven against the live process's own controlling tty and birth time.

State lives outside the plugin: TERN_MEDIA_STATE_DIR, else
~/Library/Application Support/Tern/media (macOS), else
$XDG_STATE_HOME/tern-media. Directories are 0700 and files 0600.
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

MAX_ITEM_BYTES = 64 * 1024 * 1024
# An encoded payload is bounded on the raw and on the whitespace-normalized
# text before anything is copied or decoded, so an oversized capture can never
# reach a decoded allocation.
MAX_ENCODED_CHARS = 4 * ((MAX_ITEM_BYTES + 2) // 3) + 4
# Transcripts are streamed: the file itself, and any single JSONL record in it,
# are bounded before a line is handed to the JSON parser. The record bound is
# what keeps memory flat; the file bound only refuses absurd input.
MAX_TRANSCRIPT_BYTES = 1024 * 1024 * 1024
MAX_TRANSCRIPT_LINE_BYTES = MAX_ENCODED_CHARS + 1024 * 1024
MAX_TAGS = 50
MAX_TAG_CHARS = 64
MAX_NAME_CHARS = 255
DEFAULT_LIMIT = 12
MAX_LIMIT = 40
MAX_SCAN_FILES = 200
MAX_BACKFILL_IMAGES = 200
MAX_BACKFILL_PATHS = 200
# Discovery reads a live process table and its open-file table through bounded
# host-local tools, and never reads more of a transcript than its header, so a
# hung or enormous process can neither stall nor exhaust this backend.
MAX_DISCOVER_FILES = 64
MAX_DISCOVER_HEADER_LINES = 64
MAX_DISCOVER_HEADER_BYTES = 256 * 1024
MAX_DISCOVER_OUTPUT = 1024 * 1024
# Exclude mapped binaries and directory descriptors, not arbitrary high fd numbers.
DISCOVER_FD_RANGE = "^txt,^mem,^cwd,^rtd"
LSOF_TIMEOUT_S = 4
PS_TIMEOUT_S = 4
# A terminal breadcrumb is two short lines — the cwd and the session file —
# plus an optional marker, written by OMP itself; anything larger is not one.
MAX_BREADCRUMB_BYTES = 16 * 1024
PREVIEW_PX = 512
SQLITE_TIMEOUT_S = 30.0
# Bumped only when the schema changes. Stored in PRAGMA user_version so a
# schema-current database is opened without a single write statement.
SCHEMA_VERSION = 1
TOOL_TIMEOUT_S = 20
VALID_SOURCES = ("created", "transferred", "viewed", "tagged")
DEFAULT_SOURCE = "viewed"
SESSION_FIELDS = (
    "id",
    "label",
    "cwd",
    "host",
    "pane_id",
    "window_key",
    "tern_session_id",
    "tern_session_name",
)

EXT_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "heic": "image/heic",
    "heif": "image/heif",
    "avif": "image/avif",
    "svg": "image/svg+xml",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "m4v": "video/x-m4v",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "avi": "video/x-msvideo",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
    "pdf": "application/pdf",
}

SNIFFABLE_IMAGE_EXTS = ("png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff")

# A retained original keeps a mime-appropriate suffix so native and OS file
# viewers (and Tern's own file_blocks) pick image/video/audio/PDF handling
# instead of treating a bare hash as text.
PREFERRED_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/heic": "heic",
    "image/heif": "heif",
    "image/avif": "avif",
    "image/svg+xml": "svg",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/x-m4v": "m4v",
    "video/x-matroska": "mkv",
    "video/webm": "webm",
    "video/x-msvideo": "avi",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/flac": "flac",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "application/pdf": "pdf",
}

# OMP stores retained media as `blob:sha256:<64 lowercase hex>` references whose
# bytes live in a blob directory as <hex> or <hex>.<ext>. Only that exact shape
# is ever resolved, so a reference can never escape its directory.
BLOB_REF_RE = re.compile(r"^blob:sha256:([0-9a-f]{64})$")
BLOB_EXTENSIONS = tuple(EXT_MIME)
OMP_BLOB_DIR = os.path.join(os.path.expanduser("~"), ".omp", "agent", "blobs")
# A conversation's transcript is a session file in the agent directory beside
# the blobs, named <timestamp>_<session id>.jsonl. Only files under that store
# are ever considered a conversation, so a stray JSONL elsewhere on the machine
# can never be imported into a pane.
OMP_SESSIONS_DIR = os.path.join(os.path.dirname(OMP_BLOB_DIR), "sessions")
# OMP's terminal breadcrumbs, one file per terminal, stamped synchronously on
# every session create/switch/resume. A resumed-but-idle OMP holds no
# transcript open, so this pointer is the only live proof of its conversation.
OMP_TERMINAL_SESSIONS_DIR = os.path.join(os.path.dirname(OMP_BLOB_DIR), "terminal-sessions")
# The foreground command the Tern CLI reports for OMP. Some builds report a
# version string in `name` instead of the command, so argv is checked too.
OMP_PROCESS_NAMES = ("omp", "omp-custom")

_MEDIA_EXT_ALT = "|".join(sorted(EXT_MIME, key=len, reverse=True))
# Markdown targets may contain spaces (often wrapped in <>), and quoted or
# backticked paths may contain spaces and @ (e.g. an Insync Google Drive root).
MEDIA_REF_RE = re.compile(
    r"!?\[[^\]]*\]\(\s*<?(?P<md>[^)\n]*?)>?\s*\)"
    r"|(?P<quote>[\"'`])(?P<quoted>[^\"'`\n]*?\.(?P<quoted_ext>" + _MEDIA_EXT_ALT + r"))(?P=quote)"
    r"|(?<![\w./-])(?P<bare>[^\s\"'`()\[\]<>|]+\.(?P<bare_ext>" + _MEDIA_EXT_ALT + r"))"
)
MARKDOWN_TITLE_RE = re.compile(r"""\s+("[^"]*"|'[^']*')$""")
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
# A terminal id becomes a breadcrumb filename, so anything that could leave
# the breadcrumb directory — or name a hidden file — is not a terminal.
TERMINAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def now_iso_us() -> str:
    """Microsecond, fixed-width UTC timestamp: the binding order's clock."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def state_dir() -> str:
    override = os.environ.get("TERN_MEDIA_STATE_DIR", "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Tern/media")
    base = os.environ.get("XDG_STATE_HOME", "").strip() or os.path.expanduser("~/.local/state")
    return os.path.join(base, "tern-media")


def private_dir(path: str) -> str:
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def private_file(path: str) -> str:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        label TEXT NOT NULL DEFAULT '',
        cwd TEXT NOT NULL DEFAULT '',
        host TEXT NOT NULL DEFAULT '',
        pane_id TEXT NOT NULL DEFAULT '',
        window_key TEXT NOT NULL DEFAULT '',
        tern_session_id TEXT NOT NULL DEFAULT '',
        tern_session_name TEXT NOT NULL DEFAULT '',
        touched_at TEXT NOT NULL,
        bound_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        name TEXT NOT NULL,
        path TEXT,
        source_key TEXT NOT NULL,
        stored_path TEXT NOT NULL,
        preview_path TEXT,
        sha256 TEXT NOT NULL,
        mime TEXT NOT NULL,
        kind TEXT NOT NULL,
        source TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        tags TEXT NOT NULL DEFAULT '[]',
        version INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS items_dedupe ON items (session_id, source_key, sha256)",
    "CREATE INDEX IF NOT EXISTS items_session ON items (session_id, id DESC)",
    """
    CREATE TABLE IF NOT EXISTS watches (
        session_id TEXT NOT NULL,
        path TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (session_id, path)
    )
    """,
)


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Bring a database to the current schema, exactly once.

    A single BEGIN IMMEDIATE serialises competing first-use processes through
    busy_timeout instead of letting them race the schema, and the user_version
    guard means a process that loses the race performs no writes at all.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            conn.execute("COMMIT")
            return
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "bound_at" not in columns:
            # Databases written before bound_at existed ordered pane bindings
            # by touched_at; seed that order once so pre-migration state keeps
            # resolving. No row is rewritten or removed otherwise.
            conn.execute("ALTER TABLE sessions ADD COLUMN bound_at TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE sessions SET bound_at = touched_at WHERE bound_at = ''")
        try:
            # Versions must be unique per retained source; the insert computes
            # the next version inside the same statement that writes it.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS items_version ON items (session_id, source_key, version)"
            )
        except sqlite3.IntegrityError:
            pass  # a state dir with pre-existing duplicate versions keeps working
        conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def connect(root: str) -> sqlite3.Connection:
    private_dir(root)
    db_path = os.path.join(root, "media.db")
    conn = sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT_S, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = %d" % int(SQLITE_TIMEOUT_S * 1000))
    # Opening a current schema performs no DDL, migration or journal changes.
    # Existing journal modes are preserved; new databases use SQLite's default.
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        initialize_schema(conn)
    private_file(db_path)
    return conn


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def extension_of(name_or_path: str) -> str:
    base = os.path.basename(name_or_path or "")
    dot = base.rfind(".")
    if dot <= 0 or dot == len(base) - 1:
        return ""
    return base[dot + 1 :].lower()


def kind_of_mime(mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime == "application/pdf":
        return "document"
    return ""


def sniff_mime(payload: bytes) -> str:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    if payload.startswith(b"BM"):
        return "image/bmp"
    if payload.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if payload[4:8] == b"ftyp":
        if payload[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"avif", b"avis"):
            return "image/heic"
        return "video/mp4"
    if payload.startswith(b"%PDF-"):
        return "application/pdf"
    if payload.startswith(b"OggS"):
        return "audio/ogg"
    if payload.startswith(b"fLaC"):
        return "audio/flac"
    if payload.startswith(b"ID3"):
        return "audio/mpeg"
    return ""


def classify(name: str, mime: str, payload: bytes) -> tuple[str, str]:
    """Return (kind, mime) or raise ValueError for unsupported payloads."""
    ext = extension_of(name)
    if ext in EXT_MIME:
        mime = EXT_MIME[ext]
    elif mime:
        if not kind_of_mime(mime):
            raise ValueError("unsupported media type: %s" % mime)
    else:
        sniffed = sniff_mime(payload)
        if not sniffed:
            raise ValueError("unsupported media type (no extension and unknown content)")
        mime = sniffed
        ext = next((e for e, m in EXT_MIME.items() if m == sniffed), "")
    kind = EXT_KIND.get(ext) or kind_of_mime(mime)
    if kind not in ("image", "video", "audio", "document"):
        raise ValueError("unsupported media type: %s" % (mime or ext or "unknown"))
    # A file named .png that is not actually an image must never be shown as one.
    if kind == "image" and payload and ext in SNIFFABLE_IMAGE_EXTS:
        if not sniff_mime(payload).startswith("image/"):
            raise ValueError("declared image content is not an image")
    return kind, mime


EXT_KIND = {
    ext: kind_of_mime(mime) for ext, mime in EXT_MIME.items()
}


# ---------------------------------------------------------------------------
# blobs and previews
# ---------------------------------------------------------------------------


def publish_blob(root: str, sha: str, payload: bytes, extension: str = "") -> str:
    """Retain an immutable, content-addressed snapshot as <aa>/<sha>[.<ext>]."""
    suffix = ("." + extension) if extension else ""
    target = os.path.join(root, "blobs", sha[:2], sha + suffix)
    if os.path.isfile(target) and os.path.getsize(target) == len(payload):
        return target
    private_dir(os.path.dirname(target))
    fd, tmp = tempfile.mkstemp(prefix=".blob-", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        private_file(tmp)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def _run(argv: list[str]) -> bool:
    try:
        done = subprocess.run(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TOOL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def retained_extension(mime: str, name: str = "") -> str:
    """Mime-appropriate suffix for a retained original (deterministic per mime)."""
    suffix = PREFERRED_EXT.get(mime)
    if suffix:
        return suffix
    suffix = extension_of(name)
    if suffix in EXT_MIME:
        return suffix
    return next((ext for ext, value in EXT_MIME.items() if value == mime), "")


def blob_candidates(digest: str, transcript_path: str | None) -> list[str]:
    """Validated-hash candidate paths for a `blob:sha256:<digest>` reference."""
    names = [digest] + ["%s.%s" % (digest, ext) for ext in BLOB_EXTENSIONS]
    bases = [os.path.join(state_dir(), "blobs"), OMP_BLOB_DIR]
    if transcript_path:
        session_dir = os.path.dirname(os.path.abspath(transcript_path))
        bases.extend([os.path.join(session_dir, "blobs"), session_dir])
    candidates: list[str] = []
    for base in bases:
        # Both the grouped typed layout (<base>/<aa>/<hash>[.<ext>]) and a flat
        # directory are searched, so either store layout resolves.
        for directory in (os.path.join(base, digest[:2]), base):
            candidates.extend(os.path.join(directory, name) for name in names)
    return candidates


def resolve_blob(digest: str, transcript_path: str | None) -> tuple[bytes, str] | None:
    """Bytes behind a blob reference, verified against its own hash."""
    for candidate in blob_candidates(digest, transcript_path):
        try:
            payload = read_regular_file(candidate)
        except (ValueError, OSError):
            continue
        if hashlib.sha256(payload).hexdigest() == digest:
            return payload, candidate
    return None


def make_preview(root: str, sha: str, source: str, kind: str) -> str | None:
    """Best-effort bounded PNG preview; failure never affects the original."""
    if kind not in ("image", "video"):
        return None
    target = os.path.join(root, "previews", sha[:2], sha + ".png")
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return target
    private_dir(os.path.dirname(target))
    fd, tmp = tempfile.mkstemp(prefix=".preview-", suffix=".png", dir=os.path.dirname(target))
    os.close(fd)
    produced = False
    if shutil.which("sips"):
        produced = _run(["sips", "-s", "format", "png", "-Z", str(PREVIEW_PX), source, "--out", tmp])
    if not produced and shutil.which("ffmpeg"):
        extra = ["-frames:v", "1"] if kind == "image" else ["-ss", "0.5", "-frames:v", "1"]
        produced = _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                source,
                *extra,
                "-vf",
                "scale=%d:-1:force_original_aspect_ratio=decrease" % PREVIEW_PX,
                tmp,
            ]
        )
    if produced and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
        private_file(tmp)
        os.replace(tmp, target)
        return target
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def clean(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def session_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {field: row[field] for field in SESSION_FIELDS}


def upsert_session(conn: sqlite3.Connection, raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("session must be an object")
    existing = None
    sid = clean(raw.get("id"))
    if sid:
        existing = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    elif not (clean(raw.get("host")) and clean(raw.get("pane_id"))):
        raise ValueError("session needs an id, or a host and pane_id")
    fields = {}
    for field in SESSION_FIELDS:
        fields[field] = (existing[field] if existing else "") or ""
    # bound_at is private ordering state, not a session field: it records when
    # this session last claimed its pane.
    fields["bound_at"] = (existing["bound_at"] if existing else "") or ""
    if sid:
        # An explicit id is the session identity, not a field to be enriched.
        fields["id"] = sid
    for field in SESSION_FIELDS:
        if field == "id" or field not in raw or raw[field] is None:
            continue
        # Only fields actually supplied may change: a child session's
        # {id, host} descriptor must never clear the pane/workspace binding.
        value = clean(raw[field])
        if value:
            fields[field] = value
    if clean(raw.get("pane_id")):
        # Only a descriptor that explicitly names its pane rebinds it. A child,
        # passive or historical descriptor that omits pane_id keeps the
        # previous bound_at, so it can never move the pane back to an older
        # conversation.
        fields["bound_at"] = now_iso_us()
    if not fields["id"]:
        fields["id"] = "terminal:%s:%s:%s" % (fields["host"], fields["window_key"], fields["pane_id"])
    fields["touched_at"] = now_iso()
    conn.execute(
        """
        INSERT INTO sessions (id, label, cwd, host, pane_id, window_key, tern_session_id,
                              tern_session_name, touched_at, bound_at)
        VALUES (:id, :label, :cwd, :host, :pane_id, :window_key, :tern_session_id,
                :tern_session_name, :touched_at, :bound_at)
        ON CONFLICT(id) DO UPDATE SET
            label = excluded.label, cwd = excluded.cwd, host = excluded.host,
            pane_id = excluded.pane_id, window_key = excluded.window_key,
            tern_session_id = excluded.tern_session_id,
            tern_session_name = excluded.tern_session_name,
            touched_at = excluded.touched_at,
            bound_at = excluded.bound_at
        """,
        fields,
    )
    return {field: fields[field] for field in SESSION_FIELDS}


def find_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    if not session_id:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return session_row(row)


def binding_for_pane(conn: sqlite3.Connection, pane_id: str, window_key: str) -> dict | None:
    pane_id = clean(pane_id)
    if not pane_id:
        return None
    window_key = clean(window_key)
    if window_key:
        row = conn.execute(
            "SELECT * FROM sessions WHERE pane_id = ? AND window_key = ? "
            "ORDER BY COALESCE(NULLIF(bound_at, ''), touched_at) DESC, rowid DESC LIMIT 1",
            (pane_id, window_key),
        ).fetchone()
        if row is not None:
            return session_row(row)
    # A blank window key must not widen the query beyond the pane.
    row = conn.execute(
        "SELECT * FROM sessions WHERE pane_id = ? "
        "ORDER BY COALESCE(NULLIF(bound_at, ''), touched_at) DESC, rowid DESC LIMIT 1",
        (pane_id,),
    ).fetchone()
    return session_row(row)


def item_json(row: sqlite3.Row) -> dict:
    try:
        tags = json.loads(row["tags"])
    except (ValueError, TypeError):
        tags = []
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "name": row["name"],
        "path": row["path"],
        "stored_path": row["stored_path"],
        "preview_path": row["preview_path"],
        "sha256": row["sha256"],
        "mime": row["mime"],
        "kind": row["kind"],
        "source": row["source"],
        "recorded_at": row["recorded_at"],
        "tags": tags if isinstance(tags, list) else [],
        "version": row["version"],
        "session_label": row["session_label"] if "session_label" in row.keys() else "",
    }


ITEM_SELECT = """
    SELECT items.*, COALESCE(sessions.label, '') AS session_label
    FROM items LEFT JOIN sessions ON sessions.id = items.session_id
"""


def tag_list(raw) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("tags must be an array of strings")
    out: list[str] = []
    for value in raw:
        text = value.strip() if isinstance(value, str) else ""
        if not text:
            continue
        text = text[:MAX_TAG_CHARS]
        if text not in out:
            out.append(text)
        if len(out) >= MAX_TAGS:
            break
    return out


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def open_regular_file(path: str, limit: int = MAX_ITEM_BYTES) -> tuple[int, int]:
    """Open a verified regular file and return (descriptor, size).

    The descriptor is what gets verified, never a pathname check that a
    substitution could race: O_NONBLOCK keeps a FIFO or device swapped in for
    the path from blocking the open, and the fstat that follows refuses
    anything that is not a bounded regular file.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise ValueError("symlinks are not accepted") from exc
        raise ValueError("cannot open file: %s" % exc.strerror) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        if info.st_size > limit:
            raise ValueError("file exceeds %d byte limit" % limit)
    except BaseException:
        os.close(fd)
        raise
    return fd, info.st_size


def read_regular_file(path: str) -> bytes:
    fd, size = open_regular_file(path)
    try:
        # The fstat size bounds the read, so a file that grows while it is
        # being read cannot pull the buffer past the limit.
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def decode_base64(raw: str) -> bytes:
    # Bound the raw and the normalized text before anything is copied or
    # decoded: an oversized payload never reaches a decoded allocation.
    if len(raw or "") > MAX_ENCODED_CHARS:
        raise ValueError("encoded payload exceeds %d byte limit" % MAX_ENCODED_CHARS)
    text = re.sub(r"\s+", "", raw or "")
    if len(text) > MAX_ENCODED_CHARS:
        raise ValueError("encoded payload exceeds %d byte limit" % MAX_ENCODED_CHARS)
    if not text:
        raise ValueError("empty media payload")
    padding = "=" * (-len(text) % 4)
    try:
        payload = base64.b64decode(text + padding, validate=True)
    except (binascii.Error, ValueError):
        if not re.fullmatch(r"[A-Za-z0-9+/_=-]*", text):
            raise ValueError("invalid base64 payload") from None
        try:
            payload = base64.b64decode(text.replace("-", "+").replace("_", "/") + padding)
        except (binascii.Error, ValueError):
            raise ValueError("invalid base64 payload") from None
    if len(payload) > MAX_ITEM_BYTES:
        raise ValueError("payload exceeds %d byte limit" % MAX_ITEM_BYTES)
    return payload


def ingest(
    conn: sqlite3.Connection,
    root: str,
    session: dict,
    raw,
    transcript_path: str | None = None,
) -> dict:
    """Store one item. Raises ValueError with a user-safe reason."""
    if not isinstance(raw, dict):
        raise ValueError("item must be an object")
    path = raw.get("path")
    data = raw.get("data")
    name = clean(raw.get("name"))
    mime = clean(raw.get("mime")).lower()
    source = clean(raw.get("source")) or DEFAULT_SOURCE
    if source not in VALID_SOURCES:
        source = DEFAULT_SOURCE
    tags = tag_list(raw.get("tags"))
    recorded_at = clean(raw.get("recorded_at")) or now_iso()

    if isinstance(path, str) and path.strip():
        # A relative reference belongs to the conversation's working directory;
        # this process runs from the plugin directory, which means nothing.
        expanded = os.path.expanduser(path.strip())
        if not os.path.isabs(expanded):
            base = session.get("cwd") or ""
            if base:
                expanded = os.path.join(base, expanded)
        # Canonicalize the directory before reading while keeping the leaf name
        # as written: the retained identity must not be relabelled by a path
        # that is replaced in between, and a symlink leaf is still refused by
        # the descriptor check.
        resolved = os.path.join(
            os.path.realpath(os.path.dirname(expanded) or "."), os.path.basename(expanded)
        )
        payload = read_regular_file(resolved)
        source_key = "path:" + resolved
        if not name:
            name = os.path.basename(expanded)
        path_value = resolved
    elif isinstance(data, str) and data.strip():
        reference = BLOB_REF_RE.match(data.strip())
        if reference is not None:
            # OMP hands out blob:sha256:<hex> for retained media, including the
            # live capture path; resolve it against the validated hash only.
            digest = reference.group(1)
            found = resolve_blob(digest, transcript_path)
            if found is None:
                raise ValueError("referenced blob not found: sha256:%s" % digest)
            payload, blob_path = found
            source_key = "blob:sha256:" + digest
            path_value = None
            if not name:
                name = "media"
            if extension_of(name) not in EXT_MIME:
                ext = extension_of(blob_path) or next((e for e, m in EXT_MIME.items() if m == mime), "")
                if ext:
                    name = ("%s.%s" % (name, ext))[:MAX_NAME_CHARS]
        else:
            payload = decode_base64(data)
            source_key = None  # filled once the hash is known
            path_value = None
            if not name:
                name = "media"
    else:
        raise ValueError("item has neither path nor data")

    if not payload:
        raise ValueError("empty media payload")

    name = (name or "media")[:MAX_NAME_CHARS]
    kind, mime = classify(clean(raw.get("name")) or name, mime, payload)
    digest = hashlib.sha256(payload).hexdigest()

    if source_key is None:
        ext = extension_of(name)
        if ext not in EXT_MIME:
            ext = next((e for e, m in EXT_MIME.items() if m == mime), "bin")
            name = ("%s-%s.%s" % (name, digest[:12], ext))[:MAX_NAME_CHARS]
        source_key = "name:" + name

    stored_path = publish_blob(root, digest, payload, retained_extension(mime, name))
    preview_path = make_preview(root, digest, stored_path, kind)

    # One statement: the next version is computed under the write lock, so a
    # concurrent writer can only ever land a distinct version, never a
    # duplicate; an identical event is absorbed by the dedupe index.
    values = (
        session["id"],
        name,
        path_value,
        source_key,
        stored_path,
        preview_path,
        digest,
        mime,
        kind,
        source,
        recorded_at,
        json.dumps(tags),
    )
    for _ in range(2):
        try:
            conn.execute(
                """
                INSERT INTO items (session_id, name, path, source_key, stored_path, preview_path,
                                   sha256, mime, kind, source, recorded_at, tags, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        (SELECT COALESCE(MAX(version), 0) + 1 FROM items
                         WHERE session_id = ? AND source_key = ?))
                ON CONFLICT (session_id, source_key, sha256) DO NOTHING
                """,
                values + (session["id"], source_key),
            )
            break
        except sqlite3.IntegrityError:
            continue  # a racing writer claimed this version; recompute once
    row = conn.execute(
        ITEM_SELECT + " WHERE items.session_id = ? AND items.source_key = ? AND items.sha256 = ?",
        (session["id"], source_key, digest),
    ).fetchone()
    if row is None:
        raise sqlite3.Error("stored item could not be read back")
    if tags and json.loads(row["tags"] or "[]") != tags:
        conn.execute("UPDATE items SET tags = ? WHERE id = ?", (json.dumps(tags), row["id"]))
        row = conn.execute(ITEM_SELECT + " WHERE items.id = ?", (row["id"],)).fetchone()
    return item_json(row)


def op_register(conn: sqlite3.Connection, root: str, req: dict) -> dict:
    session = upsert_session(conn, req.get("session"))
    items_raw = req.get("items")
    if not isinstance(items_raw, list):
        raise ValueError("items must be an array")
    stored: list[dict] = []
    errors: list[dict] = []
    for raw in items_raw:
        label = ""
        if isinstance(raw, dict):
            label = clean(raw.get("name")) or clean(raw.get("path")) or "item"
        try:
            stored.append(ingest(conn, root, session, raw))
        except ValueError as exc:
            errors.append({"name": label or "item", "error": str(exc)})
        except OSError as exc:
            errors.append({"name": label or "item", "error": "storage error: %s" % exc.strerror})
    return {"ok": True, "items": stored, "errors": errors}


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def supported_extension(name: str) -> bool:
    return extension_of(name) in EXT_MIME


def scan_watch_root(conn: sqlite3.Connection, root: str, session: dict, directory: str) -> list[dict]:
    """Bounded, nonrecursive, hidden/symlink-free scan of one watched directory."""
    errors: list[dict] = []
    try:
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda e: e.name)
    except OSError as exc:
        return [{"name": directory, "error": "cannot read watched directory: %s" % exc.strerror}]
    seen = 0
    for entry in entries:
        if seen >= MAX_SCAN_FILES:
            break
        if entry.name.startswith(".") or not supported_extension(entry.name):
            continue
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        seen += 1
        try:
            ingest(conn, root, session, {"path": entry.path, "source": "viewed"})
        except (ValueError, OSError) as exc:
            errors.append({"name": entry.name, "error": str(exc)})
    return errors


def op_list(conn: sqlite3.Connection, root: str, req: dict) -> dict:
    session_id = clean(req.get("session_id"))
    pane_id = clean(req.get("pane_id"))
    window_key = clean(req.get("window_key"))
    binding = find_session(conn, session_id) or binding_for_pane(conn, pane_id, window_key)
    scope = clean(req.get("scope")) or "conversation"
    if scope not in ("conversation", "workspace", "all"):
        scope = "conversation"

    errors: list[dict] = []
    watch_roots: list[str] = []
    if binding is not None:
        watch_roots = [
            row["path"]
            for row in conn.execute(
                "SELECT path FROM watches WHERE session_id = ? AND enabled = 1 ORDER BY path",
                (binding["id"],),
            )
        ]
        for directory in watch_roots:
            if os.path.isdir(directory):
                errors.extend(scan_watch_root(conn, root, binding, directory))

    target_ids: list[str] = []
    if scope == "conversation":
        if session_id:
            target_ids = [session_id]
        elif binding is not None:
            target_ids = [binding["id"]]
    elif scope == "workspace":
        tern_id = clean(req.get("tern_session_id")) or (binding["tern_session_id"] if binding else "")
        if tern_id:
            target_ids = [
                row["id"]
                for row in conn.execute("SELECT id FROM sessions WHERE tern_session_id = ?", (tern_id,))
            ]
        if not target_ids:
            return {
                "ok": True,
                "items": [],
                "total": 0,
                "session": binding,
                "sessions": known_sessions(conn),
                "watch_roots": watch_roots,
                "errors": errors,
            }

    where: list[str] = []
    params: list[object] = []
    if scope != "all":
        if not target_ids:
            return {
                "ok": True,
                "items": [],
                "total": 0,
                "session": binding,
                "sessions": known_sessions(conn),
                "watch_roots": watch_roots,
                "errors": errors,
            }
        where.append("items.session_id IN (%s)" % ",".join("?" * len(target_ids)))
        params.extend(target_ids)

    kind = clean(req.get("kind")) or "all"
    if kind in ("image", "video", "audio", "document"):
        where.append("items.kind = ?")
        params.append(kind)

    search = clean(req.get("search"))
    if search:
        needle = "%" + search.lower() + "%"
        where.append("(LOWER(items.name) LIKE ? OR LOWER(items.tags) LIKE ?)")
        params.extend([needle, needle])

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute("SELECT COUNT(*) AS n FROM items" + clause, params).fetchone()["n"]

    try:
        limit = int(req.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = DEFAULT_LIMIT if limit <= 0 else min(limit, MAX_LIMIT)
    try:
        offset = max(0, int(req.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0

    rows = conn.execute(
        ITEM_SELECT + clause + " ORDER BY items.id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return {
        "ok": True,
        "items": [item_json(row) for row in rows],
        "total": total,
        "session": binding,
        "sessions": known_sessions(conn),
        "watch_roots": watch_roots,
        "errors": errors,
    }


def known_sessions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM sessions ORDER BY touched_at DESC, rowid DESC"
    ).fetchall()
    return [session_row(row) for row in rows]


# ---------------------------------------------------------------------------
# tag / watch
# ---------------------------------------------------------------------------


def op_tag(conn: sqlite3.Connection, req: dict) -> dict:
    try:
        item_id = int(req.get("id"))
    except (TypeError, ValueError):
        raise ValueError("id must be an integer")
    tags = tag_list(req.get("tags"))
    row = conn.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ValueError("no item with id %d" % item_id)
    conn.execute("UPDATE items SET tags = ? WHERE id = ?", (json.dumps(tags), item_id))
    updated = conn.execute(ITEM_SELECT + " WHERE items.id = ?", (item_id,)).fetchone()
    return {"ok": True, "item": item_json(updated)}


def op_watch(conn: sqlite3.Connection, req: dict) -> dict:
    session_id = clean(req.get("session_id"))
    path = clean(req.get("path"))
    if not session_id:
        raise ValueError("watch requires session_id")
    if not path:
        raise ValueError("watch requires a directory path")
    directory = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(directory):
        raise ValueError("not a directory: %s" % path)
    enabled = 1 if req.get("enabled", True) else 0
    conn.execute(
        """
        INSERT INTO watches (session_id, path, enabled) VALUES (?, ?, ?)
        ON CONFLICT(session_id, path) DO UPDATE SET enabled = excluded.enabled
        """,
        (session_id, directory, enabled),
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


def walk_transcript(node, emit, counts: dict) -> None:
    """Walk parsed transcript JSON, emitting each payload as it is found."""
    if counts["images"] >= MAX_BACKFILL_IMAGES and counts["texts"] >= MAX_BACKFILL_PATHS:
        return
    if isinstance(node, dict):
        if node.get("type") == "image" and isinstance(node.get("data"), str):
            mime = node.get("mimeType") if isinstance(node.get("mimeType"), str) else ""
            if counts["images"] < MAX_BACKFILL_IMAGES:
                counts["images"] += 1
                emit("image", node["data"], mime)
        for key, value in node.items():
            if key in ("data", "output", "args", "stdout", "stderr", "patch"):
                continue  # never scan payload blobs or tool chatter for paths
            if key in ("text", "content") and isinstance(value, str):
                if counts["texts"] < MAX_BACKFILL_PATHS:
                    counts["texts"] += 1
                    emit("text", value, "")
                continue
            walk_transcript(value, emit, counts)
    elif isinstance(node, list):
        for entry in node:
            walk_transcript(entry, emit, counts)


def op_backfill(conn: sqlite3.Connection, root: str, req: dict) -> dict:
    session = upsert_session(conn, req.get("session"))
    transcript = clean(req.get("transcript_path"))
    if not transcript:
        raise ValueError("backfill requires transcript_path")
    transcript = os.path.abspath(os.path.expanduser(transcript))
    try:
        fd, _ = open_regular_file(transcript, MAX_TRANSCRIPT_BYTES)
    except ValueError as exc:
        raise ValueError("transcript is not a readable regular file (%s)" % exc) from exc

    stored: list[dict] = []
    errors: list[dict] = []
    seen: set[str] = set()
    unresolved = 0
    image_index = 0
    counts = {"images": 0, "texts": 0}

    def emit(kind: str, value: str, mime: str) -> None:
        """Ingest one walked payload immediately, so nothing accumulates."""
        nonlocal image_index, unresolved
        if kind == "image":
            image_index += 1
            entry = {
                "data": value,
                "mime": mime,
                "name": "transcript-image-%d" % image_index,
                "source": "viewed",
            }
            try:
                stored.append(ingest(conn, root, session, entry, transcript))
            except (ValueError, OSError) as exc:
                errors.append({"name": entry["name"], "error": str(exc)})
            return
        if len(seen) >= MAX_BACKFILL_PATHS:
            return
        for match in MEDIA_REF_RE.finditer(value):
            candidate = (match.group("md") or match.group("quoted") or match.group("bare") or "").strip()
            if match.group("md"):
                # [label](path "title") carries an optional quoted title.
                candidate = MARKDOWN_TITLE_RE.sub("", candidate).strip()
            candidate = candidate.strip("\"'<>").rstrip(".,;:!*_~-")
            if len(candidate) > 8192:
                if unresolved < MAX_BACKFILL_PATHS:
                    errors.append({"name": "transcript reference", "error": "media path exceeds 8192 characters"})
                    unresolved += 1
                continue
            if not candidate or candidate in seen:
                continue
            if len(seen) >= MAX_BACKFILL_PATHS:
                return
            seen.add(candidate)
            if SCHEME_RE.match(candidate) and not candidate.startswith("/"):
                if unresolved < MAX_BACKFILL_PATHS:
                    errors.append({"name": "remote media reference", "error": "external or remote path, not imported"})
                    unresolved += 1
                continue
            # A relative reference belongs to the conversation's working
            # directory; absolute paths (including ones containing @) are used
            # exactly as written.
            expanded = os.path.expanduser(candidate)
            if not os.path.isabs(expanded):
                base = session.get("cwd") or ""
                if base:
                    expanded = os.path.join(base, expanded)
            resolved = os.path.abspath(expanded)
            if not os.path.isfile(resolved) or os.path.islink(resolved):
                if unresolved < MAX_BACKFILL_PATHS:
                    errors.append({"name": candidate, "error": "referenced media not found"})
                    unresolved += 1
                continue
            try:
                stored.append(
                    ingest(conn, root, session, {"path": resolved, "source": "viewed"})
                )
            except (ValueError, OSError) as exc:
                errors.append({"name": candidate, "error": str(exc)})

    with os.fdopen(fd, "rb") as handle:
        number = 0
        total = 0
        while True:
            line = handle.readline(MAX_TRANSCRIPT_LINE_BYTES + 1)
            if not line:
                break
            number += 1
            total += len(line)
            if total > MAX_TRANSCRIPT_BYTES:
                raise ValueError("transcript exceeds %d byte limit" % MAX_TRANSCRIPT_BYTES)
            if len(line) > MAX_TRANSCRIPT_LINE_BYTES:
                raise ValueError(
                    "transcript line %d exceeds %d byte limit" % (number, MAX_TRANSCRIPT_LINE_BYTES)
                )
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except ValueError:
                if len(errors) < MAX_BACKFILL_IMAGES + MAX_BACKFILL_PATHS:
                    errors.append({"name": "line %d" % number, "error": "invalid JSON line"})
                continue
            walk_transcript(parsed, emit, counts)
    return {"ok": True, "items": stored, "errors": errors}


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


def run_text(argv: list[str], timeout: int) -> str | None:
    """Bounded stdout of a host-local command, or None when it cannot answer."""
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    output = done.stdout or ""
    if len(output) > MAX_DISCOVER_OUTPUT:
        return None
    return output


def foreground_process(pane_id: str, window_key: str = "") -> dict | None:
    """The pane's foreground process, as the local Tern CLI reports it."""
    cli = shutil.which("tern")
    if not cli:
        return None
    argv = [cli, "process", pane_id, "--json"]
    if window_key:
        argv.extend(["--window", window_key])
    output = run_text(argv, TOOL_TIMEOUT_S)
    if not output:
        return None
    try:
        payload = json.loads(output)
    except ValueError:
        return None
    foreground = payload.get("foreground") if isinstance(payload, dict) else None
    if not isinstance(foreground, dict):
        return None
    pid = foreground.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    return foreground


def is_omp_process(foreground: dict) -> bool:
    """Whether a reported foreground process is OMP itself, never a shell under it."""
    if clean(foreground.get("name")).lower() in OMP_PROCESS_NAMES:
        return True
    argv = foreground.get("argv")
    if not isinstance(argv, list):
        return False
    return bool(argv) and os.path.basename(clean(argv[0])).lower() in OMP_PROCESS_NAMES


def open_transcript_paths(pid: int) -> list[str] | None:
    """The transcript files one process holds open, or None when it cannot be read.

    A conversation is only ever identified by what its own process has open, so
    no name, timestamp or directory listing can substitute for a live session.
    """
    fd_dir = "/proc/%d/fd" % pid
    paths: list[str] = []
    if os.path.isdir(fd_dir):
        try:
            entries = os.listdir(fd_dir)
        except OSError:
            return None
        for entry in entries:
            try:
                paths.append(os.readlink(os.path.join(fd_dir, entry)))
            except OSError:
                continue
    else:
        lsof = shutil.which("lsof")
        if not lsof:
            return None
        # -a ANDs the pid and descriptor selectors, so another process's files
        # can never enter the answer; -b avoids macOS stat stalls.
        output = run_text(
            [lsof, "-nP", "-b", "-a", "-p", str(pid), "-d", DISCOVER_FD_RANGE, "-Fn"], LSOF_TIMEOUT_S
        )
        if output is None:
            return None
        paths = [line[1:] for line in output.splitlines() if line.startswith("n/")]
    found: list[str] = []
    for path in paths:
        if not path.endswith(".jsonl"):
            continue
        # An advisor or other internal transcript keeps a __-prefixed name and
        # is never the conversation the pane is working in.
        if os.path.basename(path).startswith("__"):
            continue
        if len(found) >= MAX_DISCOVER_FILES:
            return None
        if path not in found:
            found.append(path)
    return sorted(found)


def darwin_process_birth_ns(pid: int) -> int | None:
    """One process's p_starttime as epoch nanoseconds, via sysctl(3).

    p_starttime heads kinfo_proc — the p_un union is extern_proc's first
    member — so the timeval is the reply's first bytes: 64-bit tv_sec, then
    32-bit tv_usec, a layout that has held on every 64-bit macOS. Values that
    cannot be a birth time answer None, so a drifted layout fails closed
    instead of trusting a breadcrumb.
    """
    import ctypes

    try:
        libc = ctypes.CDLL("/usr/lib/libc.dylib")
        # CTL_KERN, KERN_PROC, KERN_PROC_PID, pid
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)
        size = ctypes.c_size_t(0)
        if libc.sysctl(ctypes.byref(mib), 4, None, ctypes.byref(size), None, 0) != 0:
            return None
        if not 16 <= size.value <= MAX_BREADCRUMB_BYTES:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if libc.sysctl(ctypes.byref(mib), 4, buffer, ctypes.byref(size), None, 0) != 0:
            return None
        if size.value < 16:
            return None
        seconds, micros = struct.unpack_from("<qi", buffer.raw[:16])
    except (OSError, ValueError):
        return None
    now = int(datetime.now(timezone.utc).timestamp())
    if not 0 < seconds <= now or not 0 <= micros < 1_000_000:
        return None
    return seconds * 1_000_000_000 + micros * 1_000


def process_birth_ns(pid: int) -> int | None:
    """An upper bound on process birth in epoch nanoseconds, or None.

    Breadcrumbs must postdate the current process, not its prior tty occupant.
    Round up by the kernel clock's resolution rather than accepting a stamp
    from earlier in the same microsecond/tick. Linux's whole-second btime is
    too coarse; sample realtime after BOOTTIME for a conservative epoch offset.
    Unknown sub-second telemetry never proves ownership.
    """
    if sys.platform == "darwin":
        birth = darwin_process_birth_ns(pid)
        return None if birth is None else birth + 1_000
    if not os.path.isfile("/proc/self/stat") or not hasattr(time, "CLOCK_BOOTTIME"):
        return None
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            data = handle.read(MAX_BREADCRUMB_BYTES + 1)
        if len(data) > MAX_BREADCRUMB_BYTES:
            return None
        close = data.rfind(b")")
        if close < 0:
            return None
        # comm (field 2) may hold spaces and parentheses, so fields are counted
        # from the final ')': field 3 is fields[0], starttime (22) fields[19].
        fields = data[close + 1 :].split()
        ticks = int(fields[19])
        hertz = os.sysconf("SC_CLK_TCK")
        boot_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        epoch_offset = time.time_ns() - boot_ns
    except (OSError, ValueError, IndexError):
        return None
    if hertz <= 0 or ticks < 0:
        return None
    return epoch_offset + ((ticks + 1) * 1_000_000_000 + hertz - 1) // hertz


def controlling_terminal_id(pid: int) -> str | None:
    """The terminal id OMP derives for the process's controlling tty, or None.

    ps reports the controlling terminal (ttys024, pts/3); OMP's terminal id
    strips any /dev/ prefix and hyphens the remaining slashes, so ttys024 and
    pts-3 are the breadcrumb filenames. No controlling terminal — ps answers
    ?? — or a name that could not be a plain file means no breadcrumb can
    ever be keyed to this process.
    """
    ps = shutil.which("ps")
    if not ps:
        return None
    output = run_text([ps, "-p", str(pid), "-o", "tty="], PS_TIMEOUT_S)
    if output is None:
        return None
    lines = output.strip().splitlines()
    if len(lines) != 1:
        return None
    tty = lines[0].strip()
    if tty.startswith("/dev/"):
        tty = tty[len("/dev/"):]
    terminal_id = tty.replace("/", "-")
    if not TERMINAL_ID_RE.match(terminal_id):
        return None
    return terminal_id


def terminal_breadcrumb(terminal_id: str, birth_ns: int, resumed_path: str = "") -> tuple[tuple, str, dict] | None:
    """One terminal's breadcrumb: (fingerprint, transcript path, header).

    The file is a verified bounded regular file. A stamp older than this process
    is refused unless its target is also the exact absolute --resume path in
    that process's argv: some OMP builds retain an unchanged resume breadcrumb.
    Sub-second birth bounds otherwise exclude prior tty occupants. The target is a main
    transcript in its encoded-cwd directory directly under this host's OMP
    session store: anything missing, empty, a child, an advisor nested one
    level deeper, or outside the store is refused. The fingerprint lets the
    stability re-check prove the file was not rewritten mid-read.
    """
    path = os.path.join(OMP_TERMINAL_SESSIONS_DIR, terminal_id)
    try:
        fd, _ = open_regular_file(path, MAX_BREADCRUMB_BYTES)
        try:
            info = os.fstat(fd)
            if info.st_mtime_ns < birth_ns and not resumed_path:
                return None
            content = b""
            while len(content) <= MAX_BREADCRUMB_BYTES:
                chunk = os.read(fd, min(65536, MAX_BREADCRUMB_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content += chunk
            if len(content) > MAX_BREADCRUMB_BYTES:
                return None
            fingerprint = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        finally:
            os.close(fd)
    except (ValueError, OSError):
        return None
    try:
        lines = content.decode("utf-8").strip().split("\n")
    except ValueError:
        return None
    if len(lines) < 2:
        return None
    target = lines[1]
    if not os.path.isabs(target) or not target.endswith(".jsonl"):
        return None
    if os.path.basename(target).startswith("__"):
        return None
    resolved = os.path.realpath(target)
    if info.st_mtime_ns < birth_ns and resolved != resumed_path:
        return None
    if not local_session_store(resolved):
        return None
    relative = os.path.relpath(resolved, os.path.realpath(OMP_SESSIONS_DIR))
    if len(relative.split(os.sep)) != 2:
        return None
    header = session_header(resolved)
    if header is None:
        return None
    return fingerprint, resolved, header


def names_parent(value) -> bool:
    """Whether a session record declares a parent session (a child transcript)."""
    if isinstance(value, str):
        return bool(value.strip())
    return value not in (None, "")


def session_header(path: str) -> dict | None:
    """The conversation header of one transcript, or None if it is not a main session.

    Only the bounded header prefix is read: the conversation's title and its
    later message content never enter this process.
    """
    try:
        fd, _ = open_regular_file(path, MAX_TRANSCRIPT_BYTES)
    except (ValueError, OSError):
        return None
    with os.fdopen(fd, "rb") as handle:
        total = 0
        for _ in range(MAX_DISCOVER_HEADER_LINES):
            line = handle.readline(MAX_TRANSCRIPT_LINE_BYTES + 1)
            if not line:
                break
            total += len(line)
            if total > MAX_DISCOVER_HEADER_BYTES or len(line) > MAX_TRANSCRIPT_LINE_BYTES:
                break
            try:
                record = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("type") != "session":
                continue
            identifier = clean(record.get("id"))
            if not identifier:
                return None
            # A session that names a parent is a child: its media belong to the
            # conversation that owns the pane, never to the pane itself.
            if names_parent(record.get("parentSession")):
                return None
            return {
                "id": identifier,
                "cwd": clean(record.get("cwd")),
                "title": clean(record.get("title")),
            }
    return None


def local_session_store(path: str) -> bool:
    """Whether a transcript lives in this host's own OMP session store."""
    root = os.path.realpath(OMP_SESSIONS_DIR)
    return path.startswith(root + os.sep) and os.path.isfile(path)


def cold_resumed_conversation(
    pane_id: str, window_key: str, foreground: dict, open_paths: list[str]
) -> tuple[dict, dict, str] | None:
    """The conversation of a live OMP that holds no transcript open.

    Idle OMP closes its transcript between turns. Its terminal breadcrumb must
    postdate this process, or agree with an explicit absolute resume argument.
    The process, argv, controlling terminal and breadcrumb fingerprint are
    checked twice; a changed or ambiguous conversation never binds the pane.
    """
    pid = foreground["pid"]
    birth_ns = process_birth_ns(pid)
    if birth_ns is None:
        return None
    terminal_id = controlling_terminal_id(pid)
    if terminal_id is None:
        return None
    argv = foreground.get("argv", [])
    argv = argv if isinstance(argv, list) else []
    if "--" in argv:
        argv = argv[: argv.index("--")]
    resumes = []
    for index, arg in enumerate(argv):
        if not isinstance(arg, str):
            continue
        if arg.startswith("--resume="):
            value = arg.split("=", 1)[1]
        elif arg in ("--resume", "-r"):
            value = argv[index + 1] if index + 1 < len(argv) else ""
        else:
            continue
        resumes.append(os.path.realpath(value) if isinstance(value, str) and os.path.isabs(value) else "")
    resumed_path = resumes[0] if len(resumes) == 1 else ""
    first = terminal_breadcrumb(terminal_id, birth_ns, resumed_path)
    if first is None:
        return None

    again = foreground_process(pane_id, window_key)
    if again is None or again.get("pid") != pid or not is_omp_process(again):
        return None
    if again.get("argv") != foreground.get("argv"):
        return None
    if open_transcript_paths(pid) != open_paths:
        return None
    if controlling_terminal_id(pid) != terminal_id:
        return None
    second = terminal_breadcrumb(terminal_id, birth_ns, resumed_path)
    if second is None or second != first:
        return None
    _, transcript, header = second
    return foreground, header, transcript


def active_conversation(pane_id: str, window_key: str = "") -> tuple[dict, dict, str] | None:
    """The pane's own live conversation: (foreground, header, transcript path).

    The process is identified before and after its transcript is read, and the
    transcript set must be identical both times: a discovery that ran into a
    session switch can never bind the pane to the conversation that left. A
    live OMP holding no transcript open — a resumed conversation idling
    between turns — is resolved through its terminal's own breadcrumb instead.
    """
    foreground = foreground_process(pane_id, window_key)
    if foreground is None or not is_omp_process(foreground):
        return None
    pid = foreground["pid"]
    paths = open_transcript_paths(pid)
    if paths is None:
        return None
    found: list[tuple[str, dict]] = []
    for path in paths:
        resolved = os.path.realpath(path)
        if not local_session_store(resolved):
            continue
        header = session_header(resolved)
        if header is not None:
            found.append((resolved, header))
    # OMP keeps an advisor transcript — and its other internal transcripts —
    # inside the active conversation's own stem directory. Such a file is never
    # the pane's conversation, whatever its header declares, so anything nested
    # under another candidate's stem is dropped. The advisor's header names no
    # parent, which is why the layout, not the header, is what excludes it.
    stems = [path[: -len(".jsonl")] for path, _ in found]
    candidates = [
        entry for entry in found if not any(entry[0].startswith(stem + os.sep) for stem in stems)
    ]
    if len(candidates) > 1:
        # Two open main transcripts are an ambiguity nothing — not even the
        # terminal's own breadcrumb — is allowed to resolve.
        return None
    if not candidates:
        return cold_resumed_conversation(pane_id, window_key, foreground, paths)
    transcript, header = candidates[0]

    again = foreground_process(pane_id, window_key)
    if again is None or again.get("pid") != pid or not is_omp_process(again):
        return None
    if open_transcript_paths(pid) != paths:
        return None
    if session_header(transcript) != header:
        return None
    return foreground, header, transcript


def op_discover(conn: sqlite3.Connection, root: str, req: dict) -> dict:
    """Import the media of the pane's own live conversation, then bind the pane.

    Everything is resolved from this host only: the pane's foreground process as
    the local Tern CLI reports it, and the transcript that process holds open in
    the local OMP session store. Nothing the caller sends is ever used as a
    path, so no other host's store and no unrelated conversation is reachable.
    """
    pane_id = clean(req.get("pane_id"))
    errors: list[dict] = []
    if not pane_id:
        errors.append({"name": "discover", "error": "discover requires the pane it belongs to"})
        return {"ok": True, "session": None, "items": [], "errors": errors}
    active = active_conversation(pane_id, clean(req.get("window_key")))
    if active is None:
        errors.append({"name": "discover", "error": "no live conversation is open in this pane"})
        return {"ok": True, "session": None, "items": [], "errors": errors}
    foreground, header, transcript = active

    # The conversation id is the OMP session id, exactly as the extension's own
    # binding names it, so imports land in the row the pane already works in.
    # Naming the pane is the binding, and it happens only now that the process
    # and its transcript have been confirmed twice.
    descriptor = {
        "id": header["id"],
        "cwd": header["cwd"] or clean(foreground.get("cwd")) or clean(req.get("cwd")),
        "label": clean(req.get("label")) or header["title"],
        "host": clean(req.get("host")),
        "pane_id": pane_id,
        "window_key": clean(req.get("window_key")),
        "tern_session_id": clean(req.get("tern_session_id")),
        "tern_session_name": clean(req.get("tern_session_name")),
    }
    result = op_backfill(conn, root, {"session": descriptor, "transcript_path": transcript})
    errors.extend(result["errors"])
    return {
        "ok": True,
        "session": find_session(conn, header["id"]),
        "items": result["items"],
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def dispatch(req: dict) -> dict:
    root = state_dir()
    conn = connect(root)
    try:
        op = clean(req.get("op"))
        # The connection runs in autocommit: each statement is atomic and
        # durable on its own, so no explicit commit is needed (or possible).
        if op == "touch":
            return {"ok": True, "session": upsert_session(conn, req.get("session"))}
        if op == "register":
            return op_register(conn, root, req)
        if op == "list":
            return op_list(conn, root, req)
        if op == "tag":
            return op_tag(conn, req)
        if op == "watch":
            return op_watch(conn, req)
        if op == "backfill":
            return op_backfill(conn, root, req)
        if op == "discover":
            return op_discover(conn, root, req)
        raise ValueError("unknown op: %s" % (op or "(missing)"))
    finally:
        conn.close()


def main() -> int:
    # Anything this process creates (db, WAL sidecars, blobs, previews) stays
    # owner-only even before the explicit chmod calls run.
    os.umask(0o077)
    raw = sys.stdin.read()
    try:
        request = json.loads(raw) if raw.strip() else None
        if not isinstance(request, dict):
            raise ValueError("request must be a single JSON object")
        response = dispatch(request)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    except sqlite3.Error as exc:
        print(json.dumps({"ok": False, "error": "database error: %s" % exc}))
        return 1
    except OSError as exc:
        print(json.dumps({"ok": False, "error": "storage error: %s" % exc.strerror}))
        return 1
    except Exception as exc:  # noqa: BLE001 - a crash must still answer in JSON
        print(json.dumps({"ok": False, "error": "internal error: %s" % type(exc).__name__}))
        return 1
    print(json.dumps(response))
    return 0


if __name__ == "__main__":
    sys.exit(main())
