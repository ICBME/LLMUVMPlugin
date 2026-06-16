from __future__ import annotations

from typing import Any, Protocol


class HarnessMetadataExtractorProtocol(Protocol):
    def extract(self, metadata: dict[str, Any]) -> dict[str, Any]:
        ...
