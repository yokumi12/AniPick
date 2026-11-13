#!/bin/bash
set -Eeuo pipefail
umask 077

REGION="ap-northeast-2"
INSTANCE_ID="i-0afc3cb30bd7974b5"
ORIGIN_DOMAIN="ec2-3-38-209-175.ap-northeast-2.compute.amazonaws.com"
ARTIFACT_BUCKET="${ANIPICK_ARTIFACT_BUCKET:-anipick-deploy-093356173512-04e18238}"
ARCHIVE="${ANIPICK_SOURCE_ARCHIVE:-${HOME}/anipick-source.tar.gz}"
STATE_FILE="${HOME}/.anipick-single-ec2-cloudfront-id"

if [[ ! -s "$ARCHIVE" ]]; then
    echo "Upload the source archive to CloudShell and set ANIPICK_SOURCE_ARCHIVE if needed." >&2
    exit 1
fi
if [[ -e "$STATE_FILE" ]]; then
    echo "A CloudFront distribution was already created for this deployment. Reuse its saved ID." >&2
    cat "$STATE_FILE"
    exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
if [[ "$ARTIFACT_BUCKET" != "anipick-deploy-${ACCOUNT_ID}-"* ]]; then
    echo "The artifact bucket must belong to the current AWS account." >&2
    exit 1
fi

aws s3api head-bucket --bucket "$ARTIFACT_BUCKET"
OBJECT_PREFIX="single-ec2/$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 4)"
ARCHIVE_KEY="${OBJECT_PREFIX}/anipick-source.tar.gz"
ENV_KEY="${OBJECT_PREFIX}/deployment.env"
TEMP_ENV="$(mktemp)"
TEMP_CONFIG="$(mktemp)"
trap 'rm -f -- "$TEMP_ENV" "$TEMP_CONFIG"' EXIT

MYSQL_ROOT_PASSWORD="$(openssl rand -hex 32)"
ANIPICK_MYSQL_PASSWORD="$(openssl rand -hex 32)"
ANIPICK_CLOUDFRONT_ORIGIN_SECRET="$(openssl rand -hex 32)"
ANIPICK_API_KEY_ENCRYPTION_KEY="$(python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
cat > "$TEMP_ENV" <<EOF
MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD}
ANIPICK_MYSQL_HOST=mysql
ANIPICK_MYSQL_PORT=3306
ANIPICK_MYSQL_USER=anipick_app
ANIPICK_MYSQL_PASSWORD=${ANIPICK_MYSQL_PASSWORD}
ANIPICK_MYSQL_DATABASE=anipick
ANIPICK_MYSQL_SSL_CA=/etc/anipick/certs/mysql/ca.pem
ANIPICK_API_KEY_ENCRYPTION_KEY=${ANIPICK_API_KEY_ENCRYPTION_KEY}
ANIPICK_CLOUDFRONT_ORIGIN_SECRET=${ANIPICK_CLOUDFRONT_ORIGIN_SECRET}
EOF
unset MYSQL_ROOT_PASSWORD ANIPICK_MYSQL_PASSWORD \
    ANIPICK_CLOUDFRONT_ORIGIN_SECRET ANIPICK_API_KEY_ENCRYPTION_KEY

CALLER_REFERENCE="anipick-single-ec2-$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 4)"
python3 - "$TEMP_ENV" "$TEMP_CONFIG" "$ORIGIN_DOMAIN" "$CALLER_REFERENCE" <<'PY'
import json
import sys

env_path, config_path, origin_domain, caller_reference = sys.argv[1:]
settings = dict(
    line.split("=", 1)
    for line in open(env_path, encoding="utf-8")
    if "=" in line
)
config = {
    "CallerReference": caller_reference,
    "Comment": "AniPick single EC2 HTTPS origin",
    "Enabled": True,
    "DefaultRootObject": "index.html",
    "HttpVersion": "http2",
    "PriceClass": "PriceClass_200",
    "Origins": {
        "Quantity": 1,
        "Items": [
            {
                "Id": "anipick-ec2-origin",
                "DomainName": origin_domain,
                "CustomHeaders": {
                    "Quantity": 1,
                    "Items": [
                        {
                            "HeaderName": "X-AniPick-Origin-Secret",
                            "HeaderValue": settings[
                                "ANIPICK_CLOUDFRONT_ORIGIN_SECRET"
                            ],
                        }
                    ],
                },
                "CustomOriginConfig": {
                    "HTTPPort": 8000,
                    "HTTPSPort": 443,
                    "OriginProtocolPolicy": "http-only",
                    "OriginSslProtocols": {
                        "Quantity": 1,
                        "Items": ["TLSv1.2"],
                    },
                    "OriginReadTimeout": 60,
                    "OriginKeepaliveTimeout": 5,
                },
            }
        ],
    },
    "DefaultCacheBehavior": {
        "TargetOriginId": "anipick-ec2-origin",
        "ViewerProtocolPolicy": "redirect-to-https",
        "AllowedMethods": {
            "Quantity": 7,
            "Items": ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"],
            "CachedMethods": {
                "Quantity": 3,
                "Items": ["GET", "HEAD", "OPTIONS"],
            },
        },
        "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
        "OriginRequestPolicyId": "b689b0a8-53d0-40ab-baf2-68738e2966ac",
        "Compress": True,
    },
    "Restrictions": {
        "GeoRestriction": {"RestrictionType": "none", "Quantity": 0}
    },
    "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
}
with open(config_path, "w", encoding="utf-8") as file:
    json.dump(config, file)
PY

aws cloudfront create-distribution \
    --distribution-config "file://${TEMP_CONFIG}" \
    --query 'Distribution.[Id,DomainName,Status]' \
    --output text > "${STATE_FILE}"
chmod 0600 "$STATE_FILE"
read -r DISTRIBUTION_ID DISTRIBUTION_DOMAIN DISTRIBUTION_STATUS < "$STATE_FILE"
printf 'ANIPICK_PUBLIC_ORIGIN=https://%s\n' "$DISTRIBUTION_DOMAIN" >> "$TEMP_ENV"

aws s3 cp "$ARCHIVE" "s3://${ARTIFACT_BUCKET}/${ARCHIVE_KEY}" \
    --region "$REGION" --sse AES256 --only-show-errors
aws s3 cp "$TEMP_ENV" "s3://${ARTIFACT_BUCKET}/${ENV_KEY}" \
    --region "$REGION" --sse AES256 --only-show-errors

printf 'INSTANCE_ID=%s\n' "$INSTANCE_ID"
printf 'DISTRIBUTION_ID=%s\n' "$DISTRIBUTION_ID"
printf 'HTTPS_URL=https://%s\n' "$DISTRIBUTION_DOMAIN"
printf 'DISTRIBUTION_STATUS=%s\n' "$DISTRIBUTION_STATUS"
printf 'ARCHIVE_URL=%s\n' "$(aws s3 presign "s3://${ARTIFACT_BUCKET}/${ARCHIVE_KEY}" --region "$REGION" --expires-in 3600)"
printf 'ENV_URL=%s\n' "$(aws s3 presign "s3://${ARTIFACT_BUCKET}/${ENV_KEY}" --region "$REGION" --expires-in 3600)"
printf 'ENV_S3_URI=s3://%s/%s\n' "$ARTIFACT_BUCKET" "$ENV_KEY"
printf 'ARCHIVE_S3_URI=s3://%s/%s\n' "$ARTIFACT_BUCKET" "$ARCHIVE_KEY"
