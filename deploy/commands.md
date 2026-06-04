# Streamlit UI - CLI commands only

Backend (S3 / SQS / Lambda / DynamoDB) must already be deployed via `cd backend && serverless deploy`. These commands cover the **Streamlit UI on Lightsail Containers** only.

## Variables

```bash
export REGION=us-east-1
export AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export APP_NAME=human-in-loop-browser-agent
export SERVICE_NAME=human-in-loop-browser-agent
export IAM_USER=human-in-loop-browser-agent-runtime
```

## One-time setup

```bash
# IAM user
aws iam create-user --user-name "$IAM_USER"
aws iam put-user-policy \
  --user-name "$IAM_USER" \
  --policy-name HumanInLoopRuntimePolicy \
  --policy-document file://deploy/instance-role-policy.json
aws iam create-access-key --user-name "$IAM_USER"

# Lightsail container service (Micro = $10/mo)
aws lightsail create-container-service \
  --service-name "$SERVICE_NAME" \
  --power micro \
  --scale 1 \
  --region "$REGION"

# Wait until state = READY
aws lightsail get-container-services \
  --service-name "$SERVICE_NAME" \
  --query 'containerServices[0].state' \
  --output text \
  --region "$REGION"
```

## Build and push image

```bash
docker build --platform linux/amd64 -t "$APP_NAME:latest" .

aws lightsail push-container-image \
  --service-name "$SERVICE_NAME" \
  --label app \
  --image "$APP_NAME:latest" \
  --region "$REGION"
# Copy the printed image ref, e.g. :human-in-loop-browser-agent.app.1
```

## Deploy the container

Create `deploy/lightsail-deployment.json` (do **NOT** commit; secrets inside):

```json
{
  "containers": {
    "app": {
      "image": "<image-ref-from-push>",
      "ports": { "8501": "HTTP" },
      "environment": {
        "AWS_REGION": "us-east-1",
        "DYNAMODB_TABLE_NAME": "<from-serverless-info>",
        "UPLOAD_BUCKET_NAME": "<from-serverless-info>",
        "STREAMLIT_APP_PASSWORD": "<pick-any-password>",
        "OPENAI_API_KEY": "<your-openai-key>",
        "AWS_ACCESS_KEY_ID": "<from-iam-create-access-key>",
        "AWS_SECRET_ACCESS_KEY": "<from-iam-create-access-key>"
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

Deploy:

```bash
aws lightsail create-container-service-deployment \
  --service-name "$SERVICE_NAME" \
  --cli-input-json file://deploy/lightsail-deployment.json \
  --region "$REGION"
```

## Get the public URL

```bash
aws lightsail get-container-services \
  --service-name "$SERVICE_NAME" \
  --query 'containerServices[0].{state:state,url:url}' \
  --region "$REGION"
```

## Tail logs

```bash
aws lightsail get-container-log \
  --service-name "$SERVICE_NAME" \
  --container-name app \
  --region "$REGION"
```

## Redeploy a code change

```bash
docker build --platform linux/amd64 -t "$APP_NAME:latest" .

aws lightsail push-container-image \
  --service-name "$SERVICE_NAME" \
  --label app \
  --image "$APP_NAME:latest" \
  --region "$REGION"
# Update the "image" field in deploy/lightsail-deployment.json to the new ref

aws lightsail create-container-service-deployment \
  --service-name "$SERVICE_NAME" \
  --cli-input-json file://deploy/lightsail-deployment.json \
  --region "$REGION"
```

## Teardown (Streamlit UI only - leaves backend intact)

```bash
aws lightsail delete-container-service \
  --service-name "$SERVICE_NAME" \
  --region "$REGION"

for KEY in $(aws iam list-access-keys --user-name "$IAM_USER" \
              --query 'AccessKeyMetadata[].AccessKeyId' --output text); do
  aws iam delete-access-key --user-name "$IAM_USER" --access-key-id "$KEY"
done
aws iam delete-user-policy --user-name "$IAM_USER" --policy-name HumanInLoopRuntimePolicy
aws iam delete-user --user-name "$IAM_USER"
```
