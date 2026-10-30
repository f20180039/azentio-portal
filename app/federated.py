"""Optional federated-learning track (brief §11): a minimum credible demonstration on synthetic data.

Each enrolled client organisation has its own isolated environment holding synthetic SME loan records; only weight
updates and a sample count leave it. The coordinator validates and clips updates, rejects suspected poisoning, needs a
minimum number of participants, aggregates with sample-weighted averaging (FedAvg), evaluates on a separate holdout
and registers a candidate that a person must approve before it becomes the current version (the starting point for the next
round; nothing in the portal serves predictions from it). This is plain averaging: there is no secure
aggregation and no formal differential privacy, and the UI says so."""
import json
import math
import random
import time
from dataclasses import dataclass

from . import audit
from .auth import Ctx
from .constants import Action, ModelStatus, ParticipantOutcome, Permission, Role
from .db import tx
from .policy import Denied, require_client, require_grant

FEATURES = ["DSCR", "Leverage", "Current ratio", "Years trading", "Revenue volatility"]
MIN_PARTICIPANTS = 2
CLIP_NORM = 1.5
LOCAL_EPOCHS, LEARNING_RATE = 40, 0.3

# synthetic population per client: size and how its borrowers differ (the skew the report explains)
CLIENT_PROFILES = {
    "Kestrel Bank": {"n": 320, "seed": 11, "leverage_shift": -0.3, "volatility_shift": 0.0},
    "Northwind Finance": {"n": 220, "seed": 22, "leverage_shift": 0.6, "volatility_shift": 0.2},
    "Meridian Cooperative Bank": {"n": 90, "seed": 33, "leverage_shift": 0.0, "volatility_shift": 0.7},
    "Harbour Microfinance": {"n": 140, "seed": 44, "leverage_shift": 0.4, "volatility_shift": 0.5},
}
TRUE_WEIGHTS = [-1.1, 0.9, -0.6, -0.5, 0.8]  # standardised features; used only to generate synthetic labels
TRUE_BIAS = -1.2


def _synthetic(n: int, seed: int, leverage_shift: float = 0.0, volatility_shift: float = 0.0) -> list[tuple[list[float], int]]:
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        x = [rng.gauss(0, 1), rng.gauss(leverage_shift, 1), rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(volatility_shift, 1)]
        z = TRUE_BIAS + sum(w * v for w, v in zip(TRUE_WEIGHTS, x)) + rng.gauss(0, 0.5)
        rows.append((x, 1 if rng.random() < 1 / (1 + math.exp(-z)) else 0))
    return rows


HOLDOUT = _synthetic(400, 99, 0.2, 0.3)  # separate synthetic holdout, never used for training


def _sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def _predict(w: list[float], x: list[float]) -> float:
    return _sigmoid(w[-1] + sum(a * b for a, b in zip(w[:-1], x)))


class ClientEnvironment:
    """Stands in for training inside the client's own infrastructure. Records stay here; only the update leaves."""

    def __init__(self, name: str):
        p = CLIENT_PROFILES[name]
        self.name = name
        self.__records = _synthetic(p["n"], p["seed"], p["leverage_shift"], p["volatility_shift"])

    @property
    def size(self) -> int:
        return len(self.__records)

    def train(self, global_w: list[float]) -> list[float]:
        w = list(global_w)
        for _ in range(LOCAL_EPOCHS):
            grad = [0.0] * len(w)
            for x, y in self.__records:
                err = _predict(w, x) - y
                for i, v in enumerate(x):
                    grad[i] += err * v
                grad[-1] += err
            w = [wi - LEARNING_RATE * g / len(self.__records) for wi, g in zip(w, grad)]
        return [a - b for a, b in zip(w, global_w)]  # the update, not the data


def _norm(v: list[float]) -> float:
    return math.sqrt(sum(a * a for a in v))


def _cosine(a: list[float], b: list[float]) -> float:
    na, nb = _norm(a), _norm(b)
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def _auc(scores: list[float], labels: list[int]) -> float:
    pairs = sorted(zip(scores, labels))
    pos = sum(labels)
    neg = len(labels) - pos
    if not pos or not neg:
        return 0.5
    rank_sum = sum(i + 1 for i, (_, y) in enumerate(pairs) if y == 1)
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def evaluate(w: list[float]) -> dict:
    scores = [_predict(w, x) for x, _ in HOLDOUT]
    labels = [y for _, y in HOLDOUT]
    acc = sum((s >= 0.5) == bool(y) for s, y in zip(scores, labels)) / len(labels)
    return {"auc": round(_auc(scores, labels), 3), "accuracy": round(acc, 3)}


def _base_rate() -> dict:
    rate = sum(y for _, y in HOLDOUT) / len(HOLDOUT)
    return {"auc": 0.5, "accuracy": round(max(rate, 1 - rate), 3)}


