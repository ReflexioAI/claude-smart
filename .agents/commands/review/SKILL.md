---
name: review
description: Rigorous code review of all uncommitted changes. Analyzes architecture, code quality, security, and engineering best practices. Embeds questions and assumptions inline, then summarizes all proposed changes as a plan for user approval before any edits are made.
---

# Code Review

Perform a rigorous, senior-engineer-level code review of all uncommitted changes in the working tree.

## Core Principles

- **You are a strict reviewer, not a rubber-stamper.** Flag real problems. Do not praise code just to be nice.
- **Never make changes directly.** Your output is a review report and an optional change plan. Wait for explicit user approval before editing any file.
- **Embed assumptions inline.** If you cannot tell whether something is intentional or a mistake, note your assumption in the report and flag it for confirmation.
- **Focus on substance over style.** Formatting issues caught by pre-commit hooks are low priority. **Lint violations and type errors in changed files are substantive findings** — classify them by severity alongside manual review findings.

## Review Depth

By default, run the full checklist. If the user requests a quick review (e.g., `/review --quick`), focus only on:
- Security
- Correctness & Logic
- API & Contract Design
- Error Handling & Resilience

Skip deeper analysis sections (Duplication, Missing Tests, Code Clarity) in quick mode.

## Workflow

### Phase 1 — Gather the diff

If `git diff HEAD` produces no output and `git status` shows no uncommitted changes and no untracked files, inform the user that there are no changes to review and stop.

Run these commands to understand the full scope of uncommitted work:

```bash
# Overview of changed files
git status

# Full diff of all tracked changes (staged + unstaged)
git diff HEAD

# List of untracked files that may need review
git ls-files --others --exclude-standard
```

Read the diff carefully. For every changed file, also read the **full file** (not just the diff hunk) so you understand the surrounding context — imports, class hierarchy, sibling functions, and call sites.

**Run automated checks on changed files (language-aware):**
1. Detect languages from changed file extensions.
2. For Python files (`*.py`): run `ruff check <files>` and `pyright <files>`.
3. For TypeScript/JavaScript files (`*.ts`, `*.tsx`, `*.js`, `*.jsx`, `*.mts`): run `npx tsc --noEmit` and `npx biome check <files>` from the relevant project root (`reflexio/website/` or `reflexio/public_docs/`).
4. Skip linters for languages without configured tooling.

Save the lint and type check output — these results feed into the review checklist below.

### Phase 2 — Understand context

For each changed file:

1. Read the file's component-level `README.md` if one exists (e.g., `reflexio/server/README.md`).
2. Identify the module's responsibility and how it fits into the overall architecture.
3. If the change touches an interface consumed by other modules (API schema, service base class, client method), find and read up to 3 representative consumers — prioritize callers that use the changed interface in different ways.
4. If tests are changed, read the application code under test. If application code is changed, check whether corresponding tests exist and whether they cover the new behavior.

### Phase 3 — Review checklist

Evaluate every change against the following categories. Only report findings that are **actionable** — skip categories where everything looks correct.

When you encounter ambiguity during the checklist, note your assumption inline (e.g., "assuming this is intentional — flagging for confirmation") and continue. Do not stop the review to ask questions.

#### 3.1 Security
- Is user input validated and sanitized before use?
- Are there SQL injection, command injection, or XSS risks?
- Are secrets or credentials hardcoded or logged?
- Are authorization checks in place for protected endpoints?
- Is sensitive data exposed in error messages or logs?
- Are file paths validated to prevent path traversal?

#### 3.2 Correctness & Logic
- Are there off-by-one errors, wrong comparisons, or logic inversions?
- Are edge cases handled (empty inputs, None/null, zero-length collections, boundary values)?
- Do conditional branches cover all expected states?
- Are return types consistent with what callers expect?
- Is async/await used correctly (no missing awaits, no blocking calls in async context)?

#### 3.3 Architecture & Design
- Does the change follow existing patterns in the codebase? If it deviates, is the deviation justified?
- Are responsibilities placed in the right layer (API route vs. service vs. utility)?
- Is there unnecessary coupling between modules that should be independent?
- Are new abstractions justified, or do they add complexity without benefit?
- Does the change violate separation of concerns?
- If a new service/extractor/endpoint is added, does it follow the established pattern?

