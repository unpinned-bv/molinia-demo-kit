# Migration agent: operating instructions

You are migrating a client's Snowflake dbt project, `acme_shop/`, to Molinia,
live in front of an audience. The presenter talks while you work and only
glances at the terminal. Work steadily, keep your output short, and never
print a key.

## Goal

1. Port all 10 models in `acme_shop/`, plus its macros and tests, to Molinia's dialect (DuckDB 1.5.5).
2. Build them on the `molinia` target: `tools/dbt_molinia.py run`, then `tools/dbt_molinia.py test`.
3. Prove with `agent/parity.py` that every model equals Snowflake's output.
4. End with the report described at the bottom of this file.

`agent/MIGRATION_RULES.md` is the rulebook. Follow it exactly and cite its
rule ids (P3, N4, J1 …).

## Guardrails

These are hard rules (H1 to H11 in the rulebook):

- **Snowflake.** Never touch it: no `tools/sf.py`, no `make sf-*`, no `--target snowflake`.
- **Keys.** Never print, echo, cat, grep, source or commit keys or anything under `.secrets/`, and never name `.secrets/` in a command. The kit's tools load the key themselves: `tools/dbt_molinia.py`, `agent/parity.py` and `tools/molinia.py`.
- **What you may read.** Read only `acme_shop/`, `agent/MIGRATION_RULES.md`, `agent/parity.yml` and your own `agent/scratch/`. Never read `exports/`, any `*.parquet` file, or any other file inside or outside the kit. You may *run* the kit's tools without reading them, and copy `agent/snippets/molinia_round_div.sql` into `acme_shop/macros/`. The unloaded files hold unmasked PII and every answer-key row. You see the answer key only as parity counts.
- **One key.** dbt and parity use `MOLINIA_API_KEY` only. Never use `--readonly` or `MOLINIA_READONLY_KEY`. That key is the presenter's.
- **PII.** Never read `main.raw_customer_contacts`, in a model, a test or a query.
- **Data in `main`.** Never write to `main`. Never run `tools/molinia.py ingest`, `empty` or `reset`, `make demo-reset` or `make molinia-*`. No hand-written DDL or DML.
- **Governance.** Never create or change policies, roles, grants or service accounts. Never set `warehouse_id`.
- **Counts, not rows.** Diagnostic queries return counts or aggregates only (rule H8).
- **The checker.** Never edit `agent/parity.py` or `agent/parity.yml`. Never delete or loosen a test, and never rename or drop a column to get a green result.
- **Where you may write.** Edit files inside `acme_shop/` only (copying the kit's macro snippet in is fine). Scratch notes and logs go in `agent/scratch/`. Touch nothing else in the kit.
- **Git.** Commit in small steps on the branch. Never push.
- **Rate limits.** Run one API-using command at a time, never in parallel or in the background. The budget is at most 2 full builds (a `run` plus a `test` is **one**), at most 3 full parity runs, and about 20 diagnostic queries. The free checks (`parse`, `localcheck.py`) are unlimited.
- **Questions.** Don't ask the presenter questions unless you're blocked. Decide by the rulebook and note the decision in the report.

## Commands

Run all commands from the kit root, exactly as written. `tools/dbt_molinia.py`
runs dbt in `acme_shop/` with `--target molinia --profiles-dir .` and loads the
key inside its own process, so the key never enters your shell and no command
names `.secrets/`. It accepts `parse`, `ls`, `compile`, `build`, `run`, `test`
and `retry`, and refuses `--target`, `show`, `seed`, `snapshot`, `docs` and
`run-operation`.

```bash
# free: no API calls, run these as often as you like
.venv/bin/python -c "from dbt.adapters.molinia.connections import MoliniaCredentials as C; from dbt.adapters.molinia.row_types import coerce_rows; assert 'requests_per_minute' in C.__dataclass_fields__; assert coerce_rows([['0']], [5]) == [(0,)]; print('adapter-ok')"
.venv/bin/python tools/dbt_molinia.py parse
.venv/bin/python agent/localcheck.py            # renders every model and runs it on local DuckDB
.venv/bin/python agent/localcheck.py --types    # the same, plus each model's output column types

# BUILD: run the models first, then the tests. Two commands, not one `build`.
.venv/bin/python tools/dbt_molinia.py run  2>&1 | tee agent/scratch/build-1-run.log   # ~40 req, ~1 min
.venv/bin/python tools/dbt_molinia.py test 2>&1 | tee agent/scratch/build-1-test.log  # ~125 req, ~5 min

# rebuild one model and everything downstream of it
.venv/bin/python tools/dbt_molinia.py build -s stg_orders+ 2>&1 | tee agent/scratch/build-stg_orders.log

# after a build that FINISHED with HTTP 429 failures: wait 60 s, then re-run only the failed and skipped nodes
.venv/bin/python tools/dbt_molinia.py retry 2>&1 | tee agent/scratch/build-retry.log

# parity (counts only): all models = 11 queries; a subset = 1 + n
.venv/bin/python agent/parity.py 2>&1 | tee agent/scratch/parity-1.txt
.venv/bin/python agent/parity.py --model stg_orders fct_orders

# what exists on Molinia (2 queries); a diagnostic aggregate (1 query)
.venv/bin/python tools/molinia.py status
.venv/bin/python tools/molinia.py query "select count(*) from staging.stg_orders"
```

**Run `localcheck.py` before every paid command.** It costs nothing and takes a
second: it renders each model with jinja2 and runs it on local DuckDB 1.5.5
against synthetic `raw_*` tables that have the real ingest types. It proves the
SQL binds and what types it produces. It never shows that the values match
Snowflake, which only `agent/parity.py` proves. Use it after each layer so the
build finds real problems, not typos.

**Split the build: `run`, then `test`.** That is the default, not a fallback.
Two reasons. A full `build` takes about 6 minutes against a 10-minute Bash
timeout, and a run that is cut off can leave nothing for `retry` to pick up; the
split gives each half its own 10 minutes. And `run` alone is about 1 minute, so
a broken model surfaces before you spend the 125 test requests on a build that
cannot pass. Together the two commands count as **one** full build against the
budget. A single `.venv/bin/python tools/dbt_molinia.py build` is still correct
and supported, but use it only once you have proven the models with `run`.
Don't split by folder (`-s staging` and so on): the relationships and reconcile
tests span layers, so they fail on a fresh schema.

**Timeouts.** Run every dbt and parity command in the foreground with the Bash
tool's `timeout` set to `600000` (10 minutes, the maximum). Pacing looks like a
hang: the adapter prints `Molinia adapter: pacing to requests_per_minute=25,
waiting 55.4s` and then carries on. Don't interrupt it.

**dbt tests on Molinia.** They work: generic and singular tests run as ordinary
queries and report normally on the adapter pinned in this venv. If instead every
test comes back as `ERROR: '0' is not of type 'integer'`, the venv is on the
released adapter, which mishandles the count Molinia returns as a JSON string.
The tests ran and found zero failing rows. Report that in the "Not portable"
section; never delete or loosen a test to get a green run (H9).

## Step loop

0. **Start.** Run `date +%T` and note the start time. Run
   `rm -rf agent/scratch && mkdir -p agent/scratch` — a previous run's report,
   logs and parity output would otherwise sit there and be mistaken for yours.
   Check the starting state with two commands, because `status` alone shows neither the branch nor the branch list:
   `git -C acme_shop status -sb` must print exactly `## main` and nothing else (clean tree, on `main`), and
   `git -C acme_shop branch --list` must print only `* main` (no `molinia-migration`, no leftover port).
   If either prints anything else, stop and tell the presenter.
   Then run `git -C acme_shop checkout -b molinia-migration` and the `adapter-ok` check.
1. **Read.** Read `agent/MIGRATION_RULES.md` sections 0 to 9 (the appendices are evidence and can be skipped). Then read every file in `acme_shop/` (`dbt_project.yml`, `profiles.yml`, `models/**`, `macros/**`, `tests/**`) and `agent/parity.yml`.
   Print one line: `Plan: <n> files to change, main risks: <…>`.
2. **Project changes (P1 to P4, P7).** Add the `molinia` output to `acme_shop/profiles.yml` exactly as in P1. Remove the Snowflake-only configs, point `sources.yml` at `main` with `raw_<table>` identifiers, and copy the kit's macro in: `cp agent/snippets/molinia_round_div.sql acme_shop/macros/` (don't retype it). Port the client's macros.
   Run the rulebook's section 9 **step 0** source-type query once (1 query) before rewriting any SQL: it decides the N, D and J rules.
   Commit: `git -C acme_shop add -A && git -C acme_shop commit -qm "P1-P4: molinia target, sources in main, drop Snowflake-only configs"`.
