"""OAuth 2.0 provider for Uptycs API key/secret credentials.

Claude.ai sends key as OAuth Client ID and secret as OAuth Client Secret.
At /authorize only client_id is available (auto-approve).
At /token the client_secret arrives via client_secret_post — we capture it
and validate by calling GET /juno/investigations?limit=1.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from pydantic import AnyUrl

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .auth import ApiKey, auth_headers
from .tenant import resolve_host

logger = logging.getLogger("juno_mcp.oauth")


def _parse_credentials(
    client_id: str,
    client_secret: str,
    fallback_domain: str,
    fallback_customer_id: str,
    fallback_domain_suffix: str,
    *,
    path_domain: str = "",
    path_domain_suffix: str = "",
) -> ApiKey:
    """Parse tenant-encoded OAuth credentials into an ApiKey.

    Priority for domain resolution:
      1. Explicit ``hostname:api_key`` in *client_id*
      2. *path_domain* / *path_domain_suffix* (from URL path)
      3. Server-level fallback

    Format:
      client_id     = hostname:api_key  (e.g. marvel.uptycs.net:FHVOUR...)
                      — or plain api_key when domain comes from path
      client_secret = customer_id:secret (e.g. 6ba51c6e-...:e3RZy84G...)

    Falls back to server defaults if no colon separator found.
    """
    if ":" in client_id:
        hostname, api_key = client_id.split(":", 1)
        dot_idx = hostname.index(".")
        domain = hostname[:dot_idx]
        domain_suffix = hostname[dot_idx:]
    elif path_domain:
        api_key = client_id
        domain = path_domain
        domain_suffix = path_domain_suffix
    else:
        api_key = client_id
        domain = fallback_domain
        domain_suffix = fallback_domain_suffix

    if ":" in client_secret:
        customer_id, api_secret = client_secret.split(":", 1)
    else:
        api_secret = client_secret
        customer_id = fallback_customer_id

    return ApiKey(
        key=api_key,
        secret=api_secret,
        customer_id=customer_id,
        domain=domain,
        domain_suffix=domain_suffix,
    )


def _make_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class UptycsOAuthProvider:
    """Minimal OAuth provider backed by Uptycs API key validation.

    - ``client_id`` = Uptycs API key
    - ``client_secret`` = Uptycs API secret
    - Validation: build an ApiKey from (client_id, client_secret, base config),
      call ``GET /juno/investigations?limit=1``. 200 ⇒ valid.
    """

    domain: str
    customer_id: str
    domain_suffix: str
    issuer_url: str
    default_domain_suffix: str = ""

    # In-memory stores (fine for single-process demo)
    _clients: dict[str, OAuthClientInformationFull] = field(
        default_factory=dict, repr=False,
    )
    _auth_codes: dict[str, AuthorizationCode] = field(
        default_factory=dict, repr=False,
    )
    _access_tokens: dict[str, AccessToken] = field(
        default_factory=dict, repr=False,
    )
    _refresh_tokens: dict[str, RefreshToken] = field(
        default_factory=dict, repr=False,
    )
    # Captured from /token form data before SDK processes the request.
    # Maps client_id → client_secret (Uptycs API secret).
    _pending_secrets: dict[str, str] = field(
        default_factory=dict, repr=False,
    )
    # Directory for persisting validated user API key files.
    # Each file is named <hash(client_id)>.json.
    _keys_dir: str = field(default="", repr=False)

    # ---- client management ----

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if client_id in self._clients:
            return self._clients[client_id]
        # Auto-register any client_id (it's a Uptycs API key — we validate on authorize).
        # Claude.ai uses pre-registered client flow with client_secret_post.
        client = OAuthClientInformationFull(
            client_id=client_id,
            client_secret=None,  # filled in by Claude at /token via client_secret_post
            client_id_issued_at=int(time.time()),
            redirect_uris=[
                AnyUrl("https://claude.ai/api/mcp/auth_callback"),
                AnyUrl("https://claude.com/api/mcp/auth_callback"),
            ],
            token_endpoint_auth_method="client_secret_post",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="api",
        )
        self._clients[client_id] = client
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    # ---- authorization ----

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams,
    ) -> str:
        # Auto-approve: client_secret is not available at /authorize time
        # (only client_id is in the query string).  Credential validation
        # happens later in exchange_authorization_code() when the secret
        # arrives via client_secret_post at /token.
        code = _make_token()
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 600,  # 10 min
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )

        logger.info("Authorized client %s...%s", client.client_id[:4], client.client_id[-4:])

        return construct_redirect_uri(
            str(params.redirect_uri),
            code=code,
            state=params.state,
        )

    def capture_client_secret(self, client_id: str, client_secret: str) -> None:
        """Store client_secret captured from /token form data.

        Called by the token-route wrapper in server.py before the SDK
        processes the request.
        """
        self._pending_secrets[client_id] = client_secret

    # ---- user API key persistence ----

    def _key_path(self, client_id: str) -> Path:
        h = hashlib.sha256(client_id.encode()).hexdigest()[:16]
        d = Path(self._keys_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{h}.json"

    def _save_user_api_key(
        self, client_id: str, api_key: ApiKey,
    ) -> None:
        if not self._keys_dir:
            return
        data = {
            "key": api_key.key,
            "secret": api_key.secret,
            "customerId": api_key.customer_id,
            "domain": api_key.domain,
            "domainSuffix": api_key.domain_suffix,
        }
        self._key_path(client_id).write_text(
            json.dumps(data, indent=2),
        )

    def get_user_api_key(self, client_id: str) -> ApiKey | None:
        """Load a validated user's ApiKey from disk."""
        if not self._keys_dir:
            return None
        p = self._key_path(client_id)
        if not p.exists():
            return None
        try:
            return ApiKey.from_file(str(p))
        except SystemExit:
            return None

    async def _validate_credentials(self, api_key: ApiKey) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.get(
                    f"{api_key.api_base}/investigations",
                    params={"limit": 1},
                    headers=auth_headers(api_key),
                )
            logger.info("Credential validation: %s", resp.status_code)
            return resp.is_success
        except Exception:
            logger.exception("Credential validation failed")
            return False

    # ---- token exchange ----

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str,
    ) -> AuthorizationCode | None:
        code = self._auth_codes.get(authorization_code)
        if code and code.client_id == client.client_id and code.expires_at > time.time():
            return code
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        # Remove used code
        self._auth_codes.pop(authorization_code.code, None)

        # Validate Uptycs credentials (key=client_id, secret from /token form)
        client_secret = self._pending_secrets.pop(client.client_id, None)
        if not client_secret:
            raise TokenError(
                error="invalid_client",
                error_description="Missing client_secret",
            )

        # Extract tenant from resource URL path if available.
        # e.g. https://juno.uptycs.net/marvel/mcp → "marvel"
        path_domain = ""
        path_domain_suffix = ""
        resource = authorization_code.resource
        if resource and self.default_domain_suffix:
            from urllib.parse import urlparse
            rpath = urlparse(str(resource)).path  # /marvel/mcp
            parts = rpath.strip("/").split("/")
            if len(parts) >= 2 and parts[-1] == "mcp":
                tenant_slug = "/".join(parts[:-1])
                path_domain, path_domain_suffix = resolve_host(
                    tenant_slug, self.default_domain_suffix,
                )

        api_key = _parse_credentials(
            client.client_id,
            client_secret,
            fallback_domain=self.domain,
            fallback_customer_id=self.customer_id,
            fallback_domain_suffix=self.domain_suffix,
            path_domain=path_domain,
            path_domain_suffix=path_domain_suffix,
        )
        valid = await self._validate_credentials(api_key)
        if not valid:
            raise TokenError(
                error="invalid_client",
                error_description=(
                    "Invalid Uptycs API key/secret"
                ),
            )
        logger.info(
            "Credentials validated for %s (%s)",
            api_key.key[:4] + "..." + api_key.key[-4:],
            api_key.domain,
        )
        self._save_user_api_key(client.client_id, api_key)

        access = _make_token()
        refresh = _make_token()
        expires_in = 86400  # 24 hours

        self._access_tokens[access] = AccessToken(
            token=access,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + expires_in,
            resource=authorization_code.resource,
        )
        self._refresh_tokens[refresh] = RefreshToken(
            token=refresh,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + 7 * 86400,  # 7 days
        )

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=expires_in,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
            refresh_token=refresh,
        )

    # ---- token verification ----

    async def load_access_token(self, token: str) -> AccessToken | None:
        at = self._access_tokens.get(token)
        if at and (at.expires_at is None or at.expires_at > int(time.time())):
            return at
        if at:
            self._access_tokens.pop(token, None)
        return None

    # ---- refresh tokens ----

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str,
    ) -> RefreshToken | None:
        rt = self._refresh_tokens.get(refresh_token)
        if rt and rt.client_id == client.client_id:
            if rt.expires_at is None or rt.expires_at > int(time.time()):
                return rt
            self._refresh_tokens.pop(refresh_token, None)
        return None

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str],
    ) -> OAuthToken:
        # Rotate tokens
        self._refresh_tokens.pop(refresh_token.token, None)

        access = _make_token()
        new_refresh = _make_token()
        expires_in = 86400

        self._access_tokens[access] = AccessToken(
            token=access,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            expires_at=int(time.time()) + expires_in,
        )
        self._refresh_tokens[new_refresh] = RefreshToken(
            token=new_refresh,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            expires_at=int(time.time()) + 7 * 86400,
        )

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=expires_in,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=new_refresh,
        )

    # ---- revocation ----

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._access_tokens.pop(token.token, None)
        elif isinstance(token, RefreshToken):
            self._refresh_tokens.pop(token.token, None)
