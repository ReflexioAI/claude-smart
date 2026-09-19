---
name: commit
description: Git commit workflow with precommit hook handling, lint/type checking, README updates, and API reference updates. Use when the user wants to commit changes. Handles precommit hooks that modify files (formatting, linting) by re-staging and retrying. Runs ruff lint and pyright type checks on staged Python files, and Biome lint and tsc type checks on staged TS/JS files, fixing all errors. Fixes failing unit tests automatically before committing. Updates README code maps if needed. Updates API reference docs when client.py or service_schemas.py changed. Does not push.
---

# Commit

Create a git commit with automatic precommit hook handling, test fixing, README updates, and API reference updates.

**Note:** Step 4 and the API Reference Update Guidelines are specific to the Reflexio project. They are skipped automatically when the referenced paths do not exist.

## Workflow

1. **Check git status** - Run `git status` and `git diff --cached --name-only` to see staged/unstaged changes
2. **Stage files** - Add relevant untracked/modified files if needed. Do not modify or change gitignored files, such as `.env`. Never change `.env` file even if it is modified.
3. **Check README updates** - Run through the README Update Guidelines checklist below. If ANY criteria match, update README files before proceeding.
4. **Update API Reference docs (Reflexio-specific)** — If the files `reflexio/reflexio_client/reflexio/client.py` or `reflexio/reflexio_commons/reflexio_commons/api_schema/service_schemas.py` exist AND are in the staged changeset, update `reflexio/public_docs/api-reference/` (see API Reference Update Guidelines below). Otherwise skip.
5. **Run lint and type checks on staged Python files**
   a. Get the list of staged Python files:
      ```bash
      git diff --cached --name-only --diff-filter=ACMR -- '*.py'
      ```
      If no Python files are staged, skip this step entirely.
   b. **Ruff auto-fix**: Run `ruff check --fix <files>`. Re-stage any modified files with `git add <files>`.
   c. **Ruff format**: Run `ruff format <files>`. Re-stage any modified files with `git add <files>`.
   d. **Ruff remaining errors**: Run `ruff check <files>`. If any errors remain that ruff could not auto-fix, **read each error, understand the issue, and fix the code yourself**. Re-stage fixes. Do NOT proceed with unfixed ruff errors.
   e. **Pyright type check**: Run `pyright <files>`. If any type errors are reported, **read each error, understand the type issue, and fix the code yourself**. Re-stage fixes. Do NOT proceed with unfixed pyright errors.
   f. Get the list of staged TypeScript/JavaScript files:
      ```bash
      git diff --cached --name-only --diff-filter=ACMR -- '*.ts' '*.tsx' '*.js' '*.jsx' '*.mts'
      ```
      If no TS/JS files are staged, skip steps 5g-5i entirely.
   g. **Biome auto-fix**: Run `npx biome check --write <files>` from the relevant project root
      (`reflexio/website/` or `reflexio/public_docs/` depending on file path).
      Re-stage any modified files with `git add <files>`.
   h. **Biome remaining errors**: Run `npx biome check <files>`.
      If any errors remain that Biome could not auto-fix, **read each error, understand the issue,
      and fix the code yourself**. Re-stage fixes. Do NOT proceed with unfixed Biome errors.
   i. **TypeScript type check**: Run `npx tsc --noEmit` from the relevant project root.
      If any type errors are reported, **read each error, understand the type issue,
      and fix the code yourself**. Re-stage fixes. Do NOT proceed with unfixed tsc errors.
6. **Attempt commit** - Run `git commit` which triggers precommit hooks
7. **Handle hook results**:
   - If hooks **modify files** (formatting, linting): Stage the modified files with `git add -u` and retry commit
   - If **unit tests fail**: Fix the failing tests, stage fixes, and retry commit
   - If hooks **pass**: Commit succeeds
8. **Do NOT push** - Only commit locally

## README Update Guidelines

Before committing, **always check** if README files need updates. Follow `how_to_write_readme.md` in the repo root.

**Step 1: Analyze changes with these commands:**
```bash
# See what files are being committed
git diff --cached --name-only

# See detailed changes
git diff --cached --stat

# For unstaged changes
git diff --name-only
```

