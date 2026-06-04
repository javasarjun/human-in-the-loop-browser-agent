"""
dynamodb_service.py

All DynamoDB reads and writes for the approval workflow live here.

Table: AgentApprovalRequests
Partition key: request_id (string)

We use a simple status field to track each request's lifecycle:
  pending -> approved -> submitted   (happy path)
  pending -> rejected                (user said no)
  pending -> approved -> failed      (submission tried, verification failed)
"""

import os
import uuid
from datetime import datetime, timezone

import boto3
from dotenv import load_dotenv

load_dotenv()

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
TABLE_NAME = os.getenv("DYNAMODB_TABLE_NAME", "AgentApprovalRequests")
UPLOADS_TABLE_NAME = os.getenv("DYNAMODB_UPLOADS_TABLE_NAME", "AgentUploads")

# boto3 resource gives us a simpler dict-style API than the low-level client.
_dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
_table = _dynamodb.Table(TABLE_NAME)
_uploads_table = _dynamodb.Table(UPLOADS_TABLE_NAME)


def _now() -> str:
    """Return current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def create_request(
    task_type: str,
    original_request: str,
    payload: dict,
    review_summary: str,
    extra_attributes: dict | None = None,
) -> dict:
    """
    Insert a new approval request with status='pending' and return the full item.

    `extra_attributes` lets callers attach optional top-level fields like
    upload_id / batch_id / row_number for requests created from uploaded files.
    Reserved attribute names (request_id, status, created_at, ...) cannot be
    overwritten - they are always set here.
    """
    item = {
        "request_id": str(uuid.uuid4()),
        "status": "pending",
        "task_type": task_type,
        "original_request": original_request,
        "payload": payload,
        "review_summary": review_summary,
        "created_at": _now(),
        "updated_at": _now(),
    }
    if extra_attributes:
        for key, value in extra_attributes.items():
            if key not in item:
                item[key] = value
    _table.put_item(Item=item)
    return item


def get_request(request_id: str) -> dict | None:
    """Fetch one request by id, or None if it doesn't exist."""
    response = _table.get_item(Key={"request_id": request_id})
    return response.get("Item")


def list_pending_requests() -> list:
    """
    Return all requests whose status is 'pending'.
    Uses scan with a filter - fine for a demo, would use a GSI in production.
    """
    return _list_by_status("pending")


def list_approved_requests() -> list:
    """Return all requests whose status is 'approved'."""
    return _list_by_status("approved")


def _list_by_status(status_value: str) -> list:
    """Scan and filter the approval requests table by a single status value."""
    response = _table.scan(
        FilterExpression="#s = :v",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":v": status_value},
    )
    items = response.get("Items", [])
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return items


def list_all_requests(limit: int = 50) -> list:
    """Return up to `limit` requests across all statuses, newest first."""
    response = _table.scan()
    items = response.get("Items", [])
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return items[:limit]


def approve_request(request_id: str) -> None:
    """Move a request from pending -> approved."""
    _table.update_item(
        Key={"request_id": request_id},
        UpdateExpression="SET #s = :approved, approved_at = :now, updated_at = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":approved": "approved", ":now": _now()},
    )


def reject_request(request_id: str) -> None:
    """Move a request from pending -> rejected."""
    _table.update_item(
        Key={"request_id": request_id},
        UpdateExpression="SET #s = :rejected, updated_at = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":rejected": "rejected", ":now": _now()},
    )


def mark_submitted(request_id: str, submission_result: dict) -> None:
    """Move a request to status='submitted' and store the verification result."""
    _table.update_item(
        Key={"request_id": request_id},
        UpdateExpression=(
            "SET #s = :submitted, submitted_at = :now, updated_at = :now, "
            "submission_result = :result"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":submitted": "submitted",
            ":now": _now(),
            ":result": submission_result,
        },
    )


def mark_failed(request_id: str, error_message: str, submission_result: dict | None = None) -> None:
    """Move a request to status='failed' and store the error message."""
    update_expression = "SET #s = :failed, updated_at = :now, error_message = :err"
    values = {
        ":failed": "failed",
        ":now": _now(),
        ":err": error_message,
    }
    if submission_result is not None:
        update_expression += ", submission_result = :result"
        values[":result"] = submission_result

    _table.update_item(
        Key={"request_id": request_id},
        UpdateExpression=update_expression,
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=values,
    )


def ensure_table_exists() -> None:
    """
    Create the DynamoDB table if it doesn't exist yet.
    Safe to call at startup; does nothing if the table is already there.
    """
    client = boto3.client("dynamodb", region_name=AWS_REGION)
    existing = client.list_tables().get("TableNames", [])
    if TABLE_NAME in existing:
        return

    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    # Wait until the new table is ready to accept reads/writes.
    client.get_waiter("table_exists").wait(TableName=TABLE_NAME)


# ---- Uploads table ------------------------------------------------------------
#
# A separate DynamoDB table for tracking uploaded files. The actual file bytes
# stay on local disk under uploads/; this table only stores metadata so the UI
# can list past uploads and the next lecture can drive row processing from it.
#
# Schema (string-only, no GSIs):
#   upload_id            string  (partition key)
#   original_file_name   string
#   stored_file_path     string  (path on local disk)
#   file_type            string  (csv or xlsx)
#   upload_status        string  (uploaded / processing / processed / failed)
#   uploaded_at          string  (ISO 8601)
#   error_message        string

def ensure_uploads_table_exists() -> None:
    """Create the AgentUploads table if it doesn't exist yet."""
    client = boto3.client("dynamodb", region_name=AWS_REGION)
    existing = client.list_tables().get("TableNames", [])
    if UPLOADS_TABLE_NAME in existing:
        return

    client.create_table(
        TableName=UPLOADS_TABLE_NAME,
        KeySchema=[{"AttributeName": "upload_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "upload_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=UPLOADS_TABLE_NAME)


def create_upload(record: dict) -> dict:
    """Insert a new upload metadata record and return it."""
    _uploads_table.put_item(Item=record)
    return record


def get_upload(upload_id: str) -> dict | None:
    """Fetch one upload record by id, or None if missing."""
    response = _uploads_table.get_item(Key={"upload_id": upload_id})
    return response.get("Item")


def list_uploads(limit: int = 50) -> list:
    """Return up to `limit` upload records, newest first."""
    response = _uploads_table.scan()
    items = response.get("Items", [])
    items.sort(key=lambda x: x.get("uploaded_at", ""), reverse=True)
    return items[:limit]


def update_upload_status(upload_id: str, status: str, error_message: str = "") -> None:
    """Update an upload's status (and optionally its error_message)."""
    _uploads_table.update_item(
        Key={"upload_id": upload_id},
        UpdateExpression="SET upload_status = :s, error_message = :e",
        ExpressionAttributeValues={":s": status, ":e": error_message},
    )
