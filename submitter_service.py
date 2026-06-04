"""
submitter_service.py

Orchestrates the "submit one approved request" workflow:
  1. Read the request from DynamoDB.
  2. Make sure its status is 'approved' (never submit anything else).
  3. Call browser_agent.submit_form() to fill and submit the pizza form.
  4. Depending on the verification result, update DynamoDB to 'submitted' or 'failed'.

Two ways to use this module:
  - As a library: Streamlit calls submit_approved_request(...) inline when the
    user clicks Approve.
  - As a CLI:    `python submitter_service.py` scans DynamoDB for all approved
    requests and submits each one in turn. Useful if you let multiple requests
    pile up at status='approved' (e.g. approved on a remote dashboard) and
    want to drain them in a single local run.
"""

import argparse
import json
import sys

import browser_agent
import dynamodb_service


def submit_approved_request(request_id: str, headless: bool = True) -> dict:
    """
    Submit one approved request and update DynamoDB based on the verification.

    Returns the submission_result dict from browser_agent so the UI can show it.
    """
    request = dynamodb_service.get_request(request_id)
    if request is None:
        return {
            "success": False,
            "verification_message": f"Request {request_id} not found in DynamoDB.",
        }

    # Safety guard: never submit something that hasn't been explicitly approved.
    if request.get("status") != "approved":
        return {
            "success": False,
            "verification_message": (
                f"Refusing to submit: status is {request.get('status')!r}, expected 'approved'."
            ),
        }

    payload = request.get("payload", {})

    # Run the browser and verify.
    result = browser_agent.submit_form(payload, headless=headless)

    # Persist the outcome.
    if result.get("success"):
        dynamodb_service.mark_submitted(request_id, result)
    else:
        dynamodb_service.mark_failed(
            request_id,
            error_message=result.get("verification_message", "Unknown submission error"),
            submission_result=result,
        )

    return result


def submit_all_approved(headless: bool = True) -> list[dict]:
    """
    Walk every 'approved' request currently in DynamoDB and try to submit each.
    Returns a list of result dicts in the same order they were processed.
    """
    approved = dynamodb_service.list_approved_requests()
    print(f"Found {len(approved)} approved request(s) to submit.")

    results: list[dict] = []
    for request in approved:
        request_id = request["request_id"]
        print(f"  -> submitting {request_id} ({request.get('review_summary', '')})")
        result = submit_approved_request(request_id, headless=headless)
        status = "OK" if result.get("success") else "FAILED"
        print(f"     {status}: {result.get('verification_message', '')}")
        results.append({"request_id": request_id, **result})
    return results


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Submit approved DynamoDB requests via the local browser agent."
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium headless. Default is headed so you can watch.",
    )
    parser.add_argument(
        "--request-id",
        help="Optional: submit only this single request_id instead of every approved one.",
    )
    args = parser.parse_args()

    if args.request_id:
        result = submit_approved_request(args.request_id, headless=args.headless)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("success") else 1

    results = submit_all_approved(headless=args.headless)
    failures = [r for r in results if not r.get("success")]
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