#### 3.4 API & Contract Design
- Are request/response schemas complete and correct?
- Are field names consistent with existing conventions?
- Are optional vs. required fields set correctly?
- Are default values sensible?
- Is backward compatibility preserved where needed?
- Are enums used where a fixed set of values is expected?

#### 3.5 Error Handling & Resilience
- Are exceptions caught at the right level (not swallowed silently, not leaking implementation details)?
- Are error messages actionable for the caller?
- Are external service failures handled gracefully (LLM calls, database, third-party APIs)?
- Is retry logic appropriate and bounded?
- Are resources cleaned up on failure (connections, file handles)?

#### 3.6 Type Safety & Data Integrity
- Review lint and type check output from Phase 1. All type errors in changed files are findings — classify by severity.
- For TypeScript files, review `tsc` and Biome output from Phase 1 alongside pyright guidance.
- Are type hints present and correct on new/changed functions?
- Are Pydantic models used where structured validation is needed?
- Are there implicit type coercions that could cause subtle bugs?
- Are Optional types handled with proper None checks?
- Are union types narrowed before use?

#### 3.7 Performance
- Are there N+1 query patterns or unnecessary database round-trips?
- Are there large allocations or copies that could be avoided?
- Is pagination used for list endpoints?
- Are there blocking calls in async code paths?
- Is work being repeated that could be cached or deduped?

#### 3.8 Testing
- Do new features have corresponding tests?
- Do bug fixes include a regression test?
- Are tests testing behavior (not implementation details)?
- Are test assertions specific enough to catch regressions?
- Are mocks set up correctly (not masking real bugs)?
- Do tests cover both happy-path and error cases?

#### 3.9 Missing Critical Test Cases
Go beyond checking whether tests exist for the *changed* code. Proactively identify **core logic in the changed files** that lacks test coverage, even if the logic was not modified in this diff. Focus on:

- **Security-sensitive code paths** — input validation, auth checks, path traversal guards, injection defenses. If these are untested, flag them as Significant.
- **Pure functions and utilities** — functions with clear inputs/outputs that are easy to unit test but have no tests.
- **Branching logic with edge cases** — functions with multiple `if`/`else` branches, especially error/fallback branches that are easy to miss.
- **Data transformation and serialization** — code that converts between formats (e.g., DB rows to API responses, file parsing). Incorrect transformations cause subtle bugs.
- **Integration points** — API endpoints, file I/O, external service calls. Even if mocked, the request/response contract should be tested.

For each gap found, suggest specific test cases with descriptive names (e.g., `test_get_conversation_rejects_path_traversal`) and briefly describe what the test should verify. Group suggestions by priority (security first, then correctness, then edge cases).

#### 3.10 Code Duplication & DRY Violations
Duplicated code is one of the biggest threats to long-term maintainability. When the same logic exists in multiple places, bug fixes and feature changes must be applied everywhere — and inevitably some copies get missed, creating inconsistencies and regressions.

**Actively search for duplication.** Do not limit yourself to the diff — when you see a pattern in the changed code, search the broader codebase for similar implementations. Use grep/search with concrete patterns to find duplication. Search for: the function name, distinctive lines from the implementation, shared string literals, or similar parameter signatures. Example: `grep -r 'def process_chunk' --include='*.py'` to find parallel implementations. Specifically look for:

- **Copy-pasted functions or methods** — Functions in different files/classes that do the same thing with minor variations (different variable names, slightly different parameters, same core logic). Flag these even if only one copy is in the diff.
- **Repeated code blocks within a file** — Multiple places in the same file that perform the same sequence of operations (e.g., identical error handling, identical data transformation steps, identical validation logic).
- **Parallel class hierarchies or services** — Multiple services/extractors/handlers that implement near-identical workflows with only the "content" differing. These should typically share a base class or utility.
- **Duplicated constants, prompts, or configuration** — The same string literals, magic numbers, or config structures defined in multiple places. A change to one copy without updating the others causes silent divergence.
- **Near-duplicate data models or schemas** — Pydantic models, TypeScript interfaces, or database schemas that represent the same concept with slightly different field names or types.
- **Repeated conditional logic** — The same `if`/`else` decision tree appearing in multiple places, especially feature-flag checks or permission checks.

