//! commands/lsp_cmd.rs — evocli lsp 子命令（LSP 精度层）
use anyhow::Result;
use clap::Subcommand;

#[derive(Subcommand)]
pub enum LspAction {
    /// Show call hierarchy (incoming + outgoing calls)
    Calls {
        file: String,
        line: u32,
        character: u32,
    },
    /// Find all references to a symbol
    Refs {
        file: String,
        line: u32,
        character: u32,
    },
    /// Go to definition
    Def {
        file: String,
        line: u32,
        character: u32,
    },
}

pub async fn run(action: LspAction) -> Result<()> {
    let cwd = std::env::current_dir()?;

    match action {
        LspAction::Calls {
            file,
            line,
            character,
        } => {
            let fp = cwd.join(&file);
            let mut mgr = code_intel::LspManager::new(&cwd);
            match mgr.analyze_function(&fp, line, character).await {
                Ok(analysis) => println!("{}", serde_json::to_string_pretty(&analysis)?),
                Err(e) => {
                    eprintln!("LSP error: {e:#}");
                    eprintln!("Ensure the language server is installed and on PATH.");
                    std::process::exit(1);
                }
            }
        }
        LspAction::Refs {
            file,
            line,
            character,
        } => {
            let fp = cwd.join(&file);
            let mut mgr = code_intel::LspManager::new(&cwd);
            match mgr.find_references(&fp, line, character).await {
                Ok(refs) => println!("{}", serde_json::to_string_pretty(&refs)?),
                Err(e) => {
                    eprintln!("LSP error: {e:#}");
                    std::process::exit(1);
                }
            }
        }
        LspAction::Def {
            file,
            line,
            character,
        } => {
            let fp = cwd.join(&file);
            let mut mgr = code_intel::LspManager::new(&cwd);
            match mgr.goto_definition(&fp, line, character).await {
                Ok(Some(loc)) => println!("{}", serde_json::to_string_pretty(&loc)?),
                Ok(None) => println!("No definition found"),
                Err(e) => {
                    eprintln!("LSP error: {e:#}");
                    std::process::exit(1);
                }
            }
        }
    }
    Ok(())
}
