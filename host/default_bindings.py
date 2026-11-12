"""Packaged copy of the canonical, binding-only factory mapping.

The package builder verifies this resource against root default-bindings.json.
Reading defaults never accesses a keyboard or imports a user's saved profile.
"""
from importlib.resources import files
import json

from .protocol import ProtocolError, binding_record


def default_bindings():
    """Return independent records, also when running from an installed wheel."""
    value = json.loads(files(__package__).joinpath("default-bindings.json").read_text(encoding="utf-8"))
    if value.get("schema") != "split-beat-he-default-bindings-v1":
        raise ProtocolError("Unsupported packaged default bindings schema")
    records = [binding_record(item) for item in value["bindings"]]
    if sorted(item["input"] for item in records) != list(range(28)):
        raise ProtocolError("Packaged defaults must contain exactly 28 unique input IDs")
    return sorted(records, key=lambda item: item["input"])
