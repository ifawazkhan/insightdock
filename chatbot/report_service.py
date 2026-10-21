"""Independent session-local report runner. Never uses ChatService.pending."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import re
from threading import Lock
from time import monotonic

import sqlglot
from sqlglot import exp

from chatbot.config import Settings, identifier
from chatbot.db import Database, DatabaseError, QueryResult, Schema
from chatbot.reports import (BusinessReport, ReportError, ReportEvidence, ReportQuery, ReportSource,
                             build_report, evidence_time, utc_now, validate_queries)
from chatbot.sql_policy import PolicyError, ValidatedSQL, validate_sql


class ReportService:
    MAX_RUN_IDS = 64
    BUDGET_SECONDS = 60.0

    def __init__(self, settings: Settings, database: Database, scope_table: str | None = None):
        self.settings = settings
        self.database = database
        self.scope_table = identifier(scope_table) if scope_table is not None else (
            None if settings.schema_scope else settings.table)
        self._runs: dict[str, tuple[str, ...]] = {}
        self._lock = Lock()

    @property
    def scope(self) -> tuple[str, str | None]:
        return self.settings.database, self.scope_table

    @property
    def scope_label(self) -> str:
        return self.settings.database + (f".{self.scope_table}" if self.scope_table else "")

    def scoped_schema(self, schema: Schema) -> Schema:
        if schema.database != self.settings.database:
            raise ReportError("Discovery returned a different database scope.")
        if self.scope_table is not None:
            if schema.tables is None:
                if schema.table != self.scope_table:
                    raise ReportError("The selected table is no longer available.")
            else:
                tables = tuple({**t, "foreign_keys": tuple(f for f in t.get("foreign_keys", ())
                               if f.get("referred_table") == self.scope_table
                               and f.get("referred_schema") in (None, schema.database))}
                               for t in schema.tables if t["name"] == self.scope_table)
                if not tables:
                    raise ReportError("The selected table is no longer available.")
                schema = replace(schema, table=self.scope_table, tables=tables)
        if not schema.exists:
            raise ReportError("No base tables are available in the selected scope.")
        return schema

    def _validate(self, query: ReportQuery, tables: tuple[str, ...]) -> ValidatedSQL:
        validated = validate_sql(query.sql, self.settings.database, self.scope_table or self.settings.table,
                                 allowed_tables=tables)
        if not validated.read_only:
            raise PolicyError("Reports accept read-only SELECT statements only; writes are never executed.")
        return validated

    def _copy_result(self, result: object) -> QueryResult | None:
        if result is None:
            return None
        if not isinstance(result, QueryResult) or not result.columns or any(
                len(row) != len(result.columns) for row in result.rows):
            raise ReportError("Report evidence requires a well-formed retained read result.")
        return QueryResult(deepcopy(result.columns), deepcopy(result.rows[:self.settings.max_rows]), 0,
                           result.truncated or len(result.rows) > self.settings.max_rows, result.elapsed_seconds)

    def snapshot(self, title: str, messages: list[dict]) -> BusinessReport:
        """Use provenanced successful reads only. No DB or schema discovery calls."""
        selected = [m for m in messages if m.get("read_only") is True and not m.get("error")]
        queries = validate_queries([ReportQuery(m.get("query_id"), m.get("question"), m.get("sql")) for m in selected])
        evidence = []
        for message, query in zip(selected, queries):
            if message.get("scope") not in (self.scope, self.scope_label):
                raise ReportError("Snapshot evidence must have the exact selected scope.")
            # History is not current metadata. Resolve AST references locally and
            # reuse the unchanged policy to reject unsafe/cross-database SQL.
            try:
                ast = sqlglot.parse_one(query.sql, read="mysql")
                tables = (self.scope_table,) if self.scope_table else tuple(t.name for t in ast.find_all(exp.Table))
            except sqlglot.errors.SqlglotError as exc:
                raise ReportError("Invalid retained SQL provenance.") from exc
            validated = self._validate(query, tables)
            result = self._copy_result(message.get("result"))
            evidence.append(ReportEvidence(query.id, query.question, validated.sql, result,
                                           evidence_time(message.get("executed_at")), "snapshot",
                                           "Retained result is unavailable; choose an explicit live refresh." if result is None else ""))
        return self.build_report(title, tuple(evidence), "snapshot")

    def build_report(self, title: str, evidence: tuple[ReportEvidence, ...],
                     source: ReportSource = "snapshot") -> BusinessReport:
        return build_report(title, evidence, scope=self.scope, source=source)

    def live(self, title: str, queries: list[ReportQuery] | tuple[ReportQuery, ...], run_id: str) -> BusinessReport:
        """Validate the ENTIRE plan before data execution. No retries, one-use IDs.

        Budget includes metadata discovery and execution. An in-flight blocking
        driver call cannot be interrupted here; the budget prevents further reads
        and records overruns. Existing database timeouts still apply.
        Schema drift aborts the report (raises ReportError), even after a read.
        Runtime DB errors retain a clearly marked partial section and continue.
        """
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise ReportError("Use a nonempty bounded report run ID.")
        with self._lock:
            if run_id in self._runs:
                raise ReportError("This report run ID was already consumed; no retry was executed.")
            if len(self._runs) >= self.MAX_RUN_IDS:
                raise ReportError("Report run ID capacity reached; start a new session service.")
            self._runs[run_id] = ()  # consume even failed validation/discovery
        start = monotonic()
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ReportError("Use a nonempty report title of at most 200 characters.")
        queries = validate_queries(queries)
        scope = self.scope
        schema = self.scoped_schema(self.database.discover_schema())
        validated = tuple(self._validate(query, schema.table_names) for query in queries)
        fingerprints = tuple(hashlib.sha256((schema.fingerprint + q.digest).encode()).hexdigest() for q in validated)
        self._runs[run_id] = fingerprints
        evidence: list[ReportEvidence] = []
        over_budget = False
        for query, checked, fingerprint in zip(queries, validated, fingerprints):
            error = ""
            result = None
            if monotonic() - start >= self.BUDGET_SECONDS:
                error = "Not executed: total report time budget exhausted."
            else:
                if self.scope != scope:
                    raise ReportError("Report scope changed; remaining queries were aborted.")
                try:
                    fresh = self.scoped_schema(self.database.discover_schema())
                except DatabaseError:
                    fresh = None
                    error = "Schema verification failed; this section was not executed."
                if fresh is not None:
                    if fresh.fingerprint != schema.fingerprint:
                        raise ReportError("Schema drift detected; report aborted. Review a new plan.")
                    rechecked = self._validate(query, fresh.table_names)
                    if hashlib.sha256((fresh.fingerprint + rechecked.digest).encode()).hexdigest() != fingerprint:
                        raise ReportError("Query fingerprint changed; report aborted.")
                    if self.scope != scope:
                        raise ReportError("Report scope changed; report aborted.")
                    if monotonic() - start >= self.BUDGET_SECONDS:
                        error = "Not executed: total report time budget exhausted during schema verification."
                    else:
                        try:
                            result = self._copy_result(self.database.execute(checked, fresh))
                        except DatabaseError:
                            # Never expose provider/connection text, even from a custom DB adapter.
                            error = "Database read failed; evidence unavailable. No retry was attempted."
            over_budget = over_budget or monotonic() - start >= self.BUDGET_SECONDS
            evidence.append(ReportEvidence(query.id, query.question, checked.sql, result, utc_now(), "live", error))
        if self.scope != scope:
            raise ReportError("Report scope changed; report aborted.")
        # Verify after the last in-flight read as well. Never claim stable schema
        # when the final metadata check failed; avoid discovery beyond the budget.
        final_verified = False
        if not over_budget:
            try:
                final = self.scoped_schema(self.database.discover_schema())
            except DatabaseError:
                raise ReportError("Final schema verification failed; report aborted.") from None
            if self.scope != scope or final.fingerprint != schema.fingerprint:
                raise ReportError("Schema drift or scope change detected; report aborted.")
            final_verified = True
            over_budget = monotonic() - start >= self.BUDGET_SECONDS
        report = self.build_report(title, tuple(evidence), "live")
        if over_budget:
            report = replace(report, limitations=report.limitations + (
                "Total report time budget exhausted. In-flight calls cannot be interrupted; no further queries were started. "
                + ("Final schema verification completed after the budget." if final_verified
                   else "Final schema verification was unavailable."),))
        return report