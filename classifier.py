"""Classify a contact's job title into an outreach category and (optionally)
write a personalized opening sentence, using the Claude API.

Falls back to deterministic keyword matching when no API key is configured or
the API call fails, so the tool always works even without Claude.
"""

import json
import os
import re

from templates import DEFAULT_FOCUS

CATEGORIES = ("healthcare", "hr", "procurement", "executive", "other")

SYSTEM_PROMPT = """You classify a business contact into ONE outreach category for Radixsol, a healthcare staffing and workforce-solutions company, and write one short professional opening sentence for a cold email.

Categories (choose the single best fit):
- "healthcare": clinical / hospital / health-system buyers and leaders. Medical directors, CNO/CMO, VP of Nursing, physician or nurse recruiting, anyone who buys clinical or locum staffing.
- "hr": talent acquisition, recruiting, HR, people, or workforce leaders for non-clinical hiring (TA, HRBP, CHRO, People, L&D, talent management).
- "procurement": procurement, sourcing, purchasing, supply chain, vendor/supplier or category management, contracts, contingent workforce / MSP / VMS, and commercial buying roles. Treat titles containing Procurement, Sourcing, Purchase, Purchasing, Buyer, Commercial, Materials, Stores, Contracts, Category, Vendor, Supplier, MSP, or Contingent Workforce as procurement unless they are clearly HR.
- "executive": senior enterprise leaders whose remit is NOT specifically clinical, HR, or procurement - e.g. CEO, Managing Director, Country Head/Manager, President, General Manager, CTO/CIO, GCC or site/center head, business-unit or delivery head, Partner. These people can refer you to the right stakeholder.
- "other": anything that still does not fit (individual contributors, consultants, unclear or missing titles).

Return ONLY a JSON object, no prose, no markdown fences:
{
  "category": "healthcare" | "hr" | "procurement" | "executive" | "other",
  "focus_area": "<2-4 word phrase naming what they lead, e.g. 'Procurement', 'Talent Acquisition & HR', 'Supply Chain', 'Global Operations'>",
  "subject": "<a short, specific email subject line, max 8 words, that a busy executive would actually open. You may reference the company or their function. Plain sentence case - NO ALL-CAPS, NO exclamation marks, NO spam words (free, guarantee, urgent, act now, limited), and NO claims about certifications or results.>",
  "opener": "<One or two SHORT, clear sentences (about 30 words total) that connect their specific role at the company to a relevant staffing, workforce, or supplier angle and naturally invite a reply. Reference their role and company by name. Use plain punctuation - no em-dashes or run-on sentences. Write ONLY the sentence(s) - do NOT begin with a greeting or the recipient's name (the email template already greets them). Do NOT invent any facts, metrics, news, products, or initiatives about the company. Avoid cliches and flattery.>"
}"""

# Keyword buckets for the offline fallback. Order matters: most specific first,
# executive last so functional roles (hr/procurement/healthcare) win.
_KEYWORDS = (
    ("healthcare", DEFAULT_FOCUS["healthcare"],
     ("nurse", "nursing", "clinical", "physician", "medical", "health", "cno",
      "cmo", "hospital", "patient", "locum", "rn ", "care ", "clinic")),
    ("procurement", DEFAULT_FOCUS["procurement"],
     ("procure", "sourcing", "supply chain", "vendor", "supplier", "purchas",
      "buyer", "commercial", "materials", "stores", "contract", "category manage",
      "contingent", "msp")),
    ("hr", DEFAULT_FOCUS["hr"],
     ("talent", "recruit", "hr", "human resource", "people", "workforce",
      "staffing", "hiring", "acquisition", "hrbp", "chro")),
    ("executive", DEFAULT_FOCUS["executive"],
     ("managing director", "country head", "country manager", "country director",
      "chief executive", "ceo", "chief technology", "cto", "chief information",
      "cio", "chief operating", "coo", "chief strategy", "site leader", "site head",
      "site lead", "gcc", "general manager", "president", "head of engineering",
      "centre head", "center head", "delivery head", "global head", "md &")),
)


def make_anthropic_client():
    """Return an Anthropic client if ANTHROPIC_API_KEY is set, else None."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    return Anthropic(api_key=api_key)


def classify_keywords(title):
    """Deterministic fallback: (category, focus_area) from title keywords."""
    t = (title or "").lower()
    for category, focus, keys in _KEYWORDS:
        if any(k in t for k in keys):
            return category, focus
    return "other", DEFAULT_FOCUS["other"]


def _extract_json(text):
    """Pull the first JSON object out of a model response."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object in model response: {text!r}")
    return json.loads(match.group(0))


def _classify_ai(title, company, first_name, client, model):
    user = (
        f"Contact title: {title or '(unknown)'}\n"
        f"Company: {company or '(unknown)'}\n"
        f"First name: {first_name or '(unknown)'}"
    )
    msg = client.messages.create(
        model=model,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    data = _extract_json(text)

    category = str(data.get("category", "")).strip().lower()
    if category not in CATEGORIES:
        category, _ = classify_keywords(title)
    focus_area = str(data.get("focus_area") or "").strip() or DEFAULT_FOCUS[category]
    opener = str(data.get("opener") or "").strip() or None
    subject = str(data.get("subject") or "").strip() or None
    return {"category": category, "focus_area": focus_area, "opener": opener, "subject": subject}


FOLLOWUP_SYSTEM = """You write ONE short, polite follow-up sentence for a cold-outreach email sequence from Radixsol, a healthcare staffing and workforce-solutions company. This is follow-up number N to a prior, UNANSWERED email.

The sentence should acknowledge you're following up, and lightly connect to a staffing / workforce / supplier value point relevant to their role. Stay warm and never pushy; later follow-ups should sound a little more understanding of their busy schedule.

Reply with ONLY the sentence: no greeting, no name, no quotes, plain punctuation, no em-dashes, and no invented facts about the company."""


def followup_line(title, company, first_name, step, client, model):
    """One AI-written follow-up sentence, or None to fall back to a fixed line."""
    if client is None:
        return None
    try:
        user = (f"Follow-up #{step}. Contact: {first_name or '(unknown)'}, "
                f"{title or '(unknown role)'} at {company or '(their company)'}.")
        msg = client.messages.create(
            model=model, max_tokens=120, system=FOLLOWUP_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        return text.strip().strip('"').strip() or None
    except Exception as exc:
        print(f"  [warn] follow-up line failed ({exc}); using default")
        return None


def classify_contact(title, company, first_name, client=None, model=None, override=None):
    """Return {'category', 'focus_area', 'opener'} for a contact.

    override : optional manual category from the spreadsheet; when valid it
               short-circuits Claude entirely.
    client   : Anthropic client, or None to use keyword fallback.
    """
    if override:
        cat = str(override).strip().lower()
        if cat in CATEGORIES:
            return {"category": cat, "focus_area": DEFAULT_FOCUS[cat], "opener": None, "subject": None}

    if client is not None:
        try:
            return _classify_ai(title, company, first_name, client, model)
        except Exception as exc:  # network, parse, auth -> degrade gracefully
            print(f"  [warn] Claude classification failed ({exc}); using keyword fallback")

    category, focus_area = classify_keywords(title)
    return {"category": category, "focus_area": focus_area, "opener": None, "subject": None}
