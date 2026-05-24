"""
submitter_service.py

Orchestrates the "submit one approved request" workflow:
  1. Read the request from DynamoDB.
  2. Make sure its status is 'approved' (never submit anything else).
  3. Call browser_agent.submit_form() to fill and submit the pizza form.
  4. Depending on the verification result, update DynamoDB to 'submitted' or 'failed'.

This is what Streamlit calls when the user clicks Approve.
"""

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
