---
paths:
  - "**/*.py"
---

# Python Docstring Format

Use Google-style docstrings for all public functions and classes. Include a brief description, then Args, Returns, and Raises sections as applicable.

```python
def check_string_token_overlap(str1: str, str2: str, threshold: float = 0.7) -> bool:
    """
    Check if two strings have significant token overlap, indicating they might be referring to the same thing.
    This is useful for fuzzy matching when exact string matching is too strict.

    Args:
        str1 (str): First string to compare
        str2 (str): Second string to compare
        threshold (float): Minimum overlap ratio required to consider strings as matching (0.0 to 1.0)

    Returns:
        bool: True if strings have significant overlap, False otherwise
    """
```
