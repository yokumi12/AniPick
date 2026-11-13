#!/bin/bash
set -Eeuo pipefail
umask 077

REGION="${AWS_REGION:-$(aws configure get region)}"
STACK_NAME="${ANIPICK_STACK_NAME:-anipick-prod}"
SOURCE_ARCHIVE="${ANIPICK_SOURCE_ARCHIVE:-anipick-source.tar.gz}"

if [[ "$REGION" != "ap-northeast-2" || ! -f "$SOURCE_ARCHIVE" ]]; then
    echo "Use ap-northeast-2 and provide $SOURCE_ARCHIVE in this project folder." >&2
    exit 1
fi

STACK_STATUS="$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query 'Stacks[0].StackStatus' \
    --output text)"
if [[ "$STACK_STATUS" != CREATE_COMPLETE && "$STACK_STATUS" != UPDATE_COMPLETE ]]; then
    echo "Stack $STACK_NAME is not healthy for redeployment: $STACK_STATUS" >&2
    exit 1
fi

output() {
    aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --region "$REGION" \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" \
        --output text
}

BUCKET="$(output ArtifactBucket)"
INSTANCE_ID="$(output InstanceId)"
PUBLIC_URL="$(output PublicUrl)"
OBJECT_KEY="releases/$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
aws s3 cp "$SOURCE_ARCHIVE" "s3://${BUCKET}/${OBJECT_KEY}" --region "$REGION"

for attempt in $(seq 1 60); do
    if aws ssm describe-instance-information \
        --region "$REGION" \
        --filters "Key=InstanceIds,Values=${INSTANCE_ID}" \
        --query 'InstanceInformationList[0].PingStatus' \
        --output text 2>/dev/null | grep -q Online; then
        break
    fi
    if [[ "$attempt" -eq 60 ]]; then
        echo "EC2 is not online in Systems Manager." >&2
        exit 1
    fi
    sleep 10
done

COMMANDS="$(python3 -c 'import json,sys; print(json.dumps({"commands":["sudo sed -i \u0027s|^DEPLOYMENT_OBJECT_KEY=.*|DEPLOYMENT_OBJECT_KEY="+sys.argv[1]+"|\u0027 /etc/anipick/deployment.conf && sudo /usr/local/bin/anipick-deploy "+sys.argv[2]]}))' "$OBJECT_KEY" "$PUBLIC_URL")"
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
    echo "The application did not finish its redeployment command in time." >&2
    exit 1
fi
if ! curl --fail --silent --show-error --max-time 10 "${PUBLIC_URL}/health"; then
    echo "Redeployment completed but the public health check failed." >&2
    exit 1
fi
printf 'AniPick redeployed: %s\n' "$PUBLIC_URL"
