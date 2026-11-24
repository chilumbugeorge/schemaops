# schemaops

Self-serve MariaDB/MySQL schema migrations for a Git monorepo, run by GitLab CI/CD. Development teams add migrations through merge requests; schemaops enforces governance, database standards, migration integrity and the promotion order **DEV → UAT → PROD**, one merge request per environment.

Schema changes never block traffic. Each `ALTER` is applied as instant DDL when the server can do it that way, and through `pt-online-schema-change` when it cannot; no statement waits more than 5 seconds for a metadata lock.

Safety features, each described below:

- **Instant DDL first**: metadata-only changes finish in milliseconds, whatever the table size.
- **Pre-flight checks**: the live table is inspected just before every change.
- **Automatic size escalation**: very large tables go to the DBA team even if nobody listed them.
- **The plan on the merge request**: reviewers see how each migration will run before approving.
- **Integration tests**: the deployment path is tested against a real MariaDB with the real `pt-online-schema-change`.
- **Deployment feedback**: after a migration is applied, its merge request gets a comment with the resulting table structure.
- **Nightly reconcile**: reports migrations left pending or failed, changes made outside migrations, and leftovers.

## Repository layout

```text
schemaops.yaml                      framework settings (all optional)
policy/protected_tables.yaml        hot and large tables (DBA team)
clusters/<cluster>/
  cluster.yaml                      facts shared by every schema on the cluster (DBA team)
  <schema>/
    schema.yaml                     identity, ownership and classification of the schema
    environments/{dev,uat,prod}.yaml  migrations registered per environment
    migrations/{dev,uat,prod}/      <YYYY-MM-DD>_<description>.sql
src/schemaops/                      the tool
.gitlab-ci.yml, ci/common.yml       the pipeline
Dockerfile                          the pipeline's runner image
```

