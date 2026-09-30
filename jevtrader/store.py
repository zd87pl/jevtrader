"""Append-only SQLite records in a SHA-256 hash chain. Idempotent inserts preserve the first
observation, and every stored record is committed to a chain that `verify` recomputes."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .common import canonical, digest, instant, symbol, timestamp, utc_now
from .pit import Clock, Instant, knowledge_time

KINDS = {
    "attempts",
    "cohorts",
    "disclosures",
    "bars",
    "extractions",
    "forecasts",
    "outcomes",
    "models",
    "paper_plans",
    "experiments",
    "runs",
    "securities",
}

SCHEMA_VERSION = 3
CHAINED_VERSION = 2  # the first schema with a hash chain
GENESIS = "0" * 64
MAX_PROBLEMS = 100
TRIGGERS = frozenset(
    {
        "records_no_update",
        "records_no_delete",
        "records_no_replace",
        "chain_no_update",
        "chain_no_delete",
        "chain_no_replace",
        "chain_extends_head",
        "chain_matches_record",
    }
)
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS records (
        kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
        content_hash TEXT NOT NULL, recorded_at TEXT NOT NULL,
        PRIMARY KEY (kind, id)
    )""",
    """CREATE TRIGGER IF NOT EXISTS records_no_update BEFORE UPDATE ON records
    BEGIN SELECT RAISE(ABORT, 'Ledger records are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS records_no_delete BEFORE DELETE ON records
    BEGIN SELECT RAISE(ABORT, 'Ledger records are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS records_no_replace BEFORE INSERT ON records
    WHEN EXISTS (SELECT 1 FROM records WHERE kind=NEW.kind AND id=NEW.id)
    BEGIN SELECT RAISE(ABORT, 'Ledger records are immutable'); END""",
    """CREATE TABLE IF NOT EXISTS chain (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, id TEXT NOT NULL,
        content_hash TEXT NOT NULL, prev_hash TEXT NOT NULL, chain_hash TEXT NOT NULL,
        UNIQUE (kind, id)
    )""",
    """CREATE TRIGGER IF NOT EXISTS chain_no_update BEFORE UPDATE ON chain
    BEGIN SELECT RAISE(ABORT, 'Ledger chain is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS chain_no_delete BEFORE DELETE ON chain
    BEGIN SELECT RAISE(ABORT, 'Ledger chain is immutable'); END""",
    # REPLACE deletes the old row without firing delete triggers, so block it up front.
    """CREATE TRIGGER IF NOT EXISTS chain_no_replace BEFORE INSERT ON chain
    WHEN EXISTS (SELECT 1 FROM chain WHERE (kind=NEW.kind AND id=NEW.id) OR seq=NEW.seq)
    BEGIN SELECT RAISE(ABORT, 'Ledger chain is immutable'); END""",
    f"""CREATE TRIGGER IF NOT EXISTS chain_extends_head BEFORE INSERT ON chain
    WHEN NEW.prev_hash IS NOT COALESCE(
        (SELECT chain_hash FROM chain ORDER BY seq DESC LIMIT 1), '{GENESIS}')
    BEGIN SELECT RAISE(ABORT, 'Ledger chain must extend its head'); END""",
    """CREATE TRIGGER IF NOT EXISTS chain_matches_record BEFORE INSERT ON chain
    WHEN NEW.content_hash IS NOT (SELECT content_hash FROM records
                                  WHERE kind=NEW.kind AND id=NEW.id)
    BEGIN SELECT RAISE(ABORT, 'Ledger chain must match a stored record'); END""",
)

