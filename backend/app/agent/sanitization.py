import re


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_EMAIL = re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_PROMPT_INJECTION_LINES = re.compile(
    r"(?im)^.*(?:ignore|disregard|override)\s+(?:all\s+)?"
    r"(?:previous|prior|above)\s+(?:instructions?|rules?).*$"
)


def sanitize_evidence(value: str) -> str:
    sanitized = _PROMPT_INJECTION_LINES.sub("[PROMPT_INJECTION_REMOVED]", value)
    sanitized = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", sanitized)
    sanitized = _BEARER_TOKEN.sub("Bearer [REDACTED]", sanitized)
    sanitized = _EMAIL.sub("[REDACTED_EMAIL]", sanitized)
    return _IPV4.sub("[REDACTED_IP]", sanitized)
