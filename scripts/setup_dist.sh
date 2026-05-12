#!/usr/bin/env bash
# setup.sh — EvoCLI Linux/macOS 一键环境初始化
# 功能：安装 uv -> Python 3.11 venv -> evocli-soul 依赖
# 用法：bash setup.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOUL_DIR="$SCRIPT_DIR/evocli-soul"
UV_INSTALL_DIR="$HOME/.evocli/bin"
VENV_DIR="$HOME/.evocli/venv"

echo ""
echo "━━━ EvoCLI Environment Setup (Linux/macOS) ━━━━━━━━━━━━"
echo ""

# Step 1: uv
echo "[1/3] Setting up uv (Rust-based Python manager)..."
if command -v uv &>/dev/null; then
    UV_PATH="$(command -v uv)"
    echo "  ✓  uv already in PATH: $UV_PATH"
elif [ -x "$UV_INSTALL_DIR/uv" ]; then
    UV_PATH="$UV_INSTALL_DIR/uv"
    echo "  ✓  uv found at $UV_PATH"
else
    echo "  → Installing uv from astral.sh..."
    mkdir -p "$UV_INSTALL_DIR"
    curl -fsSL https://astral.sh/uv/install.sh | UV_INSTALL_DIR="$UV_INSTALL_DIR" sh
    UV_PATH="$UV_INSTALL_DIR/uv"
    if [ ! -x "$UV_PATH" ]; then
        echo "  ✗  uv install failed. See: https://docs.astral.sh/uv/"
        exit 1
    fi
    echo "  ✓  uv installed: $UV_PATH"
fi

# Step 2: Python 3.11 venv
echo ""
echo "[2/3] Setting up Python 3.11 isolated environment..."
if [ -f "$VENV_DIR/bin/python3" ]; then
    echo "  ✓  venv exists: $VENV_DIR"
else
    echo "  → Creating venv (uv will download Python 3.11 if needed)..."
    "$UV_PATH" venv "$VENV_DIR" --python 3.11 --seed
    echo "  ✓  venv created: $VENV_DIR"
fi

# Step 3: Install evocli-soul[full] — all features included
echo ""
echo "[3/4] Installing evocli-soul[full] (all features)..."
echo "  First run: may take 3-5 minutes (downloading ML models etc.)"
"$UV_PATH" pip install -e "$SOUL_DIR[full]" --python "$VENV_DIR/bin/python3"
chmod +x "$SCRIPT_DIR/evocli" 2>/dev/null || true
echo "  ✓  evocli-soul[full] installed — all features ready"

# Step 4: Pre-download embedding model
echo ""
echo "[4/4] Pre-downloading embedding model (~570 MB, one-time)..."
echo "  Model  : jinaai/jina-embeddings-v2-base-zh (bilingual vector memory)"
echo "  Mirror : hf-mirror.com (auto, for better connectivity)"
"$VENV_DIR/bin/python3" "$SCRIPT_DIR/download_models.py"
if [ $? -ne 0 ]; then
    echo "  ⚠  Download failed — EvoCLI works with text search."
    echo "     Retry: $VENV_DIR/bin/python3 $SCRIPT_DIR/download_models.py"
else
    echo "  ✓  Embedding model cached — vector memory ready"
fi

# Done
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅  Setup complete!"
echo ""
echo "  Next steps:"
echo "    ./evocli init    <- select LLM provider + API key"
echo "    ./evocli doctor  <- verify all checks pass"
echo "    ./evocli         <- start AI coding session"
echo ""
