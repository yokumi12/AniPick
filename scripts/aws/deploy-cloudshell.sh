#!/bin/bash
set -Eeuo pipefail
umask 077

REGION="${AWS_REGION:-$(aws configure get region)}"
STACK_NAME="${ANIPICK_STACK_NAME:-anipick-prod}"
TEMPLATE="scripts/aws/infrastructure.yaml"
SOURCE_ARCHIVE="${ANIPICK_SOURCE_ARCHIVE:-anipick-source.tar.gz}"

if [[ "$REGION" != "ap-northeast-2" ]]; then
    echo "Expected AWS region ap-northeast-2; got '$REGION'." >&2
    exit 1
fi
if [[ ! -f "$TEMPLATE" || ! -f "$SOURCE_ARCHIVE" ]]; then
    echo "Run this script from the extracted AniPick project folder with $SOURCE_ARCHIVE present." >&2
    exit 1
fi
if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
    echo "Stack $STACK_NAME already exists. Refusing to modify existing infrastructure." >&2
    exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
RANDOM_SUFFIX="$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
BUCKET="anipick-deploy-${ACCOUNT_ID}-${RANDOM_SUFFIX}"
OBJECT_KEY="releases/$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
ORIGIN_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
ENCRYPTION_KEY="$(python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
PARAMETERS_FILE="$(mktemp)"
trap 'rm -f -- "$PARAMETERS_FILE"' EXIT

aws cloudformation validate-template \
    --template-body "file://${TEMPLATE}" \
    --region "$REGION" >/dev/null

aws s3api create-bucket \
    --bucket "$BUCKET" \
    --region "$REGION" \
    --create-bucket-configuration "LocationConstraint=${REGION}" \
    >/dev/null
aws s3api put-bucket-tagging \
    --bucket "$BUCKET" \
    --region "$REGION" \
    --tagging 'TagSet=[{Key=Project,Value=AniPick},{Key=Purpose,Value=DeploymentArtifact}]'
aws s3api put-public-access-block \
    --bucket "$BUCKET" \
    --region "$REGION" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption \
    --bucket "$BUCKET" \
    --region "$REGION" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3 cp "$SOURCE_ARCHIVE" "s3://${BUCKET}/${OBJECT_KEY}" --region "$REGION" >/dev/null

python3 - "$BUCKET" "$OBJECT_KEY" "$ORIGIN_SECRET" "$ENCRYPTION_KEY" > "$PARAMETERS_FILE" <<'PY'
import json
import sys
bucket, key, origin_secret, encryption_key = sys.argv[1:]
print(json.dumps([
    {"ParameterKey": "ArtifactBucket", "ParameterValue": bucket},
    {"ParameterKey": "ArtifactKey", "ParameterValue": key},
    {"ParameterKey": "OriginSecret", "ParameterValue": origin_secret},
    {"ParameterKey": "EncryptionKey", "ParameterValue": encryption_key},
]))
PY
chmod 0600 "$PARAMETERS_FILE"

echo "Creating isolated stack $STACK_NAME in $REGION. No existing stacks are updated."
aws cloudformation create-stack \
    --stack-name "$STACK_NAME" \
    --template-body "file://${TEMPLATE}" \
    --parameters "file://${PARAMETERS_FILE}" \
    --tags Key=Project,Value=AniPick \
    --region "$REGION" >/dev/null

if ! aws cloudformation wait stack-create-complete \
    --stack-name "$STACK_NAME" --region "$REGION"; then
    echo "CloudFormation did not complete. Showing failed resource events; no existing stack was changed." >&2
    aws cloudformation describe-stack-events \
        --stack-name "$STACK_NAME" \
        --region "$REGION" \
        --query 'StackEvents[?ResourceStatus==`CREATE_FAILED`].[LogicalResourceId,ResourceStatusReason]' \
        --output table
    exit 1
fi

output() {
    aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --region "$REGION" \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" \
        --output text
}

PUBLIC_URL="$(output PublicUrl)"
INSTANCE_ID="$(output InstanceId)"
echo "Waiting for the instance to register with Systems Manager."
for attempt in $(seq 1 60); do
    if aws ssm describe-instance-information \
        --region "$REGION" \
        --filters "Key=InstanceIds,Values=${INSTANCE_ID}" \
        --query 'InstanceInformationList[0].PingStatus' \
        --output text 2>/dev/null | grep -q Online; then
        break
    fi
    if [[ "$attempt" -eq 60 ]]; then
        echo "EC2 did not register with Systems Manager within ten minutes." >&2
        exit 1
    fi
    sleep 10
done

COMMANDS="$(python3 -c 'import json,sys; print(json.dumps({"commands":["for attempt in $(seq 1 90); do if [ -x /usr/local/bin/anipick-deploy ]; then break; fi; sleep 10; done; test -x /usr/local/bin/anipick-deploy && sudo /usr/local/bin/anipick-deploy "+sys.argv[1]]}))' "$PUBLIC_URL")"
COMMAND_ID="$(aws ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --document-name AWS-RunShellScript \
    --parameters "$COMMANDS" \
    --region "$REGION" \
    --query Command.CommandId \
    --output text)"
for attempt in $(seq 1 60); do
    STATUS="$(aws ssm get-command-invocation \
        --command-id "$COMMAND_ID" \
        --instance-id "$INSTANCE_ID" \
        --region "$REGION" \
        --query Status \
        --output text 2>/dev/null || true)"
    case "$STATUS" in
        Success) break ;;
        Failed|Cancelled|TimedOut)
            aws ssm get-command-invocation \
                --command-id "$COMMAND_ID" \
                --instance-id "$INSTANCE_ID" \
                --region "$REGION" \
                --query '[Status,StandardOutputContent,StandardErrorContent]' \
                --output text
            exit 1
            ;;
    esac
    sleep 10
done

if [[ "${STATUS:-}" != Success ]]; then
    echo "The application did not finish its deployment command in time." >&2
    exit 1
fi

echo "Waiting for CloudFront deployment and public health check."
for attempt in $(seq 1 90); do
    if curl --fail --silent --show-error --max-time 10 "${PUBLIC_URL}/health" >/dev/null 2>&1; then
        printf 'AniPick is live: %s\n' "$PUBLIC_URL"
        exit 0
    fi
    sleep 20
done

echo "CloudFront did not return a healthy response. Stack and database are retained for diagnosis." >&2
exit 1
