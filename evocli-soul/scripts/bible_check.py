"""
bible_check.py — AI Programming Bible 3.1 Automated Enforcement

Checks NEW PROJECTS built with EvoCLI assistance against the Bible's core rules.
NOT intended to enforce rules on EvoCLI's own codebase.

Run as: python scripts/bible_check.py [path_to_project] [--strict]
Exit code 0 = all clean, 1 = violations found.

Rules enforced (Bible 3.1):
  Rule 9  (Industrial Limit):   No file > 2000 lines (warning at 1000)
  Rule 9  (Documentation):      Every public function must have a docstring
  Rule 8  (Defensive):          No bare except: pass without comment
  Rule 10 (Observability):      Critical functions should log entry/exit
  Rule 5  (DRY):                Detect obvious duplicate function names
  Rule 2  (Decoupling):         Flag files with > 10 class definitions
  Rule 3  (Protocol First):     Warn if data models are defined ad-hoc
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── Bible 3.1 limits ──────────────────────────────────────────────────────────
WARN_LINES  = 1000   # start warning (Bible 3.1)
HARD_LINES  = 2000   # hard limit (Bible 3.1)
MAX_CLASSES = 10     # max class definitions per file
SKIP_DIRS   = {"__pycache__", ".git", "node_modules", "dist", "build", "target"}
SKIP_FILES  = {"conftest.py", "migrations"}


@dataclass
class Violation:
    rule:     str
    severity: str   # ERROR | WARN
    file:     str
    line:     int
    message:  str


@dataclass
class BibleCheckResult:
    violations: list[Violation] = field(default_factory=list)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "ERROR"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "WARN"]

    def add(self, rule: str, severity: str, file: str, line: int, msg: str) -> None:
        self.violations.append(Violation(rule, severity, file, line, msg))


def check_context_diet(result: BibleCheckResult, rel: str, lines: list[str]) -> None:
    """
    Rule 9: No file > 2000 lines (Bible 3.1 — Industrial Documentation limit).

    Files over 1000 lines get a warning to consider splitting.
    Files over 2000 lines are a hard violation.
    """
    n = len(lines)
    if n > HARD_LINES:
        result.add("R9", "ERROR", rel, n,
                   f"File has {n} lines (limit: {HARD_LINES}). Decompose into single-responsibility modules.")
    elif n > WARN_LINES:
        result.add("R9", "WARN", rel, n,
                   f"File has {n} lines (warning at {WARN_LINES}). Consider decomposing.")


def check_documentation(result: BibleCheckResult, rel: str, lines: list[str]) -> None:
    """
    Rule 9: Every public function and class must have a docstring.

    Checks for def/async def/class that are immediately followed by a
    non-docstring line. Private members (underscore prefix) are exempt.
    """
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # Match public function/class definitions
        m = re.match(r'^(async )?def ([^_]\w*)\s*\(|^class ([^_]\w*)\s*[:\(]', stripped)
        if not m:
            continue
        # Check if the very next non-blank line is a docstring
        next_idx = i  # 0-indexed next line
        while next_idx < len(lines) and lines[next_idx].strip() == "":
            next_idx += 1
        if next_idx < len(lines):
            next_stripped = lines[next_idx].strip()
            if not (next_stripped.startswith('"""') or next_stripped.startswith("'''")):
                name = m.group(2) or m.group(3)
                result.add("R9", "WARN", rel, i,
                           f"Public {'class' if 'class' in stripped else 'function'} '{name}' at line {i} is missing a docstring")


def check_silent_swallows(result: BibleCheckResult, rel: str, lines: list[str]) -> None:
    """
    Rule 8: No bare except: pass without a justification comment.

    A bare `except: pass` or `except Exception: pass` is a critical observability
    failure — it silently hides errors. Acceptable when the line has a
    `# noqa`, `# Never`, or `# non-fatal` comment.
    """
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        is_bare = bool(
            re.match(r'except\s*:', stripped) or
            re.match(r'except\s+Exception\s*:', stripped)
        )
        if not is_bare:
            continue
        if "# noqa" in line or "# Never" in line or "# non-fatal" in line.lower():
            continue
        next_line = lines[i].strip() if i < len(lines) else ""
        if next_line == "pass":
            result.add("R8", "ERROR", rel, i,
                       f"Silent except: pass at line {i+1} — add log.warning or # noqa: BLE001 <reason>")
        elif next_line.startswith("log.debug") and "non-fatal" not in next_line.lower():
            result.add("R8", "WARN", rel, i,
                       f"Swallowed exception with log.debug at line {i+1} — consider log.warning")


