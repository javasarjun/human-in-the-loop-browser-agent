"""
upload_service.py

Local file upload helpers for the Streamlit app.

This lecture covers uploading and storing files. The actual file bytes are
written to the local `uploads/` directory (so a future lecture can read them
row-by-row), and the *metadata* about each upload lives in DynamoDB in a
dedicated `AgentUploads` table.

Row processing and creating approval requests from rows is intentionally NOT
done here - that's for a later lecture.
"""

import os
import uuid
from datetime import datetime, timezone

import pandas as pd

import dynamodb_service

# ---- Constants ---------------------------------------------------------------

UPLOADS_DIR = "uploads"

# Map of allowed file extensions -> a friendly file_type label.
ALLOWED_EXTENSIONS = {
    ".csv": "csv",
    ".xlsx": "xlsx",
}


# ---- Helpers -----------------------------------------------------------------

def _ensure_uploads_dir() -> None:
    """Make sure the uploads/ directory exists."""
    os.makedirs(UPLOADS_DIR, exist_ok=True)


def _now() -> str:
    """Return current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _extension_of(filename: str) -> str:
    """Return the lowercased extension of a filename, including the dot."""
    return os.path.splitext(filename)[1].lower()


def is_allowed_file(filename: str) -> bool:
    """Return True if `filename` has an allowed extension."""
    return _extension_of(filename) in ALLOWED_EXTENSIONS


# ---- Metadata (DynamoDB-backed) ----------------------------------------------

def load_upload_metadata() -> list:
    """
    Return all upload metadata records from DynamoDB, newest first.
    Returns an empty list if the table is empty or unreachable.
    """
    try:
        return dynamodb_service.list_uploads(limit=200)
    except Exception:
        # If DynamoDB is unreachable, don't crash the UI; show no uploads.
        return []


# ---- File save ---------------------------------------------------------------

def save_uploaded_file(uploaded_file) -> dict:
    """
    Save a Streamlit UploadedFile to disk and insert a metadata record into
    the AgentUploads DynamoDB table.

    `uploaded_file` is the object returned by st.file_uploader().

    Returns the new metadata record. If the file type is not allowed or the
    write fails, the record's `upload_status` will be 'failed' and no bytes
    are written to disk.
    """
    _ensure_uploads_dir()

    original_name = uploaded_file.name
    extension = _extension_of(original_name)
    upload_id = str(uuid.uuid4())

    # Basic file-type validation (extension only - we don't sniff content yet).
    if extension not in ALLOWED_EXTENSIONS:
        record = {
            "upload_id": upload_id,
            "original_file_name": original_name,
            "stored_file_path": "",
            "file_type": extension.lstrip(".") or "unknown",
            "upload_status": "failed",
            "uploaded_at": _now(),
            "error_message": f"Unsupported file extension: {extension!r}. Allowed: .csv, .xlsx",
        }
        _persist(record)
        return record

    # Store the file with a unique name to avoid collisions, but keep the
    # original name in metadata so the UI can show something friendly.
    stored_filename = f"{upload_id}{extension}"
    stored_path = os.path.join(UPLOADS_DIR, stored_filename)

    try:
        # Streamlit's UploadedFile is a BytesIO-like object. .getbuffer() is
        # the standard way to write its contents to disk.
        with open(stored_path, "wb") as f:
            f.write(uploaded_file.getbuffer())
    except Exception as exc:
        record = {
            "upload_id": upload_id,
            "original_file_name": original_name,
            "stored_file_path": "",
            "file_type": ALLOWED_EXTENSIONS[extension],
            "upload_status": "failed",
            "uploaded_at": _now(),
            "error_message": f"Could not write file to disk: {exc}",
        }
        _persist(record)
        return record

    record = {
        "upload_id": upload_id,
        "original_file_name": original_name,
        "stored_file_path": stored_path,
        "file_type": ALLOWED_EXTENSIONS[extension],
        "upload_status": "uploaded",
        "uploaded_at": _now(),
        "error_message": "",
    }
    _persist(record)
    return record


def _persist(record: dict) -> None:
    """Insert the metadata record into DynamoDB."""
    dynamodb_service.create_upload(record)


# ---- Preview -----------------------------------------------------------------

def preview_uploaded_file(stored_file_path: str, file_type: str, n: int = 5) -> pd.DataFrame:
    """
    Load the first `n` rows of an uploaded file as a pandas DataFrame.

    Supports .csv and .xlsx (which is what we allow on upload).
    Raises ValueError for unsupported types so the caller can show a friendly
    error in the UI.
    """
    if not stored_file_path or not os.path.exists(stored_file_path):
        raise FileNotFoundError(f"File not found on disk: {stored_file_path!r}")

    if file_type == "csv":
        return pd.read_csv(stored_file_path, nrows=n)
    if file_type == "xlsx":
        # openpyxl is the engine pandas uses for .xlsx files.
        return pd.read_excel(stored_file_path, engine="openpyxl", nrows=n)

    raise ValueError(f"Unsupported file_type for preview: {file_type!r}")
