#!/usr/bin/env bash
# One-time setup script for Atlas personal assistant
set -e

echo "=== Atlas Setup ==="

# 1. Create virtual environment
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "Created virtual environment."
fi
source .venv/bin/activate

# 2. Install dependencies
pip install -q --upgrade pip
pip install -r requirements.txt
echo "Dependencies installed."

# 3. Create .env if it doesn't exist
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo ">>> Created .env from .env.example."
    echo ">>> IMPORTANT: Fill in your API keys in .env before running."
fi

# 4. Create log directory
mkdir -p logs data/memory

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env and fill in your API keys"
echo "  2. source .venv/bin/activate"
echo "  3. python main.py"
echo ""
echo "Key things to configure:"
echo "  - ANTHROPIC_API_KEY  → console.anthropic.com"
echo "  - DISCORD_BOT_TOKEN  → discord.com/developers"
echo "  - DISCORD_USER_ID    → Discord: Settings > Advanced > Developer Mode, then right-click yourself"
echo "  - TODOIST_API_TOKEN  → todoist.com/app/settings/integrations/developer"
echo "  - ICLOUD_USERNAME    → your Apple ID email"
echo "  - ICLOUD_APP_PASSWORD → appleid.apple.com > Security > App-Specific Passwords"
