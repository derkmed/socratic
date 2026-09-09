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

        return cls(token_secret=secret, service_token=service_token, ttl_millis=ttl)

    def __repr__(self) -> str:
        """Never render the secret, not even in a traceback."""
        return f"ServiceConfig(ttl_millis={self.ttl_millis}, secrets=<redacted>)"
