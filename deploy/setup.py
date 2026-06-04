"""
setup.py

One-shot recreate for everything this project deploys to AWS.

Runs, in order:
  1. `serverless deploy` for the backend (S3 + SQS + Lambda + DynamoDB).
     Reads the resulting bucket/table names from CloudFormation outputs.
  2. Creates the runtime IAM user with the policy in instance-role-policy.json.
     Generates an access key (or rotates if you pass --rotate-key).
  3. Creates the Lightsail container service (Micro tier) and waits for READY.
  4. Builds the Docker image and pushes it to Lightsail's registry.
  5. Writes deploy/lightsail-deployment.json with every env var filled in.
  6. Creates the Lightsail container deployment and prints the public URL.

Mostly idempotent: re-running picks up existing resources rather than
duplicating them. Safe to re-run after a partial failure.

Usage:
  python deploy/setup.py

The script will prompt (hidden input) for:
  - OPENAI_API_KEY        unless already set in env or .env
  - STREAMLIT_APP_PASSWORD generate one if you press enter

Flags:
  --skip-backend     don't run `serverless deploy` (use existing stack)
  --rotate-key       delete the IAM user's existing access keys and make a new one
  --skip-deploy      stop after building/pushing the image (no service deployment)
  --image-tag TAG    docker tag to build (default: latest)

Env vars (with defaults):
  AWS_REGION          us-east-1
  APP_SERVICE_NAME    human-in-loop-browser-agent
  APP_IAM_USER        human-in-loop-browser-agent-runtime
  APP_NAME            human-in-loop-browser-agent  (local docker tag)
"""

import argparse
import getpass
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import time

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv


# ---- Config ------------------------------------------------------------------

load_dotenv()

REGION = os.getenv("AWS_REGION", "us-east-1")
SERVICE_NAME = os.getenv("APP_SERVICE_NAME", "human-in-loop-browser-agent")
APP_NAME = os.getenv("APP_NAME", "human-in-loop-browser-agent")
IAM_USER = os.getenv("APP_IAM_USER", "human-in-loop-browser-agent-runtime")
IAM_POLICY_NAME = "HumanInLoopRuntimePolicy"
BACKEND_STACK = "human-in-loop-browser-agent-backend-dev"

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEPLOY_DIR = os.path.join(REPO_ROOT, "deploy")
POLICY_PATH = os.path.join(DEPLOY_DIR, "instance-role-policy.json")
DEPLOYMENT_JSON = os.path.join(DEPLOY_DIR, "lightsail-deployment.json")


def step_header(num: int, total: int, title: str) -> None:
    print(f"\n[{num}/{total}] {title}")
    print("-" * (len(title) + len(f"[{num}/{total}] ")))


def is_missing(err: ClientError, codes: set[str]) -> bool:
    return err.response.get("Error", {}).get("Code", "") in codes


def require_binary(name: str) -> None:
    """Hard-stop early if a required external tool isn't on PATH."""
    if shutil.which(name) is None:
        sys.exit(
            f"ERROR: `{name}` is not on your PATH. Install it before running setup.py "
            f"(see deploy/README.md for the prerequisites)."
        )


# ---- Step 1: deploy backend, read stack outputs ------------------------------

def deploy_backend(skip: bool) -> dict:
    """Run `serverless deploy` and then return the relevant CloudFormation outputs."""
    if not skip:
        require_binary("serverless")
        backend_dir = os.path.join(REPO_ROOT, "backend")
        print(f"  Running `serverless deploy` in {backend_dir} (this can take 2-5 minutes)...")
        result = subprocess.run(["serverless", "deploy"], cwd=backend_dir, check=False)
        if result.returncode != 0:
            sys.exit("ERROR: `serverless deploy` failed. Fix the issue and re-run.")
        print("  Backend stack deployed.")
    else:
        print("  --skip-backend was set; reading existing stack outputs instead.")

    cfn = boto3.client("cloudformation", region_name=REGION)
    try:
        stacks = cfn.describe_stacks(StackName=BACKEND_STACK)["Stacks"]
    except ClientError as exc:
        sys.exit(f"ERROR: could not describe stack {BACKEND_STACK!r}: {exc}")
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}

    needed = {"ApprovalRequestsTableName", "UploadBucketName"}
    missing = needed - set(outputs)
    if missing:
        sys.exit(f"ERROR: stack outputs missing: {missing}. Inspect with `serverless info`.")

    print(f"  DynamoDB table : {outputs['ApprovalRequestsTableName']}")
    print(f"  Upload bucket  : {outputs['UploadBucketName']}")
    return outputs


