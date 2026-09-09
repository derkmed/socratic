"""Deployment configuration, read from the environment once at startup.

The HMAC secret arrives here and goes nowhere else: it is never logged, never
put in a response, and never rendered near the `srcdoc` (#18, D15). The only
thing that ever holds it is the `TokenMinter`.
"""

import os
import pathlib
from dataclasses import dataclass, field

from socratic.adapters import collected_records

MIN_SECRET_BYTES = 32
"""#18's floor. Shorter than the HMAC's own block size buys nothing and looks
like a secret while not being one."""

DEFAULT_TTL_MILLIS = 15 * 60 * 1000

SECRET_VAR = "SOCRATIC_TOKEN_SECRET"
SERVICE_TOKEN_VAR = "SOCRATIC_SERVICE_TOKEN"
TTL_VAR = "SOCRATIC_TOKEN_TTL_MILLIS"
PUBLIC_URL_VAR = "SOCRATIC_PUBLIC_URL"
DATA_DIR_VAR = "SOCRATIC_DATA_DIR"
DATA_FLUSH_VAR = "SOCRATIC_DATA_FLUSH"

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
    data_dir: pathlib.Path | None = None
    """Where the collected records trail is written (#117, ADR-0018).

    `None` - the variable unset - means no persistence at all: the wrappers are
    simply not applied and the service behaves exactly as it did before, which
    is why no existing test needs a temporary directory.
    """
    data_flush: collected_records.FlushCadence = field(
        default=collected_records.FlushCadence.ON_CLOSE
    )

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

        data_dir = _data_dir(env.get(DATA_DIR_VAR))

        raw_flush = env.get(DATA_FLUSH_VAR)
        try:
            flush = (
                collected_records.FlushCadence.ON_CLOSE
                if not raw_flush
                else collected_records.FlushCadence(raw_flush)
            )
        except ValueError:
            expected = ", ".join(
                repr(value.value) for value in collected_records.FlushCadence
            )
            raise ConfigError(
                f"{DATA_FLUSH_VAR} must be one of {expected}"
            ) from None

        return cls(
            token_secret=secret,
            service_token=service_token,
            ttl_millis=ttl,
            public_base_url=public_url,
            data_dir=data_dir,
            data_flush=flush,
        )

    def __repr__(self) -> str:
        """Never render the secret, not even in a traceback."""
        return (
            f"ServiceConfig(ttl_millis={self.ttl_millis}, "
            f"public_base_url={self.public_base_url!r}, secrets=<redacted>)"
        )


def _data_dir(raw: str | None) -> pathlib.Path | None:
    """The trail's root, created and proved writable, or `None` if unset.

    Checked here because a *runtime* write failure is swallowed: it lands on the
    request that seals a learner's quiz, and raising would cost them their last
    answer to protect a demo artifact (ADR-0018). A startup failure has no
    learner to protect, and setting the variable is a deliberate statement that
    the records are wanted - so silently collecting nothing is precisely the
    failure #117 exists to prevent.

    The probe is a real write. `os.access` and the mode bits both lie under
    enough circumstances - Windows ACLs, a read-only mount, a container running
    as a user the host directory does not know - and this check earns its place
    only by catching those.

    Args:
      raw: The variable's value, or `None`/empty for no persistence.

    Returns:
      The directory, or `None`.

    Raises:
      ConfigError: if the path is not a directory or cannot be written to.
    """
    if not raw:
        return None
    path = pathlib.Path(raw)
    probe = path / ".socratic-write-probe"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
    except OSError as error:
        raise ConfigError(
            f"{DATA_DIR_VAR} must name a writable directory ({error.strerror})"
        ) from None
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    return path
