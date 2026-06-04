# deploy/

End-to-end walkthrough for deploying the Streamlit app to **AWS Lightsail
Container Service**.

> Note: this used to target AWS App Runner. App Runner stopped accepting
> new customers on 2026-04-30, so we switched to Lightsail Containers.
> Same Dockerfile, same password gate, same set of IAM permissions — the
> only thing that changes is the compute target and how credentials reach
> the container.

The serverless backend (S3 + SQS + Lambda + DynamoDB) is **already deployed**
via [../backend/](../backend/). This directory is only for the Streamlit UI.

---

## Layout

```
deploy/
├── README.md                    # this file
└── instance-role-policy.json    # IAM policy: what the container is allowed to do
                                 # (Lightsail has no task roles, so we attach
                                 # this to an IAM user and put its keys in env)
```

The `Dockerfile` and `.dockerignore` live at the repo root so `docker build .`
from there picks them up naturally.

---

## Prerequisites

- Docker running locally.
- AWS CLI v2 with credentials that can create IAM/Lightsail resources.
- The [Lightsail Control plugin](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-install-software.html) installed (`lightsailctl`) — required to push container images:
  - macOS: `brew install aws/tap/lightsailctl`
  - Linux: see the AWS docs (one-line curl).
- Backend already deployed (`cd ../backend && serverless info` returns the bucket and table names).

---

## One-time setup

```bash
export REGION=us-east-1
export AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export APP_NAME=human-in-loop-browser-agent
export SERVICE_NAME=human-in-loop-browser-agent
export IAM_USER=human-in-loop-browser-agent-runtime
```

### 1. Create the IAM user the container will run as

Lightsail Containers don't support IAM task roles, so we create a small IAM
user with exactly the permissions the app needs, generate an access key, and
inject it into the container as env vars.

**Before running the next command, open
[`instance-role-policy.json`](instance-role-policy.json) and confirm the
resource ARNs match your account and stack names.** It's pre-filled with the
values from this project (account `063899249535`, stage `dev`); edit if you
deployed under a different stage/account.

```bash
aws iam create-user --user-name "$IAM_USER"

aws iam put-user-policy \
  --user-name "$IAM_USER" \
  --policy-name HumanInLoopRuntimePolicy \
  --policy-document file://instance-role-policy.json

# Generate the access key. SAVE THE OUTPUT - you'll paste it into Lightsail.
aws iam create-access-key --user-name "$IAM_USER"

```

### 2. Create the Lightsail container service

```bash
aws lightsail create-container-service \
  --service-name "$SERVICE_NAME" \
  --power micro \
  --scale 1 \
  --region "$REGION"
```

| Setting | Value                                              |
|---------|----------------------------------------------------|
| Power   | `micro` (0.5 vCPU / 1 GB, $10/mo)                  |
| Scale   | 1 (one container node — plenty for a demo)         |

It takes a few minutes for the service to enter the `READY` state. Poll with:

```bash
aws lightsail get-container-services \
  --service-name "$SERVICE_NAME" \
  --query 'containerServices[0].state' \
  --output text \
  --region "$REGION"
```

---

## Build and push the image

Run from the **repo root** (one level up from this directory).

```bash
cd ..

# App Runner -> Lightsail both run on linux/amd64. Force the platform on Apple Silicon.
export APP_NAME=human-in-loop-browser-agent
docker build --platform linux/amd64 -t "$APP_NAME:latest" .


# Push directly to Lightsail (no separate ECR step needed - Lightsail manages its own registry).
aws lightsail push-container-image \
  --service-name "$SERVICE_NAME" \
  --label app \
  --image "$APP_NAME:latest" \
  --region "$REGION"

aws lightsail push-container-image \
  --service-name human-in-loop-browser-agent \
  --label app \
  --image human-in-loop-browser-agent:latest \
  --region us-east-1

```

The push command prints a refer-by name like `:human-in-loop-browser-agent.app.1`. **Copy that string** — you need it in the next step.

---

## Deploy the container

Create a deployment spec that points at the pushed image, sets env vars, and
exposes port 8501.

Save this as `lightsail-deployment.json` in the `deploy/` directory (or
anywhere you like — just remember the path):

