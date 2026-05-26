"""
processing_service.py

Reads an uploaded CSV / Excel file row by row and creates one DynamoDB
approval request per valid row.

This is local-only processing for now. Cloud processing (S3 + SQS + Lambda)
comes in a later lecture.

CSV/Excel column expectations
-----------------------------
Required columns (every row must have a non-empty value):
  custname    - customer's name
  custemail   - customer's email
  custtel     - customer's phone (digits only is best)
  size        - small | medium | large
  delivery    - 24h time like "20:30"

Optional columns:
  topping     - comma-separated, e.g. "onion, mushroom"
  comments    - free-form notes
"""

import uuid

import pandas as pd

import dynamodb_service

# What we require to be present in the file header.
REQUIRED_COLUMNS = ["custname", "custemail", "custtel", "size", "delivery"]

# Valid pizza sizes from the httpbin form.
VALID_SIZES = {"small", "medium", "large"}


def process_upload(upload_record: dict) -> dict:
    """
    Process one uploaded file. Returns a summary dict shaped like:
      {
        "already_processed": bool,
        "batch_id": str,
        "total_rows": int,
        "requests_created": int,
        "rows_skipped": int,
        "errors": [ {row_number, error}, ... ],
        "final_status": "processed" | "failed",
        "file_error": str,                 # set if file-level validation failed
      }
    """
    upload_id = upload_record["upload_id"]
    file_path = upload_record.get("stored_file_path", "")
    file_type = upload_record.get("file_type", "")
    original_name = upload_record.get("original_file_name", "")
    current_status = upload_record.get("upload_status", "")

    # Avoid duplicate work. Caller (Streamlit) should also short-circuit this,
    # but we re-check here so the function is safe to call directly.
    if current_status in {"processed", "processing"}:
        return {
            "already_processed": True,
            "batch_id": "",
            "total_rows": 0,
            "requests_created": 0,
            "rows_skipped": 0,
            "errors": [],
            "final_status": current_status,
            "file_error": "",
        }

    # Mark as in-progress so the UI reflects it immediately.
    dynamodb_service.update_upload_status(upload_id, "processing")

    # Step 1: read the file.
    try:
        df = _read_file(file_path, file_type)
    except Exception as exc:
        msg = f"Could not read file: {exc}"
        dynamodb_service.update_upload_status(upload_id, "failed", error_message=msg)
        return _failure_summary(msg)

    # Step 2: validate required columns are present.
    missing_columns = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_columns:
        msg = f"Missing required columns: {', '.join(missing_columns)}"
        dynamodb_service.update_upload_status(upload_id, "failed", error_message=msg)
        return _failure_summary(msg)

    # Step 3: per-row validation + request creation.
    batch_id = str(uuid.uuid4())
    total_rows = len(df)
    requests_created = 0
    rows_skipped = 0
    errors: list[dict] = []

    for idx, row in df.iterrows():
        # pandas index is 0-based; humans count from 1.
        row_number = int(idx) + 1

        payload, row_error = _row_to_payload(row)
        if row_error is not None:
            rows_skipped += 1
            errors.append({"row_number": row_number, "error": row_error})
            continue

        try:
            dynamodb_service.create_request(
                task_type="pizza_order",
                original_request=f"Row {row_number} from upload {original_name}",
                payload=payload,
                review_summary=_summary(payload),
                extra_attributes={
                    "upload_id": upload_id,
                    "batch_id": batch_id,
                    "row_number": row_number,
                },
            )
            requests_created += 1
        except Exception as exc:
            rows_skipped += 1
            errors.append({"row_number": row_number, "error": f"DynamoDB write failed: {exc}"})

    # Step 4: final upload status.
    if requests_created > 0:
        dynamodb_service.update_upload_status(upload_id, "processed")
        final_status = "processed"
        file_error = ""
    else:
        file_error = "No valid rows were found - nothing was created."
        dynamodb_service.update_upload_status(upload_id, "failed", error_message=file_error)
        final_status = "failed"

    return {
        "already_processed": False,
        "batch_id": batch_id,
        "total_rows": total_rows,
        "requests_created": requests_created,
        "rows_skipped": rows_skipped,
        "errors": errors,
        "final_status": final_status,
        "file_error": file_error,
    }


# ---- Internal helpers ---------------------------------------------------------

def _read_file(file_path: str, file_type: str) -> pd.DataFrame:
    """Read the file into a DataFrame based on its type."""
    if file_type == "csv":
        return pd.read_csv(file_path)
    if file_type == "xlsx":
        return pd.read_excel(file_path, engine="openpyxl")
    raise ValueError(f"Unsupported file_type: {file_type!r}")


def _row_to_payload(row: pd.Series) -> tuple[dict | None, str | None]:
    """
    Convert one DataFrame row into a request payload, or return (None, error).

    A row is invalid if any required field is empty/NaN, or if `size` is not
    one of small/medium/large.
    """
    # Required fields must be present and non-empty.
    for col in REQUIRED_COLUMNS:
        value = row.get(col)
        if pd.isna(value) or str(value).strip() == "":
            return None, f"Missing required field: {col}"

    size = str(row["size"]).strip().lower()
    if size not in VALID_SIZES:
        return None, f"Invalid size {size!r} (must be small/medium/large)"

    payload = {
        "custname": str(row["custname"]).strip(),
        "custemail": str(row["custemail"]).strip(),
        "custtel": str(row["custtel"]).strip(),
        "size": size,
        "delivery": str(row["delivery"]).strip(),
        "topping": _split_toppings(row.get("topping")),
        "comments": _optional_str(row.get("comments")),
    }
    return payload, None


def _split_toppings(value) -> list:
    """Turn a comma-separated topping string into a clean lowercased list."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    return [t.strip().lower() for t in str(value).split(",") if t.strip()]


def _optional_str(value) -> str:
    """Return a stripped string, or '' if the value is missing/NaN."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _summary(payload: dict) -> str:
    """Build a short human-readable summary, same shape as the LLM flow."""
    topping_str = ", ".join(payload["topping"]) if payload["topping"] else "no toppings"
    return (
        f"{payload['size']} pizza ({topping_str}) "
        f"for {payload['custname']} at {payload['delivery']}"
    )


def _failure_summary(message: str) -> dict:
    """Build a summary dict for a file-level failure (no rows processed)."""
    return {
        "already_processed": False,
        "batch_id": "",
        "total_rows": 0,
        "requests_created": 0,
        "rows_skipped": 0,
        "errors": [],
        "final_status": "failed",
        "file_error": message,
    }
