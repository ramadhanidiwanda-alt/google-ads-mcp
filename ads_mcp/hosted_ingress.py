"""Reject requests that did not pass the private Google Ads Kong route."""

import hmac
from typing import Any


class PrivateIngress:
    def __init__(self, app: Any, secret: str, host: str) -> None:
        if len(secret) < 32 or not host or ":" in host:
            raise ValueError("Google Ads private ingress configuration is invalid")
        self.app = app
        self.secret = secret.encode()
        self.host = host.encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        hosts = [value for name, value in headers if name.lower() == b"host"]
        proofs = [
            value
            for name, value in headers
            if name.lower() == b"x-cuan-google-ads-ingress-secret"
        ]
        if (
            len(hosts) != 1
            or hosts[0] != self.host
            or len(proofs) != 1
            or not hmac.compare_digest(proofs[0], self.secret)
        ):
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"Forbidden"})
            return
        await self.app(scope, receive, send)
