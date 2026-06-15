"""Email templates and rendering for the Radixsol outreach tool.

Outreach styles, driven by the contact's classified category:
  - healthcare  -> dedicated clinical/locum staffing template
  - hr          -> generic workforce template, focus area = "Talent Acquisition & HR"
  - procurement -> generic workforce template, focus area = "Procurement"
  - executive   -> higher-altitude leadership template (referral-oriented)
  - other       -> generic workforce template, focus area = whatever Claude identified

The bodies below are kept faithful to the wording provided. Personalization
happens only through: the first name, the {focus_area} phrase, and an optional
{opener} sentence written by Claude (the generic and executive templates).
"""

import re

# --- Healthcare -------------------------------------------------------------
HEALTHCARE_BODY = """Hello {first_name},
I hope you are doing well.!

Radixsol is a Joint Commission-certified MBE/WBE healthcare staffing and workforce solutions partner, supporting healthcare organizations with scalable, high-quality clinical staffing solutions across both physician and non-physician categories.

Our capabilities include:
- Physician & Locum Tenens Staffing
Primary Care Physicians, Surgeons, Specialty Physicians across multiple disciplines, and advanced medical professionals
- Advanced Practice Providers (APPs)
Nurse Practitioners (NPs), Physician Assistants (PAs), CRNAs, Certified Nurse Midwives (CNMs), Optometrists, Therapists, and other non-physician clinical providers
- Nursing & Allied Healthcare
Registered Nurses (RNs), LPNs, CNAs, Medical Assistants, Lab Technicians, and additional patient-care and diagnostic professionals.

Our team is focused on delivering fast turnaround times, rigorous screening standards, strong compliance management, and dependable workforce support aligned with client expectations and operational demands.

I would welcome the opportunity to schedule a brief conversation to better understand your current staffing initiatives and explore how Radixsol can support your healthcare workforce strategy, particularly across locums and clinical staffing requirements.

If there is someone else within your organization who oversees vendor onboarding, supplier partnerships, or contingent workforce programs, I would greatly appreciate it if you could direct me to the appropriate stakeholder.
Look forward to hearing from you!"""

# --- Generic (Talent Acquisition / HR, Procurement, and "other") ------------
GENERIC_BODY = """Hi {first_name},

I hope you are doing well.
Radixsol is a global workforce solutions partner supporting organizations across talent acquisition, workforce management, and strategic hiring initiatives.

{opener}

We partner with organizations across a range of industries to support permanent hiring, contingent workforce programs, and specialized talent requirements.

As we continue to expand our client partnerships, I would be grateful for any guidance regarding your vendor onboarding process.

If supplier engagement is managed by another team, I would greatly appreciate an introduction to the appropriate stakeholder responsible for workforce partnerships or supplier management.
Look forward to hearing from you!"""

# Used for the generic template when Claude is not producing a custom opener.
DEFAULT_OPENER = (
    "I came across your profile and noticed that you lead {focus_area} initiatives at {company}. "
    "I would welcome the opportunity to connect and learn more about your program objectives, "
    "supplier strategy, and workforce priorities, and areas of focus for the coming months."
)

# --- Executive / senior leadership (higher-altitude, referral-oriented) ------
EXECUTIVE_BODY = """Hi {first_name},

I hope you are doing well.
Radixsol is a Joint Commission-certified MBE/WBE workforce solutions partner, supporting organizations across clinical and non-clinical staffing, talent acquisition, and contingent workforce programs.

{opener}

We help organizations strengthen their workforce strategy through scalable staffing, locum and clinical coverage, specialized hiring, and vendor/MSP partnerships - delivered with fast turnaround, rigorous screening, and strong compliance.

As we look to grow our partnership with {company}, I would be grateful if you could point me to the right leader on your team who oversees talent acquisition, procurement, or vendor onboarding for workforce programs.

I would welcome a brief conversation at your convenience.
Look forward to hearing from you!"""

DEFAULT_OPENER_EXEC = (
    "I'm reaching out because, as a senior leader at {company}, you are well placed to help me "
    "connect with the teams shaping your talent acquisition, contingent workforce, and supplier strategy."
)

# Subject lines (company is appended only when present).
SUBJECTS = {
    "healthcare": "Radixsol - Clinical & Locum Staffing Support",
    "executive": "Radixsol - Strategic Workforce & Staffing Partnership",
    "generic": "Radixsol - Workforce & Staffing Partnership",
}

# Default focus-area phrase per category (used when Claude is skipped).
DEFAULT_FOCUS = {
    "healthcare": "Clinical Staffing",
    "hr": "Talent Acquisition & HR",
    "procurement": "Procurement",
    "executive": "Workforce & Operations",
    "other": "Workforce",
}


def _subject(key, company):
    base = SUBJECTS[key]
    return f"{base} - {company}" if company else base


def _clean_subject(subject):
    """Light sanitize of an AI-written subject line: trim quotes/length, drop
    shouty punctuation, and reject empties."""
    s = subject.strip().strip('"').strip("'").strip()
    s = s.replace("!", "").rstrip(" .")
    if len(s) > 90:
        s = s[:90].rsplit(" ", 1)[0]
    return s or None