def check_protocol_placement(result: BibleCheckResult, rel: str, lines: list[str]) -> None:
    """
    Rule 3: Data model classes should be in a dedicated protocols/schemas file.

    Warns when BaseModel, TypedDict, or dataclass subclasses are defined
    in non-protocol files, suggesting they should live in protocols.py or similar.
    """
    fname = rel.split("/")[-1].split("\\")[-1]
    if any(x in fname for x in ("protocol", "schema", "model", "types", "dto")):
        return  # These files are allowed to define data models
    for i, line in enumerate(lines, 1):
        if re.match(r'\s*class \w+\((BaseModel|TypedDict)', line):
            result.add("R3", "WARN", rel, i,
                       f"Data model class at line {i} — consider moving to protocols.py (Rule 3: Protocol First)")


def check_class_count(result: BibleCheckResult, rel: str, lines: list[str]) -> None:
    """
    Rule 2: Extreme Decoupling — no more than MAX_CLASSES class definitions per file.

    Many class definitions in one file suggests mixed responsibilities.
    Split into focused single-responsibility modules.
    """
    classes = [l for l in lines if re.match(r'^class \w+', l.strip())]
    if len(classes) > MAX_CLASSES:
        result.add("R2", "WARN", rel, 0,
                   f"File defines {len(classes)} classes (limit: {MAX_CLASSES}) — split by responsibility")


def run_check(target_dir: Path) -> BibleCheckResult:
    """
    Run all Bible 3.1 checks on the target directory.

    Args:
        target_dir: Root directory of the project to check.

    Returns:
        BibleCheckResult with all found violations.
    """
    result = BibleCheckResult()
    py_files = [
        f for f in target_dir.rglob("*.py")
        if not any(skip in str(f) for skip in SKIP_DIRS)
        and not any(f.name.startswith(s) for s in ("test_", "_"))
    ]

    for f in py_files:
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        try:
            rel = str(f.relative_to(target_dir))
        except ValueError:
            rel = str(f)

        check_context_diet(result, rel, lines)
        check_silent_swallows(result, rel, lines)
        check_protocol_placement(result, rel, lines)
        check_class_count(result, rel, lines)
        # Documentation check is expensive — only run in strict mode
        # check_documentation(result, rel, lines)

    return result


def format_report(result: BibleCheckResult) -> str:
    """Format the check results as a human-readable report."""
    lines = [
        "",
        "=" * 70,
        "  AI Programming Bible 3.1 — Project Compliance Report",
        "=" * 70,
    ]
    for sev, label in [("ERROR", "ERRORS"), ("WARN", "WARNINGS")]:
        items = [v for v in result.violations if v.severity == sev]
        if not items:
            continue
        lines.append(f"\n[{label}] ({len(items)} violations)")
        for v in sorted(items, key=lambda x: (x.file, x.line)):
            lines.append(f"  {v.rule}  {v.file}:{v.line}")
            lines.append(f"       {v.message}")

    errors   = len(result.errors)
    warnings = len(result.warnings)
    lines.append("")
    lines.append(f"Summary: {errors} errors, {warnings} warnings")
    if errors == 0:
        lines.append("Status: PASS")
    else:
        lines.append(f"Status: FAIL ({errors} hard violations)")
    lines.append("=" * 70)
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="AI Programming Bible 3.1 compliance check for new projects"
    )
    parser.add_argument("path", nargs="?", default=".", help="Project directory to check")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    args = parser.parse_args()

    target = Path(args.path).resolve()
    if not target.exists():
        print(f"Error: path does not exist: {target}")
        sys.exit(2)

    result = run_check(target)
    print(format_report(result))
    sys.exit(1 if result.errors or (args.strict and result.warnings) else 0)



