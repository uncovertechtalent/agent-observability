#!/usr/bin/env bash
# Send a mix of streamed and non-streamed requests through the metering proxy
# so every dashboard panel has data. Usage: scripts/load.sh [host] [rounds]
set -euo pipefail
HOST=${1:-localhost}
ROUNDS=${2:-5}
URL="http://$HOST:11435"
MODELS=(qwen2.5:3b llama3.2:latest)
PROMPTS=("Explain a Prometheus histogram in two sentences." "What is an error budget?" "Name three causes of high p99 latency.")

for ((r = 1; r <= ROUNDS; r++)); do
  for m in "${MODELS[@]}"; do
    p=${PROMPTS[$((RANDOM % ${#PROMPTS[@]}))]}
    curl -s "$URL/api/chat" -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"$p\"}]}" > /dev/null
    curl -s "$URL/v1/chat/completions" -H 'Content-Type: application/json' \
      -d "{\"model\":\"$m\",\"stream\":true,\"stream_options\":{\"include_usage\":true},\"messages\":[{\"role\":\"user\",\"content\":\"$p\"}]}" > /dev/null
  done
  curl -s "$URL/api/embed" -d '{"model":"nomic-embed-text:latest","input":"error budget"}' > /dev/null
  echo "round $r done"
done
