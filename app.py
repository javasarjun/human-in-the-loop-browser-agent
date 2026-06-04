"""
app.py

The Streamlit control center for the human-in-the-loop browser agent.

Two ways to create approval requests live in this UI:

  A. Natural language flow (local + OpenAI):
       User types a request -> LLM extracts a pizza order -> DynamoDB pending.

  B. CSV upload flow (cloud):
       User uploads a CSV -> S3 (uploads/) -> S3 event -> SQS -> Lambda
       -> Lambda creates one DynamoDB pending request per valid row.

Both flows surface in the same Pending Approvals dashboard, where you can
approve or reject and (for the natural-language flow) immediately submit
via the local Playwright browser agent.

Run with:
  streamlit run app.py
"""

import json
import os

import streamlit as st

import dynamodb_service
import llm_service
import submitter_service
import upload_service

# ---- Page setup ----------------------------------------------------------------

st.set_page_config(page_title="Human-in-the-Loop Browser Agent", layout="wide")

# When STREAMLIT_APP_PASSWORD is set (it is, in the App Runner deployment),
# gate the whole page behind a single shared password. Locally the env var is
# unset, so this block is a no-op.
_app_password = os.getenv("STREAMLIT_APP_PASSWORD", "")
if _app_password and not st.session_state.get("authed"):
    st.title("Sign in")
    entered = st.text_input("Password", type="password")
    if entered:
        if entered == _app_password:
            st.session_state["authed"] = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()

st.title("Human-in-the-Loop Browser Agent")
st.caption(
    "Natural language or CSV upload -> human approval -> local Playwright submission."
)

# The approval requests table is managed by the Serverless stack in backend/.
# Local dev with a hand-rolled table can call dynamodb_service.ensure_table_exists()
# directly from a REPL; we don't do it on every page load any more, partly because
# it requires the dynamodb:ListTables permission which the deployed IAM user
# doesn't need to have.

DEFAULT_REQUEST = (
    "Can you order a small vegetarian pizza with onion, mushroom for Arjun Vaid around 8:30 PM? "
    "Use email javasarjun@gmail.com and phone 4694268163. "
    "Please add a note asking for extra cheese, but don't submit until I approve."
)

# ---- Section 1: Create Approval Request (natural language) --------------------

st.header("Create Approval Request")

natural_request = st.text_area(
    "Natural Language Request",
    value=DEFAULT_REQUEST,
    height=160,
    help="Describe the pizza order in plain English. The LLM will extract the form fields.",
)

if st.button("Create Approval Request", type="primary"):
    if not natural_request.strip():
        st.error("Please enter a request before creating one.")
    else:
        with st.spinner("Asking the LLM to extract the form fields..."):
            try:
                parsed = llm_service.parse_request(natural_request)
            except Exception as exc:
                st.error(f"LLM parsing failed: {exc}")
                parsed = None

        if parsed is not None:
            payload = parsed["payload"]
            missing = parsed["missing_fields"]
            review_summary = parsed["review_summary"]

            st.subheader("Extracted Payload")
            st.json(payload)

            if missing:
                st.error(
                    "Some fields could not be extracted. Please update the request and try again."
                )
                st.write("Missing fields:", ", ".join(missing))
            else:
                try:
                    item = dynamodb_service.create_request(
                        task_type="pizza_order",
                        original_request=natural_request,
                        payload=payload,
                        review_summary=review_summary,
                    )
                    st.success(f"Saved as pending approval request: {item['request_id']}")
                except Exception as exc:
                    st.error(f"Could not save to DynamoDB: {exc}")

# ---- Section 2: Pending Approvals ---------------------------------------------

st.header("Pending Approvals")

try:
    pending = dynamodb_service.list_pending_requests()
except Exception as exc:
    st.error(f"Could not load pending requests from DynamoDB: {exc}")
    pending = []

if not pending:
    st.info("No pending approval requests. Create one above to get started.")

