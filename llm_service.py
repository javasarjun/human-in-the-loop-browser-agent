"""
llm_service.py

Uses OpenAI to parse a natural language pizza order request into
structured form fields that match the httpbin.org/forms/post demo form.

The httpbin pizza form has these fields:
  - custname     (text)
  - custtel      (text)
  - custemail    (email)
  - size         (radio: small, medium, large)
  - topping      (checkboxes: bacon, cheese, onion, mushroom)
  - delivery     (time, HH:MM 24-hour)
  - comments     (textarea)
"""

import json
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# A single shared client. The OpenAI SDK reads OPENAI_API_KEY from env automatically.
_client = OpenAI()

# The fields we want the LLM to fill in.
REQUIRED_FIELDS = ["custname", "custemail", "custtel", "size", "topping", "delivery", "comments"]

# System prompt that tells the model exactly what shape to return.
SYSTEM_PROMPT = """You extract pizza order details from natural language requests.

Return ONLY a JSON object with these keys:
  custname   string  - the customer's full name
  custemail  string  - the customer's email address
  custtel    string  - the customer's phone number (digits only, no formatting)
  size       string  - one of: "small", "medium", "large"
  topping    array of strings - any of: "bacon", "cheese", "onion", "mushroom"
  delivery   string  - 24-hour HH:MM time (e.g. "20:30" for 8:30 PM)
  comments   string  - any extra notes/instructions from the user

Rules:
- Convert times like "8:30 PM" to "20:30".
- "vegetarian" means do NOT include bacon. Default vegetarian toppings to ["cheese"] unless other veg toppings are mentioned.
- Map phrases like "extra cheese", "no onions", "leave at the door" into the comments field.
- If a field is not mentioned at all, set it to an empty string "" (or [] for topping).
- Do NOT invent personal data. Only use what is in the request.
- Output must be valid JSON. No markdown, no commentary."""


def parse_request(natural_language_request: str) -> dict:
    """
    Send the user's natural language request to OpenAI and return a dict
    with keys matching REQUIRED_FIELDS.

    Returns a dict shaped like:
      {
        "payload": { custname, custemail, custtel, size, topping, delivery, comments },
        "missing_fields": [list of field names that came back empty],
        "review_summary": "human readable one-liner",
      }
    """
    # Ask the model. response_format=json_object forces it to return valid JSON.
    response = _client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": natural_language_request},
        ],
    )

    raw = response.choices[0].message.content or "{}"
    parsed = json.loads(raw)

    # Normalize: make sure every required field exists with the right type.
    payload = {
        "custname": str(parsed.get("custname", "")).strip(),
        "custemail": str(parsed.get("custemail", "")).strip(),
        "custtel": str(parsed.get("custtel", "")).strip(),
        "size": str(parsed.get("size", "")).strip().lower(),
        "topping": _coerce_topping_list(parsed.get("topping", [])),
        "delivery": str(parsed.get("delivery", "")).strip(),
        "comments": str(parsed.get("comments", "")).strip(),
    }

    # Figure out which fields are missing.
    missing = []
    for field in REQUIRED_FIELDS:
        value = payload[field]
        # topping is a list - empty list counts as missing; comments is allowed to be empty.
        if field == "comments":
            continue
        if isinstance(value, list) and len(value) == 0:
            missing.append(field)
        elif isinstance(value, str) and value == "":
            missing.append(field)

    # Build a short human-readable summary for the approval card.
    topping_str = ", ".join(payload["topping"]) if payload["topping"] else "no toppings"
    review_summary = (
        f"{payload['size'] or '?'} pizza ({topping_str}) "
        f"for {payload['custname'] or '?'} at {payload['delivery'] or '?'}"
    )

    return {
        "payload": payload,
        "missing_fields": missing,
        "review_summary": review_summary,
    }


def _coerce_topping_list(value) -> list:
    """Accept either a list or a comma-separated string; return a clean list."""
    if isinstance(value, list):
        return [str(t).strip().lower() for t in value if str(t).strip()]
    if isinstance(value, str):
        return [t.strip().lower() for t in value.split(",") if t.strip()]
    return []
