# The quiz service (D15, ADR-0015): the domain package in its own process, a
# sibling to Open WebUI rather than something living inside it.
#
# Open WebUI is not installed here and is not importable here. That is the
# point — the portability seam of ADR-0002 is a process boundary in this
# image, so an Open WebUI import below it would not resolve.
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependency metadata first, so a source edit does not re-resolve the wheels.
COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir -e ".[service,anthropic,rendering]"

# One worker: token supersession and the repositories are both in-process.
EXPOSE 8080
CMD ["python", "-m", "socratic.service"]
