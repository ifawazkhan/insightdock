# InsightDock · MySQL analytics assistant

Local Streamlit chat for a fixed, operator-configured MySQL source. Ask in natural
language; SQL generation and execution stay in the backend. Existing dataset
generation/loading scripts are unchanged and never run automatically.

## Client interface and appearance

The interface is branded **InsightDock**, with a local database-and-insight mark.
The suggested name is a working product label; check name, domain and trademark
availability before public release. InsightDock is an independent project, not an
official MySQL product.

The sidebar contains only **User**, **New chat** and **Recents**. There are no
schema/table or scope selectors, UI preferences, direct-SQL mode or mode radios.
The native three-dot menu is hidden.

Use the explicit **Dark mode** toggle to switch between client-controlled light
and dark palettes for surfaces, controls, tables and charts. It does not depend
on the native appearance menu, execute queries, clear chats or change the source.

## Visual conversation interface

Questions appear as compact, right-aligned messaging bubbles. Charts are shown **only when useful**. Person/contact lookups, single records and ordinary lists use record tables instead of incidental salary charts. The selector uses actual fetched values, SQL evidence and the question, excludes identifier/phone fields as measures, preserves query order and ties, and discloses truncation, missing values, or chart limits. Charts do not run additional queries or send additional data to AI.

### Analytical chart types

Eligible results offer a **Chart type** selector, with **Auto** as the default. Choices depend on the actual data and proven query shape, not just words in the question:

| Chart family | Supported forms and requirements |
| --- | --- |
| Comparisons | Horizontal/vertical bars; grouped bars for comparable measures; stacked/100% stacked bars for additive components |
| Composition | Pie/donut for complete, nonnegative shares with a defensible denominator; many eligible categories can become top categories plus Other |
| Trends | Line, area and stacked area with suitable dates/periods and measures; missing periods are not silently filled with zero |
| Relationships | Scatter for paired numeric measures; bubble only when a size measure is explicitly requested |
| Distributions | Histogram and box plot using suitable raw observations, not averages disguised as observations |
| Specialized | Heatmap for observed nonduplicate cells; Pareto for complete additive categories; waterfall only with a verified opening value, ordered changes and closing total |
| Other | A value card for a single aggregate; full tables when chart suitability cannot be established |

For example, ask **“Show the percentage of employees in each department as a pie chart.”** Actual part-of-whole percentages can select pie automatically. Independent rates (such as retention in each department) do **not** become pie slices merely because they are percentages. Negative values, truncation, unknown remainders, ambiguous SQL lineage and explicit LIMITs can prevent composition charts. A total near 100 alone is not proof of a valid partition. Unsupported combinations fall back conservatively; correlation matrices and inferred waterfall baselines are not synthesized.

Charts have **Chart / Details** tabs: Details preserves every returned column for employee listings without a column dropdown or record-export button. Non-numeric results use a plain table, and empty outputs do not invent charts. Bar charts show at most 30 fetched records with an explicit notice; distributions describe fetched records only. Visual numeric labels are approximate display values; Details retains precision-preserving original Decimal strings. Ordinary results have no download or chart-export menu; explicitly generated business reports have a separate PDF download.

Answers address the requested question without unsolicited advice, follow-up or unrelated statistics. No source/output/scope cards or read-query SQL are shown. Privacy and consent are operator environment settings, not client preferences. Data-changing requests still need approval, with exact SQL accessible under **Review exact change** for informed confirmation.

### Tables and chat history

Lookup/list responses show a brief answer followed by a record table. Analytical comparisons can include charts; **Details** preserves every returned column locally. Wide tables support horizontal scrolling. AI summaries still receive best-effort identifier-redacted samples, not the unredacted local table. **Copy** copies question/answer text locally to the clipboard. **Edit → Save & resend** replaces that question and removes subsequent turns from the conversation; it never replays previous queries or undoes completed changes. An edited data-changing request still needs a new approval.

**New chat** starts a conversation; **Recents** restores session-local history without replaying queries. History is not saved to disk or guaranteed after reload/restart, and is capped at 20 chats with 20 messages each per configured source/profile. Credential/source configuration changes reset histories. Starting or switching chats cancels pending changes; it never undoes completed database changes.

**User** provides session-local conversation profiles with separate histories. Switching users cancels pending approvals and never replays queries. These are convenient workspaces, **not authenticated accounts or permission boundaries**: everyone shares the configured database access and can switch profiles. Profile/chat history is not persisted. Saved recipient preferences are the limited exception described below.

