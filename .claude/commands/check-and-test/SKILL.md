---
name: check-and-test
description: Run lint checks (ruff for Python, Biome for TS/JS), type checks (pyright for Python, tsc for TS/JS), and the standard pytest tiers (unit + e2e + tests skipped during pre-commit). Investigates failures to determine if they are application bugs or test issues, and fixes application bugs rather than weakening tests. Does not run paid-LLM real-API tests or scenario probes from the test-* command family.
---

# Check and Test

Run the standard check-and-test flow (lint + type-check + pytest tiers), including tests normally skipped during pre-commit hooks.

## Overview

This command runs the standard check-and-test tiers:
- **Python lint checks** via ruff (code quality, security, complexity)
- **Python type checks** via pyright (type safety)
- **TypeScript/JavaScript lint checks** via Biome (code quality, formatting, import sorting)
- **TypeScript/JavaScript type checks** via tsc (type safety)
- **Unit tests** in `reflexio/tests/` (excluding e2e and tests under reflexio/tests/server/llm/)
- **E2E tests** in `reflexio/tests/e2e_tests/` (skip all the low priority test by NOT setting RUN_LOW_PRIORITY env variable)
- **Tests skipped during pre-commit** (those decorated with `@skip_in_precommit`)

The key difference from pre-commit is that `PRECOMMIT` env var is NOT set, so all tests run.

## Prerequisites

Before running tests, activate the virtual environment:
```bash
source .venv/bin/activate
```

## Test Execution

**IMPORTANT**: Run all tests sequentially using `-n 0` to avoid test conflicts. The default pytest config uses parallel execution (`-n auto`), but this can cause race conditions and shared state issues between tests.

### Step 0: Run Lint and Type Checks

Run lint and type checks across the full codebase before running tests.

**0a. Ruff auto-fix:**
```bash
ruff check --fix reflexio/
ruff format reflexio/
```

**0b. Ruff remaining errors:**
```bash
ruff check reflexio/
```
If any errors remain that ruff could not auto-fix, **read each error, understand the issue, and fix the code yourself**. Do NOT skip unfixed lint errors.

**0c. Pyright type check:**
```bash
pyright
```
Pyright uses `pyrightconfig.json` for scope. If any type errors are reported, **read each error, understand the type issue, and fix the code yourself**. Do NOT skip unfixed type errors.

**0d. Biome auto-fix (TypeScript/JavaScript):**
```bash
cd reflexio/website && npx biome check --write . && cd ../..
cd reflexio/public_docs && npx biome check --write . && cd ../..
```

**0e. Biome remaining errors:**
```bash
cd reflexio/website && npx biome check . && cd ../..
cd reflexio/public_docs && npx biome check . && cd ../..
```
If any errors remain that Biome could not auto-fix, **read each error, understand the issue, and fix the code yourself**. Do NOT skip unfixed Biome errors.

**0f. TypeScript type check:**
```bash
cd reflexio/website && npx tsc --noEmit && cd ../..
cd reflexio/public_docs && npx tsc --noEmit && cd ../..
```
If any type errors are reported, **read each error, understand the type issue, and fix the code yourself**. Do NOT skip unfixed tsc errors.

**Only proceed to tests after all lint and type errors are resolved.**

### Step 1: Run All Unit Tests (excluding e2e)

Run unit tests first as they are faster and don't require external services:
```bash
pytest reflexio/tests/ --ignore=reflexio/tests/e2e_tests/ -n 0 -v
```

### Step 2: Run E2E Tests

E2E tests require the server to be running at http://localhost:8081. Run them separately:
```bash
pytest reflexio/tests/e2e_tests/ -n 0 -v
```

### Step 3: Run Full Suite (if individual runs pass)

To run everything together:
```bash
pytest reflexio/tests/ -n 0 -v
```

Note: The `-n 0` flag disables pytest-xdist parallel execution, ensuring tests run sequentially to prevent conflicts from shared database state, file locks, or other resource contention.

