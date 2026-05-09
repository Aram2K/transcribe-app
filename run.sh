#!/bin/bash
# run.sh — Launch transcribe-app on macOS
cd "$(dirname "$0")"
source venv/bin/activate
python main.py
