"""
LiveKit access token minting.

A LiveKit token is a plain HS256 JWT carrying a "video" grant — there is
no proprietary handshake. That matters architecturally: any language that
can sign a JWT can mint one, so the Drupal control plane issues browser
tokens directly without calling this service (see BACKEND-FRONTEND-STACK).

Implemented on the standard library so it runs before any pip install.
"""

import base64
import hashlib
import hmac
import json
import time


def _b64(raw: bytes) -> str:
    """Base64url without padding, as JWT requires."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def mint(api_key: str, api_secret: str, identity: str,
         grant: dict, ttl_seconds: int = 300) -> str:
    """
    Sign a LiveKit access token.

    ttl_seconds covers JOINING the room, not the length of the call. Once
    a participant is connected the session lasts as long as the call, so
    a short expiry here is correct and a common thing to get wrong.
    """
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": api_key,
        "sub": identity,
        "nbf": now - 5,          # small skew allowance
        "exp": now + ttl_seconds,
        "video": grant,
    }

    signing_input = "{}.{}".format(
        _b64(json.dumps(header, separators=(",", ":")).encode()),
        _b64(json.dumps(payload, separators=(",", ":")).encode()),
    )
    signature = hmac.new(
        api_secret.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    return "{}.{}".format(signing_input, _b64(signature))


def admin_token(api_key: str, api_secret: str) -> str:
    """Server-side token for room management calls."""
    return mint(api_key, api_secret, "server", {
        "roomCreate": True,
        "roomList": True,
        "roomAdmin": True,
    }, ttl_seconds=60)


def join_token(api_key: str, api_secret: str, room: str, identity: str) -> str:
    """Browser token: may join exactly one named room and speak in it."""
    return mint(api_key, api_secret, identity, {
        "room": room,
        "roomJoin": True,
        "canPublish": True,
        "canSubscribe": True,
        # Caller can publish audio and data messages (transcripts, UI actions, prompts)
        "canPublishData": True,
    }, ttl_seconds=300)
