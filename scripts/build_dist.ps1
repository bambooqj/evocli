# build_dist.ps1 — EvoCLI 发布构建脚本
# 构建可直接拷贝部署的发行目录
# 用法：.\scripts\build_dist.ps1 [-Clean]
#
# 产出：dist/evocli-v<version>-<platform>/
#   evocli.exe        Rust 二进制（release）
#   evocli-soul/      Python Soul 源码（binary 自动发现）
#   setup.ps1         Windows 一键环境配置
#   setup.sh          Linux/macOS 一键环境配置
#   README.md         快速开始

param([switch]$Clean, [string]$Version = "")
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent

if (-not $Version) {
    $ct = Get-Content "$Root\Cargo.toml" -Raw
    if ($ct -match 'version\s*=\s*"([^"]+)"') { $Version = $Matches[1] } else { $Version = "0.1.0" }
}
$Platform = if ($IsWindows) { "windows-x86_64" } elseif ($IsMacOS) { "macos-aarch64" } else { "linux-x86_64" }
$Ext      = if ($IsWindows) { ".exe" } else { "" }
$DistDir  = "$Root\dist\evocli-v$Version-$Platform"

Write-Host ""
Write-Host "━━━ EvoCLI Distribution Build ━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host "  Version:  $Version"
Write-Host "  Platform: $Platform"
Write-Host "  Output:   $DistDir"
Write-Host ""

if ($Clean -and (Test-Path $DistDir)) {
    Remove-Item $DistDir -Recurse -Force
    Write-Host "  Cleaned old dist" -ForegroundColor Gray
}
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

# ── 1/4: Release build ──────────────────────────────────────────────
Write-Host "[1/4] cargo build --release..." -ForegroundColor Yellow
Set-Location $Root
cargo build --release -p evocli 2>&1 | Select-String "Compiling|Finished|^error"
if ($LASTEXITCODE -ne 0) { throw "cargo build --release failed" }
$bin  = "$Root\target\release\evocli$Ext"
$bsz  = [math]::Round((Get-Item $bin).Length / 1MB, 1)
Write-Host "  ✓  evocli$Ext ($bsz MB)" -ForegroundColor Green

# ── 2/4: Copy binary ────────────────────────────────────────────────
Write-Host ""
Write-Host "[2/4] Copying binary..." -ForegroundColor Yellow
Copy-Item $bin "$DistDir\evocli$Ext" -Force
Write-Host "  ✓  evocli$Ext" -ForegroundColor Green

# ── 3/4: Copy Python Soul ───────────────────────────────────────────
Write-Host ""
Write-Host "[3/4] Copying Python Soul..." -ForegroundColor Yellow
$soulDst = "$DistDir\evocli-soul"
New-Item -ItemType Directory -Force -Path $soulDst | Out-Null
Copy-Item "$Root\evocli-soul\evocli_soul" $soulDst -Recurse -Force
Copy-Item "$Root\evocli-soul\pyproject.toml" $soulDst -Force
# 清理编译缓存
Get-ChildItem $soulDst -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $soulDst -Recurse -Filter "*.pyc"       -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
$pyCount = (Get-ChildItem "$soulDst\evocli_soul" -Recurse -Filter "*.py").Count
Write-Host "  ✓  evocli-soul/ ($pyCount .py files)" -ForegroundColor Green

# Copy scripts
Copy-Item "$Root\scripts\download_models.py" "$DistDir\download_models.py" -Force
Write-Host "  ✓  download_models.py" -ForegroundColor Green
Copy-Item "$Root\scripts\preflight.py" "$DistDir\preflight.py" -Force
Write-Host "  ✓  preflight.py" -ForegroundColor Green
Copy-Item "$Root\scripts\setup_env.py" "$DistDir\setup_env.py" -Force
Write-Host "  ✓  setup_env.py" -ForegroundColor Green

# ── 4/4: Write setup scripts & README ──────────────────────────────
Write-Host ""
Write-Host "[4/4] Writing setup scripts & README..." -ForegroundColor Yellow

# setup_env.py is the canonical cross-platform setup script.
# setup.ps1 and setup.sh are thin wrappers that bootstrap Python then delegate.

# ── setup.ps1 (Windows thin wrapper) ───────────────────────────────
$setupPs1 = @'
# setup.ps1 — EvoCLI Windows setup (delegates to setup_env.py)
# Usage: .\setup.ps1
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path $MyInvocation.MyCommand.Path -Parent

