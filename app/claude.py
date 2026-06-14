import os
import base64
import json
import logging
from anthropic import Anthropic

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"


def _client() -> Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set. Please configure it and restart.")
    return Anthropic(api_key=api_key)


def _parse_json(text: str) -> dict | list:
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


def identify_book(image_data: bytes, media_type: str = "image/jpeg") -> dict:
    client = _client()
    image_b64 = base64.standard_b64encode(image_data).decode("utf-8")

    response = client.messages.create(
        model=MODEL,
        max_tokens=256,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            "You are a book identification assistant. Examine this image of a book cover "
                            "and return JSON only, with fields: title (string), author (string), "
                            "confidence (float 0.0–1.0). Confidence should reflect how certain you are "
                            "this is a real, specific book you can identify. "
                            "Return only the JSON object, no other text."
                        ),
                    },
                ],
            }
        ],
    )

    try:
        return _parse_json(response.content[0].text)
    except (json.JSONDecodeError, IndexError, KeyError) as e:
        logger.error("Failed to parse Claude identify response: %s", e)
        return {"title": "", "author": "", "confidence": 0.0}


def get_recommendations(child_name: str, age: int, reading_history: list) -> list:
    client = _client()

    history_lines = "\n".join(
        f'- "{item["title"]}" by {item["author"]}'
        + (f' [series: {item["series"]}]' if item.get("series") else "")
        + f': {item["rating"]}'
        for item in reading_history
    )

    # Build series guidance so Claude can use judgment
    series_seen: dict[str, list[str]] = {}
    for item in reading_history:
        s = item.get("series")
        if s:
            series_seen.setdefault(s, []).append(item["title"])

    series_note = ""
    if series_seen:
        parts = [f'"{s}" ({len(books)} book{"s" if len(books) != 1 else ""} read)'
                 for s, books in series_seen.items()]
        series_note = (
            f"\n\nSeries context: {'; '.join(parts)}. "
            "Use judgment — if the child has read most/all of a series and loved it, "
            "recommend other books instead. If they've only read early books in a series "
            "they rate highly, the next book in that series is a great suggestion."
        )

    prompt = (
        f"You are a children's book recommendation assistant. "
        f"The child's name is {child_name} and they are {age} years old. "
        f"Here is their reading history with ratings:\n{history_lines}"
        f"{series_note}\n\n"
        f"Based on their preferences and age, suggest 8 books they haven't read yet. "
        f"Return JSON only: an array of objects each with fields: "
        f"title, author, reason (one sentence, why this suits this child based on their history). "
        f"No other text."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        result = _parse_json(response.content[0].text)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, IndexError) as e:
        logger.error("Failed to parse Claude recommendations response: %s", e)
        return []
