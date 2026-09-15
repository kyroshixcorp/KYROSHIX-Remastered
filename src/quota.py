"""Classifying Gemini API errors so retry logic reacts sensibly.

The old check was `"429" in s or "quota" in s or "rate" in s`, which matches
any word containing r-a-t-e (generated, moderated, separate) and lumps every
quota error together. The important distinction is whether cycling to another
API key can possibly help.

Rate limits are applied per project, not per API key. So a per-minute limit on
one key might clear by moving to a key from a different project, but a daily
quota or a plan restriction does not care how many keys you have.
"""

# phrases that show the project itself is out of allowance, not just throttled
_PROJECT_EXHAUSTED = (
    "exceeded your current quota",
    "check your plan and billing",
    "billing details",
    "quota exceeded",
    "resource_exhausted",
    "resource has been exhausted",
)

# genuine per-request throttling that another key may well get around
_THROTTLED = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "429",
)


def _norm(err) -> str:
    return str(err or "").lower()


def is_rate_limit(err) -> bool:
    """Any kind of throttling or quota rejection, key-specific or not."""
    text = _norm(err)
    if any(marker in text for marker in _PROJECT_EXHAUSTED):
        return True
    if any(marker in text for marker in _THROTTLED):
        return True
    return "quota" in text


def is_project_exhausted(err) -> bool:
    """True when the whole project is out of allowance.

    Cycling API keys cannot fix this, the limit is attached to the project
    rather than the key. Callers should back off instead of burning the pool.
    """
    text = _norm(err)
    return any(marker in text for marker in _PROJECT_EXHAUSTED)


def quota_hint(err) -> str:
    """Short, user-facing explanation of what the quota error actually means."""
    text = _norm(err)
    if "exceeded your current quota" in text or "billing details" in text:
        return (
            "this project is out of quota for now. rate limits apply per project, "
            "so switching API keys does not help. check your plan and limits at "
            "https://aistudio.google.com/rate-limit"
        )
    if "resource has been exhausted" in text or "resource_exhausted" in text:
        return (
            "the project hit a quota limit. rate limits apply per project, so "
            "switching API keys does not help. check https://aistudio.google.com/rate-limit"
        )
    return "quota limit hit, backing off"
