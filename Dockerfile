FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSLo /etc/ssl/certs/rds-global-bundle.pem \
       https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . .
RUN groupadd --system --gid 10001 anipick \
    && useradd --system --uid 10001 --gid anipick --home-dir /app anipick \
    && mkdir -p /app/data \
    && chown -R anipick:anipick /app/data

USER anipick
EXPOSE 8000
CMD ["python", "server.py"]
