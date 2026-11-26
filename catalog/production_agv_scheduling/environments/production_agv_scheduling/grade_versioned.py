from pathlib import Path
import shutil,sys,types
from evaluator import evaluate as domain_evaluate

def evaluate(candidate,context):
    proxy=types.ModuleType('optpilot_policy_endpoint')
    proxy.decide=candidate
    sys.modules[proxy.__name__]=proxy
    directory=Path(context['workspace'])/'candidate'
    directory.mkdir()
    shutil.copyfile(Path(__file__).parent/'initial/param_estimator.py',directory/'param_estimator.py')
    (directory/'scheduler.py').write_text("from optpilot_policy_endpoint import decide\nclass Scheduler:\n    def __init__(self): decide('reset')\n    def run(self,snapshot): return decide('run',snapshot)\ndef create_scheduler(): return Scheduler()\n")
    settings = dict(context['environment'], repeat_runs=1, seeds=[context['seed']])
    result=domain_evaluate({'workspace':context['workspace'],'candidateRoot':str(directory)},dict(context,settings=settings))
    report=Path(context['workspace'],'metrics.json').read_text()
    return {'metrics':result['metric_values'],'evidence':{'report':report}}
