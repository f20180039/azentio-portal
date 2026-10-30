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
from .db import tx
from .policy import Denied

ATTEMPT_TTL = 600


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


def start(conn, next_path: str) -> str:
    """Returns the Microsoft authorization URL for a fresh, single-use attempt."""
    cfg = _cfg()
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with tx(conn):
        conn.execute("DELETE FROM oidc_attempts WHERE expires_at < ?", (time.time(),))
        conn.execute("INSERT INTO oidc_attempts VALUES (?,?,?,?,?)", (state, nonce, verifier, next_path, time.time() + ATTEMPT_TTL))
    params = httpx.QueryParams({"client_id": cfg["client_id"], "response_type": "code", "redirect_uri": cfg["redirect"],
                                "response_mode": "query", "scope": "openid email profile", "state": state, "nonce": nonce,
                                "code_challenge": challenge, "code_challenge_method": "S256"})
    return f"{cfg['authorize']}?{params}"


def exchange_code(cfg: dict, code: str, verifier: str) -> str:
    resp = httpx.post(cfg["token"], timeout=15, data={"client_id": cfg["client_id"], "client_secret": cfg["secret"], "grant_type": "authorization_code",
                                                      "code": code, "redirect_uri": cfg["redirect"], "code_verifier": verifier})
    if resp.status_code != 200 or "id_token" not in resp.json():
        raise Denied(401, "Microsoft sign-in did not complete. Try again.")
    return resp.json()["id_token"]


def signing_key(cfg: dict, token: str):
    return jwt.PyJWKClient(cfg["jwks"]).get_signing_key_from_jwt(token).key


def finish(conn, code: str, state: str) -> tuple[str, str]:
    """Validates the callback and returns (verified email, next path). Every failure is a plain 401 with no detail leaked."""
    cfg = _cfg()
    with tx(conn):
        attempt = conn.execute("SELECT * FROM oidc_attempts WHERE state=?", (state,)).fetchone()
        conn.execute("DELETE FROM oidc_attempts WHERE state=?", (state,))  # single use, even when it fails
    if not attempt or attempt["expires_at"] < time.time() or not code:
        raise Denied(401, "This sign-in link has expired or was already used. Start again.")
    token = exchange_code(cfg, code, attempt["verifier"])
    try:
        claims = jwt.decode(token, signing_key(cfg, token), algorithms=["RS256"], audience=cfg["client_id"], issuer=cfg["issuer"],
                            options={"require": ["exp", "iat", "iss", "aud", "nonce"]})
    except jwt.PyJWTError:
        raise Denied(401, "Microsoft sign-in could not be verified.")
    if claims.get("nonce") != attempt["nonce"] or claims.get("tid") != cfg["tenant"]:
        raise Denied(401, "Microsoft sign-in could not be verified.")
    email = (claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    if not email.endswith("@" + cfg["domain"]):
        raise Denied(403, f"Use your @{cfg['domain']} work account.")
    return email, attempt["next"]


def sign_in(conn, email: str) -> int:
    """Finds or creates the user. A new account has no membership, so it reaches nothing until an administrator approves it."""
    with tx(conn):
        row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        user_id = row["id"] if row else conn.execute("INSERT INTO users (email, name) VALUES (?,?)",
                                                     (email, email.split("@")[0].replace(".", " ").title())).lastrowid
        audit.record(conn, user_id, "auth.signed_in", f"user:{user_id}", {"method": "entra_id"})
    return user_id