Write-Host ""
Write-Host "=== EvoCLI Environment Setup ===" -ForegroundColor Cyan
Write-Host ""

# Bootstrap: need *any* Python to run setup_env.py which handles everything else
$BootPy = (Get-Command python -ErrorAction SilentlyContinue)?.Source
if (-not $BootPy) { $BootPy = (Get-Command python3 -ErrorAction SilentlyContinue)?.Source }
if (-not $BootPy) {
    Write-Error "Python not found. Install Python 3.10+ from https://python.org"
}

Write-Host "  Bootstrapping with: $BootPy"
& $BootPy "$ScriptDir\setup_env.py" @args
'@
Set-Content "$DistDir\setup.ps1" $setupPs1 -Encoding UTF8
Write-Host "  ✓  setup.ps1" -ForegroundColor Green

# ── setup.sh (Linux/macOS thin wrapper) ────────────────────────────
$setupShLines = @(
    "#!/usr/bin/env bash",
    "# setup.sh -- EvoCLI Linux/macOS setup (delegates to setup_env.py)",
    "# Usage: bash setup.sh",
    "set -e",
    'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"',
    "",
    'echo ""',
    'echo "=== EvoCLI Environment Setup ==="',
    'echo ""',
    "",
    "# Bootstrap: use any available Python to run setup_env.py",
    'BOOT_PY=$(command -v python3 2>/dev/null || command -v python 2>/dev/null)',
    'if [ -z "$BOOT_PY" ]; then',
    '    echo "Python not found. Install Python 3.10+ from https://python.org"',
    "    exit 1",
    "fi",
    'echo "  Bootstrapping with: $BOOT_PY"',
    '"$BOOT_PY" "$SCRIPT_DIR/setup_env.py" "$@"',
    'chmod +x "$SCRIPT_DIR/evocli" 2>/dev/null || true'
)
$setupShContent = $setupShLines -join "`n"
$utf8noBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText("$DistDir\setup.sh", $setupShContent + "`n", $utf8noBom)
Write-Host "  ✓  setup.sh" -ForegroundColor Green


# ── setup.ps1 (Windows) ────────────────────────────────────────────
$setupPs1Content = @'
# setup.ps1 — EvoCLI Windows 一键环境初始化
# 功能：安装 uv -> Python 3.11 venv -> evocli-soul 依赖
# 所有内容安装在 ~/.evocli/ 下，不污染系统 Python 环境
# 用法：.\setup.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path $MyInvocation.MyCommand.Path -Parent
$SoulDir   = Join-Path $ScriptDir "evocli-soul"
$UvBin     = "$env:USERPROFILE\.evocli\bin\uv.exe"
$VenvDir   = "$env:USERPROFILE\.evocli\venv"

Write-Host ""
Write-Host "━━━ EvoCLI Environment Setup (Windows) ━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host ""

# Step 1: uv
Write-Host "[1/3] Setting up uv (Rust-based Python manager)..." -ForegroundColor Yellow
$uvPath = (Get-Command uv -ErrorAction SilentlyContinue)?.Source
if ($uvPath) {
    Write-Host "  ✓  uv already in PATH: $uvPath" -ForegroundColor Green
} elseif (Test-Path $UvBin) {
    $uvPath = $UvBin
    Write-Host "  ✓  uv found at $uvPath" -ForegroundColor Green
} else {
    Write-Host "  → Downloading uv from GitHub releases..."
    New-Item -ItemType Directory -Force -Path (Split-Path $UvBin) | Out-Null
    $zipUrl = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
    $zipTmp = "$env:TEMP\uv-latest.zip"
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipTmp -UseBasicParsing
    Expand-Archive -Path $zipTmp -DestinationPath (Split-Path $UvBin) -Force
    Remove-Item $zipTmp -ErrorAction SilentlyContinue
    if (-not (Test-Path $UvBin)) {
        Write-Error "uv installation failed. Install manually: https://docs.astral.sh/uv/getting-started/installation/"
    }
    $uvPath = $UvBin
    Write-Host "  ✓  uv installed: $uvPath" -ForegroundColor Green
}

