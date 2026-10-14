"""Write controller-generated values privately, never through a shell source/eval."""

import json
import sys
from pathlib import Path

values = json.load(sys.stdin)
path = Path(".env")
if path.exists():
    previous = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.startswith("#"):
            try:
                previous[key] = json.loads(value)
            except ValueError:
                previous[key] = value.strip("'")
    values = {**previous, **values}
# JSON double-quoted strings are accepted by python-dotenv, including escaped newlines.
path.write_text("\n".join(key + "=" + json.dumps(value) for key, value in values.items()) + "\n")
path.chmod(0o600)
