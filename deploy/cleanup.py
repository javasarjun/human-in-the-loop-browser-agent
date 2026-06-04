"""
cleanup.py

One-shot teardown for everything this project deploys.

Deletes, in safe order:
  1. The Lightsail container service (Streamlit UI)
  2. The runtime IAM user (drops access keys + inline policy first)
  3. All objects in the S3 upload bucket
  4. The Serverless Framework backend stack (S3 + SQS + Lambda + DynamoDB)
     -> shells out to `serverless remove` because that's the supported path
  5. (Optional) The OpenAI Secrets Manager entry, if you ever created one

Idempotent: each step catches "doesn't exist" errors so it's safe to re-run.

Usage:
  python deploy/cleanup.py                  # interactive confirmation
  python deploy/cleanup.py --yes            # skip confirmation (for scripts)
  python deploy/cleanup.py --dry-run        # show what would be deleted

Env vars (with defaults):
  AWS_REGION          us-east-1
  APP_SERVICE_NAME    human-in-loop-browser-agent
  APP_IAM_USER        human-in-loop-browser-agent-runtime
  APP_UPLOAD_BUCKET   human-in-loop-browser-agent-backend-dev-<account>-uploads
"""

import argparse
import os
import subprocess
import sys

import boto3
from botocore.exceptions import ClientError


# ---- Config ------------------------------------------------------------------

REGION = os.getenv("AWS_REGION", "us-east-1")
SERVICE_NAME = os.getenv("APP_SERVICE_NAME", "human-in-loop-browser-agent")
IAM_USER = os.getenv("APP_IAM_USER", "human-in-loop-browser-agent-runtime")
IAM_POLICY_NAME = "HumanInLoopRuntimePolicy"
BACKEND_STACK = "human-in-loop-browser-agent-backend-dev"
OPENAI_SECRET_ID = "human-in-loop-browser-agent/openai-api-key"


def _bucket_name(account_id: str) -> str:
    """The name Serverless gives the upload bucket - we need it before tearing down."""
    explicit = os.getenv("APP_UPLOAD_BUCKET")
    if explicit:
        return explicit
    return f"human-in-loop-browser-agent-backend-dev-{account_id}-uploads"


# ---- Per-step helpers --------------------------------------------------------

def step_header(num: int, total: int, title: str) -> None:
    print(f"\n[{num}/{total}] {title}")
    print("-" * (len(title) + len(f"[{num}/{total}] ")))


def is_missing(err: ClientError, codes: set[str]) -> bool:
    """True if the boto3 error matches one of the 'resource not found' codes."""
    return err.response.get("Error", {}).get("Code", "") in codes


def delete_lightsail_service(dry_run: bool) -> None:
    client = boto3.client("lightsail", region_name=REGION)
    try:
        client.get_container_services(serviceName=SERVICE_NAME)
    except ClientError as exc:
        if is_missing(exc, {"NotFoundException", "DoesNotExist"}):
            print(f"  Lightsail service {SERVICE_NAME!r} already gone.")
            return
        raise

    if dry_run:
        print(f"  [dry-run] would delete Lightsail container service {SERVICE_NAME!r}")
        return

    client.delete_container_service(serviceName=SERVICE_NAME)
    print(f"  Deleted Lightsail container service {SERVICE_NAME!r}.")


def delete_iam_user(dry_run: bool) -> None:
    iam = boto3.client("iam")

    # Does the user exist?
    try:
        iam.get_user(UserName=IAM_USER)
    except ClientError as exc:
        if is_missing(exc, {"NoSuchEntity"}):
            print(f"  IAM user {IAM_USER!r} already gone.")
            return
        raise

    if dry_run:
        print(f"  [dry-run] would delete IAM user {IAM_USER!r} (keys + policy + user)")
        return

    # 1. Drop every access key.
    keys = iam.list_access_keys(UserName=IAM_USER).get("AccessKeyMetadata", [])
    for key in keys:
        kid = key["AccessKeyId"]
        iam.delete_access_key(UserName=IAM_USER, AccessKeyId=kid)
        print(f"  Deleted access key {kid}.")

    # 2. Drop the inline policy (ignore if it's not there - someone might have already cleaned it).
    try:
        iam.delete_user_policy(UserName=IAM_USER, PolicyName=IAM_POLICY_NAME)
        print(f"  Deleted inline policy {IAM_POLICY_NAME!r}.")
    except ClientError as exc:
        if not is_missing(exc, {"NoSuchEntity"}):
            raise

    # 3. Drop the user.
    iam.delete_user(UserName=IAM_USER)
    print(f"  Deleted IAM user {IAM_USER!r}.")


def empty_upload_bucket(account_id: str, dry_run: bool) -> None:
    bucket = _bucket_name(account_id)
    s3 = boto3.resource("s3", region_name=REGION)

    try:
        s3.meta.client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchBucket"}:
            print(f"  S3 bucket {bucket!r} already gone (or never existed).")
            return
        # 403 etc. - re-raise so the user sees it.
        raise

    obj_count = sum(1 for _ in s3.Bucket(bucket).objects.all())
    if obj_count == 0:
        print(f"  S3 bucket {bucket!r} is already empty.")
        return

    if dry_run:
        print(f"  [dry-run] would delete {obj_count} object(s) from {bucket!r}")
        return

    s3.Bucket(bucket).objects.all().delete()
    print(f"  Emptied {obj_count} object(s) from {bucket!r}.")


