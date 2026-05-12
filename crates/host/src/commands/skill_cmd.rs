//! commands/skill_cmd.rs — evocli skill 子命令（Section 7 + v2.3 Marketplace）
use anyhow::{Context, Result};
use clap::Subcommand;
use std::path::PathBuf;

fn global_skills_dir() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_default()
        .join(".evocli")
        .join("skills")
}

#[derive(Subcommand)]
pub enum SkillAction {
    /// List available skills (global + project-local)
    List,
    /// Run a skill by ID (delegates to Python Soul)
    Run {
        skill_id: String,
        #[arg(long)]
        dry_run: bool,
    },
    /// Reload skills from disk
    Reload,
    /// Export a skill to a portable .toml file (v2.3 Marketplace)
    Export {
        /// Skill ID (filename without .toml)
        skill_id: String,
        /// Output path (default: ./<skill_id>.toml)
        #[arg(short, long)]
        output: Option<String>,
    },
    /// Import a skill from a .toml file (v2.3 Marketplace)
    Import {
        /// Path to the skill .toml file
        path: String,
        /// Install globally to ~/.evocli/skills/ (default: project-local .evocli/skills/)
        #[arg(long)]
        global: bool,
        /// Overwrite existing skill with the same ID
        #[arg(long)]
        force: bool,
    },
    /// Promote a draft Skill to trusted status (closes the Evolution flywheel)
    ///
    /// After the Evolution Engine generates a Skill draft, use this command
    /// to review and activate it. The draft's status is changed from "draft"
    /// to "trusted" in-place, making it available for automatic execution.
    ///
    /// Example:
    ///   evocli evolve drafts        # list pending drafts
    ///   evocli skill show auto_abc  # review the draft
    ///   evocli skill promote auto_abc  # activate it
    Promote {
        /// Skill ID to promote from draft to trusted
        skill_id: String,
        /// Demote instead (change trusted → draft for review)
        #[arg(long)]
        demote: bool,
    },
    /// Show detailed info for a skill
    Show { skill_id: String },
}

pub async fn run(action: SkillAction) -> Result<()> {
    match action {
        SkillAction::List => cmd_list(),
        SkillAction::Run { skill_id, dry_run } => {
            println!(
                "Running skill '{}' via Soul{}...",
                skill_id,
                if dry_run { " (dry-run)" } else { "" }
            );
            println!(
                "Tip: run 'evocli' and use '/skill run {}' inside the TUI.",
                skill_id
            );
            Ok(())
        }
        SkillAction::Reload => {
            println!("Skills will be reloaded on next Soul startup.");
            Ok(())
        }
        SkillAction::Export { skill_id, output } => cmd_export(&skill_id, output.as_deref()),
        SkillAction::Import {
            path,
            global,
            force,
        } => cmd_import(&path, global, force),
        SkillAction::Promote { skill_id, demote } => cmd_promote(&skill_id, demote),
        SkillAction::Show { skill_id } => cmd_show(&skill_id),
    }
}

// ── list ─────────────────────────────────────────────────────────────────────

fn cmd_list() -> Result<()> {
    let dirs_to_search = [global_skills_dir(), PathBuf::from(".evocli").join("skills")];
    let mut found = 0usize;
    for dir in &dirs_to_search {
        if !dir.exists() {
            continue;
        }
        let label = if dir.starts_with(dirs::home_dir().unwrap_or_default()) {
            "global (~/.evocli/skills/)"
        } else {
            "project (.evocli/skills/)"
        };
        let mut skills: Vec<_> = std::fs::read_dir(dir)?
            .flatten()
            .filter(|e| e.path().extension().map(|x| x == "toml").unwrap_or(false))
            .collect();
        if skills.is_empty() {
            continue;
        }
        skills.sort_by_key(|e| e.file_name());
        println!("\n  📁 {} — {} skill(s)", label, skills.len());
        for entry in &skills {
            let name = entry.file_name().to_string_lossy().replace(".toml", "");
            let meta = read_skill_meta(&entry.path());
            println!(
                "  ├─ {:25} {} [{}]",
                name,
                meta.0.unwrap_or_else(|| "—".into()),
                meta.1.unwrap_or_else(|| "draft".into())
            );
            found += 1;
        }
    }
    if found == 0 {
        println!("  No skills found. Create a skill TOML in ~/.evocli/skills/ or .evocli/skills/");
    }
    println!();
    Ok(())
}

// ── export ────────────────────────────────────────────────────────────────────

fn cmd_export(skill_id: &str, output: Option<&str>) -> Result<()> {
    let src = find_skill_file(skill_id)?;
    let dst = output
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(format!("{}.toml", skill_id)));

    std::fs::copy(&src, &dst)
        .with_context(|| format!("Failed to copy {} → {}", src.display(), dst.display()))?;

    println!("✅ Exported skill '{}' → {}", skill_id, dst.display());
    println!(
        "   Share this file and use: evocli skill import {}",
        dst.display()
    );
    Ok(())
}

// ── import ────────────────────────────────────────────────────────────────────

