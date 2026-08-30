"""The logging allowlist, checked against what the code actually logs.

The formatter fails closed so that nobody can leak a credential through `extra`,
which is the right trade — but it fails *quietly*. A field nobody added to the
allowlist simply never appears, and the way that is discovered is somebody
reading a log during an outage and finding the number they needed missing.

So this test walks every `extra=` in the source and insists the allowlist knows
about it. It is the only cheap way to keep a fail-closed list honest.
"""

import ast
import inspect
from pathlib import Path

import caddiej.domain.shot as domain_shot
from caddiej.__main__ import _JsonFormatter

SOURCE_ROOT = Path(inspect.getfile(domain_shot)).parents[1]


def logged_fields() -> dict[str, set[str]]:
    """Every key passed to a logger's `extra`, and where from."""

    fields: dict[str, set[str]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                    continue
                for key in keyword.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        fields.setdefault(key.value, set()).add(path.name)
    return fields


def test_every_logged_field_reaches_the_log() -> None:
    allowed = set(_JsonFormatter._CONTEXT_FIELDS)

    missing = {
        field: sorted(sources)
        for field, sources in logged_fields().items()
        if field not in allowed
    }

    assert not missing, f"these are logged but silently dropped: {missing}"


def test_the_allowlist_carries_nothing_that_looks_like_a_credential() -> None:
    # Belt and braces on the reason the allowlist exists in the first place.
    suspicious = [
        field
        for field in _JsonFormatter._CONTEXT_FIELDS
        if any(word in field for word in ("password", "secret", "token", "key"))
    ]

    assert not suspicious