# Step 2: Python 3.11 venv
Write-Host ""
Write-Host "[2/3] Setting up Python 3.11 isolated environment..." -ForegroundColor Yellow
if (Test-Path "$VenvDir\Scripts\python.exe") {
    Write-Host "  ✓  venv exists: $VenvDir" -ForegroundColor Green
} else {
    Write-Host "  → Creating venv (uv will download Python 3.11 if needed)..."
    & $uvPath venv $VenvDir --python 3.11 --seed
    if ($LASTEXITCODE -ne 0) { Write-Error "Failed to create venv" }
    Write-Host "  ✓  venv created: $VenvDir" -ForegroundColor Green
}

# Step 3: Install evocli-soul[full] — 所有功能一次性安装
Write-Host ""
Write-Host "[3/4] Installing evocli-soul[full] (all features included)..." -ForegroundColor Yellow
Write-Host "  First run: may take 3-5 minutes (downloading ML models etc.)" -ForegroundColor Gray
Write-Host "  Includes: vector memory, code intelligence, skills, evolution" -ForegroundColor Gray
& $uvPath pip install -e "$SoulDir[full]" --python "$VenvDir\Scripts\python.exe"
if ($LASTEXITCODE -ne 0) { Write-Error "Failed to install evocli-soul[full]" }
Write-Host "  ✓  evocli-soul[full] installed — all features ready" -ForegroundColor Green

# Step 4: Pre-download embedding model (jina-zh, ~570 MB, one-time)
# Uses hf-mirror.com automatically when HF_ENDPOINT is not set.
# If the download fails (no network / firewall), EvoCLI still starts with
# text-search fallback — the model can be downloaded later by re-running this step.
Write-Host ""
Write-Host "[4/4] Pre-downloading embedding model (~570 MB, one-time)..." -ForegroundColor Yellow
Write-Host "  Model  : jinaai/jina-embeddings-v2-base-zh (中英双语向量搜索)" -ForegroundColor Gray
Write-Host "  Mirror : hf-mirror.com  (auto-enabled for better connectivity)" -ForegroundColor Gray
Write-Host "  Re-run : .\download_models.py   to retry if interrupted" -ForegroundColor Gray
$VenvPython = "$VenvDir\Scripts\python.exe"
& $VenvPython "$ScriptDir\download_models.py"
if ($LASTEXITCODE -ne 0) {
    Write-Host "" 
    Write-Host "  ⚠  Model download failed or skipped." -ForegroundColor Yellow
    Write-Host "     EvoCLI works now (text search). Re-run later:" -ForegroundColor Yellow
    Write-Host "     $VenvPython $ScriptDir\download_models.py" -ForegroundColor Gray
} else {
    Write-Host "  ✓  Embedding model cached — vector memory ready" -ForegroundColor Green
}

