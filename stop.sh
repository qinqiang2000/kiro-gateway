#!/bin/bash
SETTINGS=~/.claude/settings.json
PID_FILE=/tmp/kiro-gateway.pid

# 停止进程
if [ -f $PID_FILE ]; then
    kill $(cat $PID_FILE) 2>/dev/null
    rm $PID_FILE
    echo "kiro-gateway stopped"
else
    echo "kiro-gateway not running"
fi

# 还原 claude settings 为 zshrc 里的配置
python3 -c "
import json
with open('$SETTINGS') as f:
    s = json.load(f)
s.setdefault('env', {})
s['env']['ANTHROPIC_API_KEY'] = 'sk-WsdTzqsVSJOjttd2d2sxlg'
s['env']['ANTHROPIC_BASE_URL'] = 'http://129.226.88.226:4000'
with open('$SETTINGS', 'w') as f:
    json.dump(s, f, indent=2, ensure_ascii=False)
print('claude settings -> restored')
"
