FROM python:3.11

WORKDIR /app
COPY . /app

# cloudflared is baked in so the container doesn't have to download it on first
# boot. The bot falls back to auto-installing it at runtime on other arches.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) asset=cloudflared-linux-amd64 ;; \
      arm64) asset=cloudflared-linux-arm64 ;; \
      armhf) asset=cloudflared-linux-arm ;; \
      *) asset= ;; \
    esac; \
    if [ -n "$asset" ]; then \
      curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/$asset" \
        -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared; \
    fi

RUN pip install --upgrade pip
RUN pip install -r requirements.txt

COPY . .

CMD ["python", "-m", "FileStream"]