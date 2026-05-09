#!/bin/bash
# setup_mac.sh — Install dependencies on macOS
set -e

echo "Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

echo "Installing dependencies..."
pip install -r requirements.txt

echo ""
echo "Setup complete!"
echo ""
echo "IMPORTANT: The 'keyboard' library requires accessibility permissions on macOS."
echo "Go to: System Settings → Privacy & Security → Accessibility"
echo "Add Terminal (or your IDE) to the allowed list."
echo ""
echo "Run the app with:  bash run.sh"
