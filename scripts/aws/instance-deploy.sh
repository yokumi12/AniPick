#!/bin/bash
set -Eeuo pipefail
umask 077

APP_DIR=/opt/anipick/app
CONFIG_FILE=/etc/anipick/deployment.conf

if [[ ! -r "$CONFIG_FILE" ]]; then
    echo "Missing deployment configuration: $CONFIG_FILE" >&2
    exit 1
fi

# The file is created by the stack bootstrap and contains only non-secret values.
source "$CONFIG_FILE"
PUBLIC_ORIGIN="${1:-}"
if [[ -n "$PUBLIC_ORIGIN" && ! "$PUBLIC_ORIGIN" =~ ^https://[a-z0-9-]+\.cloudfront\.net$ ]]; then
    echo "The configured public origin is not a CloudFront HTTPS URL." >&2
    exit 1
fi

archive="$(mktemp /tmp/anipick-source.XXXXXX.tar.gz)"
trap 'rm -f -- "$archive"' EXIT
aws s3 cp \
    "s3://${DEPLOYMENT_BUCKET}/${DEPLOYMENT_OBJECT_KEY}" \
    "$archive" \
    --region "$AWS_REGION"
tar -xzf "$archive" -C "$APP_DIR"
install -o root -g root -m 0755 \
    "$APP_DIR/scripts/aws/instance-deploy.sh" /usr/local/bin/anipick-deploy
chown -R 10001:10001 "$APP_DIR/data"

if [[ -n "$PUBLIC_ORIGIN" ]]; then
    awk '!/^ANIPICK_PUBLIC_ORIGIN=/' "$APP_DIR/.env" > "$APP_DIR/.env.tmp"
    printf 'ANIPICK_PUBLIC_ORIGIN=%s\n' "$PUBLIC_ORIGIN" >> "$APP_DIR/.env.tmp"
    chmod 0600 "$APP_DIR/.env.tmp"
    mv "$APP_DIR/.env.tmp" "$APP_DIR/.env"
fi

cd "$APP_DIR"
if [[ -r /etc/anipick/bootstrap.env ]]; then
    set -a
    . /etc/anipick/bootstrap.env
    set +a
    docker compose --project-name anipick build
    docker compose --project-name anipick run --rm --no-deps \
        -e ANIPICK_DB_ADMIN_USER \
        -e ANIPICK_DB_ADMIN_PASSWORD \
        -e ANIPICK_DB_APP_PASSWORD \
        app python scripts/bootstrap_db_user.py
fi
docker compose --project-name anipick up -d --build

for attempt in $(seq 1 60); do
    if curl --fail --silent --show-error --max-time 3 \
        http://127.0.0.1:8000/health >/dev/null; then
        echo "AniPick deployment is healthy."
        exit 0
    fi
    sleep 5
done

docker compose --project-name anipick ps
docker compose --project-name anipick logs --tail=80
echo "AniPick did not become healthy within five minutes." >&2
exit 1
