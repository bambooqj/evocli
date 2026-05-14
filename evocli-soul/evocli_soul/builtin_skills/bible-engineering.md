---
name: bible-engineering
description: Use when starting a new project, implementing a new feature, or reviewing code quality. Provides comprehensive engineering constraints from AI Programming Bible 3.1.
tags: [engineering, quality, architecture, documentation]
---

# AI Programming Bible 3.1 — Engineering Constraints

Apply these constraints whenever you are **building or reviewing code for a project** (not for EvoCLI itself).

## [CRITICAL] RULE 0: Zero-Debt Architecture

If this project is **NOT explicitly marked as "Released" or "Production"**:
- Assume **zero legacy users** and **zero backward compatibility requirements**
- **Boldly rewrite** schemas, interfaces, and core logic at the source — update all consumers
- **Never add** compatibility layers, deprecation warnings, or transitionary `if/else` blocks
- Broken compilation is **preferred** over introducing technical debt

## Core Architecture Rules

### Rule 1: Expectation Driven
**Before writing any code**, define:
1. The macro architecture (module boundaries, data flow)
2. The exact outcome of every new function (inputs, outputs, side effects)
3. The success criteria (what does "done" look like?)

### Rule 2: Extreme Decoupling
- Every new feature must be **pluggable and atomic**
- New logic must be confined to a **single file** — no feature spans multiple files unless architecturally necessary
- Each file has **one and only one responsibility**

### Rule 3: Protocol First
1. Define **Types/Schemas/Interfaces first** (Pydantic, TypeScript types, Rust structs)
2. Use them as the **absolute Source of Truth** — all modules import from the schema, never define inline
3. Create `protocols.py` / `types.ts` / `models.rs` **before any business logic**

### Rule 4: Presentation Separation
1. Build **pure business logic first** — no UI, no HTTP handlers
2. Validate it passes **isolated unit tests** via CLI before adding any UI
3. UI/API layer is a thin wrapper over the business logic

### Rule 5: Single-Path (DRY)
- **Scan the codebase** before writing any helper function — it may already exist
- **Never duplicate** functional paths — consolidate, don't copy
- One way to do each thing, documented and shared

### Rule 6: E2E Testing Safeguard
- **Every change** must be validated via End-to-End flows, not just unit tests
- Write the test that proves the user-facing behavior works **before** marking done
- Failing tests are better than missing tests

## Robustness Rules

### Rule 7: Incremental Modification Only
- **Prefer wrappers, inheritance, or middleware** over modifying stable working functions
- Exception: Rule 0 (Zero-Debt) overrides this when the project is unreleased
- When you must modify, make the smallest possible change

### Rule 8: Defensive Programming
Every external input **must** have:
```python
# ✅ Correct: validate at the boundary
class UserInput(BaseModel):
    email: str = Field(..., regex=r'^[^@]+@[^@]+\.[^@]+$')
    timeout_s: int = Field(default=30, ge=1, le=600)

# ❌ Wrong: raw dict access
def handle(params: dict):
    email = params["email"]  # KeyError possible, no validation
```

All async operations **must** include timeouts:
```python
# ✅ Correct
result = await asyncio.wait_for(expensive_operation(), timeout=30.0)

# ❌ Wrong
result = await expensive_operation()  # may hang indefinitely
```

### Rule 9: Industrial Documentation & File Limit
- **Every public function and class** must include a comprehensive docstring:
  - Intent (what does it do?)
  - Parameter boundaries (valid ranges, types)
  - Edge case behaviors (what happens on empty input, error, timeout?)
- **File size limit: 2000 lines** (warn at 1000)
- Run `python bible_check.py <project_dir>` to verify compliance

### Rule 10: Observability & Logging
Inject structured logs at **every critical state change**:
```python
# ✅ Correct: structured, searchable
log.info("order_created", order_id=order.id, user_id=user.id, amount=order.total)

# ❌ Wrong: unstructured string
print(f"Created order for user {user.id}")
```

## Workflow Checklist

When implementing any feature:
- [ ] Defined expected outcome before writing code (Rule 1)
- [ ] Checked for existing helpers before writing new ones (Rule 5)
- [ ] Created data schemas before business logic (Rule 3)
- [ ] Added input validation at boundaries (Rule 8)
- [ ] Added timeouts to all async operations (Rule 8)
- [ ] Each new function has a docstring (Rule 9)
- [ ] Added structured logging at key decisions (Rule 10)
- [ ] Written or updated E2E test (Rule 6)
- [ ] Run `python bible_check.py .` to verify (Rule 9)

## Quick Reference

| Rule | Summary | Tool |
|------|---------|------|
| R0 | No backward compat for unreleased | — |
| R1 | Define outcomes first | todo_write() |
| R2 | One file, one responsibility | bible_check.py |
| R3 | Schemas before code | protocols.py |
| R4 | Business logic before UI | unit tests |
| R5 | No duplicate code paths | code_semantic_search() |
| R6 | E2E test every change | test_and_capture() |
| R7 | Wrap, don't modify | — |
| R8 | Validate all inputs + timeouts | Pydantic, asyncio.wait_for |
| R9 | Docstrings + 2000 line limit | bible_check.py |
| R10 | Structured logs everywhere | trace.get_logger() |
