"""A tiny stdlib validator for the JSON Schema subset the why-log schemas use, and the adoption filter.

Supported keywords: type, required, enum, properties, items, minLength, maxLength. Any other assertion keyword
in a schema is a programming error and raises, so the subset can never silently widen.
"""
import json
import os
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schemas")
ANNOTATIONS = {"$schema", "$id", "title", "description"}
SUPPORTED = {"type", "required", "enum", "properties", "items", "minLength", "maxLength"}
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def load(name: str = "decision") -> Dict:
    with open(os.path.join(SCHEMA_DIR, f"{name}.schema.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _is_type(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def _same(a: Any, b: Any) -> bool:
    """JSON equality: true is not 1, 1 equals 1.0."""
    return isinstance(a, bool) == isinstance(b, bool) and a == b


def _check_keywords(schema: Dict) -> None:
    unknown = set(schema) - SUPPORTED - ANNOTATIONS
    if unknown:
        raise ValueError(f"unsupported JSON Schema keyword(s): {sorted(unknown)}")


def validate(value: Any, schema: Dict, path: str = "$") -> List[str]:
    """Return a list of human-readable errors (empty = valid)."""
    _check_keywords(schema)
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, t) for t in types):
            return [f"{path}: expected {'|'.join(types)}, got {type(value).__name__}"]
    errors: List[str] = []
    if "enum" in schema and not any(_same(value, option) for option in schema["enum"]):
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(value, dict):
        errors += [f"{path}: missing required '{key}'" for key in schema.get("required", []) if key not in value]
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors += validate(value[key], sub, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for n, item in enumerate(value):
            errors += validate(item, schema["items"], f"{path}[{n}]")
    return errors


def sanitize(decisions: Any, schema: Optional[Dict] = None) -> Tuple[List[Dict], List[Dict]]:
    """Split model output into (adopted, rejected). Never raises on bad data; rejected items carry their errors."""
    schema = schema or load("decision")
    if not isinstance(decisions, list):
        return [], [{"index": None, "id": None, "errors": [f"$: expected array, got {type(decisions).__name__}"]}]
    adopted: List[Dict] = []
    rejected: List[Dict] = []
    for n, item in enumerate(decisions):
        errors = validate(item, schema, f"$[{n}]")
        if errors:
            rejected.append({"index": n, "id": item.get("id") if isinstance(item, dict) else None, "errors": errors})
        else:
            adopted.append(item)
    return adopted, rejected