For each duplication found:
1. Identify **all** copies (not just the two most obvious ones).
2. Note the **differences** between copies — are they meaningful or accidental?
3. Suggest a concrete consolidation strategy: extract a shared function, create a base class, use a configuration-driven approach, etc.
4. Classify as **Significant** if the duplication spans multiple files or involves logic that is likely to change together. Classify as **Minor** if it is localized and low-risk.

#### 3.11 Code Clarity & Maintainability
- Are variable and function names descriptive and consistent with codebase conventions?
- Is complex logic explained with a comment about *why* (not *what*)?
- Are there dead code paths, unreachable branches, or leftover debug code?
- Are magic numbers or strings extracted into named constants?

#### 3.12 Frontend (when applicable)
- Are components following the project's ShadCN + Tailwind patterns?
- Is state management appropriate (server state vs. client state)?
- Are loading and error states handled in the UI?
- Is the UI consistent with existing pages?
- Are accessibility basics covered (labels, keyboard navigation)?

#### 3.13 Commit Message (when staged)
If changes are staged (`git diff --cached` is non-empty), check whether a commit message convention is used in this repo (inspect recent `git log --oneline -5`). Briefly note whether the staged changes would benefit from a conventional commit prefix, a ticket reference, or a clearer summary.

### Phase 4 — Produce the review report

Output a structured review report using this format:

```
## Code Review Summary

**Files reviewed:** <count>
**Scope:** <one-line description of what the changes do>

### Questions & Assumptions
Items where intent was ambiguous during review. Each entry states the assumption made and asks for confirmation.
- **[FILE:LINE]** — "Assuming X is intentional — is this correct, or should it be Y?"

### Critical Issues (must fix)
Items that would cause bugs, security vulnerabilities, or data loss.
- [ ] **[FILE:LINE]** — Description of the issue and why it matters.

### Significant Issues (should fix)
Items that degrade code quality, violate architecture, or hurt maintainability.
- [ ] **[FILE:LINE]** — Description and recommendation.

### Minor Issues (nice to fix)
Items that are low-risk but would improve the code.
- [ ] **[FILE:LINE]** — Description and suggestion.

### Duplication Issues
Code that is duplicated across files or within files, hurting maintainability.
- [ ] **[FILE1:LINE] ↔ [FILE2:LINE]** — Description of what is duplicated and suggested consolidation approach.

### Missing Critical Test Cases
Core logic that lacks test coverage. Prioritized by risk (security > correctness > edge cases).
- [ ] `test_name_here` — What the test should verify and why it matters.

### Lint & Type Check Results
Automated lint and type check findings on changed files.
- [ ] **[FILE:LINE]** `RULE_CODE` — Description and fix recommendation.

### Observations
Things that are not wrong but worth noting (e.g., "this module is growing large — consider splitting in the future").

### What looks good
Only include this section if there is something genuinely non-obvious that deserves recognition — a clever algorithm, an unusually robust error handling pattern, a well-designed abstraction. Skip this section entirely if there is nothing substantive to highlight.
```

### Phase 5 — Propose change plan (if issues found)

If there are Critical or Significant issues, produce a concrete change plan:

```
## Proposed Changes

### Change 1: <short title>
**File:** <path>
**Issue:** <which review finding this addresses>
**What to change:** <specific description of the modification>

### Change 2: <short title>
...
```

Order changes by severity: Critical fixes first, then Significant, then Minor. If there are only Minor issues, note that the user may choose to defer them.

Then explicitly ask the user:
> "Here is my review and proposed changes. Should I proceed with all changes, some of them, or none?"

**Do NOT make any edits until the user approves.**

## Severity Definitions

| Severity | Definition | Action |
|----------|-----------|--------|
| **Critical** | Will cause bugs, data loss, security vulnerability, or crash in production | Must fix before commit |
| **Significant** | Violates architecture, creates tech debt, missing error handling, missing tests | Should fix before commit |
| **Minor** | Naming, clarity, minor duplication, small improvements | Nice to have, can defer |
| **Observation** | Not a problem today but worth tracking | No action needed now |

## What This Review Does NOT Cover

- **Formatting and whitespace** (handled by pre-commit hooks and ruff format)
- **Import ordering** (handled by ruff `I` rules)
- Line length (handled by linters)
- Whether the feature itself is a good idea (that is a product decision, not a code review)
