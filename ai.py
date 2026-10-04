"""AI features (summary, chat, form auto-fill) using Claude through the Anthropic SDK.

The API key comes from the ANTHROPIC_API_KEY environment variable, or from the
key saved in the app's settings (config.json next to this file). Only these AI
features send the document to the internet; everything else stays local.
"""

import base64
import json
from datetime import date
from pathlib import Path

import anthropic

MODEL = "claude-opus-5-5"
CONFIG = Path(__file__).parent / "config.json"
MAX_PDF_BYTES = 30 * 1024 * 1024   # API request limit is 32 MB
MAX_PDF_PAGES = 600
MAX_TEXT_CHARS = 3_000_000         # roughly what fits in the 1M-token context


class AIError(Exception):
    pass


# ------------------------------------------------------------ settings

def _config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def saved_key():
    return _config().get("anthropic_api_key", "")


def save_key(key):
    cfg = _config()
    if key:
        cfg["anthropic_api_key"] = key.strip()
    else:
        cfg.pop("anthropic_api_key", None)
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def status():
    import os
    if saved_key():
        return {"configured": True, "source": "settings"}
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return {"configured": True, "source": "environment"}
    return {"configured": False, "source": None}


def _client():
    key = saved_key()
    try:
        return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    except Exception as e:
        raise AIError("No Anthropic API key. Open AI → Settings and paste your key "
                      "(or set ANTHROPIC_API_KEY).") from e


# ------------------------------------------------------------ requests

def _document_block(pdf_bytes, doc):
    """The PDF itself when it fits the API limits, else its extracted text."""
    if len(pdf_bytes) <= MAX_PDF_BYTES and doc.page_count <= MAX_PDF_PAGES:
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf",
                       "data": base64.standard_b64encode(pdf_bytes).decode("ascii")},
            "cache_control": {"type": "ephemeral"},
        }
    text = "\n\n".join(f"--- Page {i + 1} ---\n{p.get_text('text')}" for i, p in enumerate(doc))
    if len(text) > MAX_TEXT_CHARS:
        raise AIError(f"This document is too large for the AI features ({doc.page_count} pages, "
                      f"{len(text):,} characters of text). Extract the pages you need first.")
    return {"type": "text", "text": f"<document>\n{text}\n</document>",
            "cache_control": {"type": "ephemeral"}}


def _call(system, messages, max_tokens=16000, output_format=None):
    client = _client()
    kwargs = dict(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
        output_config={"effort": "medium"},
        # If a safety classifier declines, let the API retry on a fallback model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if output_format:
        kwargs["output_config"] = {"effort": "medium", "format": output_format}
    try:
        resp = client.beta.messages.create(**kwargs)
    except anthropic.AuthenticationError as e:
        raise AIError("The Anthropic API key was rejected. Check it in AI → Settings.") from e
    except anthropic.PermissionDeniedError as e:
        raise AIError(f"The API key lacks permission: {e.message}") from e
    except anthropic.RateLimitError as e:
        raise AIError("Rate limited by the Anthropic API. Wait a moment and try again.") from e
    except anthropic.BadRequestError as e:
        raise AIError(f"The AI request was rejected: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise AIError(f"Anthropic API error ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise AIError("Could not reach the Anthropic API. Check the internet connection.") from e
    except TypeError as e:
        raise AIError(f"The AI request could not be built: {e}") from e
    if resp.stop_reason == "refusal":
        raise AIError("Claude declined to answer this request.")
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if resp.stop_reason == "max_tokens":
        text += "\n\n(The answer was cut off because it reached the length limit.)"
    return text


SYSTEM = ("You help people understand a PDF document they have open in a PDF editor. "
          "Answer from the document. When the document does not contain the answer, say so "
          "instead of guessing. Mention page numbers when they help the reader find things. "
          "Reply in the language of the user's question (or of the document, if no question).")


def summarize(pdf_bytes, doc, length="medium"):
    target = {"short": "in 3-5 bullet points",
              "medium": "in a short overview paragraph followed by the key points as bullets",
              "long": "section by section, with the key facts, figures, dates and obligations"}.get(length)
    if target is None:
        target = "in a short overview paragraph followed by the key points as bullets"
    content = [_document_block(pdf_bytes, doc), {"type": "text", "text": f"Summarize this document {target}."}]
    return _call(SYSTEM, [{"role": "user", "content": content}])


def chat(pdf_bytes, doc, history):
    """`history` is [{"role": "user"|"assistant", "content": str}, ...] ending with a user turn."""
    msgs = [{"role": m["role"], "content": str(m["content"])} for m in history
            if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()]
    if not msgs or msgs[-1]["role"] != "user":
        raise AIError("Ask a question first.")
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    # The document goes in the first user turn so it stays cached across the chat.
    msgs[0] = {"role": "user", "content": [_document_block(pdf_bytes, doc),
                                           {"type": "text", "text": msgs[0]["content"]}]}
    return _call(SYSTEM, msgs)


AUTOFILL_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "xref": {"type": "integer"},
                        "value": {"type": "string"},
                    },
                    "required": ["xref", "value"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["fields"],
        "additionalProperties": False,
    },
}


def autofill(pdf_bytes, doc, fields, info):
    """Suggest values for form fields from free-text `info`. Returns {xref: value}."""
    if not fields:
        raise AIError("This PDF has no fillable form fields.")
    listing = json.dumps(fields, ensure_ascii=False, indent=1)
    prompt = (
        "Fill in this PDF form using the information below. Each field in the list has an xref, "
        "a name, a type, the page it is on, and nearby label text.\n\n"
        f"<information>\n{info.strip() or '(none given)'}\n</information>\n\n"
        f"<fields>\n{listing}\n</fields>\n\n"
        f"Today's date is {date.today().isoformat()}. "
        "Return only the fields you can fill from the information (or that are today's date, "
        "if a date field asks for the signing date). For check boxes and radio buttons use "
        "\"true\" or \"false\". For choice fields use one of the listed choices exactly. "
        "Leave out fields you cannot fill; never invent personal data."
    )
    content = [_document_block(pdf_bytes, doc), {"type": "text", "text": prompt}]
    text = _call(SYSTEM, [{"role": "user", "content": content}], output_format=AUTOFILL_SCHEMA)
    try:
        data = json.loads(text)
    except ValueError as e:
        raise AIError("The AI returned an unreadable answer. Try again.") from e
    valid = {f["xref"] for f in fields}
    return {int(f["xref"]): f["value"] for f in data.get("fields", []) if f.get("xref") in valid}
