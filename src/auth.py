"""API key loading and JWT token generation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ApiKey:
    """Credentials loaded from an Uptycs API key JSON file."""

    key: str
    secret: str
    customer_id: str
    domain: str
    domain_suffix: str

    _REQUIRED_FIELDS = ("key", "secret", "customerId", "domain", "domainSuffix")

    @classmethod
    def from_file(cls, path: str | Path) -> ApiKey:
        p = Path(path)
        if not p.exists():
            raise SystemExit(f"API key file not found: {p}")
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSON in API key file {p}: {exc}")
        if not isinstance(data, dict):
            raise SystemExit(
                f"API key file {p} must contain a JSON object, "
                f"got {type(data).__name__}"
            )
        missing = [f for f in cls._REQUIRED_FIELDS if f not in data]
        if missing:
            raise SystemExit(
                f"API key file {p} missing fields: {', '.join(missing)}"
            )
        return cls(
            key=data["key"],
            secret=data["secret"],
            customer_id=data["customerId"],
            domain=data["domain"],
            domain_suffix=data["domainSuffix"],
        )

    @property
    def base_url(self) -> str:
        return f"https://{self.domain}{self.domain_suffix}"

    @property
    def api_base(self) -> str:
        return (
            f"{self.base_url}/public/api/v2/customers"
            f"/{self.customer_id}/juno"
        )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_token(api_key: ApiKey) -> str:
    """Create a signed JWT (HS256, 300s expiry)."""
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps({"iss": api_key.key, "iat": now, "exp": now + 300}).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    signature = _b64url(
        hmac.HMAC(
            api_key.secret.encode(),
            signing_input,
            hashlib.sha256,
        ).digest()
    )
    return f"{header}.{payload}.{signature}"


def auth_headers(api_key: ApiKey) -> dict[str, str]:
    """Return Authorization header with a fresh JWT."""
    return {"Authorization": f"Bearer {generate_token(api_key)}"}
