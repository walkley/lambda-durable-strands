#!/usr/bin/env bash
set -euo pipefail

STACK_NAME="${1:-strands-durable-poc}"
PROMPT="${2:-calculate 123 * 456 + 789}"
SESSION_ID="test-$(date +%s)"

FUNC=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[?OutputKey==`OrchestratorAlias`].OutputValue' \
  --output text)

echo "Invoking $FUNC (session: $SESSION_ID)"

RESPONSE=$(aws lambda invoke \
  --function-name "$FUNC" \
  --invocation-type Event \
  --cli-binary-format raw-in-base64-out \
  --payload "{\"session_id\":\"$SESSION_ID\",\"prompt\":\"$PROMPT\"}" \
  /tmp/invoke-response.json 2>&1)

ARN=$(echo "$RESPONSE" | grep -o '"DurableExecutionArn": "[^"]*"' | cut -d'"' -f4)

if [ -z "$ARN" ]; then
  echo "Failed to get DurableExecutionArn"
  echo "$RESPONSE"
  exit 1
fi

echo "DurableExecutionArn: $ARN"
echo "Polling for completion..."

while true; do
  STATUS=$(aws lambda get-durable-execution \
    --durable-execution-arn "$ARN" \
    --query 'Status' --output text 2>/dev/null || echo "PENDING")

  echo "  Status: $STATUS"

  if [ "$STATUS" = "SUCCEEDED" ] || [ "$STATUS" = "FAILED" ]; then
    aws lambda get-durable-execution --durable-execution-arn "$ARN"
    break
  fi

  sleep 5
done
