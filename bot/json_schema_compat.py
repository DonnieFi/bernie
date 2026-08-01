"""Normalize Pydantic JSON Schema for Codex/OpenRouter structured output."""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

_STRIP_KEYS = frozenset({"title", "description", "default"})
_STRIP_CONSTRAINT_KEYS = frozenset({"maxLength", "minLength", "maximum", "minimum"})


def sanitize_json_schema_for_structured_output(
    schema: Mapping[str, Any] | dict[str, Any] | None,
) -> dict[str, Any]:
    """Inline ``$defs``/``$ref`` for OpenAI/Codex strict structured output."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "additionalProperties": False}

    root = copy.deepcopy(schema)
    defs: dict[str, Any] = {}
    raw_defs = root.pop("$defs", None) or root.pop("definitions", None)
    if isinstance(raw_defs, dict):
        defs.update(raw_defs)

    def _resolve(node: Any) -> Any:
        if isinstance(node, list):
            return [_resolve(item) for item in node]
        if not isinstance(node, dict):
            return node

        if "$ref" in node:
            ref = str(node["$ref"])
            key = ref.rsplit("/", 1)[-1]
            if key in defs:
                return _resolve(copy.deepcopy(defs[key]))
            return {"type": "object", "properties": {}, "additionalProperties": False}

        if "anyOf" in node and isinstance(node["anyOf"], list):
            non_null = [
                variant
                for variant in node["anyOf"]
                if not (isinstance(variant, dict) and variant.get("type") == "null")
            ]
            if len(non_null) == 1:
                return _resolve(non_null[0])

        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _STRIP_KEYS or key in _STRIP_CONSTRAINT_KEYS:
                continue
            if key == "additionalProperties" and value is True:
                out[key] = {"type": "string"}
                continue
            out[key] = _resolve(value)

        if out.get("type") == "object" and isinstance(out.get("properties"), dict):
            required = node.get("required")
            if isinstance(required, list) and required:
                out["properties"] = {
                    k: v for k, v in out["properties"].items() if k in required
                }
                out["required"] = list(required)
            else:
                out["required"] = list(out["properties"].keys())
            out["additionalProperties"] = False

        return out

    resolved = _resolve(root)
    if not isinstance(resolved, dict) or resolved.get("type") != "object":
        resolved = {"type": "object", "properties": {}, "additionalProperties": False}
    resolved.pop("$defs", None)
    resolved.pop("definitions", None)
    if "additionalProperties" not in resolved:
        resolved["additionalProperties"] = False
    if isinstance(resolved.get("properties"), dict):
        resolved.setdefault("required", list(resolved["properties"].keys()))
    return resolved
