"""
app.py

The Streamlit control center for the human-in-the-loop browser agent.

Flow:
  1. User types a natural language pizza order.
  2. We call the LLM to extract structured form fields.
  3. We save those fields as a 'pending' approval request in DynamoDB.
  4. The pending list shows each request with Approve / Reject buttons.
  5. On Approve, we call the submitter, which drives Playwright and verifies the result.
  6. The UI re-reads DynamoDB and shows the new status.

Run with:
  streamlit run app.py
"""

import json

import streamlit as st

import dynamodb_service
import llm_service
import processing_service
import submitter_service
import upload_service

# ---- Page setup ----------------------------------------------------------------

st.set_page_config(page_title="Human-in-the-Loop Browser Agent", layout="wide")
st.title("Human-in-the-Loop Browser Agent")
st.caption(
    "Natural language -> LLM-parsed pizza order -> your approval -> Playwright submits the form."
)

# Make sure both DynamoDB tables exist. Safe to call on every run.
try:
    dynamodb_service.ensure_table_exists()
    dynamodb_service.ensure_uploads_table_exists()
except Exception as exc:
    st.warning(f"Could not verify DynamoDB tables: {exc}")

DEFAULT_REQUEST = (
    "Can you order a small vegetarian pizza with onion, mushroom for Arjun Vaid around 8:30 PM? "
    "Use email javasarjun@gmail.com and phone 4694268163. "
    "Please add a note asking for extra cheese, but don't submit until I approve."
)

# ---- Section 1: Create Approval Request ----------------------------------------

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
                    st.success(
                        f"Saved as pending approval request: {item['request_id']}"
                    )
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

        with st.expander("Original natural language request"):
            st.write(req.get("original_request", ""))

        with st.expander("Parsed payload"):
            st.json(req.get("payload", {}))

        col_approve, col_reject = st.columns(2)

        # Approve button: mark approved, then run the submitter inline.
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

        # Reject button: just mark rejected and refresh.
        if col_reject.button("Reject", key=f"reject-{request_id}"):
            try:
                dynamodb_service.reject_request(request_id)
                st.success("Request rejected.")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not reject: {exc}")

# ---- Section 3: Upload CSV / Excel File ---------------------------------------

st.header("Upload CSV / Excel File")
st.caption(
    "Upload a .csv or .xlsx file. For now we just store it locally and show a preview. "
    "Row processing and approval requests will be added in the next lecture."
)

uploaded_file = st.file_uploader(
    "Choose a file to upload",
    type=["csv", "xlsx"],
    accept_multiple_files=False,
    key="file_uploader",
)

if uploaded_file is not None:
    # Only save when the user clicks the button - otherwise Streamlit would
    # re-save the same file on every rerun (e.g. each time another button is clicked).
    if st.button("Save uploaded file", key="save_upload_btn"):
        record = upload_service.save_uploaded_file(uploaded_file)
        if record["upload_status"] == "uploaded":
            st.success(
                "File uploaded successfully. "
                "Processing will be added in the next lecture."
            )
            st.json(record)
        else:
            st.error(f"Upload failed: {record.get('error_message', 'Unknown error')}")

# Show the full list of previously uploaded files.
st.subheader("Uploaded Files")

upload_records = upload_service.load_upload_metadata()
if not upload_records:
    st.caption("No files uploaded yet.")
else:
    # Newest first.
    upload_records_sorted = sorted(
        upload_records, key=lambda r: r.get("uploaded_at", ""), reverse=True
    )

    # Compact table view of the columns the lecture asked for.
    table_rows = [
        {
            "upload_id": r.get("upload_id", "")[:8] + "...",
            "file_name": r.get("original_file_name", ""),
            "status": r.get("upload_status", ""),
            "uploaded_at": r.get("uploaded_at", ""),
        }
        for r in upload_records_sorted
    ]
    st.dataframe(table_rows, use_container_width=True)

    # Let the user pick one upload to preview.
    successful_uploads = [r for r in upload_records_sorted if r.get("upload_status") == "uploaded"]
    if successful_uploads:
        options = {
            f"{r['original_file_name']}  ({r['upload_id'][:8]})": r for r in successful_uploads
        }
        choice = st.selectbox(
            "Preview an uploaded file (first 5 rows)",
            options=list(options.keys()),
            key="preview_select",
        )
        if choice:
            selected = options[choice]
            try:
                preview_df = upload_service.preview_uploaded_file(
                    selected["stored_file_path"],
                    selected["file_type"],
                    n=5,
                )
                st.dataframe(preview_df, use_container_width=True)
            except Exception as exc:
                st.error(f"Could not preview file: {exc}")

# ---- Section 4: Process Uploaded Files ----------------------------------------

st.header("Process Uploaded Files")
st.caption(
    "For each uploaded file, validate its rows and create one pending approval "
    "request per valid row. Required columns: "
    f"{', '.join(processing_service.REQUIRED_COLUMNS)}. "
    "Optional columns: topping, comments."
)

# Re-read upload metadata so this section reflects the latest statuses.
uploads_for_processing = upload_service.load_upload_metadata()

if not uploads_for_processing:
    st.caption("No uploaded files yet. Upload one in the section above.")

for record in uploads_for_processing:
    upload_id = record["upload_id"]
    status = record.get("upload_status", "")
    file_name = record.get("original_file_name", "")

    with st.container(border=True):
        cols = st.columns([3, 1, 1, 2])
        cols[0].markdown(f"**{file_name}**  \n`{upload_id}`")
        cols[1].markdown(f"**Status:** `{status}`")
        cols[2].markdown(f"**Type:** {record.get('file_type', '')}")
        cols[3].markdown(f"**Uploaded:** {record.get('uploaded_at', '')}")

        if status == "uploaded":
            if st.button("Process File", key=f"process-{upload_id}"):
                with st.spinner(f"Processing {file_name}..."):
                    result = processing_service.process_upload(record)

                if result.get("already_processed"):
                    st.warning(
                        f"This file is already in status '{result['final_status']}'. "
                        "Skipping to avoid duplicate processing."
                    )
                elif result["final_status"] == "failed":
                    st.error(
                        f"File processing failed: {result.get('file_error', 'unknown error')}"
                    )
                else:
                    st.success(
                        f"Processed {file_name}. "
                        f"batch_id = {result['batch_id']}"
                    )

                # Always show the summary so the user can see what happened.
                summary_cols = st.columns(3)
                summary_cols[0].metric("Total rows", result["total_rows"])
                summary_cols[1].metric("Requests created", result["requests_created"])
                summary_cols[2].metric("Rows skipped", result["rows_skipped"])

                if result["errors"]:
                    st.warning(f"{len(result['errors'])} row(s) had errors:")
                    st.dataframe(result["errors"], use_container_width=True)

                st.rerun()

        elif status == "processed":
            st.info("Already processed. Re-processing is disabled.")
        elif status == "processing":
            st.info("Currently processing (or interrupted mid-run).")
        elif status == "failed":
            st.error(f"Previous attempt failed: {record.get('error_message', '')}")

# ---- Section 5: Recent History -------------------------------------------------

st.header("Recent Requests (all statuses)")

try:
    history = dynamodb_service.list_all_requests(limit=25)
except Exception as exc:
    st.error(f"Could not load history: {exc}")
    history = []

# Build a small table-friendly view.
if history:
    rows = []
    for r in history:
        rows.append(
            {
                "request_id": r.get("request_id", "")[:8] + "...",
                "status": r.get("status", ""),
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
