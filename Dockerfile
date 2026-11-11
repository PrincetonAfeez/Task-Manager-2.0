# syntax=docker/dockerfile:1
#
# Multi-stage: wheels are built with a toolchain that never reaches the runtime image, so
# the shipped layer carries no compiler and a smaller attack surface.

FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf "/var/lib/apt/lists"

WORKDIR /app
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings \
    WEB_CONCURRENCY=3

RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl postgresql-client \
 && rm -rf "/var/lib/apt/lists" \
 && useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels

COPY . .

# collectstatic at build time, not at boot: with DEBUG=0 and an unpopulated static root
# every asset 404s, and doing it per container start makes rollouts slower and
# non-deterministic. The throwaway key is deliberate - collectstatic must never require
# production secrets to be present in the build environment.
RUN DJANGO_ENV=development DJANGO_DEBUG=0 DJANGO_SECRET_KEY=build-only-not-a-secret \
    python manage.py collectstatic --noinput --clear \
 && mkdir -p /app/media \
 && chown -R appuser:appuser /app

COPY --chown=appuser:appuser entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl --fail --silent http://127.0.0.1:8000/healthz || exit 1

# Worker count comes from WEB_CONCURRENCY (gunicorn reads it natively). The app is CPU-bound
# in Python, so this is the knob that scales a single container: roughly one per core.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--threads", "2", \
     "--timeout", "60", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
