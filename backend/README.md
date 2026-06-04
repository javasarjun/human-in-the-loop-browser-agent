# backend/

AWS deployment for the human-in-loop browser agent.

Provisions:

- **DynamoDB** table for approval requests (`${service}-${stage}-approval-requests`)
- **S3** bucket for CSV uploads (`${service}-${stage}-${accountId}-uploads`)
- **SQS** queue + queue policy
- **S3 ObjectCreated notification** to SQS (filtered to `uploads/*.csv`)
- **Lambda** function (`processUpload`) triggered by SQS, with IAM permissions for `s3:GetObject`, `dynamodb:PutItem`, the relevant `sqs:*` actions, and CloudWatch Logs

Lambda only **creates** pending approval requests. Browser submission stays local in the root project.

---

## Layout

```
backend/
├── serverless.yml         # CloudFormation via Serverless Framework
├── lambda_handler.py      # Lambda entry: process_upload(event, context)
├── requirements.txt       # Python deps (boto3 is provided by Lambda runtime)
├── README.md              # This file
└── src/
    ├── __init__.py
    ├── csv_processor.py   # Parse + validate CSV bytes
    ├── dynamodb_writer.py # Insert one approval request per valid row
    └── utils.py           # now_iso(), generate_request_id()
```

---

## One-time setup

```bash
# From the repo root
cd backend

# Install Serverless Framework (Node, not Python)
npm install -g serverless
```

Make sure AWS credentials are available (`aws configure`, an SSO profile, or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars).

---

## Deploy

```bash
cd backend
serverless deploy
```

Defaults: `stage=dev`, `region=us-east-1`. Override with `--stage`/`--region` if needed.

After deploy, look up the deployed resource names:

```bash
serverless info
```

You'll see outputs like:

```
ApprovalRequestsTableName: human-in-loop-browser-agent-backend-dev-approval-requests
UploadBucketName:          human-in-loop-browser-agent-backend-dev-123456789012-uploads
UploadQueueUrl:            https://sqs.us-east-1.amazonaws.com/123456789012/...
```

Copy the table name and bucket name into the **root** `.env`:

```bash
# Root /.env
DYNAMODB_TABLE_NAME=human-in-loop-browser-agent-backend-dev-approval-requests
UPLOAD_BUCKET_NAME=human-in-loop-browser-agent-backend-dev-123456789012-uploads
```

Then run the Streamlit app from the root and upload `sample_files/sample_upload.csv`:

```bash
cd ..
streamlit run app.py
```

---

## End-to-end flow

1. Streamlit uploads a CSV to `s3://${UPLOAD_BUCKET_NAME}/uploads/<timestamp>-<name>.csv`.
2. The bucket's `NotificationConfiguration` sends an `ObjectCreated` event to the SQS upload queue (filtered to prefix `uploads/`, suffix `.csv`).
3. The SQS event triggers Lambda (`processUpload`) with `batchSize: 1`.
4. Lambda parses the SQS body, extracts `bucket` and `key` from the S3 event notification, downloads the CSV via `s3:GetObject`, and parses the rows with `src/csv_processor.py`. Expected columns: `custname, custemail, custtel, size, delivery` (required) and `topping, comments` (optional).
5. Each valid row becomes one DynamoDB approval request (status `pending`, `task_type: demo_form_submission`, `source: s3_upload`).
6. The pending request shows up in the Streamlit dashboard. A human approves it, and the **local** `submitter_service.py` runs Playwright to submit the demo form.

---

## Removing the stack

```bash
serverless remove
```

The S3 bucket must be empty to delete it. If it isn't, run `aws s3 rm s3://<bucket>/uploads --recursive` first.

---

## About the S3 -> SQS "circular dependency"

CloudFormation refuses to deploy when two resources reference each other via logical IDs. The classic version of this happens with S3 -> SQS:

- The bucket's `NotificationConfiguration` references the SQS queue ARN.
- The SQS queue policy wants to allow only **this** bucket to publish, so it needs the bucket ARN.

`serverless.yml` here avoids the cycle by building the bucket ARN as a **string** from the deterministic bucket name (`${self:service}-${self:provider.stage}-${aws:accountId}-uploads`), instead of referencing the `UploadBucket` resource directly. The bucket then uses `DependsOn: UploadQueuePolicy` so the policy is in place before the bucket's notification config tries to publish.

See the comments at the top of `serverless.yml` for details.

---

## Safety

- Lambda **does not** submit any browser forms; it only writes pending approval requests.
- Browser submission stays local through `submitter_service.py` + `browser_agent.py`.
- Demo target is `httpbin.org`, which is a safe echo service. Do not point this at real third-party sites.
- No real personal data; no CAPTCHA bypassing.
