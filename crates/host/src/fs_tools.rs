//! fs_tools.rs — 文件系统工具实现（Capability Contract fs.* 工具组）
//!
//! 所有操作受 SecurityController 路径白名单约束。
//! Unified diff 应用使用 diffy crate（纯 Rust，不依赖 patch 命令）。

use anyhow::{Context, Result};
use serde_json::Value;
use std::path::PathBuf;

/// 读取文件内容
pub fn fs_read(args: &Value) -> Result<Value> {
    let path = get_path(args, "path")?;
    let content = std::fs::read_to_string(&path).with_context(|| {
        let cwd = std::env::current_dir()
            .map(|p| p.display().to_string())
            .unwrap_or_else(|_| "(unknown)".into());
        // Relative path hint: if path is relative and file doesn't exist,
        // tell the user what CWD evocli is using so they can diagnose the issue.
        if path.is_relative() {
            format!(
                "fs.read: cannot read '{}' (relative path, CWD='{}'). \
                     Tip: run evocli from your project root so relative paths resolve correctly.",
                path.display(),
                cwd
            )
        } else {
            format!("fs.read: cannot read '{}'", path.display())
        }
    })?;
    Ok(Value::String(content))
}

/// 读取文件指定行范围（1-indexed，inclusive）
///
/// 避免把 2000 行文件全部读入上下文——只注入 AI 实际需要的部分。
/// start_line 和 end_line 均为可选：
///   - 只传 start_line：从该行读到文件末尾
///   - 只传 end_line：从文件开头读到该行
///   - 两者都传：读取指定区间
///   - 都不传：等同于 fs.read（读全文件）
pub fn fs_read_range(args: &Value) -> Result<Value> {
    let path = get_path(args, "path")?;
    let start_line = args["start_line"].as_u64().map(|n| n as usize);
    let end_line = args["end_line"].as_u64().map(|n| n as usize);

    let content = std::fs::read_to_string(&path)
        .with_context(|| format!("fs.read_range: cannot read {}", path.display()))?;

    let total_lines = content.lines().count();

    // No range specified → full file (same as fs.read)
    if start_line.is_none() && end_line.is_none() {
        return Ok(serde_json::json!({
            "content": content,
            "start_line": 1,
            "end_line": total_lines,
            "total_lines": total_lines,
        }));
    }

    let start = start_line.unwrap_or(1).saturating_sub(1); // 0-indexed
    let end = end_line.unwrap_or(total_lines).min(total_lines); // inclusive, 1-indexed → exclusive

    let lines: Vec<&str> = content.lines().collect();
    let slice = &lines[start..end];
    let result = slice.join("\n");

    Ok(serde_json::json!({
        "content":     result,
        "start_line":  start + 1,    // back to 1-indexed for caller
        "end_line":    end,
        "total_lines": total_lines,
        "note": if total_lines > end || start > 0 {
            format!("Showing lines {}-{} of {} total. Use fs.read_range with different start_line/end_line to see more.", start + 1, end, total_lines)
        } else {
            String::new()
        },
    }))
}

/// 写入文件内容（原子写入：先写 .evocli_tmp，再 rename）
///
/// 实现 Cline 的 atomicWriteFile 模式：
///   1. 写入同目录下的隐藏临时文件（.filename.evocli_tmp）
///   2. 用 std::fs::rename 原子替换目标文件
///
/// 为什么需要原子写入：
///   - std::fs::write 直接覆盖目标文件，进程崩溃时会留下部分写入的损坏文件
///   - rename 在 POSIX 和 Windows NTFS（win10 1803+）上都是原子操作
///   - 避免 TUI 在写入中途读取到损坏状态
pub fn fs_write(args: &Value) -> Result<Value> {
    let path = get_path(args, "path")?;
    let content = args["content"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("fs.write: missing 'content'"))?;
    let dry_run = args["dry_run"].as_bool().unwrap_or(false);

    if dry_run {
        return Ok(serde_json::json!({ "ok": true, "dry_run": true, "path": path.to_str() }));
    }

    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    // Atomic write: write to a hidden .tmp file then rename.
    // If the process crashes mid-write, the original file is untouched.
    let file_name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("file");
    let tmp_path = path
        .parent()
        .map(|p| p.join(format!(".{file_name}.evocli_tmp")))
        .unwrap_or_else(|| std::path::PathBuf::from(format!(".{file_name}.evocli_tmp")));

    std::fs::write(&tmp_path, content)
        .with_context(|| format!("fs.write: cannot write tmp file {}", tmp_path.display()))?;
    std::fs::rename(&tmp_path, &path).with_context(|| {
        // Clean up tmp on rename failure (best-effort)
        let _ = std::fs::remove_file(&tmp_path);
        format!(
            "fs.write: rename failed {} → {}",
            tmp_path.display(),
            path.display()
        )
    })?;

    Ok(serde_json::json!({ "ok": true, "path": path.to_str() }))
}

/// 生成两段文本之间的 unified diff（使用 diffy crate，O(n log n)）
pub fn fs_diff(args: &Value) -> Result<Value> {
    let old = args["old"].as_str().unwrap_or("");
    let new = args["new"].as_str().unwrap_or("");
    let diff = diffy::create_patch(old, new).to_string();
    Ok(Value::String(diff))
}

