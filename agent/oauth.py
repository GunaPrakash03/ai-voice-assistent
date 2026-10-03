"""Task 5.2b — Sign up / sign in with Google or Microsoft (OpenID Connect, authorization-code + PKCE).

Flow:
  1. /api/v1/auth/oauth/<provider>/start   -> begin(): random state, nonce and PKCE verifier kept
     server-side (10 min) and bound to the browser by an HttpOnly cookie; redirect to the provider.
  2. /api/v1/auth/oauth/<provider>/callback -> finish(): state + cookie must match, the code is
     exchanged at the token endpoint (client secret + PKCE), and the ID token's iss/aud/exp/nonce
     are checked. The ID token comes straight from the token endpoint over TLS, so per OIDC Core
     3.1.3.7 its signature does not need separate verification.

Identity is the provider's stable account id (Google `sub`, Microsoft `tid:oid`), never the email
alone: Microsoft's email claim is not verified, and matching on it is a known account-takeover bug.
Configured through env vars; a provider without credentials is simply not offered.
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

log = logging.getLogger("voice-agent.oauth")

STATE_TTL = 600            # seconds between "start" and the provider sending the browser back
SOCIAL_SIGNUP_TTL = 1800   # seconds a verified Google/Microsoft identity waits for the rest of sign-up
STATE_COOKIE = "va_oauth"


def _provider_config(provider: str) -> Optional[Dict[str, Any]]:
    if provider == "google":
        cid, secret = os.getenv("GOOGLE_CLIENT_ID", "").strip(), os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
        if not (cid and secret):
            return None
        return {
            "name": "Google", "client_id": cid, "client_secret": secret,
            "auth_url": os.getenv("GOOGLE_OAUTH_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth"),
            "token_url": os.getenv("GOOGLE_OAUTH_TOKEN_URL", "https://oauth2.googleapis.com/token"),
            "issuers": ("https://accounts.google.com", "accounts.google.com"),
        }
    if provider == "microsoft":
        cid, secret = os.getenv("MICROSOFT_CLIENT_ID", "").strip(), os.getenv("MICROSOFT_CLIENT_SECRET", "").strip()
        if not (cid and secret):
            return None
        tenant = os.getenv("MICROSOFT_TENANT", "common").strip() or "common"
        base = os.getenv("MICROSOFT_OAUTH_BASE_URL", "https://login.microsoftonline.com").rstrip("/")
        return {
            "name": "Microsoft", "client_id": cid, "client_secret": secret, "tenant": tenant,
            "auth_url": f"{base}/{tenant}/oauth2/v2.0/authorize",
            "token_url": f"{base}/{tenant}/oauth2/v2.0/token",
            "issuer_base": base,
        }
    return None


def configured_providers() -> List[str]:
    return [p for p in ("google", "microsoft") if _provider_config(p)]


def provider_name(provider: str) -> str:
    return {"google": "Google", "microsoft": "Microsoft"}.get(provider, provider)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _now() -> float:
    return time.time()


class OAuthError(Exception):
    """Shown to the user (via a redirect) — keep messages safe and short."""


class OAuthManager:
    def __init__(self):
        self._states: Dict[str, Dict[str, Any]] = {}
        self._social: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _prune(store: Dict[str, Dict[str, Any]]) -> None:
        now = _now()
        for k in [k for k, v in store.items() if v["expires_at"] < now]:
            store.pop(k, None)

    # ── Step 1 ───────────────────────────────────────────────────────────
    def begin(self, provider: str, intent: str, redirect_uri: str) -> Dict[str, str]:
        """Returns {"url": provider authorization URL, "browser_key": value for the state cookie}."""
        cfg = _provider_config(provider)
        if not cfg:
            raise OAuthError(f"Sign-in with {provider_name(provider)} is not set up on this server")
        if intent not in ("signup", "login"):
            raise OAuthError("Unknown sign-in intent")
        self._prune(self._states)
        state, nonce, verifier, browser_key = (secrets.token_urlsafe(32) for _ in range(4))
        self._states[state] = {
            "provider": provider, "intent": intent, "nonce": nonce, "verifier": verifier,
            "redirect_uri": redirect_uri, "browser": hashlib.sha256(browser_key.encode()).hexdigest(),
            "expires_at": _now() + STATE_TTL,
        }
        params = {
            "client_id": cfg["client_id"], "response_type": "code", "redirect_uri": redirect_uri,
            "scope": "openid email profile", "state": state, "nonce": nonce,
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return {"url": cfg["auth_url"] + "?" + urllib.parse.urlencode(params), "browser_key": browser_key}

    # ── Step 2 ───────────────────────────────────────────────────────────
    def _exchange(self, cfg: Dict[str, Any], code: str, verifier: str, redirect_uri: str) -> Dict[str, Any]:
        body = urllib.parse.urlencode({
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": cfg["client_id"], "client_secret": cfg["client_secret"], "code_verifier": verifier,
        }).encode()
        req = urllib.request.Request(cfg["token_url"], data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read()[:300]
            log.warning("%s token exchange failed (%s): %s", cfg["name"], e.code, detail)
            raise OAuthError(f"{cfg['name']} did not accept the sign-in. Please try again.")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            log.warning("%s token exchange error: %s", cfg["name"], e)
            raise OAuthError(f"Could not reach {cfg['name']}. Please try again.")

    @staticmethod
    def _claims(id_token: str) -> Dict[str, Any]:
        try:
            payload = id_token.split(".")[1]
            return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except Exception:
            raise OAuthError("The sign-in response was not readable. Please try again.")

    def finish(self, provider: str, code: str, state: str, browser_key: str) -> Dict[str, Any]:
        """Validate the callback and return the verified identity plus the original intent."""
        self._prune(self._states)
        rec = self._states.pop(state or "", None)
        if not rec or rec["provider"] != provider:
            raise OAuthError("This sign-in link has expired. Please start again.")
        if not browser_key or not secrets.compare_digest(hashlib.sha256(browser_key.encode()).hexdigest(), rec["browser"]):
            raise OAuthError("This sign-in was started in a different browser. Please start again.")
        if not code:
            raise OAuthError("Sign-in was cancelled.")
        cfg = _provider_config(provider)
        if not cfg:
            raise OAuthError(f"Sign-in with {provider_name(provider)} is not set up on this server")

        tokens = self._exchange(cfg, code, rec["verifier"], rec["redirect_uri"])
        if not tokens.get("id_token"):
            raise OAuthError(f"{cfg['name']} did not return an identity. Please try again.")
        c = self._claims(tokens["id_token"])

        aud = c.get("aud")
        if (aud if isinstance(aud, list) else [aud]).count(cfg["client_id"]) != 1:
            raise OAuthError("The sign-in response was for a different app.")
        if float(c.get("exp", 0)) < _now() - 60:
            raise OAuthError("The sign-in response has expired. Please try again.")
        if not c.get("nonce") or not secrets.compare_digest(str(c["nonce"]), rec["nonce"]):
            raise OAuthError("The sign-in response could not be matched to this request.")

        if provider == "google":
            if c.get("iss") not in cfg["issuers"]:
                raise OAuthError("The sign-in response came from an unexpected issuer.")
            email = str(c.get("email") or "").strip().lower()
            if not email or c.get("email_verified") not in (True, "true"):
                raise OAuthError("Your Google account's email address isn't verified.")
            identity = f"google:{c['sub']}"
            email_verified = True
        else:
            tid, oid = str(c.get("tid") or ""), str(c.get("oid") or "")
            if not re.fullmatch(r"[0-9a-f-]{36}", tid) or not oid:
                raise OAuthError("The Microsoft sign-in response was incomplete.")
            if c.get("iss") != f"{cfg['issuer_base']}/{tid}/v2.0":
                raise OAuthError("The sign-in response came from an unexpected issuer.")
            email = str(c.get("email") or c.get("preferred_username") or "").strip().lower()
            if "@" not in email:
                raise OAuthError("Your Microsoft account didn't share an email address.")
            identity = f"microsoft:{tid}:{oid}"
            email_verified = False   # Microsoft does not guarantee the email claim is verified

        return {"provider": provider, "intent": rec["intent"], "identity": identity, "email": email,
                "email_verified": email_verified, "name": str(c.get("name") or "").strip()[:120]}

    # ── Hand-off to the sign-up form ─────────────────────────────────────
    def stash_signup(self, ident: Dict[str, Any]) -> str:
        """Keep a verified identity for the rest of sign-up; the page gets an opaque token."""
        self._prune(self._social)
        token = secrets.token_urlsafe(24)
        self._social[hashlib.sha256(token.encode()).hexdigest()] = {**ident, "expires_at": _now() + SOCIAL_SIGNUP_TTL}
        return token

    def peek_signup(self, token: str) -> Optional[Dict[str, Any]]:
        self._prune(self._social)
        return self._social.get(hashlib.sha256((token or "").encode()).hexdigest())

    def consume_signup(self, token: str) -> None:
        self._social.pop(hashlib.sha256((token or "").encode()).hexdigest(), None)


oauth_manager = OAuthManager()
