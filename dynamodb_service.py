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

# boto3 resource gives us a simpler dict-style API than the low-level client.
_dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
_table = _dynamodb.Table(TABLE_NAME)


def _now() -> str:
    """Return current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def create_request(task_type: str, original_request: str, payload: dict, review_summary: str) -> dict:
    """
    Insert a new approval request with status='pending' and return the full item.
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
    response = _table.scan(
        FilterExpression="#s = :pending",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":pending": "pending"},
    )
    items = response.get("Items", [])
    # Sort newest first so the UI shows the most recent requests on top.
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