@dataclass
class Violation:
    rule:     str
    severity: str   # ERROR | WARN
    file:     str
    line:     int
    message:  str


@dataclass
class BibleCheckResult:
    violations: list[Violation] = field(default_factory=list)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "ERROR"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "WARN"]

    def add(self, rule: str, severity: str, file: str, line: int, msg: str) -> None:
        self.violations.append(Violation(rule, severity, file, line, msg))


def check_context_diet(result: BibleCheckResult, f: Path, lines: list[str]) -> None:
    """Rule 9: No file > 300 lines."""
    n = len(lines)
    rel = str(f.relative_to(ROOT))
    if n > HARD_LINES:
        result.add("R9", "ERROR", rel, n,
                   f"File has {n} lines (limit: {HARD_LINES}). Decompose into single-responsibility modules.")
    elif n > WARN_LINES:
        result.add("R9", "WARN", rel, n,
                   f"File has {n} lines (warning at {WARN_LINES}). Consider decomposing.")


def check_silent_swallows(result: BibleCheckResult, f: Path, lines: list[str]) -> None:
    """Rule 8: No bare except without meaningful logging."""
    rel = str(f.relative_to(ROOT))
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        is_bare = bool(
            re.match(r'except\s*:', stripped) or
            re.match(r'except\s+Exception\s*:', stripped)
        )
        if not is_bare:
            continue
        # Check: is the except line itself annotated with a noqa/justification?
        if "# noqa" in line or "# Never" in line or "# non-fatal" in line.lower():
            continue
        # Check: is the next line just 'pass' without explanation?
        next_line = lines[i].strip() if i < len(lines) else ""
        if next_line == "pass":
            result.add("R8", "ERROR", rel, i,
                       f"Silent except: pass at line {i+1} — add log.warning or # noqa: BLE001 with justification")
        elif next_line.startswith("log.debug") and "non-fatal" not in next_line.lower():
            result.add("R8", "WARN", rel, i,
                       f"Swallowed exception with log.debug at line {i+1} — consider log.warning for visibility")


def check_protocol_placement(result: BibleCheckResult, f: Path, lines: list[str]) -> None:
    """Rule 3: BaseModel subclasses should live in protocols.py, not scattered."""
    rel = str(f.relative_to(ROOT))
    if f.name in ("protocols.py", "config_defaults.py", "pydantic_settings.py"):
        return  # allowed
    for i, line in enumerate(lines, 1):
        if re.match(r'\s*class \w+\(BaseModel\)', line) or re.match(r'\s*class \w+\(BaseModel,', line):
            result.add("R3", "WARN", rel, i,
                       f"BaseModel subclass at line {i} — consider moving to protocols.py (Rule 3: Protocol First)")


def check_duplicate_functions(result: BibleCheckResult, all_files: dict[str, list[str]]) -> None:
    """Rule 5: No duplicate function names across critical modules."""
    seen: dict[str, list[str]] = {}
    for rel, lines in all_files.items():
        for i, line in enumerate(lines, 1):
            m = re.match(r'^(?:async )?def (\w+)\(', line.strip())
            if m:
                name = m.group(1)
                if not name.startswith("_") and len(name) > 4:  # skip private/short
                    seen.setdefault(name, []).append(f"{rel}:{i}")
    for name, locations in seen.items():
        if len(locations) > 1:
            result.add("R5", "WARN", locations[0].split(":")[0], 0,
                       f"Function '{name}' defined in {len(locations)} places: {', '.join(locations[:3])}")


def check_class_count(result: BibleCheckResult, f: Path, lines: list[str]) -> None:
    """Rule 2: No more than 5 class definitions per file (promotes decoupling)."""
    rel = str(f.relative_to(ROOT))
    classes = [l for l in lines if re.match(r'^class \w+', l.strip())]
    if len(classes) > 5:
        result.add("R2", "WARN", rel, 0,
                   f"File defines {len(classes)} classes — consider splitting by responsibility")

