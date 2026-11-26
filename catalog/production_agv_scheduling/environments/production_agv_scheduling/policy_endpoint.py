import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/"initial"))
from scheduler import Scheduler
_policy=None

def decide(operation, snapshot=None):
    global _policy
    if operation=='reset':
        _policy=Scheduler()
        return None
    if _policy is None: _policy=Scheduler()
    return _policy.run(snapshot)
