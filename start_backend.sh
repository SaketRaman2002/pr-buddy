#!/bin/bash
cd "$(dirname "$0")/backend"
echo "Starting PR Review Backend on port 8001..."
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
