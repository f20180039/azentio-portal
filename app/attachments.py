"""Observation attachments: typed, size-limited, stored privately with the source record and served only to people
who may see that record. They never travel into a shared version, search or AIRA."""
import re
import time
from dataclasses import dataclass

from . import audit
from .auth import Ctx
from .db import tx
from .learning import get_observation, scan_sensitive
from .policy import Denied, can_edit_observation, via_support

MAX_BYTES = 2_000_000
MAX_PER_RECORD = 5


@dataclass(frozen=True)
class FileType:
    mime: str
    magic: bytes | None  # leading bytes the content must start with; None for UTF-8 text


ALLOWED = {
    ".pdf": FileType("application/pdf", b"%PDF-"),
    ".png": FileType("image/png", b"\x89PNG\r\n\x1a\n"),
    ".jpg": FileType("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": FileType("image/jpeg", b"\xff\xd8\xff"),
    ".csv": FileType("text/csv", None),
    ".txt": FileType("text/plain", None),
}


def _type_for(filename: str, data: bytes) -> FileType:
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    ftype = ALLOWED.get(ext)
    if not ftype:
        raise Denied(415, "Attach a PDF, PNG, JPEG, CSV or TXT file.")
    if ftype.magic is not None and not data.startswith(ftype.magic):
        raise Denied(415, "The file's contents don't match its type.")
    if ftype.magic is None:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise Denied(415, "Text files must be UTF-8.")
    return ftype


def listing(conn, obs_id: int):
    return conn.execute("SELECT id, filename, mime, size, flags, created_at FROM observation_attachments WHERE observation_id=? ORDER BY id",
                        (obs_id,)).fetchall()


def attach(conn, ctx: Ctx, obs_id: int, filename: str, data: bytes, declared: bool) -> int:
    obs = get_observation(conn, ctx, obs_id)
    if not can_edit_observation(ctx, obs):
        raise Denied(403, "Only the author can attach files.")
    if not declared:
        raise Denied(422, "Confirm the file contains no customer identities, account numbers or confidential terms.")
    if not 0 < len(data) <= MAX_BYTES:
        raise Denied(413, f"Files must be between 1 byte and {MAX_BYTES // 1_000_000} MB.")
    if len(listing(conn, obs_id)) >= MAX_PER_RECORD:
        raise Denied(409, f"A record can hold at most {MAX_PER_RECORD} attachments.")
    ftype = _type_for(filename, data)
    safe_name = re.sub(r"[^A-Za-z0-9._\- ]", "_", filename)[:100] or "attachment"
    flags = ", ".join(scan_sensitive(data.decode("utf-8"))) if ftype.magic is None else ""
    with tx(conn):
        aid = conn.execute("INSERT INTO observation_attachments (observation_id, uploader_id, filename, mime, size, content, flags, created_at)"
                           " VALUES (?,?,?,?,?,?,?,?)", (obs_id, ctx.user_id, safe_name, ftype.mime, len(data), data, flags, time.time())).lastrowid
        audit.record(conn, ctx.user_id, "attachment.added", f"observation:{obs_id}", {"attachment": aid, "bytes": len(data), "flags": flags})
    return aid


def fetch(conn, ctx: Ctx, obs_id: int, aid: int):
    obs = get_observation(conn, ctx, obs_id)  # same audience as the record, including the moderator and support rules
    row = conn.execute("SELECT * FROM observation_attachments WHERE id=? AND observation_id=?", (aid, obs_id)).fetchone()
    if not row:
        raise Denied(404, "Attachment not found.")
    if via_support(ctx, obs):
        with tx(conn):
            audit.record(conn, ctx.user_id, "support.attachment_read", f"observation:{obs_id}", {"attachment": aid})
    return row


def remove(conn, ctx: Ctx, obs_id: int, aid: int) -> None:
    obs = get_observation(conn, ctx, obs_id)
    if not can_edit_observation(ctx, obs):
        raise Denied(403, "Only the author can remove attachments.")
    with tx(conn):
        if not conn.execute("DELETE FROM observation_attachments WHERE id=? AND observation_id=?", (aid, obs_id)).rowcount:
            raise Denied(404, "Attachment not found.")
        audit.record(conn, ctx.user_id, "attachment.removed", f"observation:{obs_id}", {"attachment": aid})
