#!/usr/bin/python3
"""A harmless Herdr CLI stand-in: only records arguments and returns fixtures."""
import json
from pathlib import Path
import sys

base = Path(sys.argv[0])
state = json.loads(Path(str(base) + '.state.json').read_text())
with Path(str(base) + '.calls.jsonl').open('a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\n')
if sys.argv[1:3] == ['agent', 'get']:
    result = {'type':'agent_info', 'agent':state['agent']}
elif sys.argv[1:3] == ['pane', 'process-info']:
    result = {'type':'pane_process_info', 'process_info':{'foreground_process_group_id':state['pid'], 'shell_pid':1}}
elif sys.argv[1:3] == ['agent', 'prompt']:
    if state.get('uncertain'): sys.exit(1)
    result = {'type':'agent_prompted', 'agent':state['agent']}
else: sys.exit(2)
print(json.dumps({'result':result}))
