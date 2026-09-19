---
name: create-pr
description: "Create high-quality pull requests via gh pr create. Use when the user wants to create a PR, submit a PR, open a pull request, submit for review, or push changes for review. Triggers on: create a pr, create-pr, submit a pr, open a pull request, submit for review, make a pr, gh pr create."
---

# PR Creator

Create well-structured, reviewer-friendly pull requests following best practices.

**CRITICAL: This skill MUST always result in a PR being created. Do NOT stop early, do NOT ask the user to run another command first. Handle all prerequisites (committing, branching, pushing) inline and proceed to `gh pr create`.**

---

## Workflow

### Step 1: Pre-flight Checks

Run these checks before anything else:

1. **Verify GitHub CLI authentication** — run `gh api user --jq '.login'` to confirm the CLI is authenticated. If this fails, tell the user to run `gh auth login` first and stop.
2. **Determine the base branch** — default to `main`. If the user specifies a different base, use that. Capture as `$BASE_BRANCH`.

### Step 2: Commit Uncommitted Changes

Run `git status` to check for uncommitted changes. If there are uncommitted changes, commit them **directly** — do NOT delegate to another skill or tell the user to commit first.

1. **Stage changes** — run `git add <files>` for relevant modified/untracked files. Do not stage `.env` or gitignored files.
2. **Lint and format** — if Python files are staged, run:
   ```bash
   ruff check --fix <files> && ruff format <files>
   ```
   Re-stage any modified files.
3. **Commit** — write a conventional commit message based on the staged diff:
   ```bash
   git commit -m "<type>: <description>"
   ```
   If precommit hooks modify files, re-stage with `git add -u` and retry (up to 3 times).

### Step 3: Ensure Feature Branch

1. **Check current branch** — run `git branch --show-current`.
2. **If on `$BASE_BRANCH`**, create a feature branch:
   - Analyze the commits/changes to pick a descriptive name (e.g., `feat/add-auth`, `fix/login-bug`)
   - Run `git checkout -b <branch-name>`
3. **If on a different branch**, check whether the branch has PR-worthy commits vs `$BASE_BRANCH`:
   - Run `git log $BASE_BRANCH..HEAD --oneline`
   - If there are commits, use the current branch as-is
   - If there are NO commits (branch is at same point as base), this means something went wrong — investigate and fix

### Step 4: Sync with Base Branch

1. **Fetch latest** — run `git fetch origin $BASE_BRANCH`
2. **Check for divergence** — run `git log HEAD..origin/$BASE_BRANCH --oneline`
3. **Rebase if needed** — if the base branch has new commits:
   a. Run `git rebase origin/$BASE_BRANCH`
   b. If conflicts arise, surface them to the user — show both sides and ask which to keep. Do not silently resolve.
   c. After resolving, `git add <file>` and `git rebase --continue`

### Step 5: Push

Push the branch to remote:
```bash
git push -u origin HEAD
```
If the push is rejected (e.g., diverged history after rebase), use `git push --force-with-lease -u origin HEAD`.

### Step 6: Analyze Changes and Draft PR

1. **Analyze** — run these in parallel:
   - `git log $BASE_BRANCH..HEAD --oneline` — all commits
   - `git diff $BASE_BRANCH...HEAD --stat` — files changed summary
   - `git diff $BASE_BRANCH...HEAD` — full diff
2. **Check PR size** — if >500 lines changed, warn the user but continue.
3. **Draft title** — under 70 characters, conventional prefix (`feat:`, `fix:`, `refactor:`, `docs:`, `chore:`).
4. **Draft body** using this template:

```
## Summary
<1-5 bullet points explaining what changed and WHY>

## Changes
<Categorized list of what was modified — group by area/concern>

## Diagrams
<OPTIONAL — include Mermaid diagrams when visual aids clarify workflow or architecture changes>

## Test Plan
<How the changes were verified — manual testing steps, automated tests run, curl commands, etc.>
```

Guidelines:
- Explain *why*, not just *what*
- For WIP PRs, use the `--draft` flag instead of `[WIP]` prefix
- Include Mermaid diagrams when they clarify workflows or architecture
- Do NOT include any "Generated with Claude Code" footer or bot attribution
- Do NOT include `Co-Authored-By` lines

### Step 7: Create the PR

Write body to a temp file and create the PR:

```bash
cat > /tmp/pr_body.md <<'EOF'
## Summary
...

## Changes
...

## Test Plan
...
EOF
gh pr create --title "the pr title" --base $BASE_BRANCH --body-file /tmp/pr_body.md
```

**Optional flags:**
- WIP/draft: add `--draft`
- Reviewers: add `--reviewer <handle>`
- Assignees: add `--assignee <handle>`

**Do NOT add:**
- `--author` flag (gh uses the authenticated user automatically)
- Any `Co-Authored-By` trailer
- Any "Generated with Claude Code" footer

### Step 8: Report

- Return the PR URL so the user can review it
- If `gh pr create` fails, diagnose the error and retry with fixes

---

## Best Practices

**Commit History:** If the commit history is messy, suggest rebasing to clean it up before creating the PR.

**Feedback Requests:** If the user mentions wanting specific feedback, add a "Feedback Requested" section to the body.

**Screenshots:** For frontend changes, remind the user to add screenshots or recordings to the PR after creation.