All configuration files use the `.yaml` extension (`.gitlab-ci.yml` is GitLab's own fixed name). Unknown keys are rejected, so a typo is caught instead of silently ignored.

`cluster.yaml`, one per cluster:

```yaml
cluster-name: trades
description: Primary OLTP cluster for the trading platform.
engine: mariadb              # mariadb | mysql
version: "10.11"             # quoted, so 10.10 is not read as 10.1
topology: primary-replica    # optional: standalone | primary-replica | galera
dba-contact: "#dba-oncall"

# Optional, non-secret connection details. Credentials never go here.
environments:
  dev:  {host: trades-dev.example.internal, port: 3306, tls-mode: verify-identity}
  uat:  {host: trades-uat.example.internal, port: 3306, tls-mode: verify-identity}
  prod: {host: trades-prod.example.internal, port: 3306, tls-mode: verify-identity}
```

| Key | Required | Meaning |
| --- | --- | --- |
| `cluster-name` | Yes | Must equal the folder name; lower snake_case |
| `description` | Yes | What the cluster is for |
| `engine` | Yes | `mariadb` or `mysql` |
| `version` | Yes | Server version, as quoted text |
| `dba-contact` | Yes | Where to reach the DBA team about this cluster |
| `topology` | No | `standalone`, `primary-replica` or `galera` |
| `environments.<env>.host` | No | Server host name for `dev`, `uat` or `prod` |
| `environments.<env>.port` | No | 1 to 65535; defaults to 3306 |
| `environments.<env>.tls-mode` | No | `verify-identity` (default) or `verify-ca`; `required` and `disabled` only in `dev` |

Rules for `cluster.yaml`:

- **Every cluster folder needs one**, even before it has a schema. Create it with `schemaops new-cluster`.
- **No credentials.** A `user`, `password`, `token` or similar key is a governance error. Credentials stay in CI/CD variables.
- **The `environments` block is optional.** Without it, set `SCHEMAOPS_<CLUSTER>_<ENV>_HOST` (and `_PORT`, `_TLS_MODE` if needed) as variables. With both, the variable wins.
- **It is owned by the DBA team.** The host decides where credentials are sent, so `CODEOWNERS` assigns `/clusters/*/cluster.yaml` to the DBA team, and an existing `cluster.yaml` cannot be edited in the same merge request as a migration. A brand-new one may arrive together with the cluster's first schema.
- **`engine`, `version` and `topology` are descriptive today.** They are validated and reviewable, but do not yet change how migrations run.

`schema.yaml`, one per schema:

```yaml
cluster-name: trades
schema-name: accounts
owner: trades-team
description: Accounts schema for managing user accounts for daily trades.
# Optional:
owner-group: "@acme/trades-team"   # GitLab group; must own the schema's folder in CODEOWNERS
contact: "#trades-db"
escalation-contact: "trades-oncall@example.com"
status: active                     # active | frozen | retired; frozen and retired accept no new migrations
tier: 1                            # 1 (most critical) to 3; tier 1 needs DBA approval for PROD changes
data-classification: confidential  # public | internal | confidential | restricted; shown to reviewers
```

`environments/dev.yaml`, one per schema and environment:

```yaml
cluster-name: trades
schema-name: accounts
environment: dev
migrations:                  # oldest first; add at the end only
  - 2026-11-15_initial_dump.sql
  - 2026-11-20_add_users_display_name.sql
```

Applied status, timestamps and checksums are deliberately not kept in these files: they live in each database's `_schemaops_history` table, and a copy in Git would drift.

The first migration of every schema is `<date>_initial_dump.sql`: a structure-only dump (`mysqldump --no-data --skip-add-drop-table --skip-comments`). On an empty schema it is executed; on an existing schema it is recorded as the baseline without being run.

## Developer workflow

```bash
pip install -e ".[dev]"

# 0. Once per cluster, then once per schema (DBA team and owning team)
schemaops new-cluster --cluster trades --description "Primary OLTP cluster" --version 10.11 --dba-contact "#dba-oncall"
schemaops new-schema --cluster trades --schema accounts --owner trades-team --description "User accounts" \
  --tier 1 --data-classification confidential --initial-dump accounts_schema.sql

# 1. DEV: create the migration, write one statement in it, open a merge request
schemaops new-migration --cluster trades --schema accounts --name add_users_display_name
schemaops check-mr --base origin/main          # the same checks the pipeline runs

# 2. UAT: after the DEV merge request is merged and deployed, a new merge request
schemaops promote --cluster trades --schema accounts --to uat

# 3. PROD: after UAT is merged and deployed, a new merge request
schemaops promote --cluster trades --schema accounts --to prod
```

`promote` copies the file to the next environment's folder and registers it. Onboard a cluster with `schemaops new-cluster`, then each schema with `schemaops new-schema`.

| Command | Purpose |
| --- | --- |
| `validate` | Repository governance checks |
| `lint [files]` | Database standards |
| `rules` | Print the rule catalogue |
| `check-mr` | Everything a merge request must pass |
| `plan` / `deploy` / `status` | Preview, apply and inspect migrations in one environment |
| `reconcile` | Report where Git and a database disagree; changes nothing |
| `pipeline` | Write the per-cluster deployment child pipeline (used by CI) |
| `mark-applied` | DBA only: record a change applied by hand |
| `new-cluster` / `new-schema` / `new-migration` / `promote` | Scaffolding |

## Governance checks

Repository state (`validate`, and again before every deployment):

| Check | Enforces |
| --- | --- |
| `CLUSTER_CONFIG` | `cluster.yaml` exists, is complete, matches its folder, and holds valid connection details and no credentials |
| `SCHEMA_CONFIG` | `schema.yaml` exists, is complete, matches its folder, and its optional fields have allowed values |
| `OWNERSHIP` | `owner-group` is a GitLab group and owns the schema's folder in `CODEOWNERS` |
| `ENV_CONFIG` | `dev.yaml`, `uat.yaml` and `prod.yaml` exist and match their folder and environment |
| `REGISTRATION` | Every migration file is registered, every registered file exists, no duplicates |
| `FILE_NAME` | `YYYY-MM-DD_<lower_snake_description>.sql` with a real date |
| `ORDER` | Migrations are in chronological order |
| `INITIAL_DUMP` | The first migration is the single `<date>_initial_dump.sql` |
| `PROMOTION` | PROD ⊆ UAT ⊆ DEV, in the same order, with no migration skipped |
| `INTEGRITY` | A promoted file is byte-identical to the lower environment's copy |
| `POLICY` | `protected_tables.yaml` is valid and refers to known schemas |

Merge request (`check-mr`, compared with the target branch):

| Check | Enforces |
| --- | --- |
| `IMMUTABLE` | Existing migration files are never modified or deleted |
| `APPEND_ONLY` | Environment files only gain entries at the end |
| `ONE_ENVIRONMENT` | A merge request adds migrations to one environment only |
| `PROMOTION` | A migration enters UAT (PROD) only if it is already merged for DEV (UAT) on the target branch |
| `SEPARATE_CHANGES` | Migrations are not mixed with framework, policy or pipeline changes, or with edits to an existing `cluster.yaml` |
| `FROZEN` | A schema with `status: frozen` or `retired` accepts no new migrations |
| Deployment evidence | The previous environment's migration history shows the migration as deployed |
| DBA approval | A DBA has approved any change to a protected table, a table over the size limit, any new UNIQUE index, or any PROD change to a tier 1 schema |

`check-mr` also posts **the plan** as a comment on the merge request and updates it on every run: each migration, how it will run, the table's current size, and how its deployment went in the previous environment. Set `SCHEMAOPS_MR_COMMENT=1` (the pipeline does); `--report FILE` writes the same Markdown to a file. The table size shows as "unknown" unless the read-only account can see the table's metadata.

Deployment time (`deploy`), checked against the `_schemaops_history` table in each schema: applied migrations are unchanged (SHA-256), nothing applied has left Git, migrations apply strictly in order, a failed or interrupted migration blocks until a DBA decides, the previous environment's deployment succeeded with the same checksum, and only one deployment per schema runs at a time.

## Lint rules

Three levels. **error** always fails the merge request. **warn** and **advise** fail it unless the merge request has the `ignore-linting` label. To make advice informational only, remove `advise` from `lint.blocking-without-label` in `schemaops.yaml`.

Lint runs in full when a migration enters DEV. On UAT and PROD promotions the file is identical, so only errors block. Findings in the initial dump are reported as advice, because it records a schema that already exists.

| Rule | Level | Standard | Why | Instead |
| --- | --- | --- | --- | --- |
| `ONE_STATEMENT` | error | One schema change per migration file | DDL cannot be rolled back. With one statement per file a migration either applied or it did not, so a failure never leaves a half-applied file to untangle. | Split the change into several migration files, one statement each. |
| `UNSUPPORTED_STATEMENT` | error | Only online-safe schema changes are allowed | Migrations are executed automatically and without blocking traffic. Data changes, grants, session settings and partition or tablespace maintenance cannot be run that way. | Use CREATE TABLE, ALTER TABLE, CREATE/DROP INDEX, RENAME TABLE or DROP TABLE. Ask the DBA team to run anything else manually. |
| `NO_SCHEMA_QUALIFIER` | error | Do not write the schema name in a migration | A migration applies to the schema whose folder it lives in. A hard-coded schema name can send the change to a different database. | Write `users`, not `accounts.users`. |
| `REQUIRE_PRIMARY_KEY` | error | Every table must have a primary key | InnoDB stores rows in primary-key order; without one it invents a hidden key that hurts replication performance. pt-online-schema-change also needs a key to copy rows. | Add `id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY`, or a natural key. |
| `BIGINT_PRIMARY_KEY` | error | Integer primary keys must be BIGINT | INT runs out at about 2.1 billion (4.2 billion unsigned). Widening a primary key later means rebuilding the table and every table that refers to it. | Use BIGINT UNSIGNED. |
| `AUTO_INCREMENT_UNSIGNED` | error | AUTO_INCREMENT columns must be UNSIGNED | AUTO_INCREMENT never produces negative values, so a signed column wastes half its range. | Declare the column as BIGINT UNSIGNED. |
| `ENGINE_INNODB` | error | Only the InnoDB engine is allowed | InnoDB gives transactions, row-level locking and crash recovery. MyISAM, MEMORY and others lock whole tables, lose data on a crash, or break replication and backups. | Use ENGINE=InnoDB, or leave ENGINE out (InnoDB is the server default). |
| `NO_FOREIGN_KEY` | error | Foreign key constraints are not allowed | Every write must check the parent table, taking extra locks; foreign keys also block online schema changes and make sharding or moving a table much harder. | Keep the indexed reference column and enforce the relationship in the application. |
| `NO_CHECK_CONSTRAINT` | error | CHECK constraints are not allowed | Business rules in the schema are evaluated on every write and must be changed with a table rebuild; their support also differs between MariaDB and MySQL versions. | Validate values in the application. |
| `CHARSET_UTF8MB4` | error | Use utf8mb4 for character sets and collations | `utf8` in MariaDB/MySQL stores at most 3 bytes per character and silently rejects emoji and some CJK characters. Mixed character sets also prevent index use in joins. | Use CHARACTER SET utf8mb4 with a utf8mb4 collation, or omit both to inherit the default. |
| `NO_STORED_ROUTINES` | error | No views, triggers, functions, procedures or events | Logic hidden in the database is hard to test, version and observe. Triggers also conflict with pt-online-schema-change, which uses triggers of its own. | Implement the behaviour in application code. |
| `ADD_NOT_NULL_NEEDS_DEFAULT` | error | A new NOT NULL column needs a DEFAULT | Existing rows need a value for the new column. pt-online-schema-change refuses the change without a default, and writes from application versions that do not know the column fail. | Add a DEFAULT, or add the column as NULL, backfill it, then tighten it in a later migration. |
| `NO_ENUM` | warn | Avoid ENUM and SET columns | Changing the list of values is a schema change, values are stored as hidden integers that sort unexpectedly, and an invalid value can be stored as an empty string. | Use a VARCHAR validated by the application, or a small lookup table. |
| `VARCHAR_WIDTH` | warn | Avoid VARCHAR(256) or wider | Above 255 characters the length prefix takes 2 bytes, in-memory temporary tables and sort buffers reserve the full declared width, and index prefixes are limited. | Size the column to the real maximum (255 or less), or use TEXT for long free-form text. |
| `UUID_PRIMARY_KEY` | warn | Avoid UUID primary keys | Random UUIDs insert all over the clustered index (page splits, fragmentation, cold cache), and the wide key is copied into every secondary index. | Use BIGINT UNSIGNED AUTO_INCREMENT; give each cluster node its own auto_increment_offset with a shared auto_increment_increment if data must later be merged. If a UUID is required externally, keep it in a separate UNIQUE column. |
| `NO_VIRTUAL_COLUMN` | warn | Avoid generated (virtual) columns | The expression is evaluated by the database on every read or write, it cannot be changed without a rebuild, and generated columns restrict online schema change and replication. | Compute the value in the application and store it in a normal column. |
| `DESTRUCTIVE_CHANGE` | warn | DROP TABLE and DROP COLUMN destroy data | The data cannot be recovered without a restore, and application versions still reading the table or column fail immediately. | First release code that no longer uses it, confirm a backup, then drop in a later migration. |
| `RENAME` | warn | Renaming a table or column breaks running code | Old and new application versions run side by side during a deploy; a rename breaks whichever one uses the other name. Renames through pt-online-schema-change can also lose data. | Add the new column or table, write to both, migrate readers, then drop the old one. |
| `ADD_UNIQUE_INDEX` | warn | Adding a UNIQUE index to an existing table needs a DBA | pt-online-schema-change copies rows with INSERT IGNORE, so existing duplicates would be silently deleted. schemaops therefore never applies this change automatically. | Check for duplicates first; the DBA team applies the change manually. |
| `NO_ZEROFILL` | warn | ZEROFILL is deprecated | It is display formatting stored in the schema, deprecated in MySQL 8.0.17, and it silently makes the column UNSIGNED. | Pad values in the application. |
| `PREFER_DECIMAL` | advise | FLOAT and DOUBLE are approximate | Binary floating point cannot represent most decimal fractions, so sums and comparisons of money or quantities drift. | Use DECIMAL(p,s) for exact values; keep FLOAT/DOUBLE for measurements only. |
| `NO_TIMESTAMP` | advise | Prefer DATETIME to TIMESTAMP | TIMESTAMP ends in January 2038 and is converted using the session time zone, so the same row can read differently from two connections. | Use DATETIME (or DATETIME(6)) and store UTC. |
| `NO_JSON` | advise | Use JSON columns sparingly | JSON hides structure from the schema: values cannot be indexed directly, validated or migrated, and whole documents are rewritten on update. | Use typed columns for anything you filter, join or sort on. |
| `NO_TEXT_BLOB` | advise | Use TEXT and BLOB sparingly | Large values are stored off-page, force on-disk temporary tables and bloat backups and replication. | Use a sized VARCHAR where a limit exists; keep large binaries in object storage. |
| `NO_INT_DISPLAY_WIDTH` | advise | Integer display widths are deprecated | INT(11) does not limit the stored range; the width is ignored and deprecated in MySQL 8.0.17. | Write INT, BIGINT and so on without a width (TINYINT(1) for booleans is fine). |
| `NAMING` | advise | Use lower snake_case names | Identifier case sensitivity differs between operating systems (lower_case_table_names), so mixed-case names break when a schema moves between hosts. | Use names such as `user_accounts` and `created_at`. |

`SYNTAX_ERROR` (error) covers unterminated strings, unbalanced parentheses, missing semicolons or commas, misspelt keywords, unknown data types and bad primary key definitions. It is a structural check, not a full SQL grammar; `pt-online-schema-change --dry-run` is the second line of defence.

## How changes are applied

| Statement | Method |
| --- | --- |
| `ALTER TABLE`, `CREATE INDEX`, `DROP INDEX` | Instant DDL if the server supports it for that change; otherwise `pt-online-schema-change`, dry run first. Same in every environment and for every table size |
| `CREATE TABLE`, `DROP TABLE`, `RENAME TABLE` | Directly, with `lock_wait_timeout = 5` |
| Change to a protected table, in a manual environment (PROD) | Not run. Deployment stops with exit code 3; a DBA applies it and runs `mark-applied` |
| Change to a table over `auto-protect` limits (10 million rows or 10 GB by default), in a manual environment | Treated as protected, whether or not the table is listed |
| New UNIQUE index on an existing table | Not run, in any environment (pt-online-schema-change would silently drop duplicate rows) |

`pt-online-schema-change` runs with `lock_wait_timeout=5`, pauses on replica lag and load, and aborts on critical load; limits are in `schemaops.yaml`. The password is passed in a temporary 0600 option file, never on the command line.

### Instant DDL first

For every `ALTER`, schemaops first runs the change with `ALGORITHM=INSTANT` and a 5-second lock wait. Instant DDL changes only the table's metadata, so it takes milliseconds on a table of any size and causes no replica lag.

- **The server decides.** schemaops does not guess which changes qualify, because that differs between MariaDB and MySQL versions. Typically adding or dropping a column, renaming a column and changing a default are instant; adding an index is not.
- **Fallback.** If the server answers that the change cannot be instant, nothing has been changed and schemaops runs it through `pt-online-schema-change`.
- **No blocking copy, ever.** The server's own table-copying `ALTER` is never used.
- **A blocked lock is a failure, not a fallback.** If the instant change cannot get its metadata lock within 5 seconds, the migration fails and is retried later.
- **Your `ALGORITHM=` and `LOCK=` clauses are ignored.** schemaops chooses both.
- **The history records what happened**: `instant` or `pt-osc`, with the duration.

Protected tables, tables over the size limit and new UNIQUE indexes still go to the DBA team in manual environments. Set `online-schema-change.prefer-instant: false` to send every `ALTER` through `pt-online-schema-change`.

### Pre-flight checks

Just before each change, schemaops inspects the live table. `schemaops plan` shows the same checks without changing anything.

| Check | Fails when | Applies to |
| --- | --- | --- |
| Table state | The table to alter, drop or rename is missing, or the table to create already exists | Every change |
| Open transactions | A transaction has been open longer than `max-transaction-seconds` and may hold a metadata lock | Every change |
| Table size | Above the `auto-protect` limits: escalated to the DBA team in manual environments | Every change to an existing table |
| Triggers | The table has triggers (`pt-online-schema-change` cannot run on it) | Table copy only |
| Child foreign keys | Other tables reference it; a DBA must apply the change | Table copy only |
| Disk space | Free space is below `disk-headroom` × table size. Needs MariaDB's `DISKS` plugin; skipped otherwise | Table copy only |

A failed "every change" check stops the deployment before anything is changed or recorded, so a later run simply retries. The "table copy only" checks do not stand in the way of instant DDL. If the change turns out not to be instant and one of them fails, the migration is recorded as failed without the table being touched, and a DBA re-runs it with `--retry-failed` once the cause is resolved.

### After a deployment

When a migration is applied, schemaops comments on the merge request that introduced it: the environment, how the change ran (instant DDL, `pt-online-schema-change`, direct or manual), how long it took, and the table's structure as it now is (`SHOW CREATE TABLE`). A promotion merge request gets the comment for its own environment, so the DEV, UAT and PROD results each appear where that change was reviewed. A dropped table and the initial dump are reported without a structure.

A deployment covering several schemas carries on past a failure: one schema's failed migration no longer stops the others. The job still fails, and a real failure always outranks "waiting for the DBA team" in the exit code.

If `SCHEMAOPS_NOTIFY_WEBHOOK` is set to a Slack-compatible incoming webhook, a summary is posted to chat after each deployment, each `mark-applied`, and each reconcile run that finds something. A failed notification never fails the job.

### Reconcile, drift and leftovers

A deployment only looks at the schemas changed in that push, so a migration can be merged and never applied without anything noticing. `schemaops reconcile --env <env> --all` compares Git with each database and reports:

| Finding | Meaning |
| --- | --- |
| `PENDING` | Registered in Git but not applied, with how long ago it was merged |
| `FAILED` | A migration left in the `failed` or `running` state |
| `CHECKSUM` | An applied migration whose file has since changed |
| `UNKNOWN` | Applied in the database but no longer in Git |
| `DRIFT` | A table created, removed or altered outside a migration, with the difference |
| `LEFTOVER` | Tables such as `_users_new` or `pt_osc_*` triggers left by an interrupted online schema change |

It only reads; it never applies or cleans up anything, and exits non-zero when it finds something. Drift is detected against a snapshot: after every deployment and every `mark-applied`, the definition of each table is stored in `_schemaops_snapshot` in that schema. Reconcile compares the live definitions with it, ignoring `AUTO_INCREMENT` counters. A schema deployed before this feature existed has no snapshot until its next deployment, and reports no drift until then.

## Credentials and security

Credentials are read only from environment variables and are never logged. For cluster `trades` in `prod`:

| Variable | Notes |
| --- | --- |
| `SCHEMAOPS_TRADES_PROD_USER`, `_PASSWORD` | Migration account |
| `SCHEMAOPS_TRADES_PROD_HOST`, `_PORT` | Optional if `cluster.yaml` lists them; port defaults to 3306 |
| `SCHEMAOPS_TRADES_PROD_TLS_MODE` | Optional. `verify-identity` (default), `verify-ca`; `required` and `disabled` are accepted in DEV only |
| `SCHEMAOPS_TRADES_PROD_TLS_CA` | CA certificate path (GitLab "File" variable) |
| `SCHEMAOPS_TRADES_UAT_RO_USER`, `_RO_PASSWORD` | Read-only history account, used by the next environment to verify deployment |

Host, port and TLS mode are not secrets and can be listed per environment in `cluster.yaml`, where they are reviewable; a variable, when set, wins. Because the host decides where credentials are sent, `cluster.yaml` is owned by the DBA team in `CODEOWNERS` and an existing one cannot be changed in the same merge request as a migration. If you prefer to keep host names out of Git, leave `environments` out and use the variables.

The configuration is validated before any connection: host, port, TLS mode, CA file, and `root`/`admin` accounts are refused. Suggested grants:

```sql
-- migration account, one per cluster and environment
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, TRIGGER ON accounts.* TO 'schemaops_prod'@'%';
-- pt-online-schema-change replica discovery, lag and load checks (MariaDB 10.5 and later)
GRANT PROCESS, REPLICATION SLAVE, REPLICATION MASTER ADMIN, SLAVE MONITOR ON *.* TO 'schemaops_prod'@'%';
-- read-only history account
GRANT SELECT ON accounts._schemaops_history TO 'schemaops_prod_ro'@'%';
```

schemaops keeps two tables of its own in each schema: `_schemaops_history` (what was applied, how and when) and `_schemaops_snapshot` (table definitions after the last deployment).

## Pipeline

`.gitlab-ci.yml` with the shared template `ci/common.yml`:

| Job | Runs | Does |
| --- | --- | --- |
| `secret_detection` | Merge requests and merges | GitLab's secret scanner, backing the "no credentials in Git" rule |
| `build-image` | When `Dockerfile` or `pyproject.toml` changes, or by hand with `BUILD_IMAGE=1` | Builds the runner image with `git`, `percona-toolkit` and the Python dependencies |
| `unit-tests`, `integration-tests` | When the tool's code changes | The two test suites; the second uses a throwaway MariaDB service |
| `check-mr` | Every merge request | Governance, lint, DBA approval, promotion evidence; posts the plan |
| `generate-<env>` | Merge that touches that environment | Writes a child pipeline with one deployment job per changed cluster |
| `deploy-dev`, `deploy-uat` | After `generate` | Run that child pipeline; clusters deploy independently, each in its own resource group |
| `plan-prod` | Merge that touches PROD | Shows live table sizes and pre-flight results before anyone starts the deployment |
| `deploy-prod` | Started by hand, after `plan-prod` | As `deploy-uat`, behind the protected `prod` environment |
| `mark-applied` | By hand: `MARK_ENV`, `MARK_CLUSTER`, `MARK_SCHEMA`, `MARK_FILE` | Records a change the DBA team applied manually |
| `retry-failed` | By hand: `RETRY_ENV`, `RETRY_CLUSTER`, `RETRY_SCHEMA` | Re-runs a failed migration after a DBA has checked the table |
| `reconcile` | Pipeline schedule with `SCHEMAOPS_RECONCILE_ENV` | The reconcile report for that environment |

The manual jobs are started from "Run pipeline" on the default branch with their variables set.

**This pipeline has been checked for valid YAML and consistent job references, but it has not been run in GitLab.** Expect to adjust it on first use, in particular the child pipelines, the Docker-in-Docker image build and the secret detection template, which depend on your GitLab version and runners.

## GitLab setup

1. Protect the default branch; allow merges only through approved merge requests; enable "Require approval from code owners" and adapt `CODEOWNERS`, including the DBA team's ownership of `/clusters/*/cluster.yaml`, `/ci/` and the pipeline files.
2. Create environments `dev`, `uat`, `prod`; make `prod` (and ideally `uat`) a protected environment.
3. Add the connection variables as **protected**, **masked**, **environment-scoped** CI/CD variables. Protected variables are only exposed to pipelines on protected branches, so merge request pipelines never receive deployment credentials.
4. Give merge request pipelines only: the DEV and UAT `_HOST` and `_RO_*` variables (unprotected), and `SCHEMAOPS_GITLAB_TOKEN`, a project access token used to read labels and approvals and to post comments. Give it the **Reporter** role and the `api` scope: enough to comment, not enough to push or merge. With `read_api` only, everything works except the comments. The same token must be visible to default-branch pipelines for the post-deployment comment.
5. Give the PROD deploy job the UAT `_RO_*` variables and the UAT job the DEV ones, so each can verify the previous deployment. Lower environments never receive higher-environment credentials.
6. List the DBA team's GitLab user names under `governance.dba-approvers` in `schemaops.yaml`.
7. Runner image: jobs start from `python:3.12-slim` and install `git` and `percona-toolkit` each time. To avoid that, run `build-image` once (it needs a runner that allows Docker-in-Docker), then set the CI/CD variable `SCHEMAOPS_IMAGE` to `$CI_REGISTRY_IMAGE/runner:latest`.
8. Reconcile: create one pipeline schedule per environment on the default branch, each with the variable `SCHEMAOPS_RECONCILE_ENV` set to `dev`, `uat` or `prod`.
9. Notifications (optional): set `SCHEMAOPS_NOTIFY_WEBHOOK` to a Slack-compatible incoming webhook URL, masked.

After a DBA approves a merge request or a label is added, re-run the `check-mr` job. A promotion branch must be up to date with the target branch, because the checks compare against the commit the branch started from.

Hardening: `check-mr` installs schemaops from the target branch, so a merge request cannot weaken the checks it is judged by. A merge request can still edit `.gitlab-ci.yml`; code-owner approval covers that, and hosting the CI configuration in a separate project (Settings > CI/CD > CI/CD configuration file) closes it completely.

## Tests

```bash
python -m pytest
```

The unit suite covers governance, lint, planning, pre-flight checks, the merge request plan, connection validation, secret redaction and deployment logic with an in-memory database stand-in.

The integration suite runs real migrations with the real `pt-online-schema-change` against a disposable MariaDB, using exactly the grants above. It creates and drops the database `schemaops_it`:

```bash
docker run -d --name schemaops-it -p 3306:3306 -e MARIADB_ROOT_PASSWORD=it-root-pw mariadb:11.4
SCHEMAOPS_IT_HOST=127.0.0.1 SCHEMAOPS_IT_ROOT_PASSWORD=it-root-pw python -m pytest tests/integration
```

It is skipped when `SCHEMAOPS_IT_HOST` is not set, and runs in the pipeline as the `integration-tests` job. It covers the full deploy cycle, instant DDL and the fallback to `pt-online-schema-change`, baselining, live table facts, pre-flight failures, the 5-second metadata lock limit, the deployment lock, the read-only account, and the table structure, snapshot, drift and leftover detection. It does not cover TLS connections or replica lag throttling; verify those against your own clusters.
