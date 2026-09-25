#!/bin/bash
# Start Lark webhook server in background, capture output

set -e
cd /workspace/voice-persona-assessment/agent/soundmirror

# Load env
set -a
source /workspace/voice-persona-assessment/.env
set +a

# Make sure no old process
pkill -9 -f "run_webhook.py" 2>/dev/null || true
sleep 1

# Launch with -u for unbuffered, absolute python path
export PYTHONPATH=/workspace/voice-persona-assessment/agent
nohup /usr/bin/python3 -u /workspace/voice-persona-assessment/agent/soundmirror/run_webhook.py \
    > /workspace/voice-persona-assessment/agent/soundmirror/out/lark_logs/webhook.log 2>&1 &
PID=$!
echo "Started PID=$PID"
sleep 5

# Verify
if ! kill -0 $PID 2>/dev/null; then
    echo "FAIL: process died"
    cat /workspace/voice-persona-assessment/agent/soundmirror/out/lark_logs/webhook.log
    exit 1
fi

echo "OK alive"
echo "=== log ==="
cat /workspace/voice-persona-assessment/agent/soundmirror/out/lark_logs/webhook.log
echo
echo "=== health ==="
curl -s -m 3 http://127.0.0.1:18080/health || echo "no response"
echo
echo "PID=$PID" > /workspace/voice-persona-assessment/agent/soundmirror/out/lark_logs/webhook.pid
