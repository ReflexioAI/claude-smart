---
paths:
  - "**/*.py"
---

# Pythonic Code Conventions

## 1. Iterables & Collections

- Use `enumerate()` instead of manual index tracking
- Prefer list/dict/set comprehensions over `map()`/`filter()` with lambdas
- Use `zip()` to iterate multiple sequences in parallel
- Use tuple unpacking: `x, y = point` instead of `point[0]`, `point[1]`
- Use `*` unpacking: `first, *rest = items`

## 2. Modern Control Flow

- Use `match`/`case` for complex branching on structure or value
- Use walrus operator `:=` to combine assignment and condition: `if (n := len(items)) > 10:`
- Use truthiness checks: `if items:` not `if len(items) > 0:`
- Use chained comparisons: `0 < x < 10` not `x > 0 and x < 10`
- Use `any()` / `all()` for collection predicates
- Prefer dispatch tables (dicts mapping keys to callables) over long `if/elif` chains

## 3. String Formatting

- Always use f-strings for general string interpolation
- Use `%`-style or comma separation in `logging` calls to defer formatting: `logging.info("User %s logged in", username)`
- Use parameterized queries with driver placeholders (`%s`, `?`) for SQL — never f-strings — to prevent injection

## 4. Resource Management & Safety

- Always use `with` statements (context managers) for files, locks, DB connections
- Prefer EAFP (try/except) over LBYL (if/then check) for high-level logic (file access, network calls, duck-typing)
- In hot loops where failure rate exceeds ~5-10%, prefer LBYL — exception overhead is significant at scale
- Catch specific exceptions, never bare `except:`

## 5. Standard Library

- Use `collections.defaultdict`, `collections.Counter` where appropriate
- Use `NamedTuple` or `dataclasses.dataclass` instead of plain dicts for structured data
- Use `pathlib.Path` instead of `os.path` for filesystem operations
- Use `itertools` for advanced iteration patterns (chain, groupby, product, etc.)
- Use `functools.lru_cache` / `functools.cache` for memoization

## 6. Generators

- Use generator expressions `(x for x in ...)` instead of list comprehensions when only iterating once
- Use `yield` in functions that produce large sequences to avoid loading everything into memory
- Use `yield from` to delegate to sub-generators — cleaner than a manual `for` loop and correctly propagates `.send()`, `.throw()`, and return values

## 7. Decorators

- Use decorators for cross-cutting concerns: logging, auth, timing, retries, validation
- Keep decorator logic thin; delegate heavy work to the wrapped function

## 8. Function Design

- Keep functions short: 10-30 lines ideal, 50 lines max
- Each function should have a single responsibility
- Keep cyclomatic complexity under 10; refactor complex branches into helper functions or dispatch tables
- Max 3 levels of indentation per function; refactor deeper nesting with early returns, guard clauses, or helper functions
- Use guard clauses (early `return`/`raise`/`continue`) to handle edge cases upfront and keep the happy path at the left margin

## 9. Duck Typing & Type Design

### Interfaces: Protocol vs ABC
- Prefer `typing.Protocol` when the **consumer** defines the interface — enables structural subtyping without inheritance and decouples modules
- Use ABCs when the **provider** owns the contract — shared implementation (template methods), runtime instantiation guards, or `register()` for virtual subclasses
- Use Generic Protocols (`class Processor(Protocol[T]):`) for type-safe pipelines and data processors

### Signatures: Accept Broad, Return Narrow
- Accept the broadest useful type in parameters: `Iterable[T]`, `Mapping[K, V]`, `Collection[T]` — not `list[T]` when you only need iteration
- Return concrete types (`list`, `dict`, specific models) so callers get full type information

### Polymorphism
- Avoid `isinstance()` type dispatching in business logic — let duck typing and Protocols handle polymorphism
- Use `isinstance()` only at system boundaries (user input, external APIs) or with `collections.abc` for "goose typing"

### Dunder Methods
- Return `NotImplemented` (not `raise NotImplementedError`) from binary dunder methods (`__eq__`, `__add__`, etc.) to let Python try the reflected operation

### Callable Interfaces
- Use callback Protocols (classes with `__call__`) for complex callable signatures instead of `Callable[..., Any]` — improves readability and allows docstrings

### Runtime Checking
- Use `@runtime_checkable` sparingly — it only validates name existence, not type signatures; for complex validation, prefer explicit structural tests in your test suite

### Domain Safety
- Use `typing.NewType` for primitive types that represent distinct domain concepts (e.g., `UserId = NewType("UserId", int)`) to prevent logic errors that duck typing alone might overlook

## 10. Path Management

### Anchor the Module
- Define a single `_THIS_DIR = Path(__file__).resolve().parent` at the top of any module that uses filesystem paths
- All other paths derive from this anchor using semantically named uppercase constants

### Derive Paths with Semantic Names
```python
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent.parent  # reflexio_ext/scripts/ -> repo root
DATA_DIR = _PROJECT_ROOT / "data"
CONFIG_PATH = _PROJECT_ROOT / "configs" / "default.yaml"
```
- For jumps of 3+ levels, prefer `.parents[n]` with a comment over chaining `.parent`:
  ```python
  _PROJECT_ROOT = _THIS_DIR.parents[2]  # benchmarks/appworld/scripts/ -> repo root
  ```

### No Inline Chaining
- Never use raw `.parent.parent` inside logic — always assign to a named constant first
- If you reference a derived path more than once, name it

### Pathlib Exclusively
- Use `pathlib.Path` for all filesystem operations — avoid `os.path.join`, `os.path.dirname`, etc.
- Use the `/` operator for path joining: `DATA_DIR / "file.txt"`
- Use `.read_text()` / `.write_text()` for simple file I/O instead of `open()` boilerplate

### Package Data Access
- For **editable installs** and local development, `Path(package.__file__).parent` is fine
- For **distributed packages** (PyPI wheels), prefer `importlib.resources` over `__file__` navigation — wheel archives don't guarantee real filesystem paths

### Directory Creation
- Do **not** call `.mkdir()` at module level — creating directories at import time is a surprising side effect
- Call `.mkdir(parents=True, exist_ok=True)` at the point of use, when you actually need the directory to exist
