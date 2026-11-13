#!/bin/bash
set -Eeuo pipefail

REGION="ap-northeast-2"
SECURITY_GROUP_ID="sg-08bf14d37ee032412"

PREFIX_LIST_ID="$(aws ec2 describe-managed-prefix-lists \
    --region "$REGION" \
    --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
    --query 'PrefixLists[0].PrefixListId' \
    --output text)"
if [[ -z "$PREFIX_LIST_ID" || "$PREFIX_LIST_ID" == "None" ]]; then
    echo "Could not find the CloudFront origin-facing managed prefix list." >&2
    exit 1
fi

aws ec2 authorize-security-group-ingress \
    --region "$REGION" \
    --group-id "$SECURITY_GROUP_ID" \
    --ip-permissions \
    "[{\"IpProtocol\":\"tcp\",\"FromPort\":8000,\"ToPort\":8000,\"PrefixListIds\":[{\"PrefixListId\":\"${PREFIX_LIST_ID}\",\"Description\":\"CloudFront origin traffic only\"}]}]" \
    --no-cli-pager

aws ec2 revoke-security-group-ingress \
    --region "$REGION" \
    --group-id "$SECURITY_GROUP_ID" \
    --ip-permissions \
    '[{"IpProtocol":"tcp","FromPort":8000,"ToPort":8000,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]' \
    --no-cli-pager

for port in 80 443; do
    aws ec2 revoke-security-group-ingress \
        --region "$REGION" \
        --group-id "$SECURITY_GROUP_ID" \
        --ip-permissions \
        "[{\"IpProtocol\":\"tcp\",\"FromPort\":${port},\"ToPort\":${port},\"IpRanges\":[{\"CidrIp\":\"0.0.0.0/0\"}]}]" \
        --no-cli-pager
done

aws ec2 revoke-security-group-ingress \
    --region "$REGION" \
    --group-id "$SECURITY_GROUP_ID" \
    --ip-permissions \
    '[{"IpProtocol":"tcp","FromPort":22,"ToPort":22,"IpRanges":[{"CidrIp":"13.209.1.56/29"}]}]' \
    --no-cli-pager

printf 'Origin locked to CloudFront prefix list %s; temporary EC2 Instance Connect rule removed.\n' \
    "$PREFIX_LIST_ID"
