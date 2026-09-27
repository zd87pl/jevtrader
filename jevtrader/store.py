"""Append-only SQLite records. Idempotent inserts preserve the first observation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .common import canonical, digest, instant, symbol, timestamp, utc_now

KINDS = {
    "disclosures",
    "bars",
    "extractions",
    "forecasts",
    "outcomes",
    "models",
    "paper_plans",
    "experiments",
}


class Ledger:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise ValueError(f"Unsupported ledger schema {version}")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                content_hash TEXT NOT NULL, recorded_at TEXT NOT NULL,
                PRIMARY KEY (kind, id)
            );
            CREATE TRIGGER IF NOT EXISTS records_no_update BEFORE UPDATE ON records
            BEGIN SELECT RAISE(ABORT, 'Ledger records are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS records_no_delete BEFORE DELETE ON records
            BEGIN SELECT RAISE(ABORT, 'Ledger records are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS records_no_replace BEFORE INSERT ON records
            WHEN EXISTS (SELECT 1 FROM records WHERE kind=NEW.kind AND id=NEW.id)
            BEGIN SELECT RAISE(ABORT, 'Ledger records are immutable'); END;
            PRAGMA user_version=1;
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def put(self, kind: str, identity: str, payload: dict) -> bool:
        if kind not in KINDS or not isinstance(identity, str) or not identity:
            raise ValueError("Invalid ledger record kind or id")
        encoded = canonical(payload)
        existing = self.get(kind, identity)
        if existing is not None:
            if canonical(existing) != encoded:
                raise ValueError(f"Immutable record conflict: {kind}/{identity}")
            return False
        with self.db:
            self.db.execute(
                "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
                (kind, identity, encoded, digest(payload), utc_now()),
            )
        return True

    def get(self, kind: str, identity: str) -> dict | None:
        row = self.db.execute(
            "SELECT payload, content_hash FROM records WHERE kind=? AND id=?", (kind, identity)
        ).fetchone()
        if row is None:
            return None
        result = json.loads(row[0])
        if digest(result) != row[1]:
            raise ValueError(f"Corrupted record: {kind}/{identity}")
        return result

    def all(self, kind: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT payload, content_hash FROM records WHERE kind=? ORDER BY id", (kind,)
        ).fetchall()
        result = []
        for encoded, fingerprint in rows:
            value = json.loads(encoded)
            if digest(value) != fingerprint:
                raise ValueError(f"Corrupted record in {kind}")
            result.append(value)
        return result

    def prefix(self, kind: str, prefix: str) -> list[dict]:
        """Use the primary-key index for a symbol's bars instead of loading every symbol."""
        rows = self.db.execute(
            "SELECT payload, content_hash FROM records WHERE kind=? AND id>=? AND id<? ORDER BY id",
            (kind, prefix, prefix + "\uffff"),
        ).fetchall()
        result = []
        for encoded, fingerprint in rows:
            value = json.loads(encoded)
            if digest(value) != fingerprint:
                raise ValueError(f"Corrupted record in {kind}")
            result.append(value)
        return result

    def counts(self) -> dict:
        return {
            kind: count
            for kind, count in self.db.execute(
                "SELECT kind, COUNT(*) FROM records GROUP BY kind ORDER BY kind"
            )
        }

    def disclosure(self, event: dict, *, imported: bool = True) -> bool:
        event = dict(event)
        for key in ("id", "text", "source_url", "mode"):
            if not isinstance(event.get(key), str) or not event[key].strip():
                raise ValueError(f"Disclosure needs a nonempty {key}")
        if event["mode"] not in {"historical", "forward", "synthetic"}:
            raise ValueError("Disclosure mode must be historical, forward, or synthetic")
        # Imported timestamps are user assertions, never evidence of a forward collection.
        if imported and event["mode"] == "forward":
            raise ValueError("Only the live collector may create forward disclosures")
        event["symbol"] = symbol(event.get("symbol", ""))
        event["published_at"] = timestamp(event["published_at"])
        event["first_seen_at"] = timestamp(event["first_seen_at"])
        if instant(event["published_at"]) > instant(event["first_seen_at"]):
            raise ValueError("Disclosure cannot be observed before publication")
        if event["mode"] == "forward" and instant(event["first_seen_at"]) > instant(utc_now()):
            raise ValueError("Forward observation cannot be in the future")
        prior = self.get("disclosures", event["id"])
        if prior:
            # Repeated SEC requests must not move the original observation time, but an
            # imported correction (e.g. first_seen_at) must never be silently discarded.
            stable = ("symbol", "published_at", "text", "source_url", "mode")
            changed = (
                canonical(prior) != canonical(event)
                if imported
                else any(prior[key] != event[key] for key in stable)
            )
            if changed:
                raise ValueError(
                    f"Disclosure content changed for {event['id']}; use a new version id"
                )
            return False
        return self.put("disclosures", event["id"], event)
