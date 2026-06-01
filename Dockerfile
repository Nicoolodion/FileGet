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
# encrypted RAR5 volumes). The Linux archive is named rarlinux-x64-XXX.tar.gz
# and contains both `rar` and `unrar` binaries; we install only `unrar`.
ARG RAR_VERSION=722
RUN set -eux; \
    cd /tmp; \
    curl -fsSL -o rarlinux.tar.gz \
        "https://www.rarlab.com/rar/rarlinux-x64-${RAR_VERSION}.tar.gz"; \
    tar -xzf rarlinux.tar.gz; \
    install -m 0755 rar/unrar /usr/local/bin/unrar; \
    rm -rf rarlinux.tar.gz rar /tmp/*; \
    unrar 2>&1 | head -1 || true

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY plex_get ./plex_get

EXPOSE 8000

CMD ["python", "-m", "plex_get", "serve"]
