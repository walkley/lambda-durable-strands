#!/usr/bin/env bash
set -euo pipefail

STACK_NAME="${1:-strands-durable-poc}"

sam build
sam deploy \
  --stack-name "$STACK_NAME" \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset
