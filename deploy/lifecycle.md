# Lifecycle runbook

Reproducible commands to **tear everything down** and **bring it back up**
from a clean account. Use this when you want to start fresh, debug a stuck
resource, or just stop the meter for a few weeks.

The flow is:

1. [Teardown](#teardown) - delete the Lightsail container, IAM user, serverless backend
2. [Recreate](#recreate) - serverless deploy + IAM user + Lightsail container

All commands assume you are in the repo root and have AWS CLI + Docker + the
Serverless Framework + `lightsailctl` installed.

```bash
# Variables used everywhere
export REGION=us-east-1
export AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export APP_NAME=human-in-loop-browser-agent
export SERVICE_NAME=human-in-loop-browser-agent
export IAM_USER=human-in-loop-browser-agent-runtime
export UPLOAD_BUCKET=human-in-loop-browser-agent-backend-dev-${AWS_ACCOUNT}-uploads
```

---

## Teardown

Order matters: IAM users must lose keys + policies before deletion, S3 buckets
must be emptied before the serverless stack can delete them.

### 1. Delete the Lightsail container service

```bash
aws lightsail delete-container-service \
  --service-name "$SERVICE_NAME" \
  --region "$REGION"
```

### 2. Delete the runtime IAM user

```bash
# Drop every access key first
for KEY in $(aws iam list-access-keys --user-name "$IAM_USER" \
              --query 'AccessKeyMetadata[].AccessKeyId' --output text); do
  aws iam delete-access-key --user-name "$IAM_USER" --access-key-id "$KEY"
done

# Drop the inline policy and the user
aws iam delete-user-policy --user-name "$IAM_USER" --policy-name HumanInLoopRuntimePolicy
aws iam delete-user --user-name "$IAM_USER"
```

### 3. Empty the upload bucket

Serverless can't delete a non-empty bucket.

```bash
aws s3 rm "s3://${UPLOAD_BUCKET}" --recursive --region "$REGION"
```

### 4. Tear down the serverless backend

```bash
cd backend
serverless remove
cd ..
```

This removes the S3 bucket, SQS queue + policy, Lambda, DynamoDB approval
table, CloudWatch log groups, and every IAM role the stack created.

### 5. (Optional) Delete the OpenAI Secrets Manager entry

Only relevant if you ever created one (the current Lightsail flow uses env
vars, not Secrets Manager).

```bash
aws secretsmanager delete-secret \
  --secret-id human-in-loop-browser-agent/openai-api-key \
  --force-delete-without-recovery \
  --region "$REGION" 2>/dev/null || echo "no secret to delete"
```

### 6. Rotate any keys that touched config files or chat windows

`deploy/lightsail-deployment.json` and (at one point) `deploy/README.md`
held real keys. Even after step 2, the OpenAI key still works:

- OpenAI: https://platform.openai.com/api-keys → revoke and create a new one.

### 7. Verify nothing is left

```bash
aws lightsail get-container-services --region "$REGION" \
  --query 'containerServices[].serviceName' --output text
aws iam get-user --user-name "$IAM_USER" 2>/dev/null || echo "IAM user gone"
aws s3 ls "s3://${UPLOAD_BUCKET}" 2>/dev/null || echo "bucket gone"
aws cloudformation describe-stacks \
  --stack-name human-in-loop-browser-agent-backend-dev \
  --region "$REGION" 2>/dev/null || echo "stack gone"
```

---

## Recreate

### 1. Deploy the serverless backend

```bash
cd backend
serverless deploy
serverless info
cd ..
```

Note the values from `serverless info` (you'll need them for the env vars
later):

- `ApprovalRequestsTableName` → `DYNAMODB_TABLE_NAME`
- `UploadBucketName`          → `UPLOAD_BUCKET_NAME`

### 2. Create the runtime IAM user

> Make sure [`instance-role-policy.json`](instance-role-policy.json) has the
> right account ID and resource names. The file is pre-filled for
> account `063899249535` / stage `dev`; edit if you're in a different account
> or stage.

```bash
aws iam create-user --user-name "$IAM_USER"

aws iam put-user-policy \
  --user-name "$IAM_USER" \
  --policy-name HumanInLoopRuntimePolicy \
  --policy-document file://deploy/instance-role-policy.json

# Capture the AccessKeyId + SecretAccessKey from the output. You won't see
# the secret again - save it somewhere safe (NOT in this repo).
aws iam create-access-key --user-name "$IAM_USER"
```

### 3. Create the Lightsail container service

```bash
aws lightsail create-container-service \
  --service-name "$SERVICE_NAME" \
  --power micro \
  --scale 1 \
  --region "$REGION"

# Wait for state to become READY (~2-3 minutes)
until [ "$(aws lightsail get-container-services --service-name "$SERVICE_NAME" \
            --query 'containerServices[0].state' --output text --region "$REGION")" = "READY" ]; do
  echo "  ...waiting for service to be READY"
  sleep 15
done
echo "Service is READY."
```

### 4. Build and push the image

```bash
docker build --platform linux/amd64 -t "$APP_NAME:latest" .

aws lightsail push-container-image \
  --service-name "$SERVICE_NAME" \
  --label app \
  --image "$APP_NAME:latest" \
  --region "$REGION"
# -> copy the printed image ref (e.g. :human-in-loop-browser-agent.app.1)
```

### 5. Build the deployment spec

Create or update `deploy/lightsail-deployment.json` (do **not** commit it -
it contains secrets).

Template (replace every `<...>` value):

```json
{
  "containers": {
    "app": {
      "image": "<image-ref-from-step-4>",
      "ports": { "8501": "HTTP" },
      "environment": {
        "AWS_REGION": "us-east-1",
        "DYNAMODB_TABLE_NAME": "<from-serverless-info>",
        "UPLOAD_BUCKET_NAME": "<from-serverless-info>",
        "STREAMLIT_APP_PASSWORD": "<pick-something>",
        "OPENAI_API_KEY": "<your-openai-key>",
        "AWS_ACCESS_KEY_ID": "<from-step-2>",
        "AWS_SECRET_ACCESS_KEY": "<from-step-2>"
      }
    }
  },
  "publicEndpoint": {
    "containerName": "app",
    "containerPort": 8501,
    "healthCheck": {
      "path": "/_stcore/health",
      "intervalSeconds": 30,
      "timeoutSeconds": 5,
      "healthyThreshold": 2,
      "unhealthyThreshold": 5,
      "successCodes": "200"
    }
  }
}
```

### 6. Deploy

```bash
aws lightsail create-container-service-deployment \
  --service-name "$SERVICE_NAME" \
  --cli-input-json file://deploy/lightsail-deployment.json \
  --region "$REGION"

# Wait for the deployment + get the public URL
aws lightsail get-container-services \
  --service-name "$SERVICE_NAME" \
  --query 'containerServices[0].{state:state,url:url}' \
  --region "$REGION"
```

When `state` is `RUNNING`, open `url`, enter your `STREAMLIT_APP_PASSWORD`,
and exercise both flows:

- **NL flow**: *Create Approval Request* → pending row → *Approve & Submit*
- **CSV flow**: upload [../sample_files/sample_upload.csv](../sample_files/sample_upload.csv) → wait ~5s → 3 pending rows from Lambda

### 7. Tail logs if something looks off

```bash
aws lightsail get-container-log \
  --service-name "$SERVICE_NAME" \
  --container-name app \
  --region "$REGION"

aws logs tail \
  /aws/lambda/human-in-loop-browser-agent-backend-dev-processUpload \
  --follow --region "$REGION"
```

---

## Redeploy a code change (no full recreate)

```bash
docker build --platform linux/amd64 -t "$APP_NAME:latest" .

aws lightsail push-container-image \
  --service-name "$SERVICE_NAME" \
  --label app \
  --image "$APP_NAME:latest" \
  --region "$REGION"
# -> bump the "image" field in deploy/lightsail-deployment.json to the new ref

aws lightsail create-container-service-deployment \
  --service-name "$SERVICE_NAME" \
  --cli-input-json file://deploy/lightsail-deployment.json \
  --region "$REGION"
```


============

# See what would be deleted (no changes)
python deploy/cleanup.py --dry-run

# Real run, interactive (must type "delete" to confirm)
python deploy/cleanup.py

# Non-interactive (CI / scripts)
python deploy/cleanup.py --yes

# Skip parts you want to keep
python deploy/cleanup.py --skip-backend       # keep S3/SQS/Lambda/DynamoDB
python deploy/cleanup.py --skip-secret        # don't touch Secrets Manager
