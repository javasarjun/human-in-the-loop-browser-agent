"""
utils.py

Shared helpers for the Lambda processor.
"""

import uuid
from datetime import datetime, timezone


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def generate_request_id() -> str:
    """Return a fresh UUID4 string to use as a DynamoDB partition key."""
    return str(uuid.uuid4())
