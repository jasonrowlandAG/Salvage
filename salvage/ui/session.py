"""Mutable state shared across wizard pages for one visit through the flow."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from salvage.engine.models import Device, RecoveredFile, ScanMode, ScanResult


@dataclass
class ScanSession:
    device: Device | None = None
    mode: ScanMode = ScanMode.DEEP
    extensions: list[str] | None = None
    destination: Path | None = None
    timestamp: str = ""
    workdir: Path | None = None
    scan_result: ScanResult | None = None
    recovered_paths: list[Path] = field(default_factory=list)

    @property
    def session_dir(self) -> Path | None:
        if self.destination is None or not self.timestamp:
            return None
        return self.destination / f"Salvage {self.timestamp}"

    @property
    def recover_dir(self) -> Path | None:
        d = self.session_dir
        return None if d is None else d / "Recovered"

    def reset(self) -> None:
        self.device = None
        self.mode = ScanMode.DEEP
        self.extensions = None
        self.destination = None
        self.timestamp = ""
        self.workdir = None
        self.scan_result = None
        self.recovered_paths = []
