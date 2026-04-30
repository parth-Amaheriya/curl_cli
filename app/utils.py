import re


def sanitize_function_name(name: str) -> str:
    """Sanitize and validate function name"""
    if not name:
        return None
    # Remove invalid characters and ensure valid Python identifier
    sanitized = re.sub(r'[^a-zA-Z0-9_]+', '_', name.strip())
    sanitized = re.sub(r'_+', '_', sanitized).strip('_')
    if not sanitized:
        return None
    if not re.match(r'^[a-zA-Z_]', sanitized):
        sanitized = f"func_{sanitized}"
    return sanitized.lower()