for req in pending:
    request_id = req["request_id"]
    with st.container(border=True):
        st.markdown(f"**Request ID:** `{request_id}`")
        st.markdown(f"**Summary:** {req.get('review_summary', '')}")
        st.markdown(f"**Created:** {req.get('created_at', '')}")

        # Surface batch metadata when the request came from an S3/CSV upload.
        if req.get("source"):
            st.markdown(
                f"**Source:** `{req['source']}`  ·  **File:** `{req.get('source_file', '')}`"
                f"  ·  **Row:** {req.get('row_number', '')}"
            )

        with st.expander("Original natural language request"):
            st.write(req.get("original_request", "(not applicable - CSV row)"))

        with st.expander("Parsed payload"):
            st.json(req.get("payload", {}))

        col_approve, col_reject = st.columns(2)

        if col_approve.button("Approve & Submit", key=f"approve-{request_id}"):
            try:
                dynamodb_service.approve_request(request_id)
            except Exception as exc:
                st.error(f"Could not mark approved: {exc}")
            else:
                with st.spinner("Running browser agent and verifying submission..."):
                    result = submitter_service.submit_approved_request(request_id)

                if result.get("success"):
                    st.success("Submitted and verified.")
                else:
                    st.error("Submission failed verification.")
                st.json(result)
                st.rerun()

        if col_reject.button("Reject", key=f"reject-{request_id}"):
            try:
                dynamodb_service.reject_request(request_id)
                st.success("Request rejected.")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not reject: {exc}")

# ---- Section 3: Upload CSV to S3 ----------------------------------------------

st.header("Upload CSV to S3")
st.caption(
    "Upload a .csv with columns: custname, custemail, custtel, size, delivery "
    "(optional: topping, comments). The S3 ObjectCreated event triggers SQS -> "
    "Lambda, which creates one pending approval request per valid row. Watch "
    "this section's pending list refresh after a few seconds."
)

if not upload_service.UPLOAD_BUCKET_NAME:
    st.warning(
        "UPLOAD_BUCKET_NAME is not set in .env. Deploy the backend "
        "(see backend/README.md) and copy the bucket name into your .env."
    )

uploaded_file = st.file_uploader(
    "Choose a CSV file",
    type=["csv"],
    accept_multiple_files=False,
    key="file_uploader",
)

if uploaded_file is not None:
    # Only upload when the user clicks the button - otherwise Streamlit would
    # re-upload the same file on every rerun.
    if st.button("Upload to S3", key="upload_btn"):
        with st.spinner("Uploading to S3..."):
            result = upload_service.save_uploaded_file(uploaded_file)
        if result["success"]:
            st.success(f"Uploaded to s3://{result['bucket']}/{result['s3_key']}")
            st.info(
                "The Lambda processor will create pending approval requests "
                "shortly. Click rerun (or interact with the page) to refresh."
            )
            st.json(result)
        else:
            st.error(f"Upload failed: {result.get('error_message', 'Unknown error')}")

# Show a small list of recent S3 uploads so you can confirm the upload landed.
st.subheader("Recent S3 Uploads")
recent_uploads = upload_service.list_recent_uploads(limit=10)
if not recent_uploads:
    st.caption("No uploads found in S3 yet (or S3 access not configured).")
else:
    st.dataframe(recent_uploads, use_container_width=True)

# ---- Section 4: Recent History -------------------------------------------------

st.header("Recent Requests (all statuses)")

try:
    history = dynamodb_service.list_all_requests(limit=25)
except Exception as exc:
    st.error(f"Could not load history: {exc}")
    history = []

if history:
    rows = []
    for r in history:
        rows.append(
            {
                "request_id": r.get("request_id", "")[:8] + "...",
                "status": r.get("status", ""),
                "source": r.get("source", ""),
                "summary": r.get("review_summary", ""),
                "created_at": r.get("created_at", ""),
                "updated_at": r.get("updated_at", ""),
            }
        )
    st.dataframe(rows, use_container_width=True)

    with st.expander("Raw recent items"):
        st.code(json.dumps(history, indent=2, default=str), language="json")
else:
    st.caption("No requests yet.")