fn cmd_import(path: &str, global: bool, force: bool) -> Result<()> {
    let src = PathBuf::from(path);
    anyhow::ensure!(src.exists(), "File not found: {}", path);
    anyhow::ensure!(
        src.extension().map(|e| e == "toml").unwrap_or(false),
        "File must be a .toml skill file"
    );

    // Parse to validate
    let raw = std::fs::read_to_string(&src)?;
    let parsed: toml::Value =
        toml::from_str(&raw).with_context(|| format!("Invalid TOML in {}", path))?;
    let skill_id = parsed
        .get("skill")
        .and_then(|s| s.get("id"))
        .and_then(|v| v.as_str())
        .unwrap_or_else(|| {
            src.file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("unknown")
        });

    let target_dir = if global {
        global_skills_dir()
    } else {
        PathBuf::from(".evocli").join("skills")
    };
    std::fs::create_dir_all(&target_dir)?;
    let dst = target_dir.join(format!("{}.toml", skill_id));

    if dst.exists() && !force {
        anyhow::bail!(
            "Skill '{}' already exists at {}. Use --force to overwrite.",
            skill_id,
            dst.display()
        );
    }

    std::fs::copy(&src, &dst).with_context(|| format!("Failed to copy to {}", dst.display()))?;

    let scope = if global { "global" } else { "project-local" };
    println!(
        "✅ Imported skill '{}' → {} ({})",
        skill_id,
        dst.display(),
        scope
    );
    println!("   Run: evocli skill run {}", skill_id);
    Ok(())
}

// ── promote ───────────────────────────────────────────────────────────────────

/// Promote a Skill draft → trusted (or demote trusted → draft).
///
/// Closes the Evolution flywheel: Evolution Engine generates drafts, user
/// reviews with `evocli skill show <id>`, then promotes with this command.
/// The skill immediately becomes available for automatic execution by the AI.
fn cmd_promote(skill_id: &str, demote: bool) -> Result<()> {
    let path = find_skill_file(skill_id)?;
    let raw = std::fs::read_to_string(&path)
        .with_context(|| format!("Cannot read skill file: {}", path.display()))?;

    let (from_status, to_status) = if demote {
        ("trusted", "draft")
    } else {
        ("draft", "trusted")
    };

    // Validate current status
    if !raw.contains(&format!("status = \"{}\"", from_status)) {
        let current = if raw.contains("status = \"draft\"") {
            "draft"
        } else if raw.contains("status = \"trusted\"") {
            "trusted"
        } else {
            "unknown"
        };
        anyhow::bail!(
            "Skill '{}' has status '{}', cannot {}. Use evocli skill show {} to inspect.",
            skill_id,
            current,
            if demote { "demote" } else { "promote" },
            skill_id
        );
    }

    // Replace status field in TOML (simple string replacement, avoids losing comments)
    let updated = raw.replace(
        &format!("status = \"{}\"", from_status),
        &format!("status = \"{}\"", to_status),
    );
    std::fs::write(&path, updated)
        .with_context(|| format!("Cannot write skill file: {}", path.display()))?;

    if demote {
        println!("↩  Skill '{}' demoted: trusted → draft", skill_id);
        println!("   It will no longer run automatically until promoted again.");
    } else {
        println!("✅ Skill '{}' promoted: draft → trusted", skill_id);
        println!("   The AI can now discover and execute it automatically.");
        println!("   Test with: evocli skill run {} --dry-run", skill_id);
    }
    Ok(())
}

// ── show ──────────────────────────────────────────────────────────────────────

fn cmd_show(skill_id: &str) -> Result<()> {
    let path = find_skill_file(skill_id)?;
    let raw = std::fs::read_to_string(&path)?;
    let parsed: toml::Value = toml::from_str(&raw)?;

    println!("\n  Skill: {}", skill_id);
    println!("  File:  {}", path.display());
    if let Some(skill) = parsed.get("skill") {
        if let Some(name) = skill.get("name").and_then(|v| v.as_str()) {
            println!("  Name:  {}", name);
        }
        if let Some(ver) = skill.get("version").and_then(|v| v.as_str()) {
            println!("  Ver:   {}", ver);
        }
        if let Some(status) = skill.get("status").and_then(|v| v.as_str()) {
            println!("  Status:{}", status);
        }
        if let Some(steps) = skill.get("steps").and_then(|v| v.as_array()) {
            println!("\n  Steps ({}):", steps.len());
            for (i, step) in steps.iter().enumerate() {
                let id = step.get("id").and_then(|v| v.as_str()).unwrap_or("?");
                let action = step.get("action").and_then(|v| v.as_str()).unwrap_or("?");
                let req = step
                    .get("requires_approval")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                println!(
                    "    {}. {} → {}{}",
                    i + 1,
                    id,
                    action,
                    if req { "  ⚠ requires approval" } else { "" }
                );
            }
        }
    }
    println!();
    Ok(())
}

// ── helpers ───────────────────────────────────────────────────────────────────

fn find_skill_file(skill_id: &str) -> Result<PathBuf> {
    let candidates = [
        PathBuf::from(".evocli")
            .join("skills")
            .join(format!("{}.toml", skill_id)),
        global_skills_dir().join(format!("{}.toml", skill_id)),
    ];
    candidates
        .iter()
        .find(|p| p.exists())
        .cloned()
        .with_context(|| {
            format!(
                "Skill '{}' not found in .evocli/skills/ or ~/.evocli/skills/",
                skill_id
            )
        })
}

fn read_skill_meta(path: &PathBuf) -> (Option<String>, Option<String>) {
    let raw = std::fs::read_to_string(path).unwrap_or_default();
    let parsed: toml::Value = toml::from_str(&raw).unwrap_or(toml::Value::Boolean(false));
    let name = parsed
        .get("skill")
        .and_then(|s| s.get("name"))
        .and_then(|v| v.as_str())
        .map(str::to_string);
    let status = parsed
        .get("skill")
        .and_then(|s| s.get("status"))
        .and_then(|v| v.as_str())
        .map(str::to_string);
    (name, status)
}