# Step 5: Verify environment — every critical import must succeed.
# On failure: auto-reinstall broken packages and re-verify.
Write-Host ""
Write-Host "[5/5] Verifying environment (all dependencies must pass)..." -ForegroundColor Yellow
& $VenvPython "$ScriptDir\preflight.py"
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "  Auto-repairing broken packages..." -ForegroundColor Yellow
    & $uvPath pip install --force-reinstall `
        "scipy>=1.11,<2" "numpy>=1.26,<3" "onnxruntime>=1.19,<2" `
        "lancedb>=0.5,<0.35" "fastembed>=0.4,<0.9" `
        --python "$VenvDir\Scripts\python.exe"
    & $VenvPython "$ScriptDir\preflight.py"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [WARN] Some checks still failing — see output above." -ForegroundColor Red
        Write-Host "         Run: .\preflight.py  to diagnose." -ForegroundColor Gray
    } else {
        Write-Host "  [OK]  All checks passed after repair." -ForegroundColor Green
    }
} else {
    Write-Host "  [OK]  All dependencies verified — environment is healthy." -ForegroundColor Green
}

# Done
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host "  ✅  Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "  Next steps:"
Write-Host "    .\evocli.exe init    <- select LLM provider + API key"
Write-Host "    .\evocli.exe doctor  <- verify all checks pass"
Write-Host "    .\evocli.exe         <- start AI coding session"
Write-Host ""
Write-Host "  Optional: add this folder to PATH for global access"
Write-Host ""
'@
Set-Content "$DistDir\setup.ps1" $setupPs1Content -Encoding UTF8
Write-Host "  ✓  setup.ps1" -ForegroundColor Green

# ── setup.sh (Linux/macOS) ─────────────────────────────────────────
$setupShLines = @(
    "#!/usr/bin/env bash",
    "# setup.sh — EvoCLI Linux/macOS 一键环境初始化",
    "# 功能：安装 uv -> Python 3.11 venv -> evocli-soul 依赖",
    "# 用法：bash setup.sh",
    "set -e",
    "",
    'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"',
    'SOUL_DIR="$SCRIPT_DIR/evocli-soul"',
    'UV_INSTALL_DIR="$HOME/.evocli/bin"',
    'VENV_DIR="$HOME/.evocli/venv"',
    "",
    'echo ""',
    'echo "━━━ EvoCLI Environment Setup (Linux/macOS) ━━━━━━━━━━━━"',
    'echo ""',
    "",
    "# Step 1: uv",
    'echo "[1/3] Setting up uv (Rust-based Python manager)..."',
    'if command -v uv &>/dev/null; then',
    '    UV_PATH="$(command -v uv)"',
    '    echo "  ✓  uv already in PATH: $UV_PATH"',
    'elif [ -x "$UV_INSTALL_DIR/uv" ]; then',
    '    UV_PATH="$UV_INSTALL_DIR/uv"',
    '    echo "  ✓  uv found at $UV_PATH"',
    "else",
    '    echo "  → Installing uv from astral.sh..."',
    '    mkdir -p "$UV_INSTALL_DIR"',
    '    curl -fsSL https://astral.sh/uv/install.sh | UV_INSTALL_DIR="$UV_INSTALL_DIR" sh',
    '    UV_PATH="$UV_INSTALL_DIR/uv"',
    '    if [ ! -x "$UV_PATH" ]; then',
    '        echo "  ✗  uv install failed. See: https://docs.astral.sh/uv/"',
    "        exit 1",
    "    fi",
    '    echo "  ✓  uv installed: $UV_PATH"',
    "fi",
    "",
    "# Step 2: Python 3.11 venv",
    'echo ""',
    'echo "[2/3] Setting up Python 3.11 isolated environment..."',
    'if [ -f "$VENV_DIR/bin/python3" ]; then',
    '    echo "  ✓  venv exists: $VENV_DIR"',
    "else",
    '    echo "  → Creating venv (uv will download Python 3.11 if needed)..."',
    '    "$UV_PATH" venv "$VENV_DIR" --python 3.11 --seed',
    '    echo "  ✓  venv created: $VENV_DIR"',
    "fi",
    "",
    "# Step 3: Install evocli-soul[full] — all features included",
    'echo ""',
    'echo "[3/4] Installing evocli-soul[full] (all features)..."',
    'echo "  First run: may take 3-5 minutes (downloading ML models etc.)"',
    '"$UV_PATH" pip install -e "$SOUL_DIR[full]" --python "$VENV_DIR/bin/python3"',
    'chmod +x "$SCRIPT_DIR/evocli" 2>/dev/null || true',
    'echo "  ✓  evocli-soul[full] installed — all features ready"',
    "",
    "# Step 4: Pre-download embedding model",
    'echo ""',
    'echo "[4/4] Pre-downloading embedding model (~570 MB, one-time)..."',
    'echo "  Model  : jinaai/jina-embeddings-v2-base-zh (bilingual vector memory)"',
    'echo "  Mirror : hf-mirror.com (auto, for better connectivity)"',
    '"$VENV_DIR/bin/python3" "$SCRIPT_DIR/download_models.py"',
    'if [ $? -ne 0 ]; then',
    '    echo "  ⚠  Download failed — EvoCLI works with text search."',
    '    echo "     Retry: $VENV_DIR/bin/python3 $SCRIPT_DIR/download_models.py"',
    "else",
    '    echo "  ✓  Embedding model cached — vector memory ready"',
    "fi",
    "",
    "# Done",
    'echo ""',
    'echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"',
    'echo "  ✅  Setup complete!"',
    'echo ""',
    'echo "  Next steps:"',
    'echo "    ./evocli init    <- select LLM provider + API key"',
    'echo "    ./evocli doctor  <- verify all checks pass"',
    'echo "    ./evocli         <- start AI coding session"',
    'echo ""'
)
$setupShContent = $setupShLines -join "`n"
# UTF-8 without BOM (System.Text.UTF8Encoding with $false = no BOM)
$utf8noBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText("$DistDir\setup.sh", $setupShContent + "`n", $utf8noBom)
Write-Host "  ✓  setup.sh" -ForegroundColor Green

# ── README.md ──────────────────────────────────────────────────────
$readmeContent = @"
# EvoCLI v$Version — Deployable Package