def remove_serverless_backend(dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] would run `serverless remove` in backend/ to delete stack {BACKEND_STACK!r}")
        return

    backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
    if not os.path.isdir(backend_dir):
        print(f"  No backend/ directory found at {backend_dir}; skipping.")
        return

    print(f"  Running `serverless remove` in {os.path.abspath(backend_dir)} (may take a few minutes)...")
    result = subprocess.run(
        ["serverless", "remove"],
        cwd=backend_dir,
        check=False,
    )
    if result.returncode != 0:
        print("  serverless remove exited non-zero. Continuing anyway.")
    else:
        print("  Backend stack removed.")


def delete_openai_secret(dry_run: bool) -> None:
    sm = boto3.client("secretsmanager", region_name=REGION)
    try:
        sm.describe_secret(SecretId=OPENAI_SECRET_ID)
    except ClientError as exc:
        if is_missing(exc, {"ResourceNotFoundException"}):
            print(f"  Secrets Manager entry {OPENAI_SECRET_ID!r} already gone.")
            return
        raise

    if dry_run:
        print(f"  [dry-run] would force-delete Secrets Manager entry {OPENAI_SECRET_ID!r}")
        return

    sm.delete_secret(SecretId=OPENAI_SECRET_ID, ForceDeleteWithoutRecovery=True)
    print(f"  Deleted Secrets Manager entry {OPENAI_SECRET_ID!r}.")


# ---- Verification ------------------------------------------------------------

def verify(account_id: str) -> None:
    print("\nVerification:")
    lightsail = boto3.client("lightsail", region_name=REGION)
    services = lightsail.get_container_services().get("containerServices", [])
    print(f"  Lightsail services remaining: {[s['containerServiceName'] for s in services]}")

    iam = boto3.client("iam")
    try:
        iam.get_user(UserName=IAM_USER)
        print(f"  IAM user {IAM_USER!r}: STILL PRESENT (cleanup may have skipped)")
    except ClientError as exc:
        if is_missing(exc, {"NoSuchEntity"}):
            print(f"  IAM user {IAM_USER!r}: gone")
        else:
            print(f"  IAM user {IAM_USER!r}: check failed - {exc}")

    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.head_bucket(Bucket=_bucket_name(account_id))
        print(f"  S3 bucket: STILL PRESENT (serverless remove may not have finished)")
    except ClientError:
        print("  S3 bucket: gone")

    cfn = boto3.client("cloudformation", region_name=REGION)
    try:
        cfn.describe_stacks(StackName=BACKEND_STACK)
        print(f"  CloudFormation stack {BACKEND_STACK!r}: STILL PRESENT")
    except ClientError:
        print(f"  CloudFormation stack {BACKEND_STACK!r}: gone")


# ---- Main --------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Tear down all AWS infra for this project.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted, no changes.")
    parser.add_argument(
        "--skip-backend",
        action="store_true",
        help="Don't run `serverless remove` (use if you want to keep the backend stack).",
    )
    parser.add_argument(
        "--skip-secret",
        action="store_true",
        help="Don't try to delete the OpenAI Secrets Manager entry.",
    )
    args = parser.parse_args()

    account_id = boto3.client("sts").get_caller_identity()["Account"]

    print("About to tear down the following resources:")
    print(f"  AWS account:        {account_id}")
    print(f"  Region:             {REGION}")
    print(f"  Lightsail service:  {SERVICE_NAME}")
    print(f"  IAM user:           {IAM_USER}")
    print(f"  Upload bucket:      {_bucket_name(account_id)} (will be emptied before serverless removes it)")
    print(f"  Backend stack:      {BACKEND_STACK} ({'skipped' if args.skip_backend else 'serverless remove'})")
    print(f"  OpenAI secret:      {OPENAI_SECRET_ID} ({'skipped' if args.skip_secret else 'force-delete if present'})")
    print(f"  Mode:               {'DRY RUN' if args.dry_run else 'LIVE'}")

    if not args.yes and not args.dry_run:
        confirmation = input("\nProceed? Type 'delete' to continue: ").strip().lower()
        if confirmation != "delete":
            print("Aborted.")
            return 1

    total = 5 if (not args.skip_backend and not args.skip_secret) else 4

    step_header(1, total, "Delete Lightsail container service")
    delete_lightsail_service(args.dry_run)

    step_header(2, total, "Delete runtime IAM user")
    delete_iam_user(args.dry_run)

    step_header(3, total, "Empty S3 upload bucket")
    empty_upload_bucket(account_id, args.dry_run)

    step = 4
    if not args.skip_backend:
        step_header(step, total, "Remove Serverless backend stack")
        remove_serverless_backend(args.dry_run)
        step += 1

    if not args.skip_secret:
        step_header(step, total, "Delete OpenAI Secrets Manager entry (optional)")
        delete_openai_secret(args.dry_run)

    if not args.dry_run:
        verify(account_id)

    print("\nDone. Reminder: rotate any keys that ever touched committed files (the OpenAI key in particular).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
