"""Separate deterministic content/metadata hashes; no side effects during check."""
from dataclasses import dataclass
from pathlib import Path
from contracts import NORMALIZATION_VERSION, SCHEMA_VERSION
from etl_common import STATE_PATH, TRANSFORM_VERSION, atomic_write_json, canonical_json, md5_hex, read_json

@dataclass
class DeltaResult:
    content_changed_ids: list
    metadata_changed_ids: list
    content_hashes: dict
    metadata_hashes: dict
    regenerate: bool
    reason: str
    config_hash: str
    transform_version: str

    @property
    def changed(self):
        return self.regenerate or bool(self.content_changed_ids or self.metadata_changed_ids)

    def state_payload(self):
        return {
            "schema_version": SCHEMA_VERSION, "normalization_version": NORMALIZATION_VERSION,
            "transform_version": self.transform_version, "config_hash": self.config_hash,
            "content_hashes": self.content_hashes, "metadata_hashes": self.metadata_hashes,
        }

class DeltaChecker:
    def __init__(self, state_path=STATE_PATH, *, transform_version=TRANSFORM_VERSION, config=None):
        self.state_path = Path(state_path)
        self.version = transform_version
        self.config_hash = md5_hex(canonical_json(config or {}))

    def check(self, records, force=False):
        state = read_json(self.state_path)
        old = state or {}
        for key, record in records.items():
            record.validate()
            if key != record.id:
                raise ValueError("Record map key differs from record_id")
        content = {k: r.content_hash for k, r in sorted(records.items())}
        metadata = {k: r.metadata_hash for k, r in sorted(records.items())}
        changed = sorted(k for k in set(content) | set(old.get("content_hashes", {}))
                         if content.get(k) != old.get("content_hashes", {}).get(k))
        meta_changed = sorted(k for k in set(metadata) | set(old.get("metadata_hashes", {}))
                              if metadata.get(k) != old.get("metadata_hashes", {}).get(k))
        if force:
            reason = "forced"
        elif state is None:
            reason = "first_run"
        elif old.get("normalization_version") != NORMALIZATION_VERSION or old.get("transform_version") != self.version:
            reason = "version_changed"
        elif old.get("config_hash") != self.config_hash:
            reason = "config_changed"
        elif changed:
            reason = "content_changed"
        else:
            reason = "metadata_only" if meta_changed else "unchanged"
        regenerate = reason not in {"unchanged", "metadata_only"}
        return DeltaResult(changed, meta_changed, content, metadata, regenerate, reason, self.config_hash, self.version)

    def commit(self, result):
        # Standalone API. Orchestrator publishes this payload LAST in its recoverable bundle.
        atomic_write_json(self.state_path, result.state_payload())
