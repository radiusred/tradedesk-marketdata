import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class ExportMetadata:
    schema_version: str
    source: str
    symbol: str
    data_type: Literal["candles", "ticks"]
    timestamp_format: Literal["iso8601_utc"]
    price_divisor: float
    generated_at: str
    params: dict[str, Any]


def now_iso_utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_sidecar(meta: ExportMetadata, outputfile: Path) -> Path:
    sidecar = outputfile.with_suffix(outputfile.suffix + ".meta.json")
    sidecar.write_text(json.dumps(asdict(meta), indent=2, sort_keys=True) + "\n")
    return sidecar
