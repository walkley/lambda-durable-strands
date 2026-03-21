#!/usr/bin/env bash
set -euo pipefail

STACK_NAME="${1:-strands-durable-poc}"

BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[?OutputKey==`SessionBucket`].OutputValue' \
  --output text 2>/dev/null || true)

if [ -n "$BUCKET" ]; then
  echo "Emptying bucket: $BUCKET"
  aws s3 rm "s3://$BUCKET" --recursive
fi

echo "Deleting stack: $STACK_NAME"
sam delete --stack-name "$STACK_NAME" --no-prompts