3. **Rewrite models and tests.** Work in the order staging, intermediate, marts, then `tests/`, with the smallest rewrite per rule. Commit once per layer, with rule ids in the message.
   After each layer, run `tools/dbt_molinia.py parse`, `agent/localcheck.py` and the section 9 greps from the rulebook. All free.
4. **Build.** Run `tools/dbt_molinia.py run` (about 1 min), read the result, then `tools/dbt_molinia.py test` (about 5 min). Each in the foreground with the 10-minute timeout, teed to `agent/scratch/build-1-run.log` and `-test.log`. The two together are one build against the budget. If a model fails, read the error, fix it, and rebuild only that model: `-s <model>+`.
5. **Prove.** Run `agent/parity.py` (tee it to `agent/scratch/parity-1.txt`) and read the Details block against the table in section 7 of the rulebook.
6. **Fix and repeat.** Fix one cause at a time, rebuild with `-s <model>+`, then re-check with `parity.py --model <model> [children]`. Commit each fix with its rule id.
   Stop working on a model after 3 attempts and record it for the report.
7. **Finish.** Run the full parity once more and tee it to `agent/scratch/parity-final.txt`. Run `date +%T`. Write the report to `agent/scratch/REPORT.md` and print it.

Between steps, print one short status line for the room, for example
`Step 4/7: building 10 models on Molinia (≈1 min), then 63 tests (≈5 min), paced for the rate limit`.

## Final report format

```markdown
## Migration report: acme_shop → Molinia
Branch `molinia-migration` · <n> commits · <start>–<end> (<n> min) · dbt builds: <n> full, <n> selective · parity runs: <n>

### Models ported (<n>/10)
| model | materialization | Molinia relation | dbt | parity |
|---|---|---|---|---|

### Rewrites by category
| category | rule ids | count |
|---|---|---|
(Project/config P · Conditionals & NULLs S · Numbers & money N · Dates D · Semi-structured J · FLATTEN F · Aggregates & windows A · Strings X · Syntax Y; count = changed expressions in the diff)

### Parity (final run)
<the parity table exactly as printed>

### Not portable / open points
- <model.column: what, why, rule, what the reviewer must decide>  (write "none" if none)

### For the reviewer
- The 3 hunks most worth a look, and why (file:line).
- `git -C acme_shop diff main..molinia-migration --stat`
```
