"""The adapter boundary, enforced rather than documented.

The point of the GSPro adapter is that adding a second launch monitor later
touches nothing outside it. That only holds if GSPro's vocabulary genuinely
stops at the boundary, so these tests fail if it starts to leak.
"""

import ast
import inspect
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest

import caddiej.domain.events as domain_events
import caddiej.domain.shot as domain_shot
from caddiej.core.events import Event
from caddiej.domain.events import ShotIngested

SOURCE_ROOT = Path(inspect.getfile(domain_shot)).parents[1]
GSPRO_PACKAGE = "caddiej.adapters.gspro"


def python_files(relative: str) -> list[Path]:
    return sorted((SOURCE_ROOT / relative).rglob("*.py"))


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("path", python_files("domain"))
def test_the_domain_imports_no_adapter(path: Path) -> None:
    # Dependencies point inward. A subscriber must never have to import a vendor
    # adapter to understand what happened.
    leaks = {module for module in imported_modules(path) if "adapters" in module}

    assert not leaks, f"{path.name} imports {leaks}"


@pytest.mark.parametrize("path", python_files("."))
def test_only_the_gspro_adapter_knows_the_gspro_wire_format(path: Path) -> None:
    if GSPRO_PACKAGE.replace(".", "/") in path.as_posix():
        return

    wire_imports = {
        module for module in imported_modules(path) if module.endswith("gspro.wire")
    }

    assert not wire_imports, f"{path.name} imports the GSPro wire format"


def test_nothing_outside_the_adapter_imports_the_interposer_internals() -> None:
    # Callers get an adapter with a lifecycle, not a byte pump.
    offenders = []
    for path in python_files("."):
        if GSPRO_PACKAGE.replace(".", "/") in path.as_posix():
            continue
        for module in imported_modules(path):
            if module.endswith(("gspro.interposer", "gspro.framing", "gspro.assembly")):
                offenders.append(path.name)

    assert not offenders


def attribute_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def test_only_the_boundary_and_the_store_touch_a_raw_payload() -> None:
    # A shot may carry the bytes a device sent, for diagnosing that device. It is
    # opaque: the moment anything parses it or branches on it, GSPro's shape has
    # become part of CaddieJ, which is the one thing this adapter exists to
    # prevent. So the only code allowed near it is what captures it, the field
    # that declares it, and what writes it to disk.
    allowed = {
        "adapters/gspro/adapter.py",
        "domain/shot.py",
        "persistence/records.py",
        "persistence/service.py",
    }

    offenders = sorted(
        path.relative_to(SOURCE_ROOT).as_posix()
        for path in python_files(".")
        if path.relative_to(SOURCE_ROOT).as_posix() not in allowed
        and "raw_payload" in attribute_names(path)
    )

    assert not offenders


def test_no_domain_event_carries_a_vendor_payload() -> None:
    # Events are recorded and replayed. A GSPro payload inside one would freeze
    # GSPro's shape into CaddieJ's own history.
    for name, candidate in vars(domain_events).items():
        if not (isinstance(candidate, type) and issubclass(candidate, Event)):
            continue
        if not is_dataclass(candidate):
            continue
        for field in fields(candidate):
            assert "wire" not in str(field.type).lower(), f"{name}.{field.name}"
            assert "gspro" not in str(field.type).lower(), f"{name}.{field.name}"


def test_an_ingested_shot_exposes_only_normalized_types() -> None:
    shot_field = next(field for field in fields(ShotIngested) if field.name == "shot")

    assert shot_field.type is domain_shot.Shot


def test_every_domain_event_is_frozen() -> None:
    # An event is a fact that already happened; it cannot be edited afterwards.
    for name, candidate in vars(domain_events).items():
        if not (isinstance(candidate, type) and issubclass(candidate, Event)):
            continue
        if not is_dataclass(candidate):
            continue
        assert candidate.__dataclass_params__.frozen, f"{name} is not frozen"
