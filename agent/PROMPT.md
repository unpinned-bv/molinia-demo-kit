# The prompt

Paste this once into Claude Code started in the kit root. Claude Code has
already loaded `CLAUDE.md` there. Don't add anything to it. `make prompt`
copies it to the clipboard.

```text
Migrate the Snowflake dbt project in acme_shop/ to Molinia.

Follow CLAUDE.md and agent/MIGRATION_RULES.md exactly: create the branch molinia-migration in acme_shop, port every model, macro and test with the smallest rewrite that works, build on the molinia target with the kit's tools (tools/dbt_molinia.py run, then tools/dbt_molinia.py test), and prove every model with agent/parity.py. Iterate until all 10 models match, or until you can say precisely why one cannot. Commit in small steps and cite rule ids in the commit messages.

Never touch Snowflake, never print a key, never read raw_customer_contacts. Finish with the report in the format from CLAUDE.md.
```

**Why the build is named as two commands.** `tools/dbt_molinia.py` is the
agent's only dbt entry point and it refuses `--target` (it always passes
`--target molinia --profiles-dir .` itself), so a prompt saying "dbt build
--target molinia" would point at a command that is either refused or needs the
key in the agent's own shell, which rule H3 forbids. **CLAUDE.md's Commands
section stays authoritative for how the build is invoked**; this prompt only
has to agree with it.