/// 将 unified diff 应用到文件
pub fn fs_apply_diff(args: &Value) -> Result<Value> {
    let path = get_path(args, "path")?;
    let diff = args["diff"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("fs.apply_diff: missing 'diff'"))?;
    let dry_run = args["dry_run"].as_bool().unwrap_or(false);
    let run_fmt = args["run_format"].as_bool().unwrap_or(false);
    let run_tests = args["run_tests"].as_bool().unwrap_or(false);
    let auto_commit = args["auto_commit"].as_bool().unwrap_or(false);

    if dry_run {
        return Ok(serde_json::json!({ "ok": true, "dry_run": true }));
    }

    let original = std::fs::read_to_string(&path)
        .with_context(|| format!("fs.apply_diff: cannot read {}", path.display()))?;

    let patched = apply_unified_diff(&original, diff)
        .with_context(|| format!("fs.apply_diff: patch failed for {}", path.display()))?;

    std::fs::write(&path, &patched)
        .with_context(|| format!("fs.apply_diff: cannot write {}", path.display()))?;

    let mut fmt_result = None::<String>;
    let mut test_result = None::<serde_json::Value>;

    // ── 可选：格式化（Section 8）────────────────────────────────────
    if run_fmt {
        fmt_result = run_formatter(&path);
    }

    // ── 可选：运行测试（Section 8 验证）──────────────────────────────
    if run_tests {
        let cwd = path.parent().unwrap_or(std::path::Path::new("."));
        test_result = Some(run_test_suite(cwd));
    }

    // ── 可选：原子提交（Section 8 git 原子提交）──────────────────────
    let commit_hash = if auto_commit {
        let msg = args["commit_message"]
            .as_str()
            .unwrap_or("refactor: apply AI diff");
        let cwd = path.parent().unwrap_or(std::path::Path::new("."));
        let file_str = path.to_string_lossy().to_string();
        crate::git::git_commit(cwd, msg, &[file_str]).ok()
    } else {
        None
    };

    Ok(serde_json::json!({
        "ok":           true,
        "path":         path.to_str(),
        "lines_changed": patched.lines().count(),
        "format":       fmt_result,
        "tests":        test_result,
        "commit":       commit_hash,
    }))
}

// ── helpers ──────────────────────────────────────────────────────

fn get_path(args: &Value, key: &str) -> Result<PathBuf> {
    let s = args[key]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("missing '{}' argument", key))?;
    Ok(PathBuf::from(s))
}

/// 应用 unified diff（标准 `---/+++/@@` 格式）到原始文本（diffy crate）
fn apply_unified_diff(original: &str, diff: &str) -> Result<String> {
    let patch = diffy::Patch::from_str(diff).context("Invalid unified diff format")?;
    diffy::apply(original, &patch).context("Failed to apply diff: hunk(s) do not match the file")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fs_read_nonexistent() {
        let result = fs_read(&serde_json::json!({"path": "/nonexistent/file.rs"}));
        assert!(result.is_err());
    }

    #[test]
    fn test_fs_write_dry_run() {
        let result = fs_write(&serde_json::json!({
            "path": "/tmp/test.txt", "content": "hello", "dry_run": true
        }));
        assert!(result.is_ok());
    }
}

// ── Section 8: Code Apply 辅助函数（格式化 + 测试）────────────────

/// 运行格式化工具（cargo fmt / python black 等）
fn run_formatter(path: &std::path::Path) -> Option<String> {
    let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("");
    let (cmd, args) = match ext {
        "rs" => ("rustfmt", vec![path.to_str().unwrap_or("")]),
        "py" => ("black", vec![path.to_str().unwrap_or(""), "--quiet"]),
        "ts" | "js" => ("prettier", vec!["--write", path.to_str().unwrap_or("")]),
        "go" => ("gofmt", vec!["-w", path.to_str().unwrap_or("")]),
        _ => return Some("skipped (unsupported extension)".to_string()),
    };
    let output = std::process::Command::new(cmd).args(args).output();
    match output {
        Ok(o) if o.status.success() => Some("formatted".to_string()),
        Ok(o) => Some(format!(
            "formatter error: {}",
            String::from_utf8_lossy(&o.stderr).trim()
        )),
        Err(e) => Some(format!("formatter not found: {}", e)),
    }
}

/// 在指定目录运行测试套件
fn run_test_suite(cwd: &std::path::Path) -> serde_json::Value {
    // 检测项目类型并运行对应测试
    let (cmd, args) = if cwd.join("Cargo.toml").exists() {
        ("cargo", vec!["test", "--quiet"])
    } else if cwd.join("package.json").exists() {
        ("npm", vec!["test", "--silent"])
    } else if cwd.join("pyproject.toml").exists() || cwd.join("setup.py").exists() {
        ("python", vec!["-m", "pytest", "-q"])
    } else {
        return serde_json::json!({"skipped": true, "reason": "unknown project type"});
    };

    let output = std::process::Command::new(cmd)
        .args(&args)
        .current_dir(cwd)
        .output();

    match output {
        Ok(o) => {
            let stdout = String::from_utf8_lossy(&o.stdout).to_string();
            let stderr = String::from_utf8_lossy(&o.stderr).to_string();
            // char-based truncation: process output can contain Chinese paths/text
            let stdout_display: String = stdout.chars().take(500).collect();
            let stderr_display: String = stderr.chars().take(200).collect();
            serde_json::json!({
                "passed":    o.status.success(),
                "exit_code": o.status.code(),
                "stdout":    stdout_display,
                "stderr":    stderr_display,
            })
        }
        Err(e) => serde_json::json!({"error": e.to_string()}),
    }
}
