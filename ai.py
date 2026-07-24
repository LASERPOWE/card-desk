"""OpenAI integration.

- Card extraction : vision OCR via Chat Completions (JSON mode)
- Enrichment      : Responses API + web_search tool (live web research)

Cost controls (set in .env):
  EXTRACT_MODEL   default gpt-4o
  ENRICH_MODEL    default gpt-5.4-mini  (~7x cheaper than gpt-5.5)
  ENRICH_EFFORT   default low           (low | medium | high)
  SEARCH_CONTEXT  default medium        (low | medium | high)
"""
import json
import os
import re
import requests

CHAT_ENDPOINT = "https://api.openai.com/v1/chat/completions"
RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"


def _cfg(name, default):
    return os.environ.get(name, "").strip() or default


def get_api_key():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key or key.startswith("sk-your"):
        raise RuntimeError("OPENAI_API_KEY is missing. Add your real key to the .env file and restart.")
    return key


def _headers():
    return {"Authorization": "Bearer " + get_api_key(), "Content-Type": "application/json"}


# ==========================================================================
# 1. VISION EXTRACTION (business card OCR)
# ==========================================================================
EXTRACT_PROMPT = """You are an expert at reading business cards. Extract ALL visible information precisely.

Return JSON with these exact keys:
- full_name: Complete person name exactly as printed
- designation: Job title/role exactly as printed
- company: Company/organization name
- department: Department if mentioned, else empty string
- mobile: Mobile/cell numbers (keep + prefix), comma-separate if multiple
- office_phones: Array of office/landline numbers
- fax: Fax number or empty string
- emails: Array of all email addresses
- website: Website URL(s)
- address: Complete street address
- city: City name
- pin_code: ZIP/postal code
- country: Country or empty string
- confidence: 'High' | 'Medium' | 'Low' based on card readability
- notes: Taglines, social handles, QR info, anything not fitting above fields
- raw_summary: Complete verbatim text on card, line by line

Use empty string for missing text fields. Empty array for missing arrays."""


