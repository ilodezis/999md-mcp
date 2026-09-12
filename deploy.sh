#!/bin/bash
# Runs on the server from the checkout: hard-sync to origin/main, rebuild, restart.
set -e
cd "$(dirname "$0")"
git fetch origin
git reset --hard origin/main
docker compose up -d --build
echo "999md-mcp deployed"
