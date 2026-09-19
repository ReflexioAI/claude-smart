---
name: update-pr
description: "Update an existing pull request with new changes. Use when the user wants to update a PR, push follow-up changes to a PR, refresh a PR description, or sync a PR with latest commits. Triggers on: update pr, update-pr, update the pr, push to pr, refresh pr, sync pr, update pull request."
---

# PR Updater

Push follow-up changes to an existing PR and update its description to reflect the new work.

---

## Workflow

### Step 1: Pre-flight Checks

1. **Verify GitHub CLI authentication** — run `gh api user --jq '.login'` to confirm the CLI is authenticated. If this fails, the user needs to run `gh auth login` first.
2. **Find the existing PR** — run `gh pr view --json number,title,body,url,baseRefName,state` to find the PR for the current branch. If no PR exists, abort and suggest using `/create-pr` instead. Check the `state` field — if it is not `OPEN`, abort and inform the user that the PR is already merged or closed.
3. **Verify clean git state** — run `git status` to ensure no uncommitted changes. If there are uncommitted changes, run the `/commit` skill first to commit them.
4. **Sync submodules** — check if `.gitmodules` exists first. If it does, run `git submodule update --init --recursive`. If not, skip this step.

### Step 2: Sync with Base Branch

Ensure the feature branch is up-to-date with the base branch to avoid merge conflicts:

1. **Fetch latest remote** — run `git fetch origin <base-branch>` (use the base branch from the PR metadata)
2. **Check for divergence** — run `git log HEAD..origin/<base-branch> --oneline` to see if the base branch has new commits
3. **Rebase if needed** — if there are new commits on the base branch:
   a. Run `git rebase origin/<base-branch>`
   b. **Resolve conflicts** — if the rebase hits conflicts:
      - Read the conflicting files to understand both sides
      - Surface the conflict to the user — show both sides and ask which version to keep. Do not silently resolve conflicts.
      - Stage resolved files: `git add <file>`
      - Continue: `git rebase --continue`
      - Repeat until rebase completes
4. **Verify clean state** — run `git status` to confirm no unresolved conflicts remain
5. **Push the branch** — run `git push`. If the rebase changed history, use `git push --force-with-lease` instead. If `--force-with-lease` fails, it means someone else has pushed to this branch. Fetch the remote branch (`git fetch origin <branch>`), inspect the divergence (`git log HEAD..origin/<branch> --oneline`), and ask the user how to proceed — they may need to integrate the other contributor's changes first.

### Step 3: Analyze New Changes

Understand the full scope of changes now in the PR:

1. Run `git log origin/<base-branch>..HEAD --oneline` to see all commits on this branch
2. Run `git diff origin/<base-branch>...HEAD --stat` for a high-level summary of changed files
3. Run `git diff origin/<base-branch>...HEAD` to read the full diff. Note: Three-dot (`...`) syntax is intentional — it shows only the changes introduced on this branch since it diverged from the base, excluding commits on the base branch that aren't part of this PR.
4. Identify the type of change: `feat`, `fix`, `refactor`, `docs`, `chore`, etc.
5. **Review existing PR title and body** — compare the current PR title and body (from Step 1) against the full diff. Note what's already accurately described vs. what's missing, outdated, or no longer relevant.

**Important:** Look at ALL commits, not just the latest one. The updated PR description should reflect the entire branch, not just the new additions.

### Step 4: Update the PR Description

#### Title

- Under 70 characters
- Use conventional prefix: `feat:`, `fix:`, `refactor:`, `docs:`, `chore:`
- Review the existing PR title (from Step 1). If it still accurately reflects the full scope of changes, keep it. If the scope has changed or the title is misleading, update it.

#### Body

Review the existing PR body (from Step 1) and update it to reflect the current state of the branch. Preserve any still-accurate content (e.g., context, links, decisions) rather than rewriting from scratch. Use this template — scale detail with PR complexity:

```
## Summary
<1-5 bullet points explaining what changed and WHY — covering ALL changes in the branch>

## Changes
<Categorized list of what was modified — group by area/concern>

## Diagrams
<OPTIONAL — include Mermaid diagrams when visual aids clarify workflow or architecture changes>

## Test Plan
<How the changes were verified — manual testing steps, automated tests run, curl commands, etc.>
```

Guidelines for the body:
- State the purpose clearly — explain *why*, not just *what*
- Cover ALL changes in the branch, not just the latest commits
- Provide context and background with links to relevant issues/docs
- Include Mermaid diagrams when they simplify explanation of workflows or architecture
- Keep PRs focused on a single concern — suggest splitting if the PR is too large
- Before overwriting the PR body, compare the existing body against the standard template sections (Summary, Changes, Diagrams, Test Plan). Flag any sections or content that appear to have been manually added after PR creation (e.g., reviewer notes, deployment checklists, linked discussions) and ask the user whether to preserve them.
- Do NOT include any "Generated with Claude Code" footer or bot attribution lines
- Do NOT include `Co-Authored-By` lines

### Step 5: Apply Updates

Write the body to a temp file and use `--body-file` to avoid shell argument length limits:

```bash
cat > /tmp/pr_body.md <<'EOF'
## Summary
...

## Changes
...

## Test Plan
...
EOF
gh pr edit --title "the pr title" --body-file /tmp/pr_body.md
```

**Do NOT add:**
- `--author` flag
- Any `Co-Authored-By` trailer
- Any "Generated with Claude Code" footer

**Draft/Ready handling:** If the user wants to mark a draft PR as ready for review, run `gh pr ready`. If the user wants to convert to draft, run `gh pr ready --undo`.

### Step 6: Report

- Return the PR URL so the user can review it
- Summarize what changed in the PR description compared to before
- If `gh pr edit` fails, diagnose the error and suggest fixes

---

## Best Practices Encoded

**PR Size:** Keep PRs small and focused. If the diff is very large (>500 lines changed), suggest splitting into smaller PRs.

**Commit History:** If the commit history is messy, suggest rebasing to clean it up. Clean commits that explain *why* make review much easier.

**Feedback Requests:** If the user mentions wanting specific feedback, add a "Feedback Requested" section to the body.

**Screenshots:** For frontend changes, remind the user to add screenshots or recordings to the PR after updating.
