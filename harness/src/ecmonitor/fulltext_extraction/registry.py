"""Versioned chemical entity and alias registry."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from ecmonitor.fulltext_extraction.models import ChemicalMatch, ChemicalResolution


def normalize_alias(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return " ".join(normalized.split())


class ChemicalRegistry:
    """Validated aliases are readable by later documents; proposals are not."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS registry_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    registry_version INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO registry_meta(singleton, registry_version) VALUES (1, 0);

                CREATE TABLE IF NOT EXISTS chemical_entities (
                    entity_id TEXT PRIMARY KEY,
                    canonical_name TEXT NOT NULL,
                    pubchem_cid TEXT,
                    inchi TEXT,
                    inchikey TEXT,
                    canonical_smiles TEXT,
                    molecular_formula TEXT,
                    created_version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chemical_aliases (
                    alias_id TEXT PRIMARY KEY,
                    alias_text TEXT NOT NULL,
                    alias_normalized TEXT NOT NULL,
                    alias_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL REFERENCES chemical_entities(entity_id),
                    status TEXT NOT NULL CHECK(status IN ('validated','deprecated','conflicted')),
                    created_version INTEGER NOT NULL,
                    retired_version INTEGER,
                    validation_provenance TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_alias_lookup
                    ON chemical_aliases(alias_normalized, status);

                CREATE TABLE IF NOT EXISTS resolution_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    document_session_id TEXT NOT NULL,
                    raw_name TEXT NOT NULL,
                    alias_normalized TEXT NOT NULL,
                    resolver_name TEXT NOT NULL,
                    resolution_status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    proposal_status TEXT NOT NULL CHECK(
                        proposal_status IN ('proposed','promoted','rejected','conflicted')
                    ),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    decided_at TEXT,
                    decision_actor TEXT
                );

                CREATE TABLE IF NOT EXISTS document_local_aliases (
                    alias_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    doi TEXT,
                    alias_text TEXT NOT NULL,
                    alias_normalized TEXT NOT NULL,
                    canonical_name TEXT NOT NULL,
                    identity_status TEXT NOT NULL,
                    page INTEGER,
                    evidence_quote TEXT NOT NULL,
                    source_asset_sha256 TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(document_id, alias_normalized, canonical_name)
                );
                CREATE INDEX IF NOT EXISTS idx_document_local_alias_lookup
                    ON document_local_aliases(document_id, alias_normalized);
                """
            )

    def version(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "SELECT registry_version FROM registry_meta WHERE singleton = 1"
                ).fetchone()[0]
            )

    def record_document_local_alias(
        self,
        *,
        document_id: str,
        alias_text: str,
        canonical_name: str,
        identity_status: str,
        evidence_quote: str,
        doi: str | None = None,
        page: int | None = None,
        source_asset_sha256: str | None = None,
        identifiers: dict[str, Any] | None = None,
    ) -> str:
        """Persist an auditable paper-local definition without promoting it globally."""
        if not evidence_quote.strip():
            raise ValueError("document-local alias requires a source quote")
        alias_id = f"doc-alias-{uuid.uuid4()}"
        payload = dict(identifiers or {})
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO document_local_aliases(
                    alias_id, document_id, doi, alias_text, alias_normalized, canonical_name,
                    identity_status, page, evidence_quote, source_asset_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    alias_id, document_id, doi, alias_text, normalize_alias(alias_text),
                    canonical_name, identity_status, page, evidence_quote,
                    source_asset_sha256, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
        return alias_id

    def lookup_document_local(
        self, raw_name: str, *, document_id: str
    ) -> ChemicalResolution | None:
        """Resolve only within the current paper; never leaks the alias to another DOI."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM document_local_aliases
                WHERE document_id = ? AND alias_normalized = ?
                ORDER BY created_at DESC""",
                (document_id, normalize_alias(raw_name)),
            ).fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        payload = json.loads(str(row["payload_json"]))
        match = ChemicalMatch(
            source="document_local",
            source_record_id=str(row["alias_id"]),
            canonical_name=str(row["canonical_name"]),
            matched_alias=str(row["alias_text"]),
            pubchem_cid=str(payload["pubchem_cid"]) if payload.get("pubchem_cid") else None,
            cas_candidates=tuple(payload.get("cas_candidates") or ()),
            inchi=payload.get("inchi"),
            inchikey=payload.get("inchikey"),
            canonical_smiles=payload.get("canonical_smiles"),
            molecular_formula=payload.get("molecular_formula"),
            raw_payload={
                "doi": row["doi"], "page": row["page"],
                "evidence_quote": row["evidence_quote"],
                "source_asset_sha256": row["source_asset_sha256"],
                "identity_status": row["identity_status"],
            },
        )
        return ChemicalResolution(
            raw_name=raw_name, normalized_query=normalize_alias(raw_name),
            status="validated_local", resolver_name="ecmonitor_document_local_registry",
            matches=(match,), warnings=(), registry_snapshot_version=self.version(),
        )
    def lookup_validated(
        self, raw_name: str, *, at_version: int | None = None
    ) -> ChemicalResolution | None:
        alias_normalized = normalize_alias(raw_name)
        with self._connect() as connection:
            current_version = int(
                connection.execute(
                    "SELECT registry_version FROM registry_meta WHERE singleton = 1"
                ).fetchone()[0]
            )
            version = current_version if at_version is None else at_version
            if version < 0 or version > current_version:
                raise ValueError(
                    f"registry snapshot version {version} is outside 0..{current_version}"
                )
            rows = connection.execute(
                """
                SELECT a.alias_text, e.*
                FROM chemical_aliases a
                JOIN chemical_entities e ON e.entity_id = a.entity_id
                WHERE a.alias_normalized = ? AND a.status = 'validated'
                  AND a.created_version <= ?
                  AND (a.retired_version IS NULL OR a.retired_version > ?)
                  AND e.created_version <= ?
                ORDER BY e.entity_id
                """,
                (alias_normalized, version, version, version),
            ).fetchall()
        if not rows:
            return None
        matches = tuple(
            ChemicalMatch(
                source="ECMonitorChemicalRegistry",
                source_record_id=str(row["entity_id"]),
                canonical_name=str(row["canonical_name"]),
                matched_alias=str(row["alias_text"]),
                pubchem_cid=row["pubchem_cid"],
                inchi=row["inchi"],
                inchikey=row["inchikey"],
                canonical_smiles=row["canonical_smiles"],
                molecular_formula=row["molecular_formula"],
            )
            for row in rows
        )
        return ChemicalResolution(
            raw_name=raw_name,
            normalized_query=alias_normalized,
            status="validated_local" if len(matches) == 1 else "ambiguous",
            resolver_name="ecmonitor_chemical_registry",
            matches=matches,
            registry_snapshot_version=version,
            warnings=() if len(matches) == 1 else ("validated_alias_conflict",),
        )

    def record_proposal(
        self,
        *,
        document_id: str,
        document_session_id: str,
        resolution: ChemicalResolution,
    ) -> str:
        proposal_id = f"proposal-{uuid.uuid4()}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO resolution_proposals(
                    proposal_id, document_id, document_session_id, raw_name,
                    alias_normalized, resolver_name, resolution_status, payload_json,
                    proposal_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed')
                """,
                (
                    proposal_id,
                    document_id,
                    document_session_id,
                    resolution.raw_name,
                    normalize_alias(resolution.raw_name),
                    resolution.resolver_name,
                    resolution.status,
                    json.dumps(resolution.to_dict(), ensure_ascii=True, sort_keys=True),
                ),
            )
        return proposal_id

    def promote_proposal(
        self,
        proposal_id: str,
        *,
        actor: str,
        alias_type: str,
        is_individual_chemical: bool,
        selected_source_record_id: str | None = None,
    ) -> int:
        if not is_individual_chemical:
            raise ValueError("non-individual concepts cannot be promoted as chemical aliases")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            proposal = connection.execute(
                "SELECT * FROM resolution_proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise KeyError(proposal_id)
            if proposal["proposal_status"] != "proposed":
                raise ValueError(f"proposal is already {proposal['proposal_status']}")
            payload = json.loads(str(proposal["payload_json"]))
            matches = payload.get("matches", [])
            if selected_source_record_id is not None:
                matches = [
                    item
                    for item in matches
                    if str(item.get("source_record_id")) == selected_source_record_id
                ]
            if len(matches) != 1:
                raise ValueError("promotion requires exactly one selected chemical identity")
            match = matches[0]
            current_version = int(
                connection.execute(
                    "SELECT registry_version FROM registry_meta WHERE singleton = 1"
                ).fetchone()[0]
            )
            new_version = current_version + 1
            entity_id = _entity_id(match)
            connection.execute(
                """
                INSERT OR IGNORE INTO chemical_entities(
                    entity_id, canonical_name, pubchem_cid, inchi, inchikey,
                    canonical_smiles, molecular_formula, created_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity_id,
                    match["canonical_name"],
                    match.get("pubchem_cid"),
                    match.get("inchi"),
                    match.get("inchikey"),
                    match.get("canonical_smiles"),
                    match.get("molecular_formula"),
                    new_version,
                ),
            )
            alias_normalized = str(proposal["alias_normalized"])
            conflicts = connection.execute(
                """
                SELECT DISTINCT entity_id FROM chemical_aliases
                WHERE alias_normalized = ? AND status = 'validated' AND retired_version IS NULL
                """,
                (alias_normalized,),
            ).fetchall()
            if any(str(row["entity_id"]) != entity_id for row in conflicts):
                raise ValueError("alias conflicts with an existing validated chemical")
            existing = connection.execute(
                """
                SELECT alias_id FROM chemical_aliases
                WHERE alias_normalized = ? AND entity_id = ? AND status = 'validated'
                  AND retired_version IS NULL
                """,
                (alias_normalized, entity_id),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO chemical_aliases(
                        alias_id, alias_text, alias_normalized, alias_type, entity_id,
                        status, created_version, validation_provenance
                    ) VALUES (?, ?, ?, ?, ?, 'validated', ?, ?)
                    """,
                    (
                        f"alias-{uuid.uuid4()}",
                        proposal["raw_name"],
                        alias_normalized,
                        alias_type,
                        entity_id,
                        new_version,
                        json.dumps({"proposal_id": proposal_id, "actor": actor}, sort_keys=True),
                    ),
                )
            connection.execute(
                "UPDATE registry_meta SET registry_version = ? WHERE singleton = 1", (new_version,)
            )
            connection.execute(
                """
                UPDATE resolution_proposals
                SET proposal_status = 'promoted', decided_at = CURRENT_TIMESTAMP,
                    decision_actor = ?
                WHERE proposal_id = ?
                """,
                (actor, proposal_id),
            )
            connection.commit()
            return new_version


def _entity_id(match: dict[str, Any]) -> str:
    if match.get("inchikey"):
        return f"inchikey:{match['inchikey']}"
    if match.get("pubchem_cid"):
        return f"pubchem:{match['pubchem_cid']}"
    raise ValueError("promotion requires an InChIKey or PubChem CID")