AI-native coding Runtime. Local-first, long memory.

## Quick Start

### Windows
``````powershell
.\setup.ps1          # First time: set up Python environment (2-5 min)
.\evocli.exe init    # Configure LLM provider + API key
.\evocli.exe         # Start AI coding session
``````

### Linux / macOS
``````bash
bash setup.sh        # First time: set up Python environment (2-5 min)
./evocli init        # Configure LLM provider + API key
./evocli             # Start AI coding session
``````

## Package Contents

| File/Dir | Description |
|---|---|
| ``evocli$Ext`` | Main binary ($bsz MB, Rust release build) |
| ``evocli-soul/`` | Python AI engine (auto-discovered by binary) |
| ``setup.ps1`` | Windows environment setup script |
| ``setup.sh`` | Linux/macOS environment setup script |

## How It Works

1. **setup** installs ``uv`` (Rust-based Python manager) + Python 3.11 + all deps into ``~/.evocli/venv/``
2. On every startup, ``evocli`` auto-detects ``evocli-soul/`` relative to the binary and uses ``~/.evocli/venv/python`` — **no system Python dependency**
3. ``evocli init`` saves config to ``~/.evocli/config.toml``

## Key Commands

| Command | Description |
|---|---|
| ``evocli`` | Start TUI (AI coding session) |
| ``evocli init`` | Setup wizard |
| ``evocli doctor`` | Health check (10 items) |
| ``evocli index`` | Index project code symbols |
| ``evocli stats`` | Flywheel metrics dashboard |
| ``evocli skill list/export/import`` | Skill management |
| ``evocli tool register`` | Register custom tools (LLM-discoverable) |

## System Requirements

- OS: Windows 10+, macOS 12+, Linux (glibc 2.17+)
- RAM: 512 MB+ (1 GB+ during AI inference)
- Network: Access to LLM provider API (Ollama can run fully offline)

Built: $(Get-Date -Format 'yyyy-MM-dd')
"@
Set-Content "$DistDir\README.md" $readmeContent -Encoding UTF8
Write-Host "  ✓  README.md" -ForegroundColor Green

# ── Override setup scripts with the authoritative versions ─────────
# The build script generates simplified inline setup scripts above.
# We replace them with the maintained versions from scripts/ directory
# which include: -Clean / -Force flags, proper error handling, and
# step-by-step feedback for the user.
$AuthoritativeSetupPs1 = Join-Path $PSScriptRoot "setup_dist.ps1"
$AuthoritativeSetupSh  = Join-Path $PSScriptRoot "setup_dist.sh"
if (Test-Path $AuthoritativeSetupPs1) {
    Copy-Item $AuthoritativeSetupPs1 "$DistDir\setup.ps1" -Force
    Write-Host "  ✓  setup.ps1 (authoritative)" -ForegroundColor Green
}
if (Test-Path $AuthoritativeSetupSh) {
    $utf8noBom = New-Object System.Text.UTF8Encoding $false
    $content = Get-Content $AuthoritativeSetupSh -Raw
    [System.IO.File]::WriteAllText("$DistDir\setup.sh", $content, $utf8noBom)
    Write-Host "  ✓  setup.sh (authoritative)" -ForegroundColor Green
}

# ── Summary ─────────────────────────────────────────────────────────
$allFiles = Get-ChildItem $DistDir -Recurse -File
$totalSz  = [math]::Round(($allFiles | Measure-Object Length -Sum).Sum / 1MB, 1)

Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host "  ✅  Distribution ready!" -ForegroundColor Green
Write-Host ""
Write-Host "  $DistDir"
Write-Host "  Total: $totalSz MB | $($allFiles.Count) files"
Write-Host ""
Get-ChildItem $DistDir | ForEach-Object {
    if ($_.PSIsContainer) {
        $dirFiles = (Get-ChildItem $_.FullName -Recurse -File).Count
        Write-Host "  $($_.Name.PadRight(35)) (dir, $dirFiles files)"
    } else {
        $kb = [math]::Round($_.Length / 1KB)
        Write-Host "  $($_.Name.PadRight(35)) $kb KB"
    }
}
Write-Host ""
Write-Host "  To deploy:"
Write-Host "    Copy 'evocli-v$Version-$Platform' to any machine"
Write-Host "    Run:  .\setup.ps1   (Windows)"
Write-Host "    Run:  bash setup.sh  (Linux/macOS)"
Write-Host ""

