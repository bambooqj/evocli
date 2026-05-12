//! commands/jobs_cmd.rs — evocli jobs 子命令（Section 28）
use crate::job_queue::{jobs_db_path, JobQueue, JobType};
use anyhow::Result;
use clap::Subcommand;

#[derive(Subcommand)]
pub enum JobsAction {
    /// List jobs (optionally filter by status)
    List {
        #[arg(long)]
        status: Option<String>,
    },
    /// Push a job to the queue
    Run {
        /// Job type: memory-distill | code-index | evolution-scan | skill-run
        job_type: String,
    },
    /// Clear failed jobs
    ClearFailed,
    /// Show queue statistics
    Status,
}

pub fn run(action: JobsAction) -> Result<()> {
    let db_path = jobs_db_path();
    let q = JobQueue::new(&db_path)?;

    match action {
        JobsAction::List { status } => {
            let jobs = q.list(status.as_deref())?;
            if jobs.is_empty() {
                println!("No jobs found.");
                return Ok(());
            }
            println!(
                "{:<38} {:<22} {:<12} {:<5} {}",
                "Job ID", "Type", "Status", "Retry", "Created"
            );
            println!("{}", "\u{2500}".repeat(100));
            for j in &jobs {
                println!(
                    "{:<38} {:<22} {:<12} {:<5} {}",
                    &j.id[..j.id.len().min(36)],
                    j.job_type_name,
                    j.status,
                    j.retry_count,
                    &j.created_at[..j.created_at.len().min(19)]
                );
            }
        }

        JobsAction::Run { job_type } => {
            let cwd = std::env::current_dir()?;
            let jt = match job_type.as_str() {
                "memory-distill" => JobType::MemoryDistill {
                    session_id: "manual".to_string(),
                },
                "code-index" => JobType::CodeIndexUpdate {
                    project: cwd.to_string_lossy().to_string(),
                    changed_files: vec![],
                },
                "evolution-scan" => JobType::EvolutionScan {
                    project: cwd.to_string_lossy().to_string(),
                },
                "skill-run" => JobType::SkillRun {
                    skill_id: "default".to_string(),
                    project: cwd.to_string_lossy().to_string(),
                    dry_run: false,
                },
                unknown => {
                    eprintln!("Unknown job type: {}. Use: memory-distill, code-index, evolution-scan, skill-run", unknown);
                    std::process::exit(1);
                }
            };
            let id = q.push(jt)?;
            println!("Job queued: {}", id);
        }

        JobsAction::ClearFailed => {
            let n = q.clear_failed()?;
            println!("Cleared {} failed jobs.", n);
        }

        JobsAction::Status => {
            let all = q.list(None)?;
            let pending = all.iter().filter(|j| j.status == "pending").count();
            let running = all.iter().filter(|j| j.status == "running").count();
            let done = all.iter().filter(|j| j.status == "done").count();
            let failed = all.iter().filter(|j| j.status == "failed").count();
            println!("Job Queue Status ({})", db_path.display());
            println!(
                "  Pending: {}  Running: {}  Done: {}  Failed: {}",
                pending, running, done, failed
            );
        }
    }
    Ok(())
}
