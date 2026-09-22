#!/usr/bin/env python3
import sys
import json
import urllib.parse

if len(sys.argv) != 2:
    print(f"Usage: {sys.argv[0]} startup_commands.json", file=sys.stderr)
    sys.exit(1)

with open(sys.argv[1]) as f:
    commands = json.load(f)
encoded = urllib.parse.quote(json.dumps(commands), safe="")
url = f"https://ui.perfetto.dev/#!/?startupCommands={encoded}"
print(url)
