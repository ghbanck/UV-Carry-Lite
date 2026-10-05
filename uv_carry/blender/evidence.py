"""Evidence the tool keeps about what it did: closed sessions and notes, in order. The log
stays in memory, bounded; when a trace folder is set (by a test run or in the add-on
preferences), it is also written to <folder>/trace.json after every entry.
"""

import json
import os
from collections import deque

LOG_LIMIT = 500
RUN_LOG = deque(maxlen=LOG_LIMIT)      # closed sessions and notes, oldest first
TRACE_DIR = None                       # folder for trace.json; None: memory only
ENVIRONMENT = {}                       # written with the trace: Blender build, OS, commit, input kind


def jsonable(value):
    return json.loads(json.dumps(value, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)))


def add(entry):
    RUN_LOG.append(jsonable(entry))
    write_trace()


def write_trace(folder=None):
    folder = folder or TRACE_DIR
    if not folder:
        return
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "trace.json"), "w", encoding="utf-8") as fh:
        json.dump({"environment": ENVIRONMENT, "log": list(RUN_LOG)}, fh, indent=1)
