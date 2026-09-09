"""Deployment configuration, read from the environment once at startup.

The HMAC secret arrives here and goes nowhere else: it is never logged, never
put in a response, and never rendered near the `srcdoc` (#18, D15). The only
thing that ever holds it is the `TokenMinter`.
"""

import os
from dataclasses import dataclass

MIN_SECRET_BYTES = 32
"""#18's floor. Shorter than the HMAC's own block size buys nothing and looks
like a secret while not being one."""

DEFAULT_TTL_MILLIS = 15 * 60 * 1000

SECRET_VAR = "SOCRATIC_TOKEN_SECRET"
SERVICE_TOKEN_VAR = "SOCRATIC_SERVICE_TOKEN"
TTL_VAR = "SOCRATIC_TOKEN_TTL_MILLIS"
PUBLIC_URL_VAR = "SOCRATIC_PUBLIC_URL"

DEFAULT_PUBLIC_URL = "http://localhost:8080"
"""Where the *browser* reaches this service, which is not where the Pipe does.

The Pipe calls the compose DNS name; the overlay runs in the learner's browser,
outside the compose network, and `compose.yaml` publishes 8080 to the host for
exactly that reason. Held as configuration rather than accepted on a request
because it is rendered into the document: a caller-chosen URL would point every
learner's answers at whatever origin the caller named (#13, D15).
"""


class ConfigError(RuntimeError):
    """Refused at startup rather than at the first request.

    The message names the variable and never its value — a config error that
    prints the secret is a config error that leaks it into a container log.
    """


@dataclass(frozen=True)
class ServiceConfig:
    token_secret: bytes
    service_token: str
    ttl_millis: int = DEFAULT_TTL_MILLIS
    public_base_url: str = DEFAULT_PUBLIC_URL

    @classmethod
    def from_env(cls, env=None) -> "ServiceConfig":
        env = os.environ if env is None else env

        secret = env.get(SECRET_VAR, "").encode("utf-8")
        if len(secret) < MIN_SECRET_BYTES:
            raise ConfigError(
                f"{SECRET_VAR} must be at least {MIN_SECRET_BYTES} bytes"
            )

        service_token = env.get(SERVICE_TOKEN_VAR, "")
        if not service_token:
            raise ConfigError(f"{SERVICE_TOKEN_VAR} must be set")

        raw_ttl = env.get(TTL_VAR)
        try:
            ttl = int(raw_ttl) if raw_ttl else DEFAULT_TTL_MILLIS
        except ValueError:
            raise ConfigError(f"{TTL_VAR} must be an integer number of milliseconds")
        if ttl <= 0:
            raise ConfigError(f"{TTL_VAR} must be positive")

        public_url = (env.get(PUBLIC_URL_VAR) or DEFAULT_PUBLIC_URL).rstrip("/")
        if not public_url.startswith(("http://", "https://")):
            raise ConfigError(
                f"{PUBLIC_URL_VAR} must be an absolute http(s) URL: a `srcdoc` "
                "document inherits its parent's base URL, so a relative one "
                "sends every answer to the host instead of here"
            )

        return cls(
            token_secret=secret,
            service_token=service_token,
            ttl_millis=ttl,
            public_base_url=public_url,
        )

    def __repr__(self) -> str:
        """Never render the secret, not even in a traceback."""
        return (
            f"ServiceConfig(ttl_millis={self.ttl_millis}, "
            f"public_base_url={self.public_base_url!r}, secrets=<redacted>)"
        )
