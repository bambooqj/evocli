# Tool Reference

All tools available in EvoCLI. Rust tools are called via `bridge.call(tool, params)` from Python. Python tools are registered via `@agent.tool_plain` and visible to the LLM.

---

## Rust Tools (62 total)

### File System — `fs.*`

#### `fs.read`
Read file contents.
```json
params: { "path": "src/main.rs" }
returns: string (file contents)
```

#### `fs.write`
Write or overwrite a file.
```json
params: { "path": "src/main.rs", "content": "..." }
returns: { "written": true }
```

#### `fs.diff`
Generate unified diff between two strings.
```json
params: { "old": "...", "new": "...", "path": "hint for header" }
returns: string (unified diff)
```

#### `fs.apply_diff`
Apply a unified diff to a file.
```json
params: { "path": "src/main.rs", "diff": "--- ...\n+++ ...\n@@..." }
returns: { "applied": true }
```

---

### Git — `git.*`

#### `git.status`
Get working tree status.
```json
params: {}
returns: string (git status output)
```

#### `git.commit`
Commit staged changes.
```json
params: { "message": "feat: add feature", "files": ["src/main.rs"] }
returns: { "sha": "abc123" }
```

#### `git.diff`
Show diff for a file or entire working tree.
```json
params: { "path": "src/main.rs" }  // optional
returns: string (diff output)
```

#### `git.restore`
Restore file to last commit.
```json
params: { "path": "src/main.rs" }
returns: { "restored": true }
```

#### `git.snapshot`
Create a stash snapshot for rollback safety.
```json
params: { "message": "before refactor" }
returns: { "ref": "stash@{0}" }
```

