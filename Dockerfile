FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    unar \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install the official RARLAB unrar binary. The Debian repos don't ship
# the proprietary unrar (only unar / unrar-free which is unreliable for
# encrypted RAR5 volumes). We download a static build into /usr/local/bin
# so it's picked up by the `unrar` lookup in plex_get.extractor.
ARG UNRAR_VERSION=7.1.7
RUN set -eux; \
    cd /tmp; \
    curl -fsSL -o unrar.tar.gz \
        "https://www.rarlab.com/rar/unrarlinux-x64-${UNRAR_VERSION}.tar.gz"; \
    tar -xzf unrar.tar.gz; \
    install -m 0755 unrar /usr/local/bin/unrar; \
    rm -rf unrar.tar.gz unrar rarfiles.txt /tmp/*; \
    unrar 2>&1 | head -1 || true

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY plex_get ./plex_get

EXPOSE 8000

CMD ["python", "-m", "plex_get", "serve"]