Each answer has a **speaker icon** beside Copy. Click to read the complete answer text aloud; click again to stop. Playback uses one continuous utterance rather than restarting at sentence/character boundaries. Only installed **local en-US voices** are accepted: enhanced/natural quality takes priority, with male voices preferred at equivalent quality. There is no default, non-US or cloud-voice fallback. The control waits up to three seconds for delayed voices; if none qualify, it explains how to install an English (United States) voice in device speech settings and reload. Markdown decoration is removed; tables/charts are not narrated cell by cell. Smoothness and voice quality remain dependent on installed voices and the browser/OS engine. No separate speech API key is used.

## Setup

1. Use Python 3.11+ and a virtual environment. Install dependencies with `python -m pip install -r requirements.txt` using your chosen environment.
2. Copy [.env.example](.env.example) to your private environment file **only if one does not already exist**. Keep existing credentials private. Configure `CHATBOT_DATABASE` and `CHATBOT_TABLE` for the intended source; the example uses database `employees` and table `employees_messy`, but does not create them. `CHATBOT_DATABASE` overrides `MYSQL_DATABASE` for the chatbot only (otherwise it falls back to `MYSQL_DATABASE`). Loader settings are unchanged.
3. Set `CHATBOT_SCOPE=table` (the default). The only accepted values are `table` and `schema`; see **Fixed source and backend scope** below. Set `OPENAI_API_KEY` and `OPENAI_MODEL` to an available model supporting Chat Completions JSON output, and explicitly set `CHATBOT_OPENAI_CONSENT=true` to enable natural-language chat. No default model/key or silent consent is assumed; without consent and a configured provider/model, chat stays disabled with a setup notice, not a direct-SQL fallback.
4. Leave `CHATBOT_ALLOW_MUTATIONS=false` for read-only use. Enable it explicitly only if approved writes are required; every mutation still needs confirmation. Restart the app after changing environment configuration.
5. Start with `python -m streamlit run chat_app.py --server.address 127.0.0.1`, then visit http://127.0.0.1:8501. In this workspace the environment executable is `.venv/bin/python`.

### Fixed source and backend scope

The client never selects or switches databases/tables. `CHATBOT_DATABASE` fixes
the database and `CHATBOT_TABLE` fixes the table in default `CHATBOT_SCOPE=table`
mode. Metadata and SQL validation are restricted to that table, including its
self-joins/CTEs; other tables are blocked. An unavailable configured table fails
closed rather than silently choosing another source.

Only explicit operator configuration `CHATBOT_SCOPE=schema` permits backend joins
across discovered base tables in `CHATBOT_DATABASE`. This is not a client control
and does not expand MySQL privileges. The backend discovers columns, indexes and
foreign keys, validates one statement and uses actual results. Cross-database
references and undiscovered tables/views remain blocked in either mode.

### Azure OpenAI and natural-language consent