#### `git.shadow_snapshot` / `git.shadow_restore`
Shadow-git side-car snapshots (don't touch project `.git`).
```json
params: { "label": "checkpoint-1" }
```

---

### Shell — `shell.*`

All shell commands are checked against the security blacklist before execution.

#### `shell.run`
Execute a shell command.
```json
params: { "cmd": "cargo check", "cwd": "/path", "timeout_s": 30 }
returns: { "stdout": "...", "stderr": "...", "exit_code": 0 }
```

#### `shell.grep`
Grep file contents.
```json
params: { "pattern": "fn main", "path": "src/", "recursive": true }
returns: [{ "file": "...", "line": 1, "text": "..." }]
```

#### `shell.find`
Find files by name pattern.
```json
params: { "pattern": "*.rs", "path": "src/" }
returns: ["src/main.rs", ...]
```

#### `shell.ls`
List directory contents.
```json
params: { "path": "src/" }
returns: [{ "name": "main.rs", "is_dir": false, "size": 1234 }]
```

#### `shell.cat` / `shell.head` / `shell.tail`
Read file with limits.
```json
params: { "path": "file.txt", "lines": 50 }
returns: string
```

#### `shell.mkdir` / `shell.touch` / `shell.mv` / `shell.cp` / `shell.rm`
Standard file operations.
```json
// mkdir
params: { "path": "src/new_dir" }
// mv / cp
params: { "src": "a.txt", "dst": "b.txt" }
// rm
params: { "path": "a.txt" }
```

#### `shell.wc`
Word/line/byte count.
```json
params: { "path": "src/main.rs" }
returns: { "lines": 42, "words": 200, "bytes": 1500 }
```

#### `shell.sed` / `shell.awk` / `shell.sort` / `shell.uniq` / `shell.cut` / `shell.tr`
Text processing utilities.
```json
params: { "cmd": "..." }   // full command string passed to shell
```

---

### Search — `search.*`

#### `search.code`
Search codebase for a pattern using hybrid BM25+vector search.
```json
params: { "query": "authentication token", "path": "src/" }
returns: [{ "file": "...", "line": 5, "score": 0.9, "text": "..." }]
```

---

### Code Intelligence — `code_intel.*`

#### `code_intel.find_symbol`
Find a symbol by name.
```json
params: { "name": "validate_token", "kind": "function" }
returns: { "file": "...", "line": 42, "signature": "fn validate_token(...)" }
```

#### `code_intel.list_symbols`
List all symbols in a file or directory.
```json
params: { "path": "src/auth.rs" }
returns: [{ "name": "...", "kind": "...", "line": 1 }]
```

#### `code_intel.incoming_calls`
Functions that call a given symbol.
```json
params: { "symbol_id": "auth::validate_token" }
returns: [{ "name": "handle_request", "file": "...", "line": 10 }]
```

#### `code_intel.outgoing_calls`
Functions called by a given symbol.
```json
params: { "symbol_id": "auth::validate_token" }
returns: [{ "name": "verify_signature", "file": "...", "line": 5 }]
```

#### `code_intel.full_chain`
Full upstream call chain (all callers recursively).
```json
params: { "symbol_id": "auth::validate_token", "depth": 5 }
returns: { "tree": { "name": "...", "callers": [...] } }
```

#### `code_intel.full_downstream_chain`
Full downstream call chain (all callees recursively).

#### `code_intel.impact_radius`
Which symbols would be affected by changing a given symbol.
```json
params: { "symbol_id": "auth::validate_token" }
returns: { "affected": [...], "risk": "HIGH" }
```

#### `code_intel.index_status`
Check indexing status and coverage.
```json
params: {}
returns: { "indexed_files": 42, "total_symbols": 1200, "last_updated": "..." }
```

#### `code_intel.ingest_tree_sitter`
Force re-index a file or directory.
```json
params: { "path": "src/" }
```

#### `code_intel.ranked_context`
Get PageRank-weighted relevant symbols for a given context.
```json
params: { "modified_file": "src/auth.rs", "mentioned": ["validate_token"], "limit": 20 }
returns: [{ "symbol_id": "...", "score": 0.9, "snippet": "..." }]
```

---

### Symbol Analysis — `symbol.*`

#### `symbol.lookup`
Precise symbol lookup (id + file + line).
```json
params: { "name": "validate_token" }
returns: { "found": true, "symbols": [{ "id": "auth::validate_token", "file": "...", "line": 42 }] }
```

#### `symbol.variants`
All variants/implementations of an enum or trait.
```json
params: { "type_name": "AppState" }
returns: [{ "variant": "Idle" }, { "variant": "Streaming" }]
```

#### `symbol.usages`
All places a symbol is used.
```json
params: { "symbol_id": "auth::validate_token", "limit": 50 }
returns: [{ "file": "...", "line": 10, "context": "..." }]
```

#### `symbol.lifecycle`
How a symbol is created, used, and destroyed across its lifetime.
```json
params: { "symbol_id": "auth::Token" }
returns: { "created": [...], "used": [...], "dropped": [...] }
```

---

### Assumption Verifier — `assume.*`

Tools for verifying code assumptions before making changes.

#### `assume.verify`
Verify a natural-language assumption about code.
```json
params: { "assumption": "validate_token has exactly 1 caller", "subject": "auth::validate_token" }
returns: { "verified": false, "actual": "3 callers found", "evidence": [...] }
```

#### `assume.is_pure`
Check if a function has no side effects.
```json
params: { "symbol": "auth::hash_password" }
returns: { "is_pure": true, "confidence": 0.9 }
```

#### `assume.caller_count`
Count callers of a symbol.
```json
params: { "symbol": "auth::validate_token" }
returns: { "count": 3 }
```

#### `assume.has_tests`
Check if a symbol has test coverage.
```json
params: { "symbol": "auth::validate_token" }
returns: { "has_tests": true, "test_names": ["test_valid_token", ...] }
```

#### `assume.has_side_effects`
Check what side effects a function has.
```json
params: { "symbol": "auth::login" }
returns: { "effects": ["writes_db", "sends_email"] }
```

#### `assume.is_only_caller`
Check if the AI's current context is the only caller.
```json
params: { "symbol": "auth::internal_hash" }
returns: { "is_only_caller": false }
```

#### `assume.is_deprecated`
Check if a symbol is deprecated.
```json
params: { "symbol": "auth::old_login" }
returns: { "is_deprecated": true, "replacement": "auth::login_v2" }
```

#### `assume.types_match`
Verify that two types are compatible.
```json
params: { "type_a": "Token", "type_b": "AuthToken" }
returns: { "match": false, "reason": "different structs" }
```

---

### Impact Analysis — `impact.*`

#### `impact.check`
Check risk level of modifying a symbol.
```json
params: { "symbol": "auth::validate_token", "change_type": "signature" }
returns: { "risk": "CRITICAL", "affected_count": 15, "callers": [...] }
```
`change_type`: `"behavior"` | `"signature"` | `"delete"`

#### `impact.affected_tests`
Which tests would break if a symbol changed.
```json
params: { "symbol": "auth::validate_token" }
returns: [{ "test": "test_login", "file": "tests/auth_test.rs", "line": 10 }]
```

#### `impact.batch_check`
Check impact for multiple symbols at once.
```json
params: { "symbols": ["auth::validate_token", "auth::login"] }
returns: [{ "symbol": "...", "risk": "HIGH", "affected": 5 }]
```

---

### Equivalence Analysis — `equiv.*`

#### `equiv.find`
Find semantically equivalent code patterns.
```json
params: { "pattern": "token.is_expired()", "scope": "src/" }
returns: [{ "file": "...", "line": 5, "code": "...", "similarity": 0.95 }]
```

#### `equiv.check_deps`
Check if two code paths have equivalent dependencies.
```json
params: { "path_a": "src/auth.rs:42", "path_b": "src/auth_v2.rs:10" }
returns: { "equivalent": false, "differences": [...] }
```

#### `equiv.find_similar_code`
Find code similar to a given snippet.
```json
params: { "code": "fn validate(token: &str) -> bool {", "threshold": 0.8 }
returns: [{ "file": "...", "line": 5, "similarity": 0.92 }]
```

---

### Memory — `memory.*`

#### `memory.recall`
Search memory for relevant context.
```json
params: { "query": "authentication design decisions", "top_k": 10 }
returns: [{ "id": "...", "title": "...", "body": "...", "score": 0.9 }]
```

#### `memory.write`
Write a note to memory.
```json
params: { "title": "Auth uses JWT", "body": "...", "tags": ["auth", "security"] }
returns: { "id": "mem_abc123" }
```

#### `memory.constraints`
Get all active project constraints.
```json
params: {}
returns: [{ "rule": "No direct DB access from handlers", "added": "2026-01-01" }]
```

---

### Verification — `verify.*`

#### `verify.task`
Verify that a task has been completed correctly.
```json
params: { "task": "Add token expiry check", "criteria": ["test passes", "no regressions"] }
returns: { "passed": true, "checks": [...] }
```

#### `verify.coverage`
Check test coverage for a file or function.
```json
params: { "path": "src/auth.rs" }
returns: { "coverage_pct": 78, "uncovered_lines": [42, 55] }
```

#### `verify.drift`
Check if implementation has drifted from spec/constraints.
```json
params: { "path": "src/auth.rs" }
returns: { "drifted": false, "violations": [] }
```

---

### Approval & Interaction — `approval.*` / `prompt.*`

#### `approval.request`
Ask the user to approve an action before proceeding.
```json
params: { "message": "About to delete 3 files. Continue?", "action": "rm src/old_auth.rs" }
returns: { "approved": true }
```

#### `prompt.choice`
Present the user with a list of options.
```json
params: {
  "title": "How should I fix the type error?",
  "options": [
    { "id": "change_type", "label": "Change the type to String" },
    { "id": "add_cast",    "label": "Add .to_string() call" },
    { "id": "skip",        "label": "Skip for now" }
  ],
  "allow_custom": true
}
returns:
  { "type": "selected", "id": "change_type" }
  { "type": "custom",   "text": "user typed something" }
  { "type": "cancelled" }
```

---

### User Tools — `tool.*`

#### `tool.list_user`
List user-registered custom tools.
```json
params: {}
returns: [{ "name": "my_lint", "cmd": "./tools/lint.sh", "description": "Run linter" }]
```

#### `tool.run_user`
Execute a user-registered tool.
```json
params: { "name": "my_lint", "args": ["src/"] }
returns: { "stdout": "...", "exit_code": 0 }
```

---

## Python Tools (LLM-visible, 55+)

These are registered in `agent.py` via `@agent.tool_plain` and appear in the LLM's function-calling list. They call the Rust tools above via `bridge.call()`.

| Python Tool | Maps to Rust Tool | Notes |
|---|---|---|
| `fs_read` | `fs.read` | |
| `fs_write` | `fs.write` | |
| `fs_apply_diff` | `fs.apply_diff` | |
| `shell_run` | `shell.run` | Security-checked |
| `shell_grep` | `shell.grep` | |
| `shell_find` | `shell.find` | |
| `shell_ls` | `shell.ls` | |
| `shell_cat` | `shell.cat` | |
| `git_status` | `git.status` | |
| `git_commit` | `git.commit` | |
| `git_snapshot` | `git.snapshot` | |
| `git_diff` | `git.diff` | |
| `search_code` | `search.code` | |
| `memory_recall` | `memory.recall` | |
| `memory_write` | `memory.write` | |
| `memory_constraints` | `memory.constraints` | |
| `symbol_lookup` | `symbol.lookup` | |
| `symbol_variants` | `symbol.variants` | |
| `symbol_usages` | `symbol.usages` | |
| `code_intel_full_chain` | `code_intel.full_chain` | |
| `code_intel_incoming_calls` | `code_intel.incoming_calls` | |
| `code_intel_outgoing_calls` | `code_intel.outgoing_calls` | |
| `code_intel_impact_radius` | `code_intel.impact_radius` | |
| `code_intel_ranked_context` | `code_intel.ranked_context` | |
| `assume_has_tests` | `assume.has_tests` | |
| `assume_caller_count` | `assume.caller_count` | |
| `assume_is_pure` | `assume.is_pure` | |
| `assume_verify` | `assume.verify` | |
| `impact_check` | `impact.check` | |
| `impact_affected_tests` | `impact.affected_tests` | |
| `impact_batch_check` | `impact.batch_check` | |
| `equiv_find` | `equiv.find` | |
| `approval_request` | `approval.request` | Shows modal in TUI |
| `tool_list_user` | `tool.list_user` | |
| `tool_run_user` | `tool.run_user` | |
