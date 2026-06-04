# human-in-loop-browser-agent

A human-in-the-loop browser agent split into a **local Streamlit app** and a
**deployable AWS backend**.

There are two ways to create approval requests, but the same review/approve UI
and the same browser submission code handle both.

```
                   +--------------------+        +--------------------+
                   |   Streamlit UI     |        |     OpenAI         |
                   |   (root project)   |<------>|   (LLM parser)     |
                   +---------+----------+        +--------------------+
                             |
              (A) NL request | creates DynamoDB pending
                             v
+-------------+        +-----+------+        +-------------------+
| Reviewer    |<------>| DynamoDB   |<-------|  Lambda processor |
| (you, in    |        | approval   |        |  (backend/)       |
| Streamlit)  |        | requests   |        +---------+---------+
+-----+-------+        +-----+------+                  ^
      | Approve             ^                          |
      v                     | (B) one item per row     |
+-----+--------+            |                          |
| Playwright   |            |     +--------+   +-------+--------+
| browser_agent|            |     |  SQS   |<--|      S3        |
| (local)      |            |     +--------+   | uploads/*.csv  |
+--------------+            |                  +-------+--------+
                            |                          ^
                            |                  Streamlit uploads here
                            +<-------------------------+
```

Two creation paths:

- **(A) Natural language** — type a pizza order, OpenAI extracts the structured fields, request goes into DynamoDB as `pending`.
- **(B) CSV upload to S3** — Streamlit uploads a CSV. S3's ObjectCreated event triggers SQS, which triggers Lambda. Lambda reads the CSV and inserts one `pending` approval request per valid row.

