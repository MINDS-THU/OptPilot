import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'methods/coopa_solver'))
from coopa_shim import solve_problem
from coopa_solver import bounded_report_json

def propose(context):
    if context['generatedCount']: return []
    observations=context['tasks'][0]['observations']
    report=solve_problem(observations['problem'],model_id=observations['model'],agent_mode='manager',skip_formulation=False,max_refinement_iterations=2,work_dir=context['workspace'])
    value=report.get('predicted')
    return {'objective_value':float(value) if value is not None else 0.,'answer_found':value is not None,'report_json':bounded_report_json(report)}
