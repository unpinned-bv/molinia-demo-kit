# molinia-demo-kit

Migrate a Snowflake dbt project to [Molinia](https://molinia.eu) with a coding
agent, and prove the result model by model.

The agent (Claude Code) ports the SQL against a written rulebook, builds the
project on Molinia through the dbt adapter, and runs a parity check that compares
every model with what Snowflake produced. The reviewer reads two things: a diff in
which every changed line cites a rule, and a parity table. Governance holds
throughout: the agent works with a scoped service-account key, every call it makes
is audited, and a PII table stays masked for every service account.

## Status

Proven end to end on **one** project: the `acme_shop` fixture in this repo
(10 models, 7 source tables, 63 dbt tests), which reached 10/10 parity. Nothing
here has been run against a second project yet. Section 8 of
`agent/MIGRATION_RULES.md` lists what does not port today.

## What's in it

| path | what it is |
|---|---|
| `CLAUDE.md` | the agent's operating instructions: goal, guardrails, commands, step loop, report format |
| `agent/MIGRATION_RULES.md` | the Snowflake → DuckDB rulebook. Every rewrite has a rule id and was verified on DuckDB 1.5.5 (Appendix A is the log) |
| `agent/parity.py` | the acceptance check: per model, a full outer join on the key against Snowflake's output. Returns counts only, never rows |
| `agent/localcheck.py` | a free local dry run of the ported project on DuckDB, with synthetic data |
| `partner/assess.py` | scopes a client estate (Snowflake metadata plus the dbt project) into a report you can price from, and writes `agent/parity.yml` |
| `tools/` | the only way anything touches Snowflake, Molinia or object storage. The tools load keys themselves and never print them |
| `engagement.yml` | every per-client value. `tools/engagement.py check` verifies the kit agrees with it |
| `fixtures/acme_shop/` | the client project exactly as they wrote it, in the Snowflake dialect |
| `setup/snowflake/` | creates the fixture's deterministic source data in Snowflake |

## Setup

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

1. **dbt-molinia.** The kit needs a pre-release build with request pacing, 429
   retry and result-type coercion; it is not on PyPI yet. The `adapter-ok` command
   in `CLAUDE.md` tells you whether the installed adapter has both fixes.
2. **Settings.** Copy each `.secrets/*.env.example` to `.secrets/*.env` and fill it
   in. For Snowflake, put the unencrypted PKCS#8 private key at
   `.secrets/snowflake_rsa_key.p8`. Nothing in `.secrets/` except the templates is
   ever tracked.
3. `make setup` creates `acme_shop/`, the working copy of the client project, as
   its own git repository on `main`.
4. `make test-all` runs both unit suites. They are local only: no Snowflake, no
   Molinia, no object storage.

## The flow

```bash
make sf-setup sf-raw sf-build   # the client's world, in Snowflake
make sf-unload minio-upload     # unload every source table and every model to EU object storage
make molinia-ingest             # sources and answer keys land in Molinia (planned from the bucket)
```

Then move `exports/` out of the kit. It holds the unmasked Parquet and every
answer-key row, and the agent must never be able to read it (rule H11).

Start `claude` in the kit root and give it the prompt in `agent/PROMPT.md`. It
ports on a branch in `acme_shop/`, builds, tests, and runs parity until every
model matches or it can say precisely why one cannot. `make parity` re-runs the
proof at any time. `make demo-reset` drops the agent's output again. `make help`
lists every target.

## The agent's hard rules

In short (`agent/MIGRATION_RULES.md` section 1 has all eleven): it never touches
Snowflake, never sees a key, never reads the masked table or the unloaded Parquet,
gets only counts back from diagnostic queries, writes only to the dbt output
schemas, and may not edit the parity check or loosen a test to get a green result.