@dataclass
class Participant:
    org: str
    samples: int
    outcome: ParticipantOutcome
    reason: str = ""
    update_norm: float = 0.0


def validate(updates: dict[str, list[float]]) -> dict[str, str]:
    """Rejects malformed updates and suspected poisoning: direction far from the coordinate-wise median, or a norm
    far above the others. Returns {org: reason} for rejected participants."""
    rejected = {}
    dim = len(FEATURES) + 1
    for org, u in updates.items():
        if len(u) != dim or not all(math.isfinite(v) for v in u):
            rejected[org] = "malformed update"
    clean = {o: u for o, u in updates.items() if o not in rejected}
    if len(clean) >= 2:
        median = [sorted(u[i] for u in clean.values())[len(clean) // 2] for i in range(dim)]
        norms = sorted(_norm(u) for u in clean.values())
        typical = norms[len(norms) // 2] or 1e-9
        for org, u in clean.items():
            if _cosine(u, median) < 0:
                rejected[org] = "points away from the other participants (possible poisoning)"
            elif _norm(u) > 5 * typical:
                rejected[org] = "norm far above the others (possible poisoning)"
    return rejected


def _clip(u: list[float]) -> list[float]:
    n = _norm(u)
    return [v * CLIP_NORM / n for v in u] if n > CLIP_NORM else u


def enrolled(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT e.*, o.name FROM fl_enrollments e JOIN orgs o ON o.id=e.org_id"
                                          " WHERE e.withdrawn_at IS NULL ORDER BY o.name")]


def deployed(conn):
    return conn.execute("SELECT * FROM fl_models WHERE status=? ORDER BY id DESC LIMIT 1", (ModelStatus.DEPLOYED,)).fetchone()


def run_round(conn, ctx: Ctx, dropouts: set[str], poisoned: str | None = None) -> int:
    """One federated round over enrolled organisations. `dropouts` and `poisoned` simulate the failure scenarios."""
    require_grant(ctx, Permission.PORTAL_ADMIN)
    base_row = deployed(conn)
    base = json.loads(base_row["weights"]) if base_row else [0.0] * (len(FEATURES) + 1)
    participants: list[Participant] = []
    updates: dict[str, list[float]] = {}
    sizes: dict[str, int] = {}
    for e in enrolled(conn):
        env = ClientEnvironment(e["name"])
        if e["name"] in dropouts:
            participants.append(Participant(e["name"], 0, ParticipantOutcome.DROPPED_OUT, "did not return an update"))
            continue
        u = env.train(base)
        if e["name"] == poisoned:
            u = [-8 * v for v in u]  # simulated malicious client: large update in the opposite direction
        updates[e["name"]], sizes[e["name"]] = u, env.size
    rejected = validate(updates)
    accepted = {o: _clip(u) for o, u in updates.items() if o not in rejected}
    for org, u in updates.items():
        outcome = ParticipantOutcome.REJECTED if org in rejected else ParticipantOutcome.ACCEPTED
        participants.append(Participant(org, sizes[org], outcome, rejected.get(org, ""), round(_norm(u), 3)))
    if len(accepted) < MIN_PARTICIPANTS:
        with tx(conn):
            conn.execute("INSERT INTO fl_rounds (started_by, base_model_id, participants, method, metrics, created_at) VALUES (?,?,?,?,?,?)",
                         (ctx.user_id, base_row["id"] if base_row else None, json.dumps([p.__dict__ for p in participants]),
                          "aborted", json.dumps({"error": f"fewer than {MIN_PARTICIPANTS} valid participants"}), time.time()))
        raise Denied(409, f"Round aborted: fewer than {MIN_PARTICIPANTS} valid participants. Nothing was aggregated.")
    total = sum(sizes[o] for o in accepted)
    aggregate = [sum(accepted[o][i] * sizes[o] / total for o in accepted) for i in range(len(base))]
    weights = [b + d for b, d in zip(base, aggregate)]
    local_only = {o: evaluate([b + d for b, d in zip(base, updates[o])]) for o in accepted}
    metrics = {"global": evaluate(weights), "base_rate": _base_rate(), "local_only": local_only,
               "previous": evaluate(base) if base_row else None, "participants": len(accepted), "samples": total}
    method = f"FedAvg weighted by sample count; updates clipped to L2 ≤ {CLIP_NORM}; minimum {MIN_PARTICIPANTS} participants"
    with tx(conn):
        rid = conn.execute("INSERT INTO fl_rounds (started_by, base_model_id, participants, method, metrics, created_at) VALUES (?,?,?,?,?,?)",
                           (ctx.user_id, base_row["id"] if base_row else None, json.dumps([p.__dict__ for p in participants]),
                            method, json.dumps(metrics), time.time())).lastrowid
        version = (conn.execute("SELECT COALESCE(MAX(version), 0) v FROM fl_models").fetchone()["v"]) + 1
        mid = conn.execute("INSERT INTO fl_models (version, round_id, weights, metrics, status, created_at) VALUES (?,?,?,?,?,?)",
                           (version, rid, json.dumps(weights), json.dumps(metrics), ModelStatus.CANDIDATE, time.time())).lastrowid
        audit.record(conn, ctx.user_id, "fl.round", f"fl_round:{rid}", {"candidate": mid, "accepted": sorted(accepted), "rejected": rejected,
                                                                         "dropouts": sorted(dropouts)})
    return mid


def decide(conn, ctx: Ctx, mid: int, action: str, note: str) -> None:
    """Human decision: approve a candidate as the current version, reject it, or roll the current version back to the previous one."""
    require_grant(ctx, Permission.PORTAL_ADMIN)
    m = conn.execute("SELECT * FROM fl_models WHERE id=?", (mid,)).fetchone()
    if not m:
        raise Denied(404, "Model not found.")
    if len(note.strip()) < 5:
        raise Denied(422, "Record the reason for this decision (at least 5 characters).")
    now = time.time()
    with tx(conn):
        if action == Action.DEPLOY and m["status"] == ModelStatus.CANDIDATE:
            conn.execute("UPDATE fl_models SET status=? WHERE status=?", (ModelStatus.RETIRED, ModelStatus.DEPLOYED))
            conn.execute("UPDATE fl_models SET status=?, decided_by=?, decision_note=?, decided_at=? WHERE id=?",
                         (ModelStatus.DEPLOYED, ctx.user_id, note.strip(), now, mid))
        elif action == Action.REJECT and m["status"] == ModelStatus.CANDIDATE:
            conn.execute("UPDATE fl_models SET status=?, decided_by=?, decision_note=?, decided_at=? WHERE id=?",
                         (ModelStatus.REJECTED, ctx.user_id, note.strip(), now, mid))
        elif action == Action.ROLLBACK and m["status"] == ModelStatus.DEPLOYED:
            previous = conn.execute("SELECT id FROM fl_models WHERE status=? AND id<? ORDER BY id DESC LIMIT 1", (ModelStatus.RETIRED, mid)).fetchone()
            if not previous:
                raise Denied(409, "There is no earlier deployed model to roll back to.")
            conn.execute("UPDATE fl_models SET status=?, decision_note=?, decided_at=? WHERE id=?", (ModelStatus.RETIRED, "rolled back: " + note.strip(), now, mid))
            conn.execute("UPDATE fl_models SET status=? WHERE id=?", (ModelStatus.DEPLOYED, previous["id"]))
        else:
            raise Denied(409, "That decision does not apply to this model's current status.")
        audit.record(conn, ctx.user_id, f"fl.model_{action}", f"fl_model:{mid}", {"note": note.strip()[:200]})


def set_consent(conn, ctx: Ctx, opt_in: bool) -> None:
    """Programme consent is separate from membership and from sharing consent; only the client administrator decides."""
    require_client(ctx)
    if ctx.role != Role.ADMIN:
        raise Denied(403, "Only your organisation's administrator can change programme consent.")
    if ctx.org["name"] not in CLIENT_PROFILES:
        raise Denied(409, "This organisation has no synthetic dataset in the demonstration.")
    with tx(conn):
        if opt_in:
            conn.execute("INSERT INTO fl_enrollments (org_id, consented_by, consented_at) VALUES (?,?,?)"
                         " ON CONFLICT(org_id) DO UPDATE SET consented_by=excluded.consented_by, consented_at=excluded.consented_at, withdrawn_at=NULL",
                         (ctx.org_id, ctx.user_id, time.time()))
        else:
            conn.execute("UPDATE fl_enrollments SET withdrawn_at=? WHERE org_id=? AND withdrawn_at IS NULL", (time.time(), ctx.org_id))
        audit.record(conn, ctx.user_id, "fl.consent_" + ("given" if opt_in else "withdrawn"), f"org:{ctx.org_id}")


def overview(conn) -> dict:
    rounds = [dict(r) | {"participants": json.loads(r["participants"]), "metrics": json.loads(r["metrics"])}
              for r in conn.execute("SELECT * FROM fl_rounds ORDER BY id DESC LIMIT 10")]
    models = [dict(r) | {"metrics": json.loads(r["metrics"])} for r in conn.execute("SELECT * FROM fl_models ORDER BY id DESC LIMIT 10")]
    return {"enrolled": enrolled(conn), "rounds": rounds, "models": models, "features": FEATURES,
            "profiles": CLIENT_PROFILES, "holdout": len(HOLDOUT), "min_participants": MIN_PARTICIPANTS, "clip": CLIP_NORM}