Azure credentials are supported directly: set `CHATBOT_AI_PROVIDER=azure`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION`, and `AZURE_OPENAI_DEPLOYMENT_MINI`. Optionally set `AZURE_OPENAI_DEPLOYMENT` to choose a different deployment. A deployment name, not a generic OpenAI model name, is passed to Azure. Existing Azure credentials are reused without copying their values. Azure is also auto-detected when only an Azure key is present.

`CHATBOT_OPENAI_CONSENT=true` explicitly authorizes questions and scoped schema to your configured OpenAI/Azure OpenAI provider. Separately, `CHATBOT_AI_SUMMARIES=true` authorizes bounded, best-effort identifier-redacted result samples for AI answers. Both flags default to false in the template; neither is silently enabled and neither has a sidebar checkbox. A configured provider key and model/deployment are also required for natural-language chat. Result summaries are optional: leave them false to keep result values local. Answers appear before visualizations; failed AI summaries retain a local deterministic answer/status and actual results.

The app binds to localhost by default. It has **no authentication**, so do not expose it publicly, change the bind address, or put it behind a public proxy without adding authentication and authorization. Local OS users and processes may access a locally running server. Do not run this as a production service.

## Using the chatbot

- **Ask a question:** once the operator has configured consent and the provider/model, try “How many employees are there?” or “Average salary by department, excluding salaries less than or equal to zero.” Dirty department labels and invalid salaries are not silently normalized. There is no direct-SQL input mode.
- **Source:** all questions use the configured source and backend scope. Cross-table joins require explicit `CHATBOT_SCOPE=schema`; the model uses foreign keys or clear matching columns and asks for clarification when relationships are ambiguous. System schemas are read-only.
- **Output:** conversational answers appear first, without SQL or technical execution commentary. Lookups use record tables; useful comparisons get charts with **Details** for original rows. Empty queries produce no chart. PDF downloads are available only for explicitly generated business reports.
- **Mutations:** when enabled by the operator, review the exact canonical SQL and click “Confirm & execute once.” Destructive operations additionally require typing `CONFIRM` followed by the actual referenced table name(s), as shown. A new prompt, edit, chat/profile switch or source configuration change cancels the previous proposal; reruns cannot repeat it. Creation of undiscovered new tables is not supported in schema-wide mode. No tables or rows are created automatically.

## Business reports and PDF

Ask **“Create a workforce report”**, or include the business question in your report request. Submitting the prompt immediately builds the report once and displays the **full report inline at that conversation turn**. There is no sidebar report builder, composer, focus form or extra Build button. Rerendering, changing theme and downloading do not rebuild it. Ambiguous requirements are clarified rather than silently broadened.

The app automatically reuses relevant aggregate-safe answers from the current chat and configured scope, without rerunning them. If none are usable, it runs up to six validated read-only analyses. A generic workforce report uses available headcount, department/status/city composition, positive-salary/data-quality metrics and monthly hiring (including typed `join_date`). Text salaries/dates are not silently converted. Include **fresh**, **current**, **latest** or **live** in the request for fresh analysis rather than saved answers. Specific requests may use AI planning only with question/schema consent. Explicit schema mode uses one eligible table for the deterministic overview, never invented joins; name the intended table in the request when needed.

Reports follow the **business question**, not a generic collection of query outputs. For example, ask **“Which departments or roles have the highest and lowest pay? Build a business report.”** The report retains that question under a concise title, answers the pay comparison first, then presents supported breakdowns, explanatory commentary, relevant charts and key findings. **Recommendations and follow-up appear only when requested.** The same analytical document drives the screen and PDF; it does not copy example figures or currency.

Pay profiles include all-record headcount, positive salary observation counts and average positive salary. Missing, zero and negative salaries are excluded from those averages and disclosed. Role sections appear only for actual supported role columns; missing role evidence and tied rankings are reported honestly. Complete count breakdowns can include **Percentage of returned total**, including missing categories; separate totals, selection limits and averages are not assumed to prove population shares. Saved headcounts alone cannot answer a pay comparison, so insufficient saved pay evidence falls back to read-only table analysis. Custom period/filter requests use consented planning rather than silently running a broad replacement.

Reports contain at most six key findings and three relevant charts. Long internal query IDs, raw findings and SQL are removed from the main presentation. Methodology, provenance and detailed limitations are collapsed under report details; material partial-data/filter limitations remain briefly visible where necessary. Details retain timestamps, exact aggregate values and SQL provenance. Display numbers use commas and up to two decimals without changing the evidence. Client-facing report text is computed locally from verified aggregates; normal chat AI summary settings remain separate. Small groups are **not guaranteed anonymous**.

Click **Download PDF** beneath the inline report—there is no preparation step. The cached compact A4 document includes title/question/date, striped aggregate tables, commentary, static charts, findings, relevant limitations and an author/page footer; recommendations/follow-up are included only when requested. Table headers repeat across pages. Neither export nor download reruns SQL. Raw individual records and SQL are omitted from the PDF. Export tables are capped at 50 rows and 12 columns per section with disclosure; a chart renderer failure falls back to an aggregate table. The bundled font supports Latin, Greek and Cyrillic; other scripts may need a custom font. PDFs are capped at 10 MiB and remain session-local until downloaded or deliberately emailed.

### Saved recipient and email delivery

Completed reports offer **Download PDF**, a saved editable **Recipient email** and explicit **Send to email**. The recipient can be replaced with any valid address; enter an address and commit the field to save it locally for the current profile name. The same profile name restores it across chats and restarts; clear the field to forget it. Only recipient addresses and hashed profile-name keys are stored in a gitignored local JSON preference store, with restrictive local permissions. No SMTP passwords, reports or chat history are persisted there. Local profiles are not authenticated: other users with access to this app/device can access these preferences.

Configure `CHATBOT_SMTP_HOST`, `CHATBOT_SMTP_PORT`, `CHATBOT_SMTP_TLS_MODE`, `CHATBOT_SMTP_FROM`, and (when required) `CHATBOT_SMTP_USER`/`CHATBOT_SMTP_PASSWORD` privately in your local environment. The template lists the fields. Use STARTTLS on the provider's recommended port (usually 587), or `ssl` (usually 465). Certificate verification is mandatory; there is no plaintext fallback. Use an app password if your provider requires one; OAuth-only providers need an SMTP-compatible relay. Never paste passwords into chat.

Sending happens **only** when you click **Send to email**, sharing the same PDF with your SMTP provider and recipient. Building, rerendering and downloading do not send mail. Server acceptance does not guarantee inbox delivery; check spam or provider logs. Accepted or uncertain sends are not automatically repeated. If the connection fails during sending, check delivery before creating another send; an uncertain outcome is blocked from retry for that report/recipient in the current chat. Email setup is optional—PDF downloads work without it.

### Reporting safeguards and limits

- Every report plan has one to six queries. The entire plan is validated read-only against the active table/schema before execution; writes cannot enter the report runner. Live reads recheck schema fingerprints, consume a one-use job ID even on failure and are never automatically retried.
- A 60-second job budget prevents additional queries from starting; it does not forcibly interrupt an in-flight database call. Partial failures are disclosed. Reads use separate connections, **not** one consistent transactional snapshot.
- SQL scope, row caps, database permissions and existing mutation approvals remain in force. Generating a report cancels any pending mutation rather than approving it. Source configuration/profile/chat changes close unfinished report plans without replaying queries.
- At most three finished reports are retained per chat. Editing an earlier question invalidates its subsequent report context. Results, reports and cached PDFs remain in their session-local profile/schema/table/chat workspace; profiles are not authentication boundaries. A report runner accepts at most 64 live jobs before a new chat is required.
- Only proven aggregate projections and recognized business dimensions are presented in report sections. Raw records, ambiguous nested/window results and unproven identifying groups can be omitted even when their SQL is valid. Original chat results remain separate.
- Salary totals are **not** full employer cost. Generic sales/order amounts are **not** automatically revenue or profit. Currency, salary period, tax, benefits, costs, targets and causal claims require explicit definitions and available data. Reports do not invent forecasts, missing periods or business recommendations unsupported by evidence.

## Supported SQL and boundaries

| Supported | Conditions |
| --- | --- |
| SELECT, CTEs, UNION, JOINs, self-joins, nested queries | Every real reference must be in the configured scope; cross-table joins only with explicit `CHATBOT_SCOPE=schema`; common built-ins only |
| INSERT, UPDATE, DELETE | One target table; confirmation; no multi-table UPDATE/DELETE |
| CREATE TABLE | Simple columns for an allowed table name only; no AS/LIKE, foreign keys or table options |
| ALTER TABLE | Tested ADD COLUMN, DROP COLUMN and supported ALTER COLUMN syntax; typed confirmation |
| CREATE INDEX / DROP INDEX | Only a discovered table; DROP INDEX uses an unqualified ON table name |
| DROP TABLE, TRUNCATE TABLE | One discovered table at a time; typed confirmation |

This is **not unrestricted “any SQL.”** Cross-database references, undiscovered tables, views (which can hide cross-schema access), schema administration, grants, stored procedures/triggers, table renames, filesystem operations, UDFs, locking reads, variables, comments, backslash escapes, multiple statements and unverified forms are rejected. INTERSECT/EXCEPT are blocked for portability. Unsupported forms fail closed. Canonical SQL is generated by the pinned SQLGlot major version and shown/executed identically.

Supported analytical functions include aggregates, CASE, COALESCE, numeric/string/date functions and common window functions. The explicit allowlist is in [chatbot/sql_policy.py](chatbot/sql_policy.py); unsupported built-ins require adding regression-tested support. MySQL ultimately decides whether syntax and columns are valid on your server.

### Permissions and destructive operations

**Back up the target table before enabling destructive access.** MySQL DDL (including TRUNCATE) may implicitly commit and cannot reliably be rolled back. Transactional DML is committed on success; failures roll back where the table engine supports transactions. Triggers and existing schema relationships can cause side effects beyond the submitted statement: use a dedicated test database or remove such relationships if strict table isolation is required. Application validation alone cannot guarantee isolation.

Use a dedicated MySQL account, not `root`, with privileges only on application schemas/tables you intend to expose. Grant `SELECT` for read-only use and only necessary mutation privileges for approved writes. Do not grant global privileges, FILE or EXECUTE. The app does not create accounts or change grants. Configuring schema scope does not grant additional access; MySQL permissions remain the final boundary. Test actual denial outside authorized schemas.

On connection loss during a mutation the outcome may be **unknown**, including whether it committed. The app never automatically retries it. Verify directly in MySQL before resubmitting. Confirmation is tied to session-local proposal identity and a live schema fingerprint; external changes between the final schema check and execution cannot be eliminated without stronger database-level coordination.

## Privacy and limits

- The private environment file stays local and is gitignored; never paste credentials into chat. No application query/result logs or persistent chat history are created. At most 20 messages remain per chat with up to 20 session-local chats per configured source/profile; starting a new chat does not undo database changes. Saved recipient addresses are the limited local persistence described above.
- OpenAI receives questions, up to four prior user questions and live schema for translation. No database credentials or previous query outputs are supplied to generation. API use may incur costs and is subject to your provider's data policies.
- Result summaries require the separate operator opt-in `CHATBOT_AI_SUMMARIES=true`, not a client checkbox. They send at most the configured sample rows within a 10,000-character result payload. Common identifier columns (names, emails, phone/address/employee IDs) are best-effort redacted. **Aliases or expressions may evade redaction; employee data may still be sent.** Leave summaries disabled to keep all result values local. Questions themselves can contain personal data.
- Summaries are explicitly sample-based. If the provider fails, actual query results remain available. Mutation status is always deterministic, not model-generated.
- `CHATBOT_MAX_ROWS` defaults to 500 (maximum 5000); fetching one extra row detects truncation. Queries are not rewritten with hidden LIMITs. Use SQL LIMIT and selective predicates to reduce work. Server-side cursors bound fetched-row memory, but closing a cursor may still drain rows; row caps do not guarantee reduced server work or network traffic. Individual values can also be large.
- `CHATBOT_SAMPLE_ROWS` defaults to 20 (maximum 50). `CHATBOT_TIMEOUT_SECONDS` defaults to 15. When a live capability probe detects MySQL `max_execution_time`, SELECT execution uses `CHATBOT_SELECT_TIMEOUT_MS` (default 10,000); MariaDB does not use this setting. Driver timeouts do **not** guarantee cancellation of writes or DDL.
- Date values serialize to ISO format, Decimal to precision-preserving strings and bytes to hex in Details. Numeric charts use finite floating-point display values; original data is not rewritten.

## Tests

Run `python -m pytest -q` in the selected environment. Tests cover allowed/blocked ASTs, cross-table/administrative bypasses, confirmation expiry and single-use execution, schema changes, provider failures/privacy payloads, local export safety and Streamlit UI behavior.

Live destructive integration tests are opt-in and require `CHATBOT_TEST_DATABASE` naming a **dedicated disposable database starting with `chatbot_test_`** plus `CHATBOT_RUN_INTEGRATION=1`. They use your configured MySQL connection credentials and may create/drop `employees_messy` inside that disposable database, never the configured production database. Create the disposable database and restricted test account yourself. Otherwise these tests are skipped. Never point them at real employee data.

## Restricted cloud pilot (separate from local setup)

The local-mode authentication/public-exposure warnings above still apply to
local mode. Explicit `CHATBOT_MODE=cloud` uses native Google OIDC, an operator
invite list of Google **subject IDs (not emails)**, per-session public TLS MySQL
connections and read-only access. It does not read the local environment file.
Cloud reports offer PDF download only, without email or persistent recipients.

**Deployment blocked:** these are the cloud modules' intended capabilities; the
current app entry point still takes local initialization in cloud mode and fails
closed before onboarding. Entry-point integration must land and pass the new CI
gate before a pilot can run. See the observed blocker in the deployment guide.

See [docs/cloud-deployment.md](docs/cloud-deployment.md) for the gated Azure
Container Apps pilot checklist, private OIDC file mount, required AI settings,
network egress controls, billing approval and unverified live-service checks.
[Dockerfile](Dockerfile) uses Python 3.14 slim and a nonroot user;
[requirements-cloud.txt](requirements-cloud.txt) contains curated direct runtime
pins. [.github/workflows/cloud-checks.yml](.github/workflows/cloud-checks.yml) runs
offline tests and Linux container checks only—no resources, push or deployment.
This is **not a public-production-ready deployment**. Keep one process/worker and
min/max one replica; session state and hourly quotas reset on process restart.