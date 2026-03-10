#!/bin/bash
cd "$(dirname "$0")"

SETTINGS=~/.claude/settings.json
PID_FILE=/tmp/kiro-gateway.pid

# 注入 kiro 配置到 claude settings
python3 -c "
import json, sys
with open('$SETTINGS') as f:
    s = json.load(f)
s.setdefault('env', {})
s['env']['ANTHROPIC_API_KEY'] = 'my-super-secret-password-123'
s['env']['ANTHROPIC_BASE_URL'] = 'http://localhost:8000'
with open('$SETTINGS', 'w') as f:
    json.dump(s, f, indent=2, ensure_ascii=False)
print('claude settings -> kiro-gateway')
"

nohup python3.11 main.py > /tmp/kiro-gateway.log 2>&1 &
echo $! > $PID_FILE
echo "kiro-gateway started, PID: $(cat $PID_FILE), log: /tmp/kiro-gateway.log"