# Schema 3 (ADR-0005): one creation record per ledger. Its root enters every later link.
IDENTITY_FIELDS = (
    "nonce",
    "created_at",
    "migrated_from",
    "legacy_seq",
    "legacy_head",
    "legacy_recorded_at",
)
IDENTITY_TRIGGERS = frozenset(
    {
        "ledger_identity_no_update",
        "ledger_identity_no_delete",
        "ledger_identity_single",
        "chain_links_v3",
    }
)
# SQLite resolves a trigger's functions when a statement that fires it is prepared. Only a
# schema-3 connection registers this one, so a writer still running pre-upgrade code fails
# loudly ("no such function") instead of appending a schema-2 link after the upgrade.
LINK_FUNCTION = "jevtrader_link_v3"
_IDENTITY_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS ledger_identity (
        nonce TEXT NOT NULL, created_at TEXT NOT NULL, migrated_from INTEGER NOT NULL,
        legacy_seq INTEGER NOT NULL, legacy_head TEXT NOT NULL,
        legacy_recorded_at TEXT NOT NULL, root TEXT NOT NULL
    )""",
    """CREATE TRIGGER IF NOT EXISTS ledger_identity_no_update BEFORE UPDATE ON ledger_identity
    BEGIN SELECT RAISE(ABORT, 'Ledger identity is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS ledger_identity_no_delete BEFORE DELETE ON ledger_identity
    BEGIN SELECT RAISE(ABORT, 'Ledger identity is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS ledger_identity_single BEFORE INSERT ON ledger_identity
    WHEN EXISTS (SELECT 1 FROM ledger_identity)
    BEGIN SELECT RAISE(ABORT, 'Ledger identity is immutable'); END""",
)
# Every link after the creation record must use the schema-3 formula (ADR-0005). A link that
# fails the replace, head or record checks is left to those triggers, so their messages stay precise.
_LINK_TRIGGER = f"""CREATE TRIGGER IF NOT EXISTS chain_links_v3 BEFORE INSERT ON chain
    WHEN NOT EXISTS (SELECT 1 FROM chain WHERE (kind=NEW.kind AND id=NEW.id) OR seq=NEW.seq)
    AND NEW.prev_hash IS COALESCE(
        (SELECT chain_hash FROM chain ORDER BY seq DESC LIMIT 1), '{GENESIS}')
    AND NEW.content_hash IS (SELECT content_hash FROM records WHERE kind=NEW.kind AND id=NEW.id)
    AND NEW.chain_hash IS NOT {LINK_FUNCTION}(
        NEW.prev_hash, NEW.kind, NEW.id, NEW.content_hash,
        (SELECT recorded_at FROM records WHERE kind=NEW.kind AND id=NEW.id),
        (SELECT root FROM ledger_identity ORDER BY rowid LIMIT 1))
    BEGIN SELECT RAISE(ABORT, 'Ledger chain link must use the schema-3 formula'); END"""

# A derived, rebuildable index for as_of reads (ADR-0004). It is outside the hash chain;
# as_of recomputes every returned row's knowledge time, so a stale or edited index can hide
# a row but never leak one known after the requested instant.
_KNOWLEDGE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS knowledge (
        kind TEXT NOT NULL, id TEXT NOT NULL, knowledge_time TEXT NOT NULL,
        PRIMARY KEY (kind, id)
    )""",
    "CREATE INDEX IF NOT EXISTS knowledge_by_time ON knowledge (kind, knowledge_time, id)",
)


def link_hash(
    prev_hash: str,
    kind: str,
    identity: str,
    content_hash: str,
    recorded_at: str | None = None,
    root: str | None = None,
) -> str:
    """The schema-2 link, or with ``recorded_at`` and ``root`` the schema-3 link (ADR-0005)."""
    # Hashes are fixed-width hex and kinds and timestamps contain no "|", so the preimage is
    # unambiguous.
    preimage = f"{prev_hash}|{kind}|{identity}|{content_hash}"
    if recorded_at is not None or root is not None:
        preimage += f"|{recorded_at}|{root}"
    return hashlib.sha256(preimage.encode()).hexdigest()


def new_nonce() -> str:
    """128 random bits, the identity of a new or upgraded ledger."""
    return secrets.token_hex(16)


def identity_root(fields: dict) -> str:
    return hashlib.sha256(
        canonical({name: fields[name] for name in IDENTITY_FIELDS}).encode()
    ).hexdigest()


def legacy_recorded_at(rows: list[tuple[str, str, str]]) -> str:
    """SHA-256 over ``[[kind, id, recorded_at], ...]`` of the pre-upgrade chain, in seq order."""
    return hashlib.sha256(canonical([list(row) for row in rows]).encode()).hexdigest()


class Ledger:
    def __init__(
        self,
        path: str | Path,
        *,
        readonly: bool = False,
        create: bool = True,
        clock: Clock | None = None,
    ):
        self.path = str(path)
        self.clock = clock
        self.readonly = readonly
        memory = self.path == ":memory:"
        if memory and (readonly or not create):
            raise ValueError(
                "An in-memory ledger is always new; readonly and create=False need a file"
            )
        if not memory and (readonly or not create) and not Path(path).is_file():
            raise ValueError(f"No ledger at {path}; run init first")
        if readonly or not create:
            # URI modes stop SQLite from creating a file that vanished after the check above.
            mode = "ro" if readonly else "rw"
            target, uri = f"{Path(path).resolve().as_uri()}?mode={mode}", True
        else:
            if not memory:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            target, uri = self.path, False
        self.db = sqlite3.connect(target, uri=uri, timeout=10, isolation_level=None)
        self.db.create_function(LINK_FUNCTION, 6, _v3_link, deterministic=True)
        try:
            self.version = self._open(create)
        except BaseException:
            self.db.close()
            raise

    def _open(self, create: bool) -> int:
        version = self._user_version()
        if version not in (0, 1, CHAINED_VERSION, SCHEMA_VERSION):
            raise ValueError(f"Unsupported ledger schema {version}")
        if (self.readonly or not create) and not self._has_table("records"):
            raise ValueError(f"No ledger at {self.path}; run init first")
        if self.readonly:
            self.db.execute("PRAGMA query_only=ON")
            return version
        self.db.execute("PRAGMA journal_mode=WAL")
        if version < SCHEMA_VERSION:
            self._migrate()
        elif not self._has_trigger("chain_links_v3") and self._has_table("ledger_identity"):
            with self._transaction():  # a schema-3 ledger written before the trigger existed
                self.db.execute(_LINK_TRIGGER)
        self._index_knowledge()
        return SCHEMA_VERSION

    def _index_knowledge(self) -> None:
        """Create the knowledge index and backfill rows written before it existed."""
        if self._has_table("knowledge") and not self._unindexed():
            return  # the common case skips the write lock
        with self._transaction():
            for statement in _KNOWLEDGE_SCHEMA:
                self.db.execute(statement)
            missing = self.db.execute(
                "SELECT r.kind, r.id, r.payload, r.recorded_at FROM records r "
                "WHERE NOT EXISTS (SELECT 1 FROM knowledge k WHERE k.kind=r.kind AND k.id=r.id)"
            ).fetchall()
            for kind, identity, encoded, recorded_at in missing:
                known = knowledge_time(kind, json.loads(encoded), recorded_at)
                self._index(kind, identity, known)

    def _unindexed(self) -> bool:
        query = (
            "SELECT 1 FROM records r WHERE NOT EXISTS "
            "(SELECT 1 FROM knowledge k WHERE k.kind=r.kind AND k.id=r.id) LIMIT 1"
        )
        return self.db.execute(query).fetchone() is not None

    def _index(self, kind: str, identity: str, known: Instant) -> None:
        self.db.execute(
            "INSERT INTO knowledge (kind, id, knowledge_time) VALUES (?, ?, ?)",
            (kind, identity, known.iso()),
        )

    def now(self) -> str:
        """The injected clock's reading; without one, the system clock (``utc_now``)."""
        return utc_now() if self.clock is None else self.clock.now().iso()

    def _migrate(self) -> None:
        with self._transaction():
            # Another process may have upgraded the file while this one waited for the lock.
            version = self._user_version()
            if version == SCHEMA_VERSION:
                return
            if version not in (0, 1, CHAINED_VERSION):
                raise ValueError(f"Unsupported ledger schema {version}")
            if version < CHAINED_VERSION:
                for statement in _SCHEMA:
                    self.db.execute(statement)
                rows = self.db.execute(
                    "SELECT kind, id, content_hash FROM records ORDER BY recorded_at, kind, id"
                ).fetchall()
                head = GENESIS
                for kind, identity, content_hash in rows:
                    head = self._link(kind, identity, content_hash, head)
            self._create_identity(version)
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _create_identity(self, migrated_from: int) -> None:
        """Write the schema-3 creation record over the chain as it stands (ADR-0005)."""
        for statement in _IDENTITY_SCHEMA:
            self.db.execute(statement)
        seq, head = self._chain_head()
        legacy = self.db.execute(
            "SELECT c.kind, c.id, r.recorded_at FROM chain c "
            "JOIN records r ON r.kind=c.kind AND r.id=c.id ORDER BY c.seq"
        ).fetchall()
        fields = {
            "nonce": new_nonce(),
            "created_at": self.now(),
            "migrated_from": migrated_from,
            "legacy_seq": seq,
            "legacy_head": head,
            "legacy_recorded_at": legacy_recorded_at(legacy),
        }
        self.db.execute(
            "INSERT INTO ledger_identity VALUES (?, ?, ?, ?, ?, ?, ?)",
            (*(fields[name] for name in IDENTITY_FIELDS), identity_root(fields)),
        )
        self.db.execute(_LINK_TRIGGER)

    def _identity_rows(self) -> list[dict]:
        if not self._has_table("ledger_identity"):
            return []
        columns = (*IDENTITY_FIELDS, "root")
        query = f"SELECT {', '.join(columns)} FROM ledger_identity ORDER BY rowid"
        return [dict(zip(columns, row, strict=True)) for row in self.db.execute(query)]

    def identity(self) -> str | None:
        """The ledger's nonce (ADR-0005); None before schema 3 or when the row is missing."""
        if self.version < SCHEMA_VERSION:
            return None
        rows = self._identity_rows()
        return rows[0]["nonce"] if rows else None

    def _root(self) -> str | None:
        rows = self._identity_rows() if self.version >= SCHEMA_VERSION else []
        return rows[0]["root"] if rows else None

    def _chain_head(self) -> tuple[int, str]:
        query = "SELECT seq, chain_hash FROM chain ORDER BY seq DESC LIMIT 1"
        found = self.db.execute(query).fetchone()
        return (0, GENESIS) if found is None else (found[0], found[1])

    def _user_version(self) -> int:
        return self.db.execute("PRAGMA user_version").fetchone()[0]

    def _has_trigger(self, name: str) -> bool:
        query = "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?"
        return self.db.execute(query, (name,)).fetchone() is not None

    def _has_table(self, name: str) -> bool:
        query = "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?"
        return self.db.execute(query, (name,)).fetchone() is not None

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self.readonly:
            raise ValueError(f"Ledger {self.path} is open read-only")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def _link(
        self,
        kind: str,
        identity: str,
        content_hash: str,
        prev_hash: str,
        recorded_at: str | None = None,
        root: str | None = None,
    ) -> str:
        if root is None:
            result = link_hash(prev_hash, kind, identity, content_hash)
        else:
            result = link_hash(prev_hash, kind, identity, content_hash, recorded_at, root)
        self.db.execute(
            "INSERT INTO chain (kind, id, content_hash, prev_hash, chain_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, identity, content_hash, prev_hash, result),
        )
        return result

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def put(self, kind: str, identity: str, payload: dict) -> bool:
        def identical(existing: dict) -> None:
            if canonical(existing) != canonical(payload):
                raise ValueError(f"Immutable record conflict: {kind}/{identity}")

        return self._append(kind, identity, payload, identical)

    def _append(
        self, kind: str, identity: str, payload: dict, check_existing: Callable[[dict], None]
    ) -> bool:
        """Check and insert under one write lock, so concurrent writers cannot both insert."""
        if kind not in KINDS or not isinstance(identity, str) or not identity:
            raise ValueError("Invalid ledger record kind or id")
        encoded = canonical(payload)
        content_hash = hashlib.sha256(encoded.encode()).hexdigest()
        with self._transaction():
            # Another process may have changed the schema since this handle opened the file.
            if self._user_version() != self.version:
                raise ValueError(
                    f"Ledger schema changed to {self._user_version()} while open; reopen it"
                )
            existing = self.get(kind, identity)
            if existing is not None:
                check_existing(existing)
                return False
            recorded_at = self.now()
            self.db.execute(
                "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
                (kind, identity, encoded, content_hash, recorded_at),
            )
            self._index(kind, identity, knowledge_time(kind, payload, recorded_at))
            root = self._root()
            if root is None:
                raise ValueError("Ledger identity is missing; run verify")
            prev = self.head()["chain_hash"]
            self._link(kind, identity, content_hash, prev, recorded_at, root)
        return True

    def head(self) -> dict:
        """The latest chain entry; publishing it lets anyone later detect a rewritten history."""
        if self.version < CHAINED_VERSION:
            raise ValueError(self._unchained())
        seq, chain_hash = self._chain_head()
        return {"seq": seq, "chain_hash": chain_hash}

    def anchor(self) -> dict:
        """head() plus the ledger's identity and root (ADR-0005 §5): the value to publish.

        verify(anchor=...) then checks the root too, which a nonce alone cannot pin down."""
        result = self.head()
        found = self._identity_rows() if self.version >= SCHEMA_VERSION else []
        if found:
            result |= {"identity": found[0]["nonce"], "root": identity_root(found[0])}
        return result

    def _unchained(self) -> str:
        return (
            f"Ledger schema {self.version} has no hash chain; "
            "open it once without readonly to upgrade"
        )

    def verify(self, *, anchor: dict | None = None) -> dict:
        """Recompute every record hash and the whole chain; `anchor` is an earlier head()."""
        if anchor is not None and (
            not isinstance(anchor, dict)
            or type(anchor.get("seq")) is not int
            or anchor["seq"] < 0
            or not isinstance(anchor.get("chain_hash"), str)
            or not isinstance(anchor.get("identity", ""), str)
            or not isinstance(anchor.get("root", ""), str)
        ):
            raise ValueError(
                "Anchor must be a head() or anchor() result: {'seq': int, 'chain_hash': hex}, "
                "optionally with 'identity': str and 'root': hex"
            )
        # One read transaction: a writer committing mid-scan (the service writes on every
        # poll) must not look like a chain entry without its record.
        snapshot = not self.db.in_transaction
        if snapshot:
            self.db.execute("BEGIN")
        try:
            return self._verify(anchor)
        finally:
            if snapshot and self.db.in_transaction:
                self.db.execute("COMMIT")

    def _verify(self, anchor: dict | None) -> dict:
        problems: list[str] = []
        recorded, stamps = self._verify_records(problems)
        if self.version < CHAINED_VERSION or not self._has_table("chain"):
            problems.append(
                self._unchained() if self.version < CHAINED_VERSION else "Chain table is missing"
            )
            return _report(len(recorded), 0, GENESIS, problems)
        present = {
            name
            for (name,) in self.db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        expected = TRIGGERS | IDENTITY_TRIGGERS if self.version >= SCHEMA_VERSION else TRIGGERS
        problems.extend(
            f"Immutability trigger is missing: {name}" for name in sorted(expected - present)
        )
        found = self._verify_identity(problems) if self.version >= SCHEMA_VERSION else None
        nonce = None if found is None else found["nonce"]
        length, head = self._verify_chain(recorded, stamps, found, problems)
        if anchor is not None:
            self._verify_anchor(anchor, found, problems)
        return _report(len(recorded), length, head, problems, nonce)

    def _verify_identity(self, problems: list[str]) -> dict | None:
        """Check the schema-3 creation record (ADR-0005); return it, or None when missing."""
        rows = self._identity_rows()
        if not rows:
            problems.append("Ledger identity is missing")
            return None
        if len(rows) > 1:
            problems.append(f"Ledger identity must be one row, found {len(rows)}")
        found = rows[0]
        if identity_root(found) != found["root"]:
            problems.append("Ledger identity does not match its root")
        seq = found["legacy_seq"]
        row = self.db.execute("SELECT chain_hash FROM chain WHERE seq=?", (seq,)).fetchone()
        if (GENESIS if seq == 0 else row and row[0]) != found["legacy_head"]:
            problems.append(f"Ledger identity does not match the chain head at seq {seq}")
        legacy = self.db.execute(
            "SELECT c.kind, c.id, r.recorded_at FROM chain c "
            "JOIN records r ON r.kind=c.kind AND r.id=c.id WHERE c.seq<=? ORDER BY c.seq",
            (seq,),
        ).fetchall()
        if legacy_recorded_at(legacy) != found["legacy_recorded_at"]:
            problems.append("Legacy recorded_at digest does not match the ledger identity")
        return found

    def _verify_records(
        self, problems: list[str]
    ) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
        recorded, stamps = {}, {}
        rows = self.db.execute(
            "SELECT kind, id, payload, content_hash, recorded_at FROM records ORDER BY kind, id"
        )
        for kind, identity, encoded, content_hash, recorded_at in rows:
            recorded[(kind, identity)] = content_hash
            stamps[(kind, identity)] = recorded_at
            if not _intact(encoded, content_hash):
                problems.append(f"Record content does not match its hash: {kind}/{identity}")
        return recorded, stamps

    def _verify_chain(
        self,
        recorded: dict[tuple[str, str], str],
        stamps: dict[tuple[str, str], str],
        found: dict | None,
        problems: list[str],
    ) -> tuple[int, str]:
        prev, last, length, chained = GENESIS, 0, 0, set()
        # Schema 2 links up to the creation record; schema-3 links after it (ADR-0005). In a
        # schema-3 ledger without its identity every link is held to the schema-3 formula.
        if self.version < SCHEMA_VERSION:
            legacy_seq, root = None, ""
        else:
            legacy_seq = 0 if found is None else found["legacy_seq"]
            root = "" if found is None else found["root"]
        rows = self.db.execute(
            "SELECT seq, kind, id, content_hash, prev_hash, chain_hash FROM chain ORDER BY seq"
        )
        for seq, kind, identity, content_hash, prev_hash, stored in rows:
            where = f"chain seq {seq} ({kind}/{identity})"
            if prev_hash != prev:
                problems.append(f"Chain link is broken at {where}")
            stamp = stamps.get((kind, identity))
            if legacy_seq is None or seq <= legacy_seq:
                expected = link_hash(prev_hash, kind, identity, content_hash)
            elif stamp is None:  # no record to take recorded_at from; reported below
                expected = stored
            else:
                expected = link_hash(prev_hash, kind, identity, content_hash, stamp, root)
            if stored != expected:
                problems.append(f"Chain hash does not match its contents at {where}")
            if (kind, identity) not in recorded:
                problems.append(f"Chain entry has no record: {where}")
            elif recorded[(kind, identity)] != content_hash:
                problems.append(f"Record hash differs from its chain entry: {kind}/{identity}")
            chained.add((kind, identity))
            prev, last, length = stored, seq, length + 1
        problems.extend(
            f"Record is missing from the chain: {kind}/{identity}"
            for kind, identity in sorted(recorded.keys() - chained)
        )
        # The AUTOINCREMENT counter outlives deleted rows, so it exposes a cut-off tail.
        counter = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='chain'").fetchone()
        if counter and counter[0] > last:
            problems.append(
                f"Chain was truncated: entries reached seq {counter[0]}, head is {last}"
            )
        return length, prev

    def _verify_anchor(self, anchor: dict, found: dict | None, problems: list[str]) -> None:
        seq = anchor["seq"]
        row = self.db.execute("SELECT chain_hash FROM chain WHERE seq=?", (seq,)).fetchone()
        if seq and row is None:
            problems.append(f"Anchored head seq {seq} is not in the chain")
        elif (row[0] if seq else GENESIS) != anchor["chain_hash"]:
            problems.append(f"Chain differs from the anchored head at seq {seq}")
        if "identity" in anchor and anchor["identity"] != (found and found["nonce"]):
            problems.append("Ledger identity differs from the anchored identity")
        # The nonce is public once an anchor is published; the root also commits to the
        # creation time and the legacy prefix, so a forged identity row cannot match it.
        if "root" in anchor and anchor["root"] != (found and identity_root(found)):
            problems.append("Ledger root differs from the anchored root")

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

    def sequence(self, kind: str, identity: str) -> int | None:
        """The record's position in the hash chain: which of two records was appended first."""
        row = self.db.execute(
            "SELECT seq FROM chain WHERE kind=? AND id=?", (kind, identity)
        ).fetchone()
        return None if row is None else int(row[0])

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

    def as_of(self, kind: str, t: Instant | str, *, prefix: str | None = None) -> list[dict]:
        """Records of ``kind`` whose knowledge time is at or before ``t``, ordered by id.

        Reads through the ``knowledge_by_time`` index; ``prefix`` narrows ids as ``prefix()``
        does. Each returned row is hash-checked and its knowledge time recomputed, so the
        result never holds a row the ledger learned after ``t`` (ADR-0004)."""
        if kind not in KINDS:
            raise ValueError(f"Invalid ledger record kind: {kind}")
        boundary = Instant.coerce(t)
        low, high = ("", "\uffff") if prefix is None else (prefix, prefix + "\uffff")
        columns = "r.id, r.payload, r.content_hash, r.recorded_at"
        # The second column says whether the row came through the index (1) or, lacking an
        # index row, from a scan (0) that as_of filters here.
        unindexed = f"SELECT {columns}, 0 FROM records r WHERE r.kind=? AND r.id>=? AND r.id<?"
        if self._has_table("knowledge"):
            query = (
                f"SELECT {columns}, 1 FROM knowledge k "
                "JOIN records r ON r.kind=k.kind AND r.id=k.id "
                "WHERE k.kind=? AND k.knowledge_time<=? AND k.id>=? AND k.id<? "
                f"UNION ALL {unindexed} AND NOT EXISTS "
                "(SELECT 1 FROM knowledge k WHERE k.kind=r.kind AND k.id=r.id) ORDER BY 1"
            )
            params: tuple = (kind, boundary.iso(), low, high, kind, low, high)
        else:
            query, params = f"{unindexed} ORDER BY 1", (kind, low, high)
        result = []
        for identity, encoded, fingerprint, recorded_at, indexed in self.db.execute(query, params):
            value = json.loads(encoded)
            if digest(value) != fingerprint:
                raise ValueError(f"Corrupted record in {kind}")
            if knowledge_time(kind, value, recorded_at) <= boundary:
                result.append(value)
            elif indexed:
                raise ValueError(f"Knowledge index disagrees with its record: {kind}/{identity}")
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
        if event["mode"] == "forward" and instant(event["first_seen_at"]) > instant(self.now()):
            raise ValueError("Forward observation cannot be in the future")

        def unchanged(prior: dict) -> None:
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

        return self._append("disclosures", event["id"], event, unchanged)


def _v3_link(
    prev_hash: str, kind: str, identity: str, content_hash: str, recorded_at: str, root: str
) -> str:
    return link_hash(prev_hash, kind, identity, content_hash, recorded_at, root)


def _intact(encoded: str, content_hash: str) -> bool:
    try:
        value = json.loads(encoded)
        return canonical(value) == encoded and digest(value) == content_hash
    except (TypeError, ValueError):
        return False


def _report(
    records: int, length: int, head: str, problems: list[str], identity: str | None = None
) -> dict:
    if len(problems) > MAX_PROBLEMS:
        hidden = len(problems) - MAX_PROBLEMS
        problems = [*problems[:MAX_PROBLEMS], f"... and {hidden} more problems"]
    return {
        "ok": not problems,
        "records": records,
        "chain_length": length,
        "head": head,
        "identity": identity,
        "problems": problems,
    }
