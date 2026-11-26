from evaluator import evaluate as domain_evaluate

def evaluate(candidate, context):
    result=domain_evaluate(candidate,dict(context,settings=context["environment"]))
    return {"metrics":result.get("metric_values",result)}
