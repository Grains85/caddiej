"""Reading the vx13 reply schema the way the API does: through its `$defs`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from caddiej.coaching import vx


def resolved(schema: Mapping[str, Any], node: Mapping[str, Any]) -> Mapping[str, Any]:
    """`node` with every `$ref` followed into `schema["$defs"]`."""

    while "$ref" in node:
        name = str(node["$ref"]).removeprefix("#/$defs/")
        node = schema["$defs"][name]
    return node


def nullable(schema: Mapping[str, Any], node: Mapping[str, Any]) -> Mapping[str, Any]:
    """The non-null choice of a `null | x` node, asserting the null is there."""

    choices = resolved(schema, node)["anyOf"]
    assert choices[0] == {"type": "null"}
    return resolved(schema, choices[1])


def fixed_length(node: Mapping[str, Any]) -> int:
    """The one length a fixed-length array allows."""

    assert node["type"] == "array"
    assert node["minItems"] == node["maxItems"]
    return int(node["minItems"])


def wire_list(schema: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """One checkpoint list, keyed `<phase>_<camera>` (`transition_dtl`)."""

    return schema["properties"]["checkpoints"]["properties"][key]


def checkpoint_slot(schema: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """What one position of list `key` may hold."""

    return resolved(schema, wire_list(schema, key)["items"])


def checkpoint_entry(schema: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """The non-null entry: `[score, cell, confidence, direction]`."""

    return nullable(schema, wire_list(schema, key)["items"])


def entry_choices(schema: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    """What any one item of a non-null entry of list `key` may be."""

    return [
        resolved(schema, choice)
        for choice in checkpoint_entry(schema, key)["items"]["anyOf"]
    ]


def citable_cells(schema: Mapping[str, Any], key: str) -> list[str]:
    """The cell IDs list `key` may cite: the entry's inline string choice."""

    integer, cells, direction, null = checkpoint_entry(schema, key)["items"]["anyOf"]
    assert integer["type"] == "integer" and null == {"type": "null"}
    assert direction == {"$ref": "#/$defs/dir"}
    return list(cells["enum"])


def metric_entry(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    """The non-null entry: `[value, unit, direction]`."""

    return nullable(schema, schema["properties"]["metrics"]["items"])


def listed_cells(evidence: str) -> list[str]:
    """The camera cell IDs the evidence text gives the model, in order."""

    line = next(
        text for text in evidence.splitlines() if text.startswith("Camera cells")
    )
    listed = line.split(": ", 1)[1].removesuffix(".")
    return [item.split(" ")[0] for item in listed.split(", ")]


def wire_line(evidence: str, wire: str, key: str) -> str:
    """The numbered evidence line for checkpoint `key` within list `wire`."""

    lines = evidence.splitlines()
    start = lines.index(f"{wire} ({len(vx.WIRE_LISTS[wire])}):")
    return next(text for text in lines[start + 1 :] if text.split(" ")[1:2] == [key])
