//! Git operations via system `git` command.
//!
//! Regular git operations use `std::process::Command` in the project repo.
//! Side-git (shadow) operations use a separate bare repo in `.evocli/shadow-git`
//! that NEVER touches the project's own `.git`. (Section 27)

use anyhow::{bail, Context, Result};
use serde::Serialize;
use std::path::{Path, PathBuf};
use std::process::Command;

// ── Shared types ─────────────────────────────────────────────────────────

/// One entry from `git status --porcelain=v1`.
#[derive(Debug, Clone, Serialize)]
pub struct StatusEntry {
    pub code: String,
    pub path: String,
}

/// One entry from shadow git log.
#[derive(Debug, Clone, Serialize)]
pub struct SnapshotEntry {
    pub hash: String,
    pub label: String,
    pub age: String,
}

// ── Internal helpers ──────────────────────────────────────────────────────

fn run_git(repo: &Path, args: &[&str]) -> Result<String> {
    let out = Command::new("git")
        .args(args)
        .current_dir(repo)
        .output()
        .with_context(|| format!("failed to run: git {}", args.join(" ")))?;
    if !out.status.success() {
        bail!(
            "git {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&out.stderr).trim()
        );
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Run git with explicit --git-dir and --work-tree for shadow repo.
fn shadow_cmd(shadow: &Path, work_tree: &Path) -> Command {
    let mut cmd = Command::new("git");
    cmd.arg("--git-dir")
        .arg(shadow)
        .arg("--work-tree")
        .arg(work_tree);
    cmd
}

fn run_shadow(shadow: &Path, work_tree: &Path, args: &[&str]) -> Result<String> {
    let out = shadow_cmd(shadow, work_tree)
        .args(args)
        .output()
        .with_context(|| format!("shadow git {}", args.join(" ")))?;
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

// ── Regular git API ───────────────────────────────────────────────────────

pub fn git_status(repo: &Path) -> Result<Vec<StatusEntry>> {
    let raw = run_git(repo, &["status", "--porcelain=v1"])?;
    Ok(raw
        .lines()
        .filter(|l| l.len() >= 4)
        .map(|l| StatusEntry {
            code: l[..2].to_string(),
            path: l[3..].to_string(),
        })
        .collect())
}

pub fn git_snapshot(repo: &Path) -> Result<String> {
    let msg = run_git(repo, &["stash", "push", "-u", "-m", "evocli-snapshot"])?;
    if msg.contains("No local changes") {
        bail!("nothing to snapshot — working tree clean");
    }
    // Return the actual commit hash (not the positional "stash@{0}" reference).
    // refs/stash always points to the commit just pushed. This survives interleaved
    // stash operations because git_restore resolves the hash back to a positional ref.
    let hash = run_git(repo, &["rev-parse", "refs/stash"])?;
    Ok(hash)
}

pub fn git_restore(repo: &Path, stash_ref: &str) -> Result<()> {
    // If stash_ref is a full SHA hash (40 hex chars), resolve to the positional
    // stash@{n} reference first, since `git stash pop` requires the positional form.
    let positional = if stash_ref.len() >= 40 && stash_ref.chars().all(|c| c.is_ascii_hexdigit()) {
        let list = run_git(repo, &["stash", "list", "--format=%H %gd"])?;
        list.lines()
            .find(|l| l.starts_with(stash_ref))
            .and_then(|l| l.split_whitespace().nth(1))
            .map(|s| s.to_string())
            .unwrap_or_else(|| "stash@{0}".to_string())
    } else {
        stash_ref.to_string()
    };
    run_git(repo, &["stash", "pop", &positional])?;
    Ok(())
}

pub fn git_commit(repo: &Path, message: &str, files: &[String]) -> Result<String> {
    if files.is_empty() {
        bail!("no files specified for commit");
    }
    let mut add_args: Vec<&str> = vec!["add", "--"];
    for f in files {
        add_args.push(f.as_str());
    }
    run_git(repo, &add_args)?;
    run_git(repo, &["commit", "-m", message])?;
    run_git(repo, &["rev-parse", "--short", "HEAD"])
}

pub fn git_branch(repo: &Path) -> Result<String> {
    run_git(repo, &["rev-parse", "--abbrev-ref", "HEAD"])
}

/// Available for future CLI commands (e.g., evocli git log).
#[allow(dead_code)]
pub fn git_log(repo: &Path, count: usize) -> Result<String> {
    run_git(repo, &["log", "--oneline", "-n", &count.to_string()])
}

pub fn git_diff(repo: &Path) -> Result<String> {
    let unstaged = run_git(repo, &["diff"])?;
    let staged = run_git(repo, &["diff", "--cached"])?;
    let mut result = String::new();
    if !staged.is_empty() {
        result.push_str("=== STAGED ===\n");
        result.push_str(&staged);
        result.push('\n');
    }
    if !unstaged.is_empty() {
        result.push_str("=== UNSTAGED ===\n");
        result.push_str(&unstaged);
    }
    Ok(result)
}

/// Extended diff with parameter control.
///
/// Parameters:
///   path:   specific file path to diff (empty = whole tree)
///   staged: true = only staged changes; false = only unstaged; None = both
///   stat:   true = summary stats (files changed, insertions, deletions)
///   base:   compare HEAD against a branch/commit, e.g. "main", "origin/main", "abc123"
pub fn git_diff_ext(
    repo: &Path,
    path: &str,
    staged: Option<bool>,   // Some(true)=staged, Some(false)=unstaged, None=both
    stat: bool,
    base: &str,
) -> Result<String> {
    let mut args_base: Vec<&str> = vec!["diff"];

    // stat mode: show summary instead of full diff
    if stat { args_base.push("--stat"); }

    // base: compare against a reference (branch/commit)
    // e.g. "git diff main...HEAD" or "git diff abc123"
    if !base.is_empty() { args_base.push(base); }

    // staged/unstaged
    let do_staged   = staged.unwrap_or(true);
    let do_unstaged = staged.map_or(true, |s| !s);

    let path_args: Vec<&str> = if path.is_empty() {
        vec![]
    } else {
        vec!["--", path]
    };

    let mut result = String::new();

    if do_staged {
        let mut a = args_base.clone();
        a.push("--cached");
        a.extend(path_args.iter());
        let out = run_git(repo, &a)?;
        if !out.is_empty() {
            if staged.is_none() { result.push_str("=== STAGED ===\n"); }
            result.push_str(&out);
            result.push('\n');
        }
    }

    if do_unstaged && base.is_empty() {
        let mut a = args_base.clone();
        a.extend(path_args.iter());
        let out = run_git(repo, &a)?;
        if !out.is_empty() {
            if staged.is_none() { result.push_str("=== UNSTAGED ===\n"); }
            result.push_str(&out);
        }
    }

    Ok(result)
}

// ── Side-Git：影子 Git 仓库（Section 27）────────────────────────────────
//
// Creates `.evocli/shadow-git/` — a bare git repo whose work-tree is the
// project directory. The project's own `.git` is NEVER touched.

pub fn shadow_git_dir(project: &Path) -> PathBuf {
    project.join(".evocli").join("shadow-git")
}

/// Initialise shadow repo (idempotent).
pub fn shadow_init(project: &Path) -> Result<()> {
    let shadow = shadow_git_dir(project);
    if shadow.join("HEAD").exists() {
        return Ok(());
    }
    std::fs::create_dir_all(&shadow)?;

    let out = Command::new("git")
        .args(["init", "--bare"])
        .arg(&shadow)
        .output()
        .context("shadow git init")?;
    if !out.status.success() {
        bail!(
            "shadow git init failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    if let Some(s) = project.to_str() {
        let _ = run_shadow(&shadow, project, &["config", "core.worktree", s]);
    }
    Ok(())
}

/// Commit a snapshot. `turn_label`: e.g. "before-1", "after-1", "manual".
/// Returns short hash (8 chars).
pub fn shadow_snapshot(project: &Path, turn_label: &str) -> Result<String> {
    let shadow = shadow_git_dir(project);
    shadow_init(project)?;

    // add -A (ignore if nothing to add)
    let _ = shadow_cmd(&shadow, project).args(["add", "-A"]).output();

    let out = shadow_cmd(&shadow, project)
        .args([
            "commit",
            "--allow-empty",
            "-m",
            turn_label,
            "--author",
            "EvoCLI <evocli@local>",
        ])
        .output()
        .context("shadow commit")?;
    if !out.status.success() {
        bail!(
            "shadow commit failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
    }

    let hash = run_shadow(&shadow, project, &["rev-parse", "HEAD"]).unwrap_or_default();
    let short = hash[..hash.len().min(8)].to_string();
    tracing::info!("shadow snapshot '{}': {}", turn_label, short);
    Ok(short)
}

/// Restore workspace to snapshot. `snapshot` can be a hash prefix or a label.
pub fn shadow_restore(project: &Path, snapshot: &str) -> Result<()> {
    let shadow = shadow_git_dir(project);
    if !shadow.join("HEAD").exists() {
        bail!("shadow git not initialised for {}", project.display());
    }

    // If looks like a label (not all hex), find its hash via log --grep
    let hash = if snapshot.chars().all(|c| c.is_ascii_hexdigit()) {
        snapshot.to_string()
    } else {
        run_shadow(
            &shadow,
            project,
            &[
                "log",
                "--oneline",
                "--all",
                &format!("--grep={}", snapshot),
                "-1",
            ],
        )
        .unwrap_or_default()
        .split_whitespace()
        .next()
        .unwrap_or(snapshot)
        .to_string()
    };

    let out = shadow_cmd(&shadow, project)
        .args(["checkout", &hash, "--", "."])
        .output()
        .context("shadow restore")?;
    if !out.status.success() {
        bail!(
            "shadow restore failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    tracing::info!("shadow restored to {}", &hash[..hash.len().min(8)]);
    Ok(())
}

/// List recent snapshots (newest first).
pub fn shadow_log(project: &Path, limit: usize) -> Result<Vec<SnapshotEntry>> {
    let shadow = shadow_git_dir(project);
    if !shadow.join("HEAD").exists() {
        return Ok(vec![]);
    }

    let raw = run_shadow(
        &shadow,
        project,
        &["log", "--format=%h|%s|%cr", &format!("-{}", limit)],
    )
    .unwrap_or_default();

    Ok(raw
        .lines()
        .filter_map(|line| {
            let p: Vec<&str> = line.splitn(3, '|').collect();
            (p.len() >= 2).then(|| SnapshotEntry {
                hash: p[0].to_string(),
                label: p[1].to_string(),
                age: p.get(2).copied().unwrap_or("").to_string(),
            })
        })
        .collect())
}

/// Revert to the Nth "before-" snapshot (undo N turns).
pub fn shadow_revert_turns(project: &Path, turns: usize) -> Result<()> {
    let entries = shadow_log(project, turns * 2 + 4)?;
    let befores: Vec<_> = entries
        .iter()
        .filter(|e| e.label.starts_with("before-"))
        .collect();
    let target = befores.get(turns.saturating_sub(1)).ok_or_else(|| {
        anyhow::anyhow!(
            "不够 {} 轮可以撤销（仅有 {} 个 before- 快照）",
            turns,
            befores.len()
        )
    })?;
    shadow_restore(project, &target.hash)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn status_on_cwd() {
        let _ = git_status(&PathBuf::from("."));
    }

    #[test]
    fn shadow_dir_path() {
        let shadow = shadow_git_dir(&PathBuf::from("."));
        assert!(shadow.to_str().unwrap().contains("shadow-git"));
    }
}
