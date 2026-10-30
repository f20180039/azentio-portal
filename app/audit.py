import hashlib
import json
import time


def record(conn, actor_id, action: str, target: str, detail: dict | None = None) -> None:
    """Append to the hash-chained audit log. Callers run inside a transaction."""
    prev = conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = prev["hash"] if prev else "genesis"
    at = time.time()
    body = json.dumps(detail or {}, sort_keys=True)
    digest = hashlib.sha256(f"{prev_hash}|{at}|{actor_id}|{action}|{target}|{body}".encode()).hexdigest()
    conn.execute(
        "INSERT INTO audit (at, actor_id, action, target, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
        (at, actor_id, action, target, body, prev_hash, digest),
    )


def verify_chain(conn) -> bool:
    prev_hash = "genesis"
    for row in conn.execute("SELECT * FROM audit ORDER BY id"):
        expected = hashlib.sha256(
            f"{prev_hash}|{row['at']}|{row['actor_id']}|{row['action']}|{row['target']}|{row['detail']}".encode()
        ).hexdigest()
        if row["prev_hash"] != prev_hash or row["hash"] != expected:
            return False
        prev_hash = row["hash"]
    return True