def extract_with_vision(base64_data, mime_type):
    payload = {
        "model": _cfg("EXTRACT_MODEL", "gpt-4o"),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": EXTRACT_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": "data:" + mime_type + ";base64," + base64_data}},
            ],
        }],
        "response_format": {"type": "json_object"},
        "max_tokens": 1200,
    }
    resp = requests.post(CHAT_ENDPOINT, headers=_headers(), json=payload, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError("Vision extraction failed ({}): {}".format(
            resp.status_code, resp.text[:200]))
    data = json.loads(resp.json()["choices"][0]["message"]["content"])
    for f in ("office_phones", "emails", "website", "mobile"):
        v = data.get(f)
        if isinstance(v, list):
            data[f] = ", ".join(str(x).strip() for x in v if x)
    return data


# ==========================================================================
# 2. ENRICHMENT — live web research across ALL sources
# ==========================================================================
def strip_company_suffix(name):
    if not name:
        return ""
    s = str(name)
    s = re.sub(r"\s*(Pvt\.?|Private)\s+(Ltd\.?|Limited)\.?$", "", s, flags=re.I)
    s = re.sub(r"\s+(Ltd\.?|Limited|LLC|LLP|Inc\.?|Corp\.?|Corporation|Co\.?)\.?$", "", s, flags=re.I)
    return s.rstrip(".,").strip()


def build_enrich_prompt(full_name, designation, company, company_short):
    return """You are a professional business researcher with a live web search tool.
Research the person and company below using ALL available web sources - not just LinkedIn:
official company website, news articles, business directories (Crunchbase, ZaubaCorp, Tofler,
Justdial, IndiaMART), regulatory/industry portals (AMFI, SEBI, MCA where relevant), conference
bios, press releases, interviews, and ALL social platforms: Twitter/X, Facebook, Instagram,
YouTube, and any professional listing.

PERSON: {name}
DESIGNATION: {desig}
COMPANY: {company} (search also as "{company_short}")

TASKS:
1. linkedin_url - Find the exact profile URL matching linkedin.com/in/<slug>.
   Try: site:linkedin.com/in "{name}" {company_short}  then broader searches.
   STRICT: the slug must clearly correspond to THIS person's name (it normally
   contains their first and/or last name) AND the result snippet must mention
   their company or role. NEVER invent a slug. No search-page URLs. A wrong
   profile is worse than none - if unsure, return "".
2. linkedin_photo_url - Direct profile image URL ONLY if a real CDN URL
   (media.licdn.com / static.licdn.com etc.) appears in results. Else "".
3. summary - 2-3 plain-English sentences: who this person is, what the company does,
   and anything notable. Written for a directory card. No bullets here.
4. linkedin_position - Bullet-point PERSON profile from all sources combined:
   • Current Role: ...
   • Career History: (one line per role with company and dates if known)
   • Education: ...
   • Expertise: ...
   • Notable: (awards, press, talks, funds managed, publications)
   • Network: ...
   Write "Not publicly available" for empty bullets.
5. company_core_business - Bullet-point COMPANY profile:
   • Overview • Founded • Headquarters • Core Business • Products/Services
   • Industry • Key Leadership • Market Position • Recent News • Official Website
6. other_web_profiles - Hunt the person's SOCIAL profiles specifically. Run targeted
   searches: site:facebook.com "{name}" {company_short} | site:x.com OR site:twitter.com
   "{name}" {company_short} | site:instagram.com "{name}". List every VERIFIED profile
   or mention URL one per line, labelled like:
   Facebook: <url>
   Twitter: <url>
   Instagram: <url>
   YouTube: <url>
   Crunchbase: <url>
   News: <url>
   Only URLs actually seen in search results - never guess or invent handles.
7. enrich_sources - URLs you actually used, comma-separated.

RULES: Every fact must come from live search results - never invent.
Respond with ONLY this JSON (no markdown fences):
{{"linkedin_url":"", "linkedin_photo_url":"", "summary":"", "linkedin_position":"", "other_web_profiles":"", "company_core_business":"", "enrich_sources":""}}""".format(
        name=full_name or "Unknown", desig=designation or "Unknown",
        company=company or "Unknown", company_short=company_short or company or "")


def enrich_person(full_name, designation, company):
    """Returns (data_dict, api_ok). api_ok=False means transient failure (retry later).
    api_ok=True with empty linkedin_url means genuinely not found (never retried)."""
    company_short = strip_company_suffix(company)
    payload = {
        "model": _cfg("ENRICH_MODEL", "gpt-5.4-mini"),
        "input": build_enrich_prompt(full_name, designation, company, company_short),
        "reasoning": {"effort": _cfg("ENRICH_EFFORT", "low")},
        "tools": [{"type": "web_search",
                   "search_context_size": _cfg("SEARCH_CONTEXT", "medium")}],
        "tool_choice": "required",
        "max_output_tokens": 6000,
    }
    try:
        resp = requests.post(RESPONSES_ENDPOINT, headers=_headers(), json=payload, timeout=300)
    except requests.RequestException as e:
        return _fallback("Network error: " + str(e)[:200]), False

    if resp.status_code != 200:
        return _fallback("API error " + str(resp.status_code) + ": " + resp.text[:200]), False

    try:
        result = resp.json()
    except ValueError:
        return _fallback("Response parse error"), False

    raw_text = ""
    for item in result.get("output", []):
        if item.get("type") != "message":
            continue
        for block in item.get("content", []):
            if block.get("type") == "output_text" and block.get("text"):
                raw_text += block["text"]

    if not raw_text:
        return _fallback("Empty model output"), False

    return parse_and_validate(raw_text), True


def _fallback(reason):
    return {
        "linkedin_url": "",
        "linkedin_photo_url": "",
        "summary": "",
        "linkedin_position": "Research issue: " + reason,
        "other_web_profiles": "",
        "company_core_business": "Research issue: " + reason,
        "enrich_sources": "Fallback: " + reason,
    }


def _to_str(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        return ", ".join(str(x).strip() for x in v)
    return str(v).strip()


def parse_and_validate(raw_text):
    cleaned = re.sub(r"```json|```", "", raw_text).strip()
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if not m:
        return _fallback("No JSON in model response")
    try:
        parsed = json.loads(m.group(0))
    except ValueError:
        return _fallback("JSON parse failed")

    li_url = _to_str(parsed.get("linkedin_url"))
    if li_url and not is_valid_linkedin_url(li_url):
        li_url = ""

    photo = _to_str(parsed.get("linkedin_photo_url"))
    if photo and not is_valid_photo_url(photo):
        photo = ""

    others = _to_str(parsed.get("other_web_profiles"))
    others_clean = "\n".join(
        u.strip() for u in re.split(r"[\n,]", others)
        if u.strip() and is_valid_http_url(u.strip())
    )

    return {
        "linkedin_url": li_url,
        "linkedin_photo_url": photo,
        "summary": _to_str(parsed.get("summary")),
        "linkedin_position": _to_str(parsed.get("linkedin_position")),
        "other_web_profiles": others_clean,
        "company_core_business": _to_str(parsed.get("company_core_business")),
        "enrich_sources": _to_str(parsed.get("enrich_sources")),
    }


# ==========================================================================
# 3. VALIDATORS
# ==========================================================================
def is_valid_linkedin_url(url):
    return bool(re.match(
        r"^https?://([a-z]{2,}\.)?linkedin\.com/in/[a-zA-Z0-9\-_%\.]+/?(\?.*)?$",
        url.strip(), re.I))


def is_valid_photo_url(url):
    allowed = [
        "media.licdn.com", "media-exp1.licdn.com", "media-exp2.licdn.com",
        "static.licdn.com", "pbs.twimg.com", "unavatar.io",
        "gravatar.com", "githubusercontent.com", "lh3.googleusercontent.com",
    ]
    try:
        domain = re.sub(r"^https?://", "", url).split("/")[0].lower()
        return any(d in domain for d in allowed)
    except Exception:
        return False


def is_valid_http_url(url):
    return bool(re.match(r"^https?://.+\..+", url.strip()))


def derive_avatar_from_linkedin(linkedin_url):
    """unavatar.io with fallback=false: returns 404 when no real photo exists,
    letting the frontend fall back to initials instead of a grey placeholder."""
    if not linkedin_url:
        return ""
    m = re.search(r"linkedin\.com/in/([a-zA-Z0-9\-_%\.]+)", str(linkedin_url), re.I)
    return ("https://unavatar.io/linkedin/" + m.group(1) + "?fallback=false") if m else ""
