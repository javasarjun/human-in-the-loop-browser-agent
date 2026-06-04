"""
lambda_handler.py

Lambda entry point for the SQS-driven CSV processor.

Pipeline:
  1. S3 receives a new CSV under uploads/*.csv.
  2. S3 sends an ObjectCreated event to the SQS upload queue.
  3. Lambda is invoked with an SQS event. Each SQS record's `body` is the
     JSON-encoded S3 event notification.
  4. For each S3 record, this handler downloads the CSV and turns each valid
     row into a pending DynamoDB approval request.

We never submit any browser forms here - that still happens locally via
submitter_service.py + browser_agent.py.
"""

import json
import logging
import urllib.parse

import boto3

from src.csv_processor import process_csv_bytes
from src.dynamodb_writer import write_approval_request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_s3 = boto3.client("s3")


def process_upload(event, context):
    """SQS-triggered Lambda handler."""
    records = event.get("Records", []) or []
    logger.info("Received %s SQS record(s)", len(records))

    total_requests_created = 0
    total_errors: list[str] = []

    for sqs_record in records:
        body = sqs_record.get("body", "")
        try:
            message = json.loads(body)
        except json.JSONDecodeError as exc:
            logger.error("Skipping SQS record with non-JSON body: %s", exc)
            total_errors.append(f"Non-JSON SQS body: {exc}")
            continue

        # An S3 event notification looks like:
        #   { "Records": [ { "s3": { "bucket": {...}, "object": {...} } } ] }
        s3_records = message.get("Records", []) or []
        if not s3_records:
            logger.info("SQS message has no S3 records, skipping: %s", message)
            continue

        for s3_event in s3_records:
            try:
                bucket = s3_event["s3"]["bucket"]["name"]
                raw_key = s3_event["s3"]["object"]["key"]
            except KeyError as exc:
                logger.error("Malformed S3 event, missing %s", exc)
                total_errors.append(f"Malformed S3 event: missing {exc}")
                continue

            # S3 event keys are URL-encoded. unquote_plus also handles '+' as space.
            key = urllib.parse.unquote_plus(raw_key)

            # Defensive filter - the bucket notification config already filters
            # by suffix .csv, but we double-check so a stray object can't trip us.
            if not key.lower().endswith(".csv"):
                logger.info("Ignoring non-CSV object: s3://%s/%s", bucket, key)
                continue

            requests_created, file_errors = _process_single_object(bucket, key)
            total_requests_created += requests_created
            total_errors.extend(file_errors)

    logger.info(
        "Done. Created %s approval requests; %s errors.",
        total_requests_created,
        len(total_errors),
    )
    return {
        "statusCode": 200,
        "requests_created": total_requests_created,
        "errors": total_errors,
    }


def _process_single_object(bucket: str, key: str) -> tuple[int, list[str]]:
    """Download one S3 CSV, parse it, and write one approval request per valid row."""
    logger.info("Processing s3://%s/%s", bucket, key)

    try:
        response = _s3.get_object(Bucket=bucket, Key=key)
        content = response["Body"].read()
    except Exception as exc:
        msg = f"Could not download s3://{bucket}/{key}: {exc}"
        logger.error(msg)
        return 0, [msg]

    valid_rows, parse_errors = process_csv_bytes(content)
    if parse_errors:
        logger.warning("Parse errors for %s: %s", key, parse_errors)

    created = 0
    write_errors: list[str] = []
    for row in valid_rows:
        try:
            write_approval_request(
                source_file=key,
                row_number=row["row_number"],
                payload=row["payload"],
            )
            created += 1
        except Exception as exc:
            msg = f"DynamoDB write failed for row {row['row_number']} of {key}: {exc}"
            logger.error(msg)
            write_errors.append(msg)

    return created, parse_errors + write_errors
