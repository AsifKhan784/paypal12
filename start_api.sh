#!/usr/bin/env bash
# Launcher for the PayPal Charge API.
# Starts uvicorn, detaches from parent shell, logs to /tmp/pp_api.log
cd "$(dirname "$0")"
LOG=/tmp/pp_api.log
echo "[$(date)] starting paypal charger..." > "$LOG"
nohup python -c "
import os, sys
sys.path.insert(0, '.')
import uvicorn
from main import app
uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 8765)), log_level='info')
" >> "$LOG" 2>&1 &
PID=$!
disown
echo "API started, PID=$PID, log=$LOG"
echo "PID=$PID" > /tmp/pp_api.pid
sleep 4
echo "--- first 10 lines of log ---"
head -10 "$LOG"
