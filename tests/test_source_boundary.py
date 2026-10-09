"""
The boundary between the framework and the sources, and the source registry.

The framework (every module of the package outside ``sources/``) names no
source and imports none; the CLI finds sources through the registry only.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import tradedesk_marketdata
from tradedesk_marketdata import sources
from tradedesk_marketdata.source import Source, SourceDescription, SourceOption

PACKAGE = Path(tradedesk_marketdata.__file__).parent
FRAMEWORK = sorted(p for p in PACKAGE.glob("*.py"))


def test_the_framework_names_no_source() -> None:
    """``grep -ri "<source>" tradedesk_marketdata/ --include=*.py -l`` lists only sources/."""
    offenders = [
        (p.name, name)
        for p in FRAMEWORK
        for name in sources.names()
        if name in p.read_text().lower()
    ]
    assert offenders == []


def _imports(path: Path) -> set[str]:
    """The modules a file imports, relative ones as ``.name``."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            found.add(base)
            found.update(f"{base}.{a.name}" if node.module else base + a.name for a in node.names)
    return found


def test_only_the_cli_imports_the_registry() -> None:
    importers = [
        p.name
        for p in FRAMEWORK
        if any(
            m in (".sources", "tradedesk_marketdata.sources") or ".sources." in m
            for m in _imports(p)
        )
    ]
    assert importers == ["cli.py"]


def test_importing_the_framework_loads_no_source() -> None:
    code = (
        "import sys, tradedesk_marketdata.export, tradedesk_marketdata.parallel; "
        "print(sorted(m for m in sys.modules if m.startswith('tradedesk_marketdata.sources')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registered_sources_are_listed_by_name() -> None:
    assert sources.names() == sorted(sources.names())
    for name in sources.names():
        cls = sources.get(name)
        assert cls.name == name
        assert cls.summary and cls.unit and cls.partial_commit_rule


def test_an_unknown_source_names_the_available_ones() -> None:
    with pytest.raises(sources.UnknownSourceError) as exc:
        sources.get("nowhere")
    assert ", ".join(sources.names()) in str(exc.value)


def test_create_hands_the_options_to_the_source() -> None:
    src = sources.create("dukascopy", {"price_divisor": 100.0})
    assert src.option("price_divisor") == 100.0
    assert src.option("probe_ticks", 10) == 10  # not given: the source's default


class _Clashing(Source):
    name = "clashing"
    summary = "declares another source's flag"
    unit = "thing"
    partial_commit_rule = "none"
    options = (SourceOption("--price-divisor", help="mine too"),)

    def describe(self, symbol):  # pragma: no cover - never run
        return SourceDescription(price_divisor=1.0)

    def open(self, ctx):  # pragma: no cover - never run
        raise NotImplementedError


def test_a_source_cannot_declare_another_sources_flag() -> None:
    with pytest.raises(ValueError, match="--price-divisor is already an option of source"):
        sources.register(_Clashing)
    assert "clashing" not in sources.names()
