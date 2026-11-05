#!/usr/bin/env bash
# One-time AWS setup for deploying HackerGit from GitHub Actions.
# Run in AWS CloudShell (the >_ icon in the AWS console) in the account that should host HackerGit:
#
#   EMAIL=you@example.com bash aws-bootstrap.sh
#
# It creates:
#   1. GitHub's sign-in provider (OIDC), so GitHub Actions can log in without stored AWS keys
#   2. A role "hackergit-deploy" that only the main branch of the HackerGit repo can use,
#      and that can only manage Lightsail
#   3. A $10/month budget that emails you if spending goes over, counting usage before credits
set -euo pipefail

REPO=${REPO:-murtazayusuf/hackergit}
EMAIL=${EMAIL:?Set EMAIL=you@example.com for budget alerts}
ROLE=hackergit-deploy
export AWS_PAGER=""
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
PROVIDER_ARN="arn:aws:iam::${ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"

# 1. GitHub OIDC provider
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$PROVIDER_ARN" >/dev/null 2>&1; then
  echo "GitHub sign-in provider already exists."
else
  aws iam create-open-id-connect-provider \
    --url https://token.actions.githubusercontent.com --client-id-list sts.amazonaws.com >/dev/null
  echo "Created GitHub sign-in provider."
fi

# 2. Deploy role
cat > /tmp/hackergit-trust.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "${PROVIDER_ARN}" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:${REPO}:ref:refs/heads/main"
      }
    }
  }]
}
EOF
if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE" --policy-document file:///tmp/hackergit-trust.json
  echo "Updated role $ROLE."
else
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document file:///tmp/hackergit-trust.json \
    --description "GitHub Actions deploys of HackerGit to Lightsail" >/dev/null
  echo "Created role $ROLE."
fi
aws iam put-role-policy --role-name "$ROLE" --policy-name lightsail-only --policy-document \
  '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"lightsail:*","Resource":"*"}]}'

# 3. Budget alert (IncludeCredit=false so it reacts to real usage even while credits cover the bill)
if aws budgets describe-budget --account-id "$ACCOUNT" --budget-name hackergit-monthly >/dev/null 2>&1; then
  echo "Budget already exists."
else
  aws budgets create-budget --account-id "$ACCOUNT" \
    --budget '{"BudgetName":"hackergit-monthly","BudgetLimit":{"Amount":"10","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST","CostTypes":{"IncludeCredit":false}}' \
    --notifications-with-subscribers "[
      {\"Notification\":{\"NotificationType\":\"ACTUAL\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":100,\"ThresholdType\":\"PERCENTAGE\"},
       \"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"${EMAIL}\"}]},
      {\"Notification\":{\"NotificationType\":\"FORECASTED\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":100,\"ThresholdType\":\"PERCENTAGE\"},
       \"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"${EMAIL}\"}]}
    ]"
  echo "Created \$10/month budget alert to ${EMAIL}."
fi

echo
echo "Done. Add this as the GitHub secret AWS_ROLE_ARN:"
echo "  arn:aws:iam::${ACCOUNT}:role/${ROLE}"
