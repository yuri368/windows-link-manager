"""Editable configuration state, independent of Qt and disk operations."""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import LinkSpec, parse_config, serialize_config


@dataclass
class ConfigDraft:
    raw_text: str = ""
    specs: list[LinkSpec] = field(default_factory=list)
    dirty: bool = False
    raw_draft: bool = False

    @classmethod
    def from_text(cls, text: str) -> ConfigDraft:
        return cls(raw_text=text, specs=parse_config(text))

    def text(self) -> str:
        return self.raw_text if self.raw_draft or not self.dirty else serialize_config(self.specs)

    def structured_changed(self) -> None:
        self.raw_draft = False
        self.dirty = True

    def adopt_text(self, text: str) -> None:
        specs = parse_config(text)
        self.raw_text, self.specs = text, specs
        self.raw_draft = self.dirty = True

    def saved(self, text: str) -> None:
        self.raw_text = text
        self.raw_draft = self.dirty = False