**Step 2: Check if README update is REQUIRED** (update if ANY are true):
- [ ] New directory created (e.g., `services/email/`)
- [ ] New Python module/file added to existing component
- [ ] New API endpoint added
- [ ] New service or extractor added
- [ ] Architecture pattern changed
- [ ] Component relationships changed

If **none** of the above criteria match, skip README updates entirely and proceed to the next step.

**Pre-write criteria — before writing any README changes, confirm:**
1. Can an LLM currently find the right file with the existing README? If yes, no update needed.
2. Are there new files/directories that need documented paths?
3. Are new API endpoints to list?
4. Are there anti-patterns to highlight (NEVER/ALWAYS)?

Only proceed with the update if at least one criterion identifies a gap.

**Step 3: Update process:**
1. Identify affected component(s) from the file paths
2. Read existing README(s) in affected directories
3. Update component-level README first (e.g., `reflexio/server/README.md`)
4. Update main `README.md` if high-level structure changed
5. Stage README changes: `git add README.md src/*/README.md`

**Two-tier approach:**
- **Main Code Map** (`README.md`) - High-level overview of all components
- **Component Code Maps** (e.g., `reflexio/server/README.md`) - Detailed documentation

## API Reference Update Guidelines

When changes are made to the Reflexio client or service schemas, update the API reference documentation.

**Source files to watch:**
- `reflexio/reflexio_client/reflexio/client.py` - Client class methods
- `reflexio/reflexio_commons/reflexio_commons/api_schema/service_schemas.py` - Request/Response models and enums

**Documentation to update:**
- `reflexio/public_docs/api-reference/client.md` - Client method documentation
- `reflexio/public_docs/api-reference/schemas.md` - Schema/model documentation

**When to update:**
- New client method added → Add method documentation to `client.md`
- Client method signature changed → Update parameters/return types in `client.md`
- New request/response model added → Add model documentation to `schemas.md`
- Model fields changed → Update field tables in `schemas.md`
- New enum added or enum values changed → Update enum documentation in `schemas.md`

**Update process:**
1. Read the changed source file to understand what was added/modified
2. Read the existing documentation file(s) to understand current format
3. Update documentation to match the source code changes:
   - For methods: Include signature, parameter table, return type, response schema, and examples
   - For schemas: Include field table with Type, Description, and Default columns
   - For enums: List all values with descriptions
4. Maintain consistent formatting with existing documentation
5. **Verify format consistency:** After updating, re-read the full documentation file and confirm the new section matches the structure and formatting of an adjacent existing section (same heading levels, table format, example style).

**Documentation style:**
- Use markdown tables for parameters and fields
- Include code examples for new methods
- Reference related schemas using links (e.g., `[UserProfile](#userprofile)`)
- Keep descriptions concise but complete

## Commit Message Format

Use conventional commit style:
```
<type>: <short description>

<optional body explaining why>
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `style`

**How to write the message:**
- Read `git diff --cached` to understand the full scope of staged changes.
- The subject line should summarize the *why*, not list files changed.
- Include a body only for non-trivial changes — explain motivation, trade-offs, or context that isn't obvious from the diff.
- If the staged changes span multiple unrelated concerns, note this to the user and suggest splitting into separate commits rather than writing a vague summary.
- Check `git log --oneline -5` and match the existing commit style of the repo.

**Do NOT include** `Co-Authored-By` lines in commit messages.

## Precommit Hook Retry Logic

When precommit hooks fail due to file modifications:
```bash
# Check what was modified by hooks before re-staging
git diff --name-only
# Verify no unexpected files (binaries, generated assets) are being re-staged
git add -u
git commit -m "..."
```
Before running `git add -u`, review the output of `git diff --name-only`. If unexpected files appear (compiled assets, binary files, generated artifacts), stage only the expected files explicitly instead of using `git add -u`.

Retry up to 3 times. If the commit still fails after 3 retries, stop and report the hook output to the user rather than retrying further.

## Unit Test Failure Handling

When pytest fails during precommit:
1. Read the test failure output to understand what failed
2. Fix the application code causing the failure first. Only modify the test itself if the test is clearly wrong (e.g., it tests old behavior that the current commit intentionally changes).
3. **Never** delete or comment out assertions to make tests pass.
4. Stage the fixes with `git add`
5. Retry the commit

If tests still fail after 3 fix attempts, stop and report the failures to the user.
