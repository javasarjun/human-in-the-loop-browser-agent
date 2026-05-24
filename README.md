# human-in-loop-browser-agent

A small demo of a **human-in-the-loop browser agent**:

1. You type a pizza order in plain English in **Streamlit**.
2. **OpenAI** extracts the structured form fields.
3. The request is saved as `pending` in **DynamoDB**.
4. You review it and click **Approve**.
5. **Playwright** opens the [httpbin.org pizza form](https://httpbin.org/forms/post) and submits it.
6. The agent **verifies** the submission against the httpbin response before marking the request `submitted` (or `failed`).

> Safe demo only. The target form is `httpbin.org/forms/post`, which just echoes the submitted values back. No login, no file uploads, no real personal data beyond the demo values.

---

## Project layout

```
app.py                # Streamlit control center
llm_service.py        # OpenAI -> structured form payload
dynamodb_service.py   # DynamoDB CRUD for approval requests
browser_agent.py      # Playwright fills + submits + verifies
submitter_service.py  # Orchestrates approve -> submit -> verify -> update status
main.py               # Optional CLI for testing each piece
requirements.txt
.env                  # OPENAI_API_KEY, AWS_REGION, DYNAMODB_TABLE_NAME
```

---

## Setup

```bash
# 1. Use the existing venv (or create one with: python3 -m venv venv)
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install the Playwright browser
playwright install chromium
```

Make sure your `.env` is filled in:

```
OPENAI_API_KEY=sk-...
AWS_REGION=us-east-1
DYNAMODB_TABLE_NAME=AgentApprovalRequests
```

You also need AWS credentials available to boto3 (e.g. `aws configure`, an SSO profile, or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in your environment).

The DynamoDB table is created automatically on first run (`PAY_PER_REQUEST` billing).

---

## Run the Streamlit app

```bash
streamlit run app.py
```

Then in the UI:

1. Leave the default request or edit it.
2. Click **Create Approval Request** — the LLM parses it, the payload appears, and a pending row shows up below.
3. Click **Approve & Submit** — Playwright drives the browser, the agent verifies, and the status updates to `submitted` or `failed`.
4. Click **Reject** to discard a request without submitting.

---

## CLI testing

```bash
# Parse the default request
python main.py parse

# Parse a custom request
python main.py parse "medium pizza with cheese for Sam at 7pm, phone 5551234567, email s@x.com"

# List all pending requests
python main.py list-pending

# Submit an approved request by id
python main.py submit <request_id>

# Run only the browser agent against a hardcoded payload
python main.py browser-only

# End-to-end: parse -> create pending -> approve -> submit
python main.py demo
```

---

## DynamoDB schema

Table: `AgentApprovalRequests`
Partition key: `request_id` (string)

| Attribute            | Type     | Notes                                                                   |
|----------------------|----------|-------------------------------------------------------------------------|
| `request_id`         | string   | UUID, partition key                                                     |
| `status`             | string   | `pending`, `approved`, `rejected`, `submitted`, `failed`                |
| `task_type`          | string   | `pizza_order`                                                           |
| `original_request`   | string   | The user's natural language input                                       |
| `payload`            | map      | Extracted form fields (`custname`, `custemail`, `custtel`, `size`, ...) |
| `review_summary`     | string   | Short human-readable summary for the approval card                      |
| `created_at`         | string   | ISO 8601 timestamp                                                      |
| `updated_at`         | string   | ISO 8601 timestamp                                                      |
| `approved_at`        | string   | Set when approved                                                       |
| `submitted_at`       | string   | Set when verification succeeds                                          |
| `error_message`      | string   | Set when verification fails                                             |
| `submission_result`  | map      | Result returned by `browser_agent.submit_form()`                        |

---

## Status lifecycle

```
       Create
         |
         v
      pending  --(Reject)-->  rejected
         |
       Approve
         |
         v
      approved
         |
      Playwright submits + agent verifies httpbin response
         |
    +----+----+
    |         |
verified   failed
    |         |
submitted  failed (error_message saved)
```

A request is **only** marked `submitted` when the verifier finds every submitted field in the httpbin response page. Anything else is `failed`.

---

## Safety rules baked into the agent

- The browser never submits automatically — submission only runs after `approve_request()` sets status to `approved`.
- `submitter_service.submit_approved_request()` refuses to submit if the request's status is anything other than `approved`.
- The target site is `httpbin.org`, which is a safe echo service.
- No login, no file uploads, no CAPTCHA bypassing.
- Only demo personal data is used (the default request uses public demo values).
