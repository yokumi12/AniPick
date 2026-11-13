#!/bin/bash
set -Eeuo pipefail
umask 077

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
CERT_DIR="${APP_DIR}/certs/mysql"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script with sudo." >&2
    exit 1
fi

systemctl enable --now docker

for name in MYSQL_ROOT_PASSWORD ANIPICK_MYSQL_PASSWORD ANIPICK_MYSQL_DATABASE \
    ANIPICK_API_KEY_ENCRYPTION_KEY ANIPICK_CLOUDFRONT_ORIGIN_SECRET \
    ANIPICK_PUBLIC_ORIGIN; do
    if ! grep -q "^${name}=." "${APP_DIR}/.env"; then
        echo "Missing required setting ${name} in ${APP_DIR}/.env." >&2
        exit 1
    fi
done

mkdir -p "${CERT_DIR}" "${APP_DIR}/data"
if [[ ! -s "${CERT_DIR}/ca.pem" || ! -s "${CERT_DIR}/server-cert.pem" \
    || ! -s "${CERT_DIR}/server-key.pem" ]]; then
    rm -f "${CERT_DIR}/ca.pem" "${CERT_DIR}/ca-key.pem" \
        "${CERT_DIR}/ca.srl" "${CERT_DIR}/server-cert.pem" \
        "${CERT_DIR}/server-key.pem" "${CERT_DIR}/server.csr"
    openssl req -new -x509 -nodes -newkey rsa:3072 -days 3650 \
        -keyout "${CERT_DIR}/ca-key.pem" \
        -out "${CERT_DIR}/ca.pem" \
        -subj "/CN=AniPick MySQL CA" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign"
    openssl req -new -nodes -newkey rsa:2048 \
        -keyout "${CERT_DIR}/server-key.pem" \
        -out "${CERT_DIR}/server.csr" \
        -subj "/CN=mysql"
    printf 'subjectAltName=DNS:mysql\nextendedKeyUsage=serverAuth\n' \
        > "${CERT_DIR}/server.ext"
    openssl x509 -req -in "${CERT_DIR}/server.csr" \
        -CA "${CERT_DIR}/ca.pem" -CAkey "${CERT_DIR}/ca-key.pem" \
        -CAcreateserial -out "${CERT_DIR}/server-cert.pem" \
        -days 825 -sha256 -extfile "${CERT_DIR}/server.ext"
fi

chmod 0755 "${CERT_DIR}"
chmod 0644 "${CERT_DIR}/ca.pem" "${CERT_DIR}/server-cert.pem"
chown 999:999 "${CERT_DIR}/server-key.pem"
chmod 0400 "${CERT_DIR}/server-key.pem"
chmod 0600 "${CERT_DIR}/ca-key.pem"
rm -f "${CERT_DIR}/server.csr" "${CERT_DIR}/server.ext" "${CERT_DIR}/ca.srl"
chown -R 10001:10001 "${APP_DIR}/data"

cd "${APP_DIR}"
docker compose --env-file .env -f compose.single-ec2.yaml up -d --build

for attempt in $(seq 1 60); do
    if curl --fail --silent --show-error --max-time 3 \
        http://127.0.0.1:8000/health >/dev/null; then
        echo "AniPick is healthy on the EC2 instance."
        exit 0
    fi
    sleep 5
done

docker compose --env-file .env -f compose.single-ec2.yaml ps
docker compose --env-file .env -f compose.single-ec2.yaml logs --tail=80
echo "AniPick did not become healthy within five minutes." >&2
exit 1
