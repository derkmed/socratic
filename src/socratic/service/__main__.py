"""The container's entry point: `python -m socratic.service`.

Imported by nothing else, so that everything above it stays testable without an
environment, a server or an API key. This module is the only place the service
reads `os.environ`, constructs the Anthropic adapter, or binds a port.

**One worker, deliberately.** `TokenMinter` keeps `{session -> current token
id}` in process (#18), and the repositories are in-memory (ADR-0005). A second
worker would hold a second, disagreeing copy of both, so a learner's rotated
token would be rejected by whichever worker did not mint it.
"""

import os

import uvicorn

from socratic.adapters.anthropic_client import AnthropicModelClient
from socratic.domain.authoring import QuizAuthoring
from socratic.domain.repositories import (
    InMemoryAttemptRepository,
    InMemoryRatingRepository,
)
from socratic.domain.session import QuizSession
from socratic.domain.tokens import TokenMinter
from socratic.service.app import create_app
from socratic.service.config import ServiceConfig
from socratic.service.deps import ServiceDependencies


def build_app():
    config = ServiceConfig.from_env()
    model_client = AnthropicModelClient()
    attempts = InMemoryAttemptRepository()

    return create_app(
        ServiceDependencies(
            authoring=QuizAuthoring(model_client=model_client, attempts=attempts),
            session=QuizSession(model_client=model_client, attempts=attempts),
            attempts=attempts,
            ratings=InMemoryRatingRepository(),
            minter=TokenMinter(config.token_secret, ttl_millis=config.ttl_millis),
            service_token=config.service_token,
        )
    )


def main() -> None:
    uvicorn.run(
        build_app(),
        host=os.environ.get("SOCRATIC_HOST", "0.0.0.0"),
        port=int(os.environ.get("SOCRATIC_PORT", "8080")),
        workers=1,
    )


if __name__ == "__main__":
    main()
