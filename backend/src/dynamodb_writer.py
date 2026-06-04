"""
dynamodb_writer.py

Writes approval request items into the DynamoDB approval requests table.

The table name is read from the APPROVAL_TABLE_NAME environment variable that
the Serverless deployment injects into the Lambda runtime.
"""

import logging
import os

import boto3

from .utils import generate_request_id, now_iso

logger = logging.getLogger(__name__)

_TABLE_NAME = os.environ.get("APPROVAL_TABLE_NAME", "")
_dynamodb = boto3.resource("dynamodb")


def write_approval_request(source_file: str, row_number: int, payload: dict) -> dict:
    """
    Insert one pending approval request and return the item.

    Fields written:
      request_id     unique uuid4 (partition key)
      status         "pending" (always starts here)
      task_type      "demo_form_submission"
      source         "s3_upload"
      source_file    S3 key the CSV came from, e.g. uploads/20260525-...csv
      row_number     1-based row index within the CSV
      payload        {"name": ..., "email": ..., "message": ...}
      review_summary short human-readable summary for the approval card
      created_at     ISO 8601 timestamp
      updated_at     ISO 8601 timestamp
      error_message  empty string at creation time
    """
    if not _TABLE_NAME:
        raise RuntimeError("APPROVAL_TABLE_NAME environment variable is not set.")

    table = _dynamodb.Table(_TABLE_NAME)

    item = {
        "request_id": generate_request_id(),
        "status": "pending",
        "task_type": "demo_form_submission",
        "source": "s3_upload",
        "source_file": source_file,
        "row_number": row_number,
        "payload": payload,
        "review_summary": _build_summary(payload),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "error_message": "",
    }

    table.put_item(Item=item)
    logger.info("Wrote approval request %s for %s row %s", item["request_id"], source_file, row_number)
    return item


def _build_summary(payload: dict) -> str:
    """Short one-liner for the approval dashboard."""
    name = payload.get("custname", "?")
    size = payload.get("size", "?")
    toppings = payload.get("topping") or []
    topping_str = ", ".join(toppings) if toppings else "no toppings"
    delivery = payload.get("delivery", "?")
    return f"{size} pizza ({topping_str}) for {name} at {delivery}"
