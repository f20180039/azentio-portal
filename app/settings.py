"""Configurator: typed portal settings with limits, defaults from the environment, and an audited change history."""
import os
import time
from dataclasses import dataclass

from . import audit
from .auth import Ctx
from .constants import Permission, Setting
from .db import tx
from .policy import Denied, require_grant


@dataclass(frozen=True)
class Spec:
    label: str
    help: str
    group: str
    default: int | bool
    low: int = 0
    high: int = 1

    @property
    def is_flag(self) -> bool:
        return isinstance(self.default, bool)


SPECS: dict[Setting, Spec] = {
    Setting.MIN_COHORT: Spec("Minimum contributing organisations", "Themes, their items and filtered results stay hidden from clients below this. "
                             "The brief requires at least 3 for judging.", "Privacy", int(os.environ.get("MIN_COHORT", "3")), 3, 20),
    Setting.SMALL_SAMPLE: Spec("Small-sample warning below", "Themes with fewer organisations than this carry a small-sample warning.",
                               "Privacy", int(os.environ.get("SMALL_SAMPLE", "5")), 3, 50),
    Setting.SUPPORT_MINUTES: Spec("Support access lasts (minutes)", "How long a client-approved support grant stays active.", "Access", 30, 5, 240),
    Setting.INVITE_DAYS: Spec("Invitations expire after (days)", "Applies to invitations created from now on.", "Access", 7, 1, 30),
    Setting.AIRA_ENABLED: Spec("AIRA summaries", "When off, AIRA lists the matching approved contributions without calling any AI model.", "AI", True),
    Setting.AIRA_PER_MINUTE: Spec("AIRA questions per person per minute", "Rate limit for AIRA and Playground explanations.", "AI", 8, 1, 60),
    Setting.PUBLISHING_PAUSED: Spec("Pause new shared publications", "Moderators cannot publish while paused. Already published items stay visible "
                                    "and withdrawals still work.", "Operations", False),
}


def _decode(spec: Spec, raw: str) -> int | bool:
    return raw == "1" if spec.is_flag else int(raw)


def get(conn, key: Setting) -> int | bool:
    spec = SPECS[key]
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return _decode(spec, row["value"]) if row else spec.default


def overview(conn, ctx: Ctx) -> list[dict]:
    require_grant(ctx, Permission.PORTAL_ADMIN)
    rows = {r["key"]: r for r in conn.execute("SELECT s.*, u.name FROM settings s LEFT JOIN users u ON u.id=s.updated_by")}
    return [{"key": key, "spec": spec, "value": get(conn, key), "changed_by": rows[key]["name"] if key in rows else None,
             "changed_at": rows[key]["updated_at"] if key in rows else None} for key, spec in SPECS.items()]


def update(conn, ctx: Ctx, form: dict) -> list[str]:
    """Validates every field before saving any; returns the labels that changed."""
    require_grant(ctx, Permission.PORTAL_ADMIN)
    new: dict[Setting, int | bool] = {}
    for key, spec in SPECS.items():
        if spec.is_flag:
            new[key] = form.get(key) == "on"
            continue
        raw = (form.get(key) or "").strip()
        if not raw.isdigit() or not spec.low <= int(raw) <= spec.high:
            raise Denied(422, f"{spec.label} must be a whole number from {spec.low} to {spec.high}.")
        new[key] = int(raw)
    if new[Setting.SMALL_SAMPLE] < new[Setting.MIN_COHORT]:
        raise Denied(422, "The small-sample warning cannot start below the minimum cohort.")
    changed = []
    with tx(conn):
        for key, value in new.items():
            old = get(conn, key)
            if value == old:
                continue
            conn.execute("INSERT INTO settings (key, value, updated_by, updated_at) VALUES (?,?,?,?)"
                         " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                         (key, "1" if value is True else "0" if value is False else str(value), ctx.user_id, time.time()))
            audit.record(conn, ctx.user_id, "config.changed", f"setting:{key}", {"from": old, "to": value})
            changed.append(SPECS[key].label)
    return changed
