"""Session-local proposals and one-use confirmation independent of Streamlit reruns."""

from dataclasses import dataclass, replace
import secrets

from chatbot.config import Settings
from chatbot.db import Database, QueryResult, Schema
from chatbot.sql_policy import PolicyError, ValidatedSQL, validate_sql


@dataclass(frozen=True)
class Proposal:
    query: ValidatedSQL
    intent: str
    schema_fingerprint: str
    token: str


class ChatService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.pending: Proposal | None = None
        self.scope_table: str | None = None

    @property
    def history_key(self) -> str | tuple[str, str]:
        return (self.settings.database, self.scope_table) if self.scope_table else self.settings.database

    def scoped_schema(self, schema: Schema) -> Schema:
        """Use the same narrowed metadata for AI, validation and fresh execution checks."""
        if self.scope_table is None:
            return schema
        if schema.tables is None:
            if schema.table != self.scope_table:
                raise PolicyError("The selected table is no longer available.")
            return schema
        tables = tuple({**t, "foreign_keys": tuple(f for f in t.get("foreign_keys", ())
                        if f.get("referred_table") == self.scope_table
                        and f.get("referred_schema") in (None, schema.database))}
                       for t in schema.tables if t["name"] == self.scope_table)
        if not tables:
            raise PolicyError("The selected table is no longer available. Choose another query scope.")
        return replace(schema, table=self.scope_table, tables=tables)

    def cancel(self) -> None:
        self.pending = None

    def propose(self, sql: str, schema: Schema, intent: str = "Direct SQL") -> Proposal:
        self.cancel()
        schema = self.scoped_schema(schema)
        query = validate_sql(sql, self.settings.database, self.settings.table,
                     allowed_tables=schema.table_names if schema.tables is not None else None)
        if self.settings.cloud_mode and not query.read_only:
            raise PolicyError("The cloud chatbot supports read-only queries only.")
        if not query.read_only and self.settings.database.lower() in {"mysql", "information_schema", "performance_schema", "sys"}:
            raise PolicyError("System schemas are read-only in this chatbot.")
        if not query.read_only and not self.settings.allow_mutations:
            raise PolicyError("Writes are disabled. Set CHATBOT_ALLOW_MUTATIONS=true in .env, then restart.")
        if not schema.exists and query.operation != "CREATE":
            raise PolicyError("The table is missing. Only a confirmed CREATE TABLE is available.")
        proposal = Proposal(query, intent, schema.fingerprint, secrets.token_urlsafe(24))
        self.pending = proposal
        return proposal

    @property
    def confirmation_phrase(self) -> str:
        if self.pending and self.pending.query.target_tables:
            return "CONFIRM " + ", ".join(self.pending.query.target_tables)
        return f"CONFIRM {self.settings.table}"

    def execute(self, proposal: Proposal, *, confirmed: bool = False, phrase: str = "") -> QueryResult:
        if self.pending is not proposal or self.pending.query.digest != proposal.query.digest:
            raise PolicyError("This proposal has expired or already been executed.")
        if not proposal.query.read_only:
            if not confirmed:
                raise PolicyError("Every mutation requires explicit confirmation.")
            if proposal.query.destructive and phrase != self.confirmation_phrase:
                raise PolicyError(f"Type {self.confirmation_phrase} exactly to confirm this operation.")
        fresh = self.scoped_schema(self.database.discover_schema())
        if fresh.fingerprint != proposal.schema_fingerprint:
            self.cancel()
            raise PolicyError("The schema changed. Submit the request again and review the new query.")
        # Consume before executing: exceptions, network loss and Streamlit reruns never retry a mutation.
        self.cancel()
        return self.database.execute(proposal.query, fresh)