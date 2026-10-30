"""Microsoft Entra ID sign-in (OpenID Connect authorization code flow with PKCE), enabled only when configured.
A verified identity is not access: the normal membership rules still decide what the person can do."""
import base64
import hashlib
import os
import secrets
import time

import httpx
import jwt

from . import audit
from .auth import require_active
from .db import tx
from .policy import Denied

ATTEMPT_TTL = 600
COOKIE = "azp_oidc"  # binds the attempt to the browser that started it (login CSRF protection)


def browser_binding(state: str) -> str:
    return hashlib.sha256(("oidc:" + state).encode()).hexdigest()


def config() -> dict | None:
    tenant, client, secret = (os.environ.get(k) for k in ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET"))
    if not (tenant and client and secret):
        return None
    base = f"https://login.microsoftonline.com/{tenant}"
    return {"tenant": tenant, "client_id": client, "secret": secret, "issuer": f"{base}/v2.0",
            "authorize": f"{base}/oauth2/v2.0/authorize", "token": f"{base}/oauth2/v2.0/token", "jwks": f"{base}/discovery/v2.0/keys",
            "domain": os.environ.get("ENTRA_ALLOWED_DOMAIN", "azentio.com").lower(),
            "redirect": os.environ.get("PUBLIC_URL", "https://azentio-portal.onrender.com").rstrip("/") + "/auth/entra/callback"}


def enabled() -> bool:
    return config() is not None


def _cfg() -> dict:
    cfg = config()
    if not cfg:
        raise Denied(404, "Not found.")
    return cfg


def start(conn, next_path: str) -> tuple[str, str]:
    """Returns (Microsoft authorization URL, state) for a fresh, single-use attempt; the caller sets the binding cookie."""
    cfg = _cfg()
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with tx(conn):
        conn.execute("DELETE FROM oidc_attempts WHERE expires_at < ?", (time.time(),))
        conn.execute("INSERT INTO oidc_attempts VALUES (?,?,?,?,?)", (state, nonce, verifier, next_path, time.time() + ATTEMPT_TTL))
    params = httpx.QueryParams({"client_id": cfg["client_id"], "response_type": "code", "redirect_uri": cfg["redirect"],
                                "response_mode": "query", "scope": "openid email profile", "state": state, "nonce": nonce,
                                "code_challenge": challenge, "code_challenge_method": "S256"})
    return f"{cfg['authorize']}?{params}", state


def exchange_code(cfg: dict, code: str, verifier: str) -> str:
    resp = httpx.post(cfg["token"], timeout=15, data={"client_id": cfg["client_id"], "client_secret": cfg["secret"], "grant_type": "authorization_code",
                                                      "code": code, "redirect_uri": cfg["redirect"], "code_verifier": verifier})
    if resp.status_code != 200 or "id_token" not in resp.json():
        raise Denied(401, "Microsoft sign-in did not complete. Try again.")
    return resp.json()["id_token"]


def signing_key(cfg: dict, token: str):
    return jwt.PyJWKClient(cfg["jwks"]).get_signing_key_from_jwt(token).key


def finish(conn, code: str, state: str, binding: str | None) -> tuple[dict, str]:
    """Validates the callback and returns (identity, next path). Every failure is a plain 401 with no detail leaked."""
    cfg = _cfg()
    with tx(conn):
        attempt = conn.execute("SELECT * FROM oidc_attempts WHERE state=?", (state,)).fetchone()
        conn.execute("DELETE FROM oidc_attempts WHERE state=?", (state,))  # single use, even when it fails
    if not attempt or attempt["expires_at"] < time.time() or not code:
        raise Denied(401, "This sign-in link has expired or was already used. Start again.")
    if not binding or not secrets.compare_digest(binding, browser_binding(state)):
        raise Denied(401, "This sign-in was started in a different browser. Start again here.")
    token = exchange_code(cfg, code, attempt["verifier"])
    try:
        claims = jwt.decode(token, signing_key(cfg, token), algorithms=["RS256"], audience=cfg["client_id"], issuer=cfg["issuer"],
                            options={"require": ["exp", "iat", "iss", "aud", "nonce"]})
    except jwt.PyJWTError:
        raise Denied(401, "Microsoft sign-in could not be verified.")
    if claims.get("nonce") != attempt["nonce"] or claims.get("tid") != cfg["tenant"]:
        raise Denied(401, "Microsoft sign-in could not be verified.")
    # the sign-in name (UPN) is set by the tenant's administrators; the optional 'email' claim is user-editable, so it never decides
    email = (claims.get("preferred_username") or "").strip().lower()
    if not email.endswith("@" + cfg["domain"]) or not claims.get("oid"):
        raise Denied(403, f"Use your @{cfg['domain']} work account.")
    return {"email": email, "subject": f"{claims['tid']}:{claims['oid']}"}, attempt["next"]


def sign_in(conn, identity: dict) -> int:
    """Signs in the user bound to this Microsoft identity, binding it on first use. A user already bound to a different
    identity is refused. A new account has no membership, so it reaches nothing until an administrator approves it."""
    email, subject = identity["email"], identity["subject"]
    with tx(conn):
        bound = conn.execute("SELECT user_id FROM entra_identities WHERE subject=?", (subject,)).fetchone()
        if bound:
            user_id = bound["user_id"]
        else:
            row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if row and conn.execute("SELECT 1 FROM entra_identities WHERE user_id=?", (row["id"],)).fetchone():
                raise Denied(403, "This portal account is linked to a different Microsoft identity.")
            user_id = row["id"] if row else conn.execute("INSERT INTO users (email, name) VALUES (?,?)",
                                                         (email, email.split("@")[0].replace(".", " ").title())).lastrowid
            conn.execute("INSERT INTO entra_identities VALUES (?,?,?)", (subject, user_id, time.time()))
        require_active(conn, user_id)
        audit.record(conn, user_id, "auth.signed_in", f"user:{user_id}", {"method": "entra_id"})
    return user_id