def _clean_opener(opener, first_name):
    """Strip a leading salutation Claude sometimes prepends (e.g. 'Hi Anita,'),
    so it doesn't duplicate the template's own greeting; also normalize dashes."""
    op = opener.strip()
    # Normalize em/en dashes (AI sometimes ignores the "no dashes" instruction).
    op = re.sub(r"\s*[—–]\s*", ", ", op)
    op = re.sub(r",\s*,", ", ", op)
    n = 0
    # Greeting + the recipient's name + comma  ->  "Hi Anita, "
    if first_name:
        op, c = re.subn(rf"^(hi|hello|hey|dear|greetings)\s+{re.escape(first_name)}\s*[,.:-]\s*",
                        "", op, flags=re.IGNORECASE)
        n += c
    # Generic greeting with a short trailing token  ->  "Hello there, " / "Hi Sam Roy,"
    op, c = re.subn(r"^(hi|hello|hey|dear|greetings)\s+[A-Za-z][A-Za-z .'-]{0,30},\s+",
                    "", op, flags=re.IGNORECASE)
    n += c
    op = op.strip()
    if n and op:  # re-capitalize the now-leading word after removing a greeting
        op = op[0].upper() + op[1:]
    return op


# --- Follow-ups -------------------------------------------------------------
# Fixed, user-supplied follow-up bodies, sent as RE: <original subject> when
# there's no reply. Only {first_name} is merged in.
FOLLOWUP_BODIES = [
    # 1 (+2d)
    """Hi {first_name},

I hope you're doing well.
Would you be able to advise who oversees strategic vendor management, workforce solutions, and professional services partnerships?

I would appreciate the opportunity to connect with the appropriate stakeholder for partnerships.
Thank you for your guidance, and I would be grateful for an introduction if appropriate.""",
    # 2 (+5d)
    """Hi {first_name},

Checking on this!
Could you kindly direct me to the appropriate leader or team responsible for strategic vendor management and partner engagement for workforce solutions and professional services?
I would greatly appreciate an introduction to the relevant stakeholder.""",
    # 3 (+7d)
    """Hi {first_name},

I hope you're doing well!
As I haven't heard back from you, I'm assuming that you might have missed my previous messages or might be busy with other priorities.
I am excited to connect back with you to share mutual introductions.
Please let me know what date/time works best for you and I will send a calendar invitation. Or, if there is a dedicated person, could you kindly direct me to the appropriate leader for strategic vendor/supplier management.
Look forward to hearing from you!""",
    # 4 (+10d)
    """Hi {first_name},

Hope you are doing well!
I understand that your inbox is bursting at the seams, but I wanted to gently follow up with you on all my previous emails.
We are excited to connect with you to share mutual introductions. Would you have some time to connect for an introduction call with us this week or next?
Look forward to hearing from you!""",
    # 5 (+20d)
    """Hi {first_name},

I hope you are doing well!
Checking on this!""",
]


def render_followup(step, first_name, base_subject=None):
    """Return (subject, body) for follow-up number `step` (1-based)."""
    first_name = (first_name or "there").strip()
    idx = max(1, min(step, len(FOLLOWUP_BODIES))) - 1
    body = FOLLOWUP_BODIES[idx].format(first_name=first_name)

    subject = (base_subject or "Following up").strip()
    if not subject.lower().startswith("re:"):
        subject = "RE: " + subject
    return subject, body


def render(category, focus_area, opener, first_name, company, subject=None):
    """Return (subject, body) for a contact.

    category   : 'healthcare' | 'hr' | 'procurement' | 'executive' | 'other'
    focus_area : short phrase slotted into the generic opener
    opener     : optional Claude-written opening sentence (generic/executive only)
    first_name : recipient's first name (falls back to 'there')
    company    : recipient's company (optional)
    subject    : optional Claude-written subject; falls back to a fixed one
    """
    first_name = (first_name or "there").strip()
    company = (company or "").strip()
    focus_area = (focus_area or DEFAULT_FOCUS.get(category, "Workforce")).strip()
    company_phrase = company or "your organization"
    ai_subject = _clean_subject(subject) if subject else None

    if category == "healthcare":
        subj = ai_subject or _subject("healthcare", company)
        body = HEALTHCARE_BODY.format(first_name=first_name)
        return subj, body

    if category == "executive":
        subj = ai_subject or _subject("executive", company)
        op = _clean_opener(opener, first_name) if opener else DEFAULT_OPENER_EXEC.format(company=company_phrase)
        body = EXECUTIVE_BODY.format(first_name=first_name, opener=op, company=company_phrase)
        return subj, body

    subj = ai_subject or _subject("generic", company)
    if opener:
        op = _clean_opener(opener, first_name)
    else:
        op = DEFAULT_OPENER.format(focus_area=focus_area, company=company_phrase)
    body = GENERIC_BODY.format(first_name=first_name, opener=op)
    return subj, body
