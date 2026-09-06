"""Mutable state shared across wizard pages for one visit through the flow."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from salvage.engine.ios import IOSDevice
from salvage.engine.local_media import FoundMedia, MediaSource
from salvage.engine.models import Device, RecoveredFile, ScanMode, ScanResult
from salvage.ui.ios_facade import IOSParsedData


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

    # --- iPhone flow ---------------------------------------------------
    ios_device: IOSDevice | None = None
    ios_existing_backup_dir: Path | None = None
    ios_categories: set[str] = field(default_factory=set)
    ios_backup_dir: Path | None = None
    ios_password: str | None = None
    ios_parsed: IOSParsedData | None = None

    # --- "This Mac" media search flow -----------------------------------
    media_sources: list[MediaSource] = field(default_factory=list)
    media_min_size: int = 20_000
    media_hash_dupes: bool = True
    media_found: list[FoundMedia] = field(default_factory=list)

    @property
    def session_dir(self) -> Path | None:
        if self.destination is None or not self.timestamp:
            return None
        return self.destination / f"Salvage {self.timestamp}"

    @property
    def recover_dir(self) -> Path | None:
        d = self.session_dir
        return None if d is None else d / "Recovered"

    @property
    def ios_recover_dir(self) -> Path | None:
        d = self.session_dir
        return None if d is None else d / "Recovered" / "iPhone"

    @property
    def media_export_dir(self) -> Path | None:
        d = self.session_dir
        return None if d is None else d / "Photos and videos"

    def reset(self) -> None:
        self.device = None
        self.mode = ScanMode.DEEP
        self.extensions = None
        self.destination = None
        self.timestamp = ""
        self.workdir = None
        self.scan_result = None
        self.recovered_paths = []
        self.ios_device = None
        self.ios_existing_backup_dir = None
        self.ios_categories = set()
        self.ios_backup_dir = None
        self.ios_password = None
        self.ios_parsed = None
        self.media_sources = []
        self.media_min_size = 20_000
        self.media_hash_dupes = True
        self.media_found = []
