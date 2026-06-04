"""
upload_service.py

Uploads CSV files from the Streamlit UI to the S3 upload bucket.

After the upload, the deployed backend handles everything else:
  S3 ObjectCreated -> SQS -> Lambda -> DynamoDB approval requests.

This module deliberately does NOT:
  - send SQS messages (S3's bucket notification does that automatically)
  - write metadata to DynamoDB
  - keep a local copy of the file
"""

import os
import re
from datetime import datetime, timezone

import boto3
from dotenv import load_dotenv

load_dotenv()

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
UPLOAD_BUCKET_NAME = os.getenv("UPLOAD_BUCKET_NAME", "")

# The deployed backend only processes CSV; restrict the UI to match.
ALLOWED_EXTENSIONS = {".csv"}

_s3 = boto3.client("s3", region_name=AWS_REGION)


# ---- Helpers -----------------------------------------------------------------

def _extension_of(filename: str) -> str:
    """Return the lowercased extension of `filename`, including the dot."""
    return os.path.splitext(filename)[1].lower()


def is_allowed_file(filename: str) -> bool:
    """Return True if `filename` has an allowed extension."""
    return _extension_of(filename) in ALLOWED_EXTENSIONS


def _safe_basename(filename: str) -> str:
    """
    Strip directory parts and replace anything that's not alnum/dot/dash/_ with
    an underscore. Keeps S3 keys clean and avoids surprises with weird names.
    """
    base = os.path.basename(filename)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)


def _generate_s3_key(original_filename: str) -> str:
    """Build an S3 key like uploads/20260525-203045-original_name.csv."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"uploads/{timestamp}-{_safe_basename(original_filename)}"


# ---- Public API --------------------------------------------------------------

def save_uploaded_file(uploaded_file) -> dict:
    """
    Upload a Streamlit UploadedFile to S3.

    `uploaded_file` is the object returned by st.file_uploader().

    Returns a dict shaped like:
      {
        "success": bool,
        "original_file_name": str,
        "bucket": str,                # only if success
        "s3_key": str,                # only if success
        "uploaded_at": str,           # ISO 8601, only if success
        "error_message": str,         # only if not success
      }
    """
    original_name = uploaded_file.name
    extension = _extension_of(original_name)

    if extension not in ALLOWED_EXTENSIONS:
        return {
            "success": False,
            "original_file_name": original_name,
            "error_message": (
                f"Unsupported file extension: {extension!r}. Only .csv is allowed."
            ),
        }

    if not UPLOAD_BUCKET_NAME:
        return {
            "success": False,
            "original_file_name": original_name,
            "error_message": (
                "UPLOAD_BUCKET_NAME is not set. Deploy the backend (see backend/README.md) "
                "and copy the bucket name into the root .env file."
            ),
        }

    s3_key = _generate_s3_key(original_name)

    try:
        # Make sure we start from the beginning of the buffer; st.file_uploader
        # may have advanced the position if anything else read from it.
        uploaded_file.seek(0)
        _s3.upload_fileobj(
            Fileobj=uploaded_file,
            Bucket=UPLOAD_BUCKET_NAME,
            Key=s3_key,
            ExtraArgs={"ContentType": "text/csv"},
        )
    except Exception as exc:
        return {
            "success": False,
            "original_file_name": original_name,
            "error_message": f"S3 upload failed: {exc}",
        }

    return {
        "success": True,
        "original_file_name": original_name,
        "bucket": UPLOAD_BUCKET_NAME,
        "s3_key": s3_key,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }


def list_recent_uploads(limit: int = 25) -> list[dict]:
    """
    List the most recent objects under uploads/ in the S3 bucket, newest first.

    Returns an empty list if the bucket is unset or unreachable. This is best
    effort - we only use it for a nice "recent uploads" panel in the UI.
    """
    if not UPLOAD_BUCKET_NAME:
        return []

    try:
        response = _s3.list_objects_v2(Bucket=UPLOAD_BUCKET_NAME, Prefix="uploads/")
    except Exception:
        return []

    contents = response.get("Contents", []) or []
    contents.sort(key=lambda obj: obj.get("LastModified"), reverse=True)
    return [
        {
            "s3_key": obj["Key"],
            "size_bytes": obj.get("Size", 0),
            "last_modified": obj.get("LastModified").isoformat() if obj.get("LastModified") else "",
        }
        for obj in contents[:limit]
    ]