Approval/submission stays local: you click **Approve & Submit** in Streamlit, and the local `submitter_service.py` drives Playwright to fill the demo form on [httpbin.org](https://httpbin.org/forms/post).

> Safe demo only. No login, no real personal data, no CAPTCHA bypassing.

---

## Repository layout

```
human-in-loop-browser-agent/
├── app.py                    # Streamlit UI
├── browser_agent.py          # Playwright + verification
├── dynamodb_service.py       # DynamoDB CRUD for approval requests
├── llm_service.py            # OpenAI -> structured payload
├── main.py                   # Optional CLI tester
├── processing_service.py     # Local CSV processor (alternative to Lambda)
├── submitter_service.py      # Submit approved requests (lib + `python submitter_service.py`)
├── upload_service.py         # Upload files to S3
├── requirements.txt
├── README.md                 # This file
├── .env-example
├── .gitignore
├── prompt.txt
├── sample_files/
│   └── sample_upload.csv
├── uploads/                  # (kept for local-only experiments; not used in S3 path)
└── backend/
    ├── serverless.yml        # AWS infra (S3, SQS, Lambda, DynamoDB, IAM)
    ├── lambda_handler.py     # Lambda entry point (SQS-triggered)
    ├── requirements.txt
    ├── README.md
    └── src/
        ├── __init__.py
        ├── csv_processor.py
        ├── dynamodb_writer.py
        └── utils.py
```

---

## Environment variables (root `.env`)

Copy `.env-example` to `.env` and fill in:

| Variable              | Used by         | What it is                                                                                  |
|-----------------------|-----------------|---------------------------------------------------------------------------------------------|
| `OPENAI_API_KEY`      | `llm_service`   | Your OpenAI API key (needed only for the natural-language flow).                            |
| `AWS_REGION`          | boto3           | AWS region for DynamoDB and S3 (default: `us-east-1`).                                      |
| `DYNAMODB_TABLE_NAME` | `dynamodb_service` | Approval requests table. After `serverless deploy`, copy the name from `serverless info`. |
| `UPLOAD_BUCKET_NAME`  | `upload_service`   | S3 upload bucket. After `serverless deploy`, copy the name from `serverless info`.        |

The backend does **not** need `OPENAI_API_KEY` — the Lambda only reads CSVs and writes to DynamoDB.

You also need AWS credentials available to boto3 (`aws configure`, an SSO profile, or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in your environment).

---

## Local development

```bash
# 1. Virtual environment
python3 -m venv venv
source venv/bin/activate

# 2. Install root requirements
pip install -r requirements.txt
playwright install chromium

# 3. Set up .env
cp .env-example .env
#    -> edit .env with your real values

# 4. Run Streamlit
streamlit run app.py
```

Then in the browser tab Streamlit opens:

5. **Either** type a pizza order and click **Create Approval Request** (natural-language flow), **or** upload `sample_files/sample_upload.csv` and click **Upload to S3** (CSV flow).
6. Watch the **Pending Approvals** section.
7. To submit a request manually (separate process):

```bash
# Submits every request currently at status='approved'
python submitter_service.py
```

You can also approve+submit inline by clicking **Approve & Submit** in the UI.

---

## Backend deployment

See [backend/README.md](backend/README.md) for the full walkthrough. Short version:

```bash
cd backend
npm install -g serverless
serverless deploy
serverless info
# Copy `ApprovalRequestsTableName` and `UploadBucketName` into your root .env.
```

---

## Hosting the Streamlit UI on AWS Lightsail Containers

> Originally planned on AWS App Runner. App Runner stopped accepting new
> customers on 2026-04-30; we switched to Lightsail Containers. The Dockerfile
> and IAM permissions are unchanged.

The full walkthrough is in [deploy/README.md](deploy/README.md). Short version:

```bash
# 0. Variables
export REGION=us-east-1
export APP_NAME=human-in-loop-browser-agent
export SERVICE_NAME=human-in-loop-browser-agent
export IAM_USER=human-in-loop-browser-agent-runtime

# 1. One-time: IAM user + access key the container will run as
aws iam create-user --user-name "$IAM_USER"
aws iam put-user-policy --user-name "$IAM_USER" \
  --policy-name HumanInLoopRuntimePolicy \
  --policy-document file://deploy/instance-role-policy.json
aws iam create-access-key --user-name "$IAM_USER"   # save the output

# 2. Create the Lightsail container service (Micro = $10/mo)
aws lightsail create-container-service \
  --service-name "$SERVICE_NAME" \
  --power micro --scale 1 --region "$REGION"

# 3. Build and push the image (linux/amd64 for Lightsail)
docker build --platform linux/amd64 -t "$APP_NAME:latest" .
aws lightsail push-container-image \
  --service-name "$SERVICE_NAME" \
  --label app --image "$APP_NAME:latest" --region "$REGION"
# -> note the printed image reference, e.g. :human-in-loop-browser-agent.app.1

# 4. Deploy with env vars (edit deploy/lightsail-deployment.json from the
#    template in deploy/README.md - it has placeholders for the image ref,
#    OPENAI_API_KEY, and the AWS access key/secret from step 1)
aws lightsail create-container-service-deployment \
  --service-name "$SERVICE_NAME" \
  --cli-input-json file://deploy/lightsail-deployment.json \
  --region "$REGION"
```

After ~3 minutes the service prints a `https://<random>.<region>.cs.amazonaws.com` URL with a free TLS cert. Open it, enter your `STREAMLIT_APP_PASSWORD`, and verify both flows end-to-end.

Redeploy a new image:

```bash
docker build --platform linux/amd64 -t "$APP_NAME:latest" .
aws lightsail push-container-image --service-name "$SERVICE_NAME" \
  --label app --image "$APP_NAME:latest" --region "$REGION"
# bump the image ref in deploy/lightsail-deployment.json, then:
aws lightsail create-container-service-deployment \
  --service-name "$SERVICE_NAME" \
  --cli-input-json file://deploy/lightsail-deployment.json \
  --region "$REGION"
```

---

## CLI testing (root)

```bash
python main.py parse                  # parse default natural-language request
python main.py list-pending           # list pending DynamoDB requests
python main.py submit <request_id>    # submit one approved request
python main.py browser-only           # run Playwright against a hardcoded payload
python main.py demo                   # parse -> create pending -> approve -> submit
```

---

## DynamoDB schema

Table: `${service}-${stage}-approval-requests` (set via `DYNAMODB_TABLE_NAME`)
Partition key: `request_id` (string)

| Attribute            | Notes                                                                                  |
|----------------------|----------------------------------------------------------------------------------------|
| `request_id`         | UUID, partition key                                                                    |
| `status`             | `pending`, `approved`, `rejected`, `submitted`, `failed`                               |
| `task_type`          | `pizza_order` (NL flow) or `demo_form_submission` (CSV flow)                           |
| `source`             | (CSV flow) `s3_upload`                                                                 |
| `source_file`        | (CSV flow) S3 key the row came from                                                    |
| `row_number`         | (CSV flow) 1-based row index                                                           |
| `original_request`   | The user's natural language input (NL flow)                                            |
| `payload`            | Form fields                                                                            |
| `review_summary`     | Short human-readable summary                                                           |
| `created_at`         | ISO 8601                                                                               |
| `updated_at`         | ISO 8601                                                                               |
| `approved_at`        | Set when approved                                                                      |
| `submitted_at`       | Set when verification succeeds                                                         |
| `error_message`      | Set when verification fails                                                            |
| `submission_result`  | Result returned by `browser_agent.submit_form()`                                       |

---

## Safety rules

- The browser never submits automatically — submission only runs after a human explicitly approves.
- `submitter_service.submit_approved_request()` refuses to submit anything whose status isn't `approved`.
- Lambda **never** submits forms; it only creates pending records.
- Demo target is `httpbin.org`, which is a safe echo service.
- No login, no file uploads, no CAPTCHA bypassing.
- Use only demo personal data.
