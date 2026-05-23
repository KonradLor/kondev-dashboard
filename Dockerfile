# ============================================================================
# Dashboard Backend - Docker image
# ============================================================================
# FastAPI servisas, kuris valdo kitus Docker konteinerius per /var/run/docker.sock.
# Be ARM-specific niuansų - veikia tiek arm64, tiek x86_64.
# ============================================================================

FROM python:3.11-slim

# Non-root vartotojas, kaip ir KonradVault'e (UID 1001 = host ubuntu)
# Be to, pridėjome jį prie docker grupės (GID 988), kad galėtų pasiekti
# Docker socket'ą.
RUN groupadd -g 988 docker && \
    useradd -m -u 1001 -s /bin/bash -G docker dashboard

WORKDIR /app

# Python paketai (atskiras layer cache'ui)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Aplikacijos kodas
COPY backend/app          ./app
COPY backend/services.yaml .

# /data direktorija eventų logui (Docker volume override'ins)
RUN mkdir -p /data && chown -R dashboard:dashboard /app /data

USER dashboard

EXPOSE 8000

# Sveikatos patikrinimas
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health').read()" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
