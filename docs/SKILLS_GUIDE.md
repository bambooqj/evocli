# Skill Authoring Guide

Skills are reusable multi-step workflows defined in TOML files. The AI can discover, suggest, and execute skills automatically. Users can also run them with `/skill <name>`.

---

## File Locations

Skills are loaded from these directories (in priority order):

1. `{project}/.evocli/skills/` — project-specific skills
2. `~/.evocli/skills/` — user-global skills
3. Built-in: `evocli-soul/evocli_soul/builtin_skills/` (shipped with EvoCLI)

---

## Skill File Format

```toml
# Required metadata
[skill]
id          = "fix_compile_error"         # unique identifier (snake_case)
name        = "Fix Compile Error"         # human-readable name
description = "Run cargo check and fix any compile errors using LLM analysis"
version     = "1.0"
tags        = ["rust", "fix", "compile"]  # used for discovery

# Optional: approval requirement
require_approval = true   # user must approve before execution (default: false)

# Steps executed in order
[[steps]]
id     = "check"
action = "shell.run"
params = { cmd = "cargo check 2>&1" }

[[steps]]
id     = "analyze"
action = "llm.analyze"
params = {
  prompt_template = "fix_error",
  input = "${steps.check.stdout}",
  tier  = "smart"             # "fast" or "smart" (default: "smart")
}

[[steps]]
id     = "apply"
action = "fs.apply_diff"
params = {
  path = "${steps.analyze.file}",
  diff = "${steps.analyze.diff}"
}
```

---

## Available Actions

### `shell.run`
Execute a shell command.
```toml
[[steps]]
action = "shell.run"
params = { cmd = "cargo test 2>&1", timeout_s = 60 }
```
Output variables: `${steps.<id>.stdout}`, `${steps.<id>.stderr}`, `${steps.<id>.exit_code}`

### `fs.read`
Read a file.
```toml
[[steps]]
action = "fs.read"
params = { path = "src/main.rs" }
```
Output: `${steps.<id>.content}`

### `fs.write`
Write content to a file.
```toml
[[steps]]
action = "fs.write"
params = { path = "src/main.rs", content = "${steps.generate.text}" }
```

### `fs.apply_diff`
Apply a unified diff.
```toml
[[steps]]
action = "fs.apply_diff"
params = { path = "${steps.analyze.file}", diff = "${steps.analyze.diff}" }
```

### `llm.analyze`
Send a prompt to the LLM and get a structured response.
```toml
[[steps]]
action = "llm.analyze"
params = {
  prompt_template = "fix_error",   # name from PromptManager, or inline prompt
  input           = "${steps.check.stdout}",
  output_format   = "diff",        # "diff" | "text" | "structured"
  tier            = "smart"
}
```
Output variables (when `output_format = "diff"`):
- `${steps.<id>.file}` — target file path
- `${steps.<id>.diff}` — unified diff to apply

### `llm.generate`
Generate free-form text.
```toml
[[steps]]
action = "llm.generate"
params = {
  prompt  = "Write a git commit message for: ${steps.diff.content}",
  context = "${steps.status.stdout}",
  tier    = "fast"
}
```
Output: `${steps.<id>.text}`

### `git.commit`
Commit changes.
```toml
[[steps]]
action = "git.commit"
params = { message = "${steps.msg.text}" }
```

### `approval.request`
Pause and ask the user to approve before continuing.
```toml
[[steps]]
action = "approval.request"
params = { message = "About to apply diff to ${steps.analyze.file}. Proceed?" }
```

---

## Variable Interpolation

Use `${steps.<step_id>.<field>}` to reference previous step outputs.

```toml
[[steps]]
id = "read"
action = "fs.read"
params = { path = "src/auth.rs" }

[[steps]]
id = "review"
action = "llm.analyze"
params = {
  prompt = "Review this code for security issues:",
  input  = "${steps.read.content}"   # ← references step "read" output "content"
}
```

Special variables:
- `${project_dir}` — absolute path to project root
- `${current_file}` — currently open file (if context available)

---

## Conditional Steps

```toml
[[steps]]
id        = "test"
action    = "shell.run"
params    = { cmd = "cargo test 2>&1" }

[[steps]]
id         = "fix"
action     = "llm.analyze"
condition  = "${steps.test.exit_code} != 0"   # only run if tests failed
params     = { prompt_template = "fix_test_failure", input = "${steps.test.stdout}" }
```

---

## Prompt Templates

Reference built-in templates by name in `llm.analyze`:

| Template Name | Purpose |
|---|---|
| `fix_error` | Fix a compile/runtime error from stderr output |
| `generate_test` | Generate unit tests for a function |
| `review_diff` | Review a git diff for quality and issues |
| `refactor_function` | Refactor a function with suggestions |
| `explain_code` | Explain what code does (Chinese output) |
| `analyze_unwrap_usage` | Find and fix Rust `unwrap()` calls |

Add custom templates in `~/.evocli/prompt_templates/*.toml`:

```toml
# ~/.evocli/prompt_templates/my_template.toml
[template]
name = "my_template"
system = "You are a code reviewer specializing in security."
prompt = """
Review the following code for security vulnerabilities:

{{ input }}

Return a unified diff with fixes.
"""
output_format = "diff"
```

---

## Guidance Skills (Markdown)

For complex multi-step workflows that need LLM judgment at each step, use a Markdown guidance skill instead of TOML:

```markdown
<!-- ~/.evocli/skills/my-workflow/SKILL.md -->
# My Workflow

## When to use
When the user asks to [specific trigger].

## Steps
1. Run `cargo check` to find errors
2. For each error, analyze the root cause
3. Apply minimal fix
4. Run tests to verify

## Important rules
- Never change public API signatures
- Always run tests after applying fixes
```

The LLM uses this as context when orchestrating the workflow. Guidance skills are discovered by `/skill list` and can be run with `/skill my-workflow`.

---

## Full Example: Auto-commit Skill

```toml
[skill]
id          = "auto_commit"
name        = "Auto Commit"
description = "Stage all changes, generate a commit message, and commit"
tags        = ["git", "commit", "workflow"]

[[steps]]
id     = "status"
action = "shell.run"
params = { cmd = "git diff --staged && git status" }

[[steps]]
id     = "diff"
action = "shell.run"
params = { cmd = "git diff HEAD" }

[[steps]]
id     = "message"
action = "llm.generate"
params = {
  prompt = "Write a concise conventional commit message for these changes:\n\n${steps.diff.stdout}",
  tier   = "fast"
}

[[steps]]
id     = "approve"
action = "approval.request"
params = { message = "Commit with message: '${steps.message.text}'?" }

[[steps]]
id     = "commit"
action = "git.commit"
params = { message = "${steps.message.text}" }
```

---

## Installing Skills

```bash
# From a git repo
evocli skill import --from https://github.com/user/evocli-skills.git

# From a local TOML file
evocli skill import --from ./my_skill.toml

# Export a skill
evocli skill export fix_compile_error --output ./fix_compile_error.toml

# List installed skills
evocli skill list

# Run a skill
evocli skill run auto_commit
```