```json
{
  "containers": {
    "app": {
      "image": ":human-in-loop-browser-agent.app.1",
      "ports": { "8501": "HTTP" },
      "environment": {
        "AWS_REGION": "us-east-1",
        "DYNAMODB_TABLE_NAME": "human-in-loop-browser-agent-backend-dev-approval-requests",
        "UPLOAD_BUCKET_NAME": "human-in-loop-browser-agent-backend-dev-063899249535-uploads",
        "STREAMLIT_APP_PASSWORD": "admin123456",
        "OPENAI_API_KEY":"REDACTED_OPENAI_KEY",
        "AWS_ACCESS_KEY_ID": "REDACTED_AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY": "REDACTED_AWS_SECRET_ACCESS_KEY"
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

**Important**:
- Replace the `:human-in-loop-browser-agent.app.1` image reference with the actual one printed by `push-container-image`.
- Replace `OPENAI_API_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `STREAMLIT_APP_PASSWORD` with real values.
- Don't commit this file to git — it contains real secrets. Consider keeping it in `~/.aws/` and referencing by absolute path, or use a templating step.

Deploy:

```bash
aws lightsail create-container-service-deployment \
  --service-name "$SERVICE_NAME" \
  --cli-input-json file://deploy/lightsail-deployment.json \
  --region "$REGION"
```

Wait for the deployment to finish (~2-3 minutes):

```bash
aws lightsail get-container-services \
  --service-name "$SERVICE_NAME" \
  --query 'containerServices[0].{state:state,url:url}' \
  --region "$REGION"
```

When `state` is `RUNNING`, hit the `url` (Lightsail auto-provisions a `https://<random>.<region>.cs.amazonaws.com` URL with a free cert).

---

## Verify

1. Open the Lightsail URL → enter `STREAMLIT_APP_PASSWORD`.
2. **NL flow**: *Create Approval Request* → pending row appears → *Approve & Submit* → row flips to `submitted`.
3. **CSV flow**: upload [../sample_files/sample_upload.csv](../sample_files/sample_upload.csv) → wait ~5s → 3 pending rows appear.
4. View logs:
   ```bash
   aws lightsail get-container-log \
     --service-name "$SERVICE_NAME" \
     --container-name app \
     --region "$REGION"
   ```

---

## Redeploy a new image

```bash
cd ..
docker build --platform linux/amd64 -t "$APP_NAME:latest" .
aws lightsail push-container-image \
  --service-name "$SERVICE_NAME" \
  --label app \
  --image "$APP_NAME:latest" \
  --region "$REGION"
# Then either bump the image ref in lightsail-deployment.json and re-create
# the deployment, OR use the Lightsail console: Services -> your service ->
# Deployments -> Modify your deployment.
aws lightsail create-container-service-deployment \
  --service-name "$SERVICE_NAME" \
  --cli-input-json file://deploy/lightsail-deployment.json \
  --region "$REGION"
```

---

## Tear down

```bash
aws lightsail delete-container-service \
  --service-name "$SERVICE_NAME" \
  --region "$REGION"

aws iam delete-access-key --user-name "$IAM_USER" --access-key-id <id>
aws iam delete-user-policy --user-name "$IAM_USER" --policy-name HumanInLoopRuntimePolicy
aws iam delete-user --user-name "$IAM_USER"
```

---

## Cost expectation

| Item                                 | Cost            |
|--------------------------------------|-----------------|
| Lightsail Container Service (Micro)  | $10/mo flat     |
| Public endpoint / TLS                | $0 (included)   |
| IAM user                             | $0              |
| Data transfer (light demo use)       | <$0.50/mo       |
| **Total**                            | **~$10-11/mo**  |

Well within your $15/mo target. Note Lightsail is **always-on** — there's no pause-on-idle, so the bill is the same whether you use it for 1 hour or 100.


docker build --platform linux/amd64 -t human-in-loop-browser-agent:latest .

aws lightsail push-container-image \
  --service-name human-in-loop-browser-agent \
  --label app \
  --image human-in-loop-browser-agent:latest \
  --region us-east-1
# -> note the new image ref (e.g. :human-in-loop-browser-agent.app.2)

# bump "image" in deploy/lightsail-deployment.json to the new ref, then:
aws lightsail create-container-service-deployment \
  --service-name human-in-loop-browser-agent \
  --cli-input-json file://deploy/lightsail-deployment.json \
  --region us-east-1


  ========

python deploy/cleanup.py            # tear everything down
python deploy/setup.py              # bring it all back up
python deploy/setup.py --skip-backend   # redeploy code only (most common)