# ---- Step 2: IAM user + access key -------------------------------------------

def ensure_iam_user(rotate_key: bool) -> tuple[str, str]:
    """
    Make sure the runtime IAM user exists with the right policy, return a
    fresh (AccessKeyId, SecretAccessKey) pair for the container env vars.
    """
    iam = boto3.client("iam")

    # 1. Create or reuse the user.
    try:
        iam.create_user(UserName=IAM_USER)
        print(f"  Created IAM user {IAM_USER!r}.")
    except ClientError as exc:
        if is_missing(exc, {"EntityAlreadyExists"}):
            print(f"  IAM user {IAM_USER!r} already exists.")
        else:
            raise

    # 2. (Re)attach the inline policy. put_user_policy is idempotent.
    with open(POLICY_PATH, "r", encoding="utf-8") as f:
        policy_doc = f.read()
    iam.put_user_policy(
        UserName=IAM_USER,
        PolicyName=IAM_POLICY_NAME,
        PolicyDocument=policy_doc,
    )
    print(f"  Attached inline policy {IAM_POLICY_NAME!r}.")

    # 3. Access keys. Existing keys can't be read back, so we either rotate
    #    or skip with a clear message.
    keys = iam.list_access_keys(UserName=IAM_USER).get("AccessKeyMetadata", [])
    if keys and not rotate_key:
        sys.exit(
            f"\nERROR: IAM user {IAM_USER!r} already has {len(keys)} access key(s).\n"
            f"  Existing secrets cannot be read back. Either:\n"
            f"    1. paste the previous secret into deploy/lightsail-deployment.json yourself, OR\n"
            f"    2. re-run with --rotate-key to delete the old keys and mint a new one."
        )

    for key in keys:
        iam.delete_access_key(UserName=IAM_USER, AccessKeyId=key["AccessKeyId"])
        print(f"  Deleted old access key {key['AccessKeyId']}.")

    response = iam.create_access_key(UserName=IAM_USER)["AccessKey"]
    print(f"  Generated new access key {response['AccessKeyId']}.")
    return response["AccessKeyId"], response["SecretAccessKey"]


# ---- Step 3: Lightsail container service -------------------------------------

def ensure_container_service() -> None:
    """Create the Lightsail container service if it doesn't exist, then wait for READY."""
    client = boto3.client("lightsail", region_name=REGION)

    try:
        services = client.get_container_services(serviceName=SERVICE_NAME).get("containerServices", [])
        state = services[0]["state"] if services else None
    except ClientError as exc:
        if is_missing(exc, {"NotFoundException", "DoesNotExist"}):
            state = None
        else:
            raise

    if state is None:
        print(f"  Creating Lightsail container service {SERVICE_NAME!r} (Micro)...")
        client.create_container_service(
            serviceName=SERVICE_NAME,
            power="micro",
            scale=1,
        )
    else:
        print(f"  Lightsail container service {SERVICE_NAME!r} already exists (state={state}).")

    # Wait for READY before we can push an image.
    print("  Waiting for service to be READY...", end="", flush=True)
    deadline = time.time() + 600
    while time.time() < deadline:
        state = client.get_container_services(serviceName=SERVICE_NAME)["containerServices"][0]["state"]
        if state == "READY":
            print(" READY.")
            return
        if state in {"FAILED", "DELETING", "DISABLED"}:
            sys.exit(f"\nERROR: service is in state {state!r}, can't proceed.")
        print(".", end="", flush=True)
        time.sleep(15)
    sys.exit("\nERROR: timed out waiting for Lightsail service to reach READY.")