## Test Failure Investigation Protocol

When a test fails, **ALWAYS** investigate thoroughly before making any changes. Never take shortcuts.

### Investigation Steps

1. **Read the full test failure output**
   - Understand what assertion failed
   - Note the expected vs actual values
   - Check the stack trace for the error location

2. **Read the failing test code**
   - Understand what the test is validating
   - Identify what behavior the test expects

3. **Read the application code being tested**
   - Trace through the code path
   - Understand the intended behavior

4. **Determine root cause - Is this an application bug or test issue?**

### Decision Framework

**It's an APPLICATION BUG if:**
- The code doesn't match documented/expected behavior
- A recent change broke existing functionality
- The test correctly validates a contract that the code violates
- Edge cases are not handled properly
- Error handling is missing or incorrect

**It's a TEST ISSUE if:**
- The test has incorrect assertions (wrong expected values)
- The test setup is flawed (missing mocks, wrong fixtures)
- The test is testing implementation details that legitimately changed
- The test has race conditions or timing issues
- The test dependencies are not properly configured

### Critical Rules

1. **NEVER weaken tests to pass** - If a test checks for important behavior, fix the application
2. **NEVER remove assertions** - Unless they are genuinely wrong
3. **NEVER skip tests** - Unless there's a documented reason and a plan to fix
4. **ALWAYS fix the root cause** - Don't just make the error go away

### Fixing Application Bugs

When you identify an application bug:

1. **Understand the intended behavior** - Check docstrings, README, related tests
2. **Fix the application code** - Make it behave as expected
3. **Verify the fix** - Re-run the failing test
4. **Check for regressions** - Run related tests to ensure nothing else broke
5. **Document if needed** - Add comments explaining non-obvious fixes

### Fixing Test Issues

When you identify a test issue:

1. **Fix the test setup** - Correct fixtures, mocks, or configuration
2. **Update assertions** - Only if the expected values were genuinely wrong
3. **Improve test clarity** - Add comments explaining what's being tested
4. **Re-run to verify** - Ensure the test passes with correct behavior

## Example Investigation

```
FAILED test_user_profile_update - AssertionError: expected 'active' but got 'pending'
```

**Bad approach (NEVER DO THIS):**
```python
# Just change the assertion to pass
assert status == 'pending'  # Changed from 'active'
```

**Good approach:**
1. Read `test_user_profile_update` to understand what it's testing
2. Check if `'active'` is the correct expected status after an update
3. If yes, investigate why the code returns `'pending'` - this is an app bug
4. Fix the application code to return `'active'` when appropriate
5. Re-run the test to confirm the fix

## Test Categories

### Tests with `@skip_in_precommit`

These tests are skipped during pre-commit but should run in full test suite:
- Integration tests requiring real API calls
- Long-running tests
- Tests requiring specific infrastructure

Location: Check `reflexio/tests/server/test_utils.py` for the decorator definition.

### E2E Tests

End-to-end tests in `reflexio/tests/e2e_tests/`:
- Require server running at http://localhost:8081
- Use real database and external services
- Test complete user workflows

### Unit Tests

Fast, isolated tests that:
- Use mocked LLM responses (via conftest.py)
- Don't require external services
- Test individual functions and classes

## Common Issues and Solutions

### LLM Mock Issues
- Unit tests use mocked LLM responses
- E2E tests use real API calls
- Check `conftest.py` for mock behavior

### Database Issues
- E2E tests require running database (SQLite by default)

### Server Not Running
- E2E tests need server at http://localhost:8081
- Start with appropriate command before running e2e tests

## Summary

1. Run all tests without `PRECOMMIT=1` to include skipped tests
2. Investigate every failure thoroughly
3. Distinguish between app bugs and test issues
4. Fix application code when the test is correct
5. Never weaken tests just to make them pass
