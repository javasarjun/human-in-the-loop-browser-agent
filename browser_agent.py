"""
browser_agent.py

Uses Playwright to fill and submit the httpbin pizza form, then verifies
that the submitted values actually showed up on the response page.

The form lives at https://httpbin.org/forms/post and submits to
https://httpbin.org/post, which echoes the posted form data back as JSON
inside the HTML page. We parse that page text to verify success.
"""

from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

FORM_URL = "https://httpbin.org/forms/post"
EXPECTED_RESPONSE_URL = "https://httpbin.org/post"


def submit_form(payload: dict, headless: bool = True) -> dict:
    """
    Open the demo form, fill it from `payload`, submit, and verify.

    Returns a dict shaped like:
      {
        "success": bool,
        "final_url": str,
        "submitted_form_data": dict,   # the values we tried to submit
        "verification_message": str,
        "submitted_at": iso timestamp,
        "page_text_excerpt": str,      # first 1000 chars of response page (for debugging)
      }
    """
    submitted_at = datetime.now(timezone.utc).isoformat()
    final_url = ""
    page_text = ""
    success = False
    verification_message = ""

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()

            # Step 1: open the form.
            page.goto(FORM_URL, wait_until="domcontentloaded")

            # Step 2: fill in the text/email/tel/time/textarea inputs.
            # The httpbin form uses name="custname", "custtel", "custemail",
            # "delivery", "comments" - we target those by name attribute.
            page.fill('input[name="custname"]', payload.get("custname", ""))
            page.fill('input[name="custtel"]', payload.get("custtel", ""))
            page.fill('input[name="custemail"]', payload.get("custemail", ""))
            page.fill('input[name="delivery"]', payload.get("delivery", ""))
            page.fill('textarea[name="comments"]', payload.get("comments", ""))

            # Step 3: pick the pizza size (radio button).
            size = (payload.get("size") or "").lower().strip()
            if size in {"small", "medium", "large"}:
                page.check(f'input[name="size"][value="{size}"]')

            # Step 4: tick each topping checkbox.
            for topping in payload.get("topping", []) or []:
                topping = str(topping).lower().strip()
                if not topping:
                    continue
                # The form uses name="topping" with values: bacon, cheese, onion, mushroom.
                selector = f'input[name="topping"][value="{topping}"]'
                if page.locator(selector).count() > 0:
                    page.check(selector)

            # Step 5: submit. The submit control is a <button> at the bottom of the form.
            # We click it and wait for the navigation to /post to settle.
            with page.expect_navigation(wait_until="domcontentloaded"):
                page.click('button[type="submit"], button:has-text("Submit order")')

            # Step 6: capture what we landed on.
            final_url = page.url
            page_text = page.content()

            browser.close()

        # Step 7: verify. The /post endpoint echoes the form values back into the page.
        success, verification_message = _verify_submission(payload, final_url, page_text)

    except Exception as exc:
        verification_message = f"Browser run raised an exception: {exc}"
        success = False

    return {
        "success": success,
        "final_url": final_url,
        "submitted_form_data": payload,
        "verification_message": verification_message,
        "submitted_at": submitted_at,
        # Keep an excerpt so the UI can show "what came back" without flooding the screen.
        "page_text_excerpt": page_text[:1000] if page_text else "",
    }


def _verify_submission(payload: dict, final_url: str, page_text: str) -> tuple[bool, str]:
    """
    Check that the response page (from httpbin.org/post) actually contains
    the values we tried to submit. Returns (success, message).
    """
    if not final_url.startswith(EXPECTED_RESPONSE_URL):
        return False, f"Did not land on {EXPECTED_RESPONSE_URL} after submit (got {final_url!r})."

    if not page_text:
        return False, "Response page was empty."

    # Fields we expect to find in the echoed response.
    checks = {
        "custname": payload.get("custname", ""),
        "custemail": payload.get("custemail", ""),
        "custtel": payload.get("custtel", ""),
        "size": payload.get("size", ""),
        "delivery": payload.get("delivery", ""),
        "comments": payload.get("comments", ""),
    }

    missing = []
    for field, value in checks.items():
        if not value:
            # Skip fields the user never filled in.
            continue
        if value not in page_text:
            missing.append(field)

    # Toppings are an array - just check that each one appears somewhere on the page.
    for topping in payload.get("topping", []) or []:
        if topping and topping not in page_text:
            missing.append(f"topping:{topping}")

    if missing:
        return False, f"Could not find these submitted values in the response: {', '.join(missing)}"

    return True, "All submitted fields were found in the httpbin response."
