#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "No .env file found. Copy .env.example and add your ANTHROPIC_API_KEY."
  exit 1
fi

python3 app.py
