import random

def propose(context):
    rng=random.Random(context['seed']+context['generatedCount'])
    schema=context['tasks'][0]['decisionContract']['schema']
    result={}
    for key,d in schema.items():
        kind=d['valueType']
        if kind=='categorical': value=rng.choice(d['values'])
        elif kind=='bool': value=bool(rng.getrandbits(1))
        elif kind=='int': value=rng.randint(d.get('min',0),d.get('max',100))
        else: value=rng.uniform(d.get('min',0),d.get('max',1))
        result[key]=value
    return result