# ---- Step 4: build + push image ----------------------------------------------

def build_and_push_image(image_tag: str) -> str:
    """Build the local image and push it to Lightsail. Returns the Lightsail image ref."""
    require_binary("docker")
    require_binary("aws")
    require_binary("lightsailctl")

    local_image = f"{APP_NAME}:{image_tag}"
    print(f"  Building {local_image} for linux/amd64 (Lightsail runs amd64)...")
    result = subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "-t", local_image, "."],
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        sys.exit("ERROR: docker build failed.")

    print(f"  Pushing {local_image} to Lightsail registry...")
    push = subprocess.run(
        [
            "aws", "lightsail", "push-container-image",
            "--service-name", SERVICE_NAME,
            "--label", "app",
            "--image", local_image,
            "--region", REGION,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    sys.stdout.write(push.stdout)
    sys.stderr.write(push.stderr)
    if push.returncode != 0:
        sys.exit("ERROR: lightsail push-container-image failed.")

    # The push command prints the image ref on stderr in current AWS CLI versions.
    image_ref = _extract_image_ref(push.stderr + push.stdout)
    if not image_ref:
        sys.exit(
            "ERROR: could not parse the image reference from push output. "
            "Look for a string like ':human-in-loop-browser-agent.app.<N>' in the output above."
        )
    print(f"  Pushed image reference: {image_ref}")
    return image_ref


def _extract_image_ref(output: str) -> str | None:
    """Pull the ':service.label.N' ref out of `aws lightsail push-container-image` output."""
    needle = f":{SERVICE_NAME}.app."
    for token in output.split():
        token = token.strip().strip(".").strip(",").strip(")")
        if token.startswith(needle):
            return token
    return None


# ---- Step 5: deployment spec -------------------------------------------------

def secret_value(env_var: str, prompt: str, generate_if_empty: bool = False) -> str:
    """Pull a secret from env or interactively prompt for it (hidden input)."""
    value = os.environ.get(env_var, "").strip()
    if value:
        return value
    entered = getpass.getpass(prompt).strip()
    if entered:
        return entered
    if generate_if_empty:
        alphabet = string.ascii_letters + string.digits
        generated = "".join(secrets.choice(alphabet) for _ in range(20))
        print(f"  (generated {env_var}: {generated})")
        return generated
    sys.exit(f"ERROR: {env_var} is required.")


def write_deployment_json(
    image_ref: str,
    table_name: str,
    bucket_name: str,
    access_key_id: str,
    secret_access_key: str,
    openai_key: str,
    streamlit_password: str,
) -> None:
    spec = {
        "containers": {
            "app": {
                "image": image_ref,
                "ports": {"8501": "HTTP"},
                "environment": {
                    "AWS_REGION": REGION,
                    "DYNAMODB_TABLE_NAME": table_name,
                    "UPLOAD_BUCKET_NAME": bucket_name,
                    "STREAMLIT_APP_PASSWORD": streamlit_password,
                    "OPENAI_API_KEY": openai_key,
                    "AWS_ACCESS_KEY_ID": access_key_id,
                    "AWS_SECRET_ACCESS_KEY": secret_access_key,
                },
            }
        },
        "publicEndpoint": {
            "containerName": "app",
            "containerPort": 8501,
            "healthCheck": {
                "path": "/_stcore/health",
                "intervalSeconds": 30,
                "timeoutSeconds": 5,
                "healthyThreshold": 2,
                "unhealthyThreshold": 5,
                "successCodes": "200",
            },
        },
    }
    with open(DEPLOYMENT_JSON, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2)
    # Tight perms - this file holds secrets.
    os.chmod(DEPLOYMENT_JSON, 0o600)
    print(f"  Wrote {DEPLOYMENT_JSON} (mode 600). Do NOT commit this file.")


# ---- Step 6: deploy ----------------------------------------------------------

def create_deployment() -> None:
    print("  Submitting deployment to Lightsail...")
    result = subprocess.run(
        [
            "aws", "lightsail", "create-container-service-deployment",
            "--service-name", SERVICE_NAME,
            "--cli-input-json", f"file://{DEPLOYMENT_JSON}",
            "--region", REGION,
        ],
        check=False,
    )
    if result.returncode != 0:
        sys.exit("ERROR: create-container-service-deployment failed.")

    client = boto3.client("lightsail", region_name=REGION)
    print("  Waiting for deployment to be ACTIVE...", end="", flush=True)
    deadline = time.time() + 600
    while time.time() < deadline:
        svc = client.get_container_services(serviceName=SERVICE_NAME)["containerServices"][0]
        dep = svc.get("currentDeployment") or {}
        state = dep.get("state", "PENDING")
        if state == "ACTIVE":
            print(" ACTIVE.")
            print(f"\nApp URL: {svc.get('url', 'unknown')}")
            return
        if state == "FAILED":
            sys.exit("\nERROR: deployment FAILED. Inspect logs with `aws lightsail get-container-log`.")
        print(".", end="", flush=True)
        time.sleep(20)
    sys.exit("\nERROR: timed out waiting for deployment to be ACTIVE.")


# ---- Main --------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Recreate all AWS infra for this project.")
    parser.add_argument("--skip-backend", action="store_true",
                        help="Don't run `serverless deploy` (assumes the stack already exists).")
    parser.add_argument("--rotate-key", action="store_true",
                        help="Delete the IAM user's existing access keys and mint a new one.")
    parser.add_argument("--skip-deploy", action="store_true",
                        help="Stop after pushing the image (no service deployment).")
    parser.add_argument("--image-tag", default="latest",
                        help="Local Docker tag for the image (default: latest).")
    args = parser.parse_args()

    print("This will create / update the following resources:")
    print(f"  Region:             {REGION}")
    print(f"  Backend stack:      {BACKEND_STACK} ({'skipped' if args.skip_backend else 'serverless deploy'})")
    print(f"  IAM user:           {IAM_USER}")
    print(f"  Lightsail service:  {SERVICE_NAME} (Micro, ~$10/mo if left running)")
    print(f"  Image tag:          {APP_NAME}:{args.image_tag}")
    print()

    total = 6 if not args.skip_deploy else 4

    step_header(1, total, "Deploy serverless backend")
    outputs = deploy_backend(skip=args.skip_backend)

    step_header(2, total, "Create runtime IAM user + access key")
    access_key_id, secret_access_key = ensure_iam_user(rotate_key=args.rotate_key)

    step_header(3, total, "Create Lightsail container service")
    ensure_container_service()

    step_header(4, total, "Build and push the image")
    image_ref = build_and_push_image(args.image_tag)

    if args.skip_deploy:
        print("\n--skip-deploy was set; stopping after image push.")
        return 0

    step_header(5, total, "Write deployment spec")
    openai_key = secret_value("OPENAI_API_KEY", "OpenAI API key (input hidden): ")
    streamlit_password = secret_value(
        "STREAMLIT_APP_PASSWORD",
        "Pick a Streamlit gate password (press Enter to auto-generate): ",
        generate_if_empty=True,
    )
    write_deployment_json(
        image_ref=image_ref,
        table_name=outputs["ApprovalRequestsTableName"],
        bucket_name=outputs["UploadBucketName"],
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        openai_key=openai_key,
        streamlit_password=streamlit_password,
    )

    step_header(6, total, "Create Lightsail deployment")
    create_deployment()

    print("\nDone. Open the URL printed above, sign in with STREAMLIT_APP_PASSWORD, "
          "and run both flows end-to-end to verify.")
    print("To redeploy a code change later: rerun `python deploy/setup.py --skip-backend`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
