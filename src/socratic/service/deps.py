"""What the service needs, injected rather than constructed.

`create_app` takes one of these. That is what lets the tests drive the real
routes in-process against a stub model client and in-memory repositories, with
no environment, no network and no uvicorn — and it is why the wiring, which is
where the authorization rules actually live, is testable at all.
"""

from dataclasses import dataclass

from socratic.domain import ids, repositories
from socratic.domain.authoring import QuizAuthoring
from socratic.domain.registry import ModeRegistry, default_registry
from socratic.domain.session import QuizSession
from socratic.domain.tokens import TokenMinter
from socratic.service.config import DEFAULT_PUBLIC_URL


@dataclass(frozen=True)
class ServiceDependencies:
    """The collaborators a running service holds.

    `service_token` is the Pipe's credential, not a learner's: authoring
    creates the session a capability token would be scoped to, so it cannot be
    authorized by one. The two are deliberately different fields so no handler
    can accept either.
    """

    authoring: QuizAuthoring
    session: QuizSession
    attempts: repositories.AttemptRepository
    ratings: repositories.RatingRepository
    minter: TokenMinter
    service_token: str
    settings: repositories.LearnerSettingsRepository = None  # type: ignore[assignment]
    """The learner's current settings, recorded by the Pipe (#14, ADR-0010).

    Defaulted rather than required, the way `registry` is: a deployment that
    never records any settings behaves exactly as it did before the field
    existed, because the answering path falls back to the attempt's own
    `probe_cadence_at_authoring`."""
    registry: ModeRegistry = None  # type: ignore[assignment]
    clock: ids.Clock = ids.system_clock
    public_base_url: str = DEFAULT_PUBLIC_URL
    """The service's browser-facing origin, rendered into the overlay (#13).

    Configuration, never a request field. The overlay is the only consumer."""

    def __post_init__(self) -> None:
        if self.registry is None:
            object.__setattr__(self, "registry", default_registry())
        if self.settings is None:
            object.__setattr__(
                self, "settings", repositories.InMemoryLearnerSettingsRepository()
            )
        if not self.service_token:
            raise ValueError("the service token must not be empty")
