FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LIBRIS_CONFIG=/config/config.yaml

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY librisrecorder ./librisrecorder

# Runs unprivileged; UID/GID are overridable at build time so the bind-mounted
# recordings directory ends up owned by your host user.
ARG UID=1000
ARG GID=1000
RUN groupadd -g "${GID}" libris 2>/dev/null || true \
    && useradd -u "${UID}" -g "${GID}" -m -s /usr/sbin/nologin libris 2>/dev/null || true \
    && mkdir -p /recordings /config \
    && chown -R "${UID}:${GID}" /recordings /config /app

USER ${UID}:${GID}

VOLUME ["/recordings", "/config"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD python -m librisrecorder --check-config || exit 1

ENTRYPOINT ["python", "-m", "librisrecorder"]
