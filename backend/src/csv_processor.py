"""
csv_processor.py

Parses CSV bytes downloaded from S3 and returns the rows that are valid
enough to turn into approval requests.

Schema (matches the local pizza-order flow so the browser agent can submit
either path):
  Required columns: custname, custemail, custtel, size, delivery
  Optional columns: topping, comments
  Valid sizes:      small, medium, large
"""

import csv
import io
import logging

REQUIRED_COLUMNS = ["custname", "custemail", "custtel", "size", "delivery"]
OPTIONAL_COLUMNS = ["topping", "comments"]
VALID_SIZES = {"small", "medium", "large"}

logger = logging.getLogger(__name__)


def process_csv_bytes(content: bytes) -> tuple[list[dict], list[str]]:
    """
    Parse CSV bytes and return (valid_rows, errors).

    valid_rows looks like:
      {
        "row_number": int,                 # 1-based data row
        "payload": {
          "custname": str, "custemail": str, "custtel": str,
          "size": str, "delivery": str,
          "topping": list[str], "comments": str,
        },
      }
    """
    errors: list[str] = []
    valid_rows: list[dict] = []

    # utf-8-sig transparently strips a UTF-8 BOM if Excel/Numbers added one;
    # otherwise it behaves identically to utf-8.
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    # Header field names can pick up stray whitespace; normalize so comparisons work.
    fieldnames = [(name or "").strip() for name in (reader.fieldnames or [])]

    missing_columns = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
    if missing_columns:
        msg = f"Missing required columns: {', '.join(missing_columns)}"
        logger.warning(msg)
        errors.append(msg)
        return valid_rows, errors

    for index, row in enumerate(reader, start=1):
        row_errors = _validate_row(row)
        if row_errors:
            msg = f"Row {index} skipped: {'; '.join(row_errors)}"
            logger.info(msg)
            errors.append(msg)
            continue

        valid_rows.append(
            {
                "row_number": index,
                "payload": _row_to_payload(row),
            }
        )

    return valid_rows, errors


def _validate_row(row: dict) -> list[str]:
    """Return a list of validation problems for a single row (empty list = valid)."""
    problems: list[str] = []
    for column in REQUIRED_COLUMNS:
        value = (row.get(column) or "").strip()
        if not value:
            problems.append(f"missing {column}")

    size = (row.get("size") or "").strip().lower()
    if size and size not in VALID_SIZES:
        problems.append(f"invalid size {size!r} (expected small/medium/large)")
    return problems


def _row_to_payload(row: dict) -> dict:
    """Turn a validated row into the form payload the browser agent expects."""
    return {
        "custname": row["custname"].strip(),
        "custemail": row["custemail"].strip(),
        "custtel": row["custtel"].strip(),
        "size": row["size"].strip().lower(),
        "delivery": row["delivery"].strip(),
        "topping": _split_toppings(row.get("topping")),
        "comments": (row.get("comments") or "").strip(),
    }


def _split_toppings(value: str | None) -> list[str]:
    """Split a comma-separated toppings cell into a clean lowercased list."""
    if not value:
        return []
    return [piece.strip().lower() for piece in value.split(",") if piece.strip()]
