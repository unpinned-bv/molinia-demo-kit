# Molinia partner demo kit. Run from the kit root. Every tool loads .secrets/*.env.
# Contract: DESIGN.md ("Tool CLIs"). Snowflake credits and Molinia rate limits
# are real: each target does one thing; nothing here runs implicitly.

PY      := .venv/bin/python
SQL_DIR := setup/snowflake
# The client's dbt project, from engagement.yml. Recursive `=`: only resolved
# when a recipe uses it, so `make help` costs nothing.
PROJECT  = $(shell $(PY) tools/engagement.py show --json | $(PY) -c \
             'import json,sys; print(json.load(sys.stdin)["project_dir"])')

.DEFAULT_GOAL := help
.PHONY: help setup prompt engagement engagement-check sf-setup sf-raw sf-build sf-unload \
        minio-upload molinia-ingest molinia-ingest-raw molinia-ingest-expected \
        molinia-empty molinia-reset molinia-status parity demo-reset localcheck test \
        test-partner test-all

help: ## list targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-z][a-z-]*:.*## / {printf "  %-24s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## first run: create the client project acme_shop/ from fixtures/ (left alone if it exists)
	@if [ -d acme_shop/.git ]; then echo "acme_shop/ exists: left alone"; else \
	  cp -R fixtures/acme_shop acme_shop && git -C acme_shop init -q -b main && \
	  git -C acme_shop add -A && \
	  git -C acme_shop -c user.name="Acme Shop" -c user.email="data@acme.invalid" \
	    commit -qm "Acme Shop analytics (Snowflake)" && \
	  echo "acme_shop/ created on main: the client's Snowflake project, as they wrote it"; fi

prompt: ## copy the demo prompt to the clipboard, and check the terminal is wide enough
	@sed -n '/^```text$$/,/^```$$/p' agent/PROMPT.md | sed '1d;$$d' | pbcopy
	@echo "prompt copied to the clipboard. Paste into Claude Code (Cmd+V), then press Enter."
	@cols=$$(tput cols 2>/dev/null || echo 0); \
	if [ "$$cols" -ge 120 ]; then echo "terminal width: $$cols columns (ok)"; \
	else echo "terminal width: $$cols columns — WIDEN to at least 120, or the parity table wraps"; fi

engagement: ## print the resolved engagement (engagement.yml) and where it came from
	$(PY) tools/engagement.py show

engagement-check: ## check the kit agrees with engagement.yml (H2, parity.yml, prose)
	$(PY) tools/engagement.py check

sf-setup: ## Snowflake: warehouse, database, schemas, stage (00_setup.sql)
	$(PY) tools/sf.py run $(SQL_DIR)/00_setup.sql

sf-raw: ## Snowflake: generate the deterministic raw tables (01_raw_data.sql)
	$(PY) tools/sf.py run $(SQL_DIR)/01_raw_data.sql

sf-build: ## Snowflake: dbt build of the engagement's project against the snowflake target
	$(PY) tools/sf.py dbt -- build

sf-unload: ## Snowflake: unload raw + models to Parquet, GET into exports/
	$(PY) tools/sf.py unload

minio-upload: ## copy exports/<export_prefix>/ to the MinIO bucket (or print manual steps, exit 2)
	$(PY) tools/minio_upload.py

molinia-ingest: ## Molinia: ingest sources + answer keys from the bucket (CREATE OR REPLACE)
	$(PY) tools/molinia.py ingest

molinia-ingest-raw: ## live opening, beat 1: the client's source tables land from the bucket
	$(PY) tools/molinia.py ingest --only raw

molinia-ingest-expected: ## live opening, beat 2: Snowflake's answer keys land (parity proves against them)
	$(PY) tools/molinia.py ingest --only expected

molinia-empty: ## drop the ingested sources + answer keys; masked tables stay (presenter only)
	$(PY) tools/molinia.py empty --yes

molinia-reset: ## Molinia: drop the engagement's dbt schemas (never the source schema)
	$(PY) tools/molinia.py reset

molinia-status: ## Molinia: ingested sources, answer keys and dbt output with row counts
	$(PY) tools/molinia.py status

parity: ## compare every ported model on Molinia with its Snowflake answer key
	$(PY) agent/parity.py

demo-reset: ## ready the demo to run again: drop the agent's Molinia output, show what is left
	$(PY) tools/molinia.py reset
	$(PY) tools/molinia.py status
	@p="$(PROJECT)"; if [ -d "$$p/.git" ]; then \
	  echo "$$(basename $$p): branch $$(git -C "$$p" rev-parse --abbrev-ref HEAD), $$(git -C "$$p" status --porcelain | wc -l | tr -d ' ') uncommitted change(s)"; \
	  echo "  (not touched: restore the client's Snowflake version by hand if the agent changed it)"; \
	fi

localcheck: ## dry-run the ported project on local DuckDB (local only, no API calls)
	$(PY) agent/localcheck.py

test: ## unit tests (local only: no Snowflake, no Molinia, no MinIO)
	$(PY) -m unittest discover -s agent/tests -p 'test_*.py' -v

test-partner: ## the assessor's unit tests (local only)
	$(PY) -m unittest discover -s partner/tests -p 'test_*.py'

test-all: test test-partner ## both suites
