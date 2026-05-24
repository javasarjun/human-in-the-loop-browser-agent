"""
main.py

Optional CLI tester. Lets you exercise each piece without launching Streamlit.

Examples:
  python main.py parse "small veggie pizza for Arjun at 8:30 PM, phone 4694268163, email a@b.com"
  python main.py list-pending
  python main.py submit <request_id>
  python main.py demo            # full end-to-end with the default request

This is purely for local testing. The real workflow runs through app.py.
"""

import argparse
import json
import sys

import browser_agent
import dynamodb_service
import llm_service
import submitter_service

DEFAULT_REQUEST = (
    "Can you order a small vegetarian pizza with onion, mushroom for Arjun Vaid around 8:30 PM? "
    "Use email javasarjun@gmail.com and phone 4694268163. "
    "Please add a note asking for extra cheese, but don't submit until I approve."
)


def cmd_parse(args):
    text = args.text or DEFAULT_REQUEST
    result = llm_service.parse_request(text)
    print(json.dumps(result, indent=2))


def cmd_list_pending(_args):
    items = dynamodb_service.list_pending_requests()
    print(json.dumps(items, indent=2, default=str))


def cmd_submit(args):
    # CLI submission runs non-headless so you can watch what's happening.
    result = submitter_service.submit_approved_request(args.request_id, headless=False)
    print(json.dumps(result, indent=2, default=str))


def cmd_demo(_args):
    """End-to-end demo: parse -> create pending -> approve -> submit."""
    dynamodb_service.ensure_table_exists()

    print("1) Parsing default request with LLM...")
    parsed = llm_service.parse_request(DEFAULT_REQUEST)
    print(json.dumps(parsed, indent=2))

    if parsed["missing_fields"]:
        print(f"Missing fields: {parsed['missing_fields']}; aborting demo.")
        sys.exit(1)

    print("\n2) Creating pending request in DynamoDB...")
    item = dynamodb_service.create_request(
        task_type="pizza_order",
        original_request=DEFAULT_REQUEST,
        payload=parsed["payload"],
        review_summary=parsed["review_summary"],
    )
    request_id = item["request_id"]
    print(f"   request_id = {request_id}")

    print("\n3) Approving (simulating user click)...")
    dynamodb_service.approve_request(request_id)

    print("\n4) Submitting via Playwright (headed so you can watch)...")
    result = submitter_service.submit_approved_request(request_id, headless=False)
    print(json.dumps(result, indent=2, default=str))


def cmd_browser_only(args):
    """Test only the browser agent against a hardcoded payload."""
    payload = {
        "custname": "Arjun Vaid",
        "custemail": "javasarjun@gmail.com",
        "custtel": "4694268163",
        "size": "small",
        "topping": ["onion", "mushroom"],
        "delivery": "20:30",
        "comments": "Extra cheese please.",
    }
    result = browser_agent.submit_form(payload, headless=args.headless)
    print(json.dumps(result, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CLI tester for the browser agent workflow.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_parse = subparsers.add_parser("parse", help="Parse a natural language request with the LLM.")
    p_parse.add_argument("text", nargs="?", help="Request text (defaults to the demo request).")
    p_parse.set_defaults(func=cmd_parse)

    p_list = subparsers.add_parser("list-pending", help="List pending requests from DynamoDB.")
    p_list.set_defaults(func=cmd_list_pending)

    p_submit = subparsers.add_parser("submit", help="Submit an approved request by id.")
    p_submit.add_argument("request_id")
    p_submit.set_defaults(func=cmd_submit)

    p_demo = subparsers.add_parser("demo", help="Run the full end-to-end demo flow.")
    p_demo.set_defaults(func=cmd_demo)

    p_browser = subparsers.add_parser("browser-only", help="Run only browser_agent with a fixed payload.")
    p_browser.add_argument("--headless", action="store_true", help="Run Chromium headless.")
    p_browser.set_defaults(func=cmd_browser_only)

    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    args.func(args)
