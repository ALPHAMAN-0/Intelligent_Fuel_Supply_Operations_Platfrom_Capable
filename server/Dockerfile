# One image, two services (ADR-009). Build context: repository root.
#   backend       uvicorn fuelops.backend.main:app --port 8080   (also serves the static operator console)
#   intelligence  uvicorn fuelops.intelligence.main:app --port 8090
#
# BASE_IMAGE defaults to python:3.11-slim. Offline builds: point BASE_IMAGE at any local Python 3.11 image, put the
# missing wheels in server/wheelhouse/ and pass --build-arg PIP_EXTRA=--no-index.
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ARG PIP_EXTRA=""
ARG INSTALL_DEV=0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/fuelops/server
# requirements*.txt always matches, so the optional wheelhouse glob never breaks the build.
COPY server/requirements*.txt server/wheelhouse* /tmp/req/
RUN if [ "$INSTALL_DEV" = "1" ]; then REQ=/tmp/req/requirements-dev.txt; else REQ=/tmp/req/requirements.txt; fi && \
    pip install --no-cache-dir --find-links /tmp/req $PIP_EXTRA -r $REQ && rm -rf /tmp/req

COPY server/pyproject.toml ./
COPY server/fuelops ./fuelops
COPY server/models ./models
COPY server/tests ./tests
COPY frontend /srv/fuelops/frontend

RUN (id -u fuelops >/dev/null 2>&1 || useradd --create-home --uid 10001 fuelops) && \
    mkdir -p /srv/fuelops/server/data && chown -R fuelops /srv/fuelops/server/data
USER fuelops

ENV STATIC_DIR=/srv/fuelops/frontend \
    DATABASE_PATH=/srv/fuelops/server/data/fuelops.db \
    MODELS_DIR=/srv/fuelops/server/models

EXPOSE 8080 8090
CMD ["uvicorn", "fuelops.backend.main:app", "--host", "0.0.0.0", "--port", "8080"]
