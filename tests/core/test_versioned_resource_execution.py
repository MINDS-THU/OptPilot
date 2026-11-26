"""Resource admission, evidence and recovery through the versioned experiment API."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from optpilot import experiment, experiment_manual
from optpilot import experiment_resource_plan as plans
from optpilot import experiment_resources as ledger


class ResourceExecutionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = self.root / 'records'
        env = patch.dict(os.environ, {'OPTPILOT_RESOURCE_STORE': str(self.root / 'registry')})
        env.start()
        self.addCleanup(env.stop)
        self.req = {'capability':'solver', 'unit':'seat', 'mode':'capacity', 'minimum':1}
        self.data = {'tasks':[{'id':'score', 'evaluator':'score.py:score', 'metrics':['value'],
                               'decisionContract':{'format':'parameters'}}],
                     'instances':[{'id':'sample','task':'score'}],
                     'candidates':[{'id':'base','format':'parameters','value':{'x':7},
                                    'resourceRequirements':[self.req]}]}
        (self.root / 'score.py').write_text('def score(c,ctx):\n    return {"value":c["x"]}\n')

    def review(self, store=None, **selection):
        (self.root / 'optpilot.experiment.yaml').write_text(json.dumps(self.data))
        return experiment.review(self.root, store or self.store, dict(instanceIds=['sample'], candidateIds=['base']) | selection)

    def register(self, cap=1):
        return ledger.register({'id':'shared','name':'Shared solver', 'capability':'solver',
                                'unit':'seat','mode':'capacity','capacity':cap,'provider':'managed'})

    def test_missing_has_no_attempt_and_same_run_rechecks_after_registration(self):
        study = self.review()
        first_plan = plans.preview_plan(self.store,study['id'])
        self.assertFalse(first_plan['ready'])
        self.assertEqual(first_plan['planId'], plans.preview_plan(self.store,study['id'])['planId'])
        self.assertEqual(first_plan['placement']['evaluator'],'local-python')
        run = experiment.execute(self.store,study['id'],'r')
        self.assertEqual(run['state'],'waiting')
        self.assertEqual(run['trials'][0]['attempts'],[])
        self.assertEqual(run['usedVersionIds'],[])
        self.register()
        completed = experiment.execute(self.store,study['id'],'r')
        self.assertEqual(completed['state'],'succeeded',completed.get('error'))
        usage = completed['resourceUsage']
        self.assertEqual(usage['initialPlan']['planId'], first_plan['planId'])
        self.assertTrue(usage['initialPlan']['observedAt'])
        self.assertEqual(usage['activities'][0]['initialPlanId'], first_plan['planId'])
        self.assertTrue(first_plan['planId'].startswith('plan-'))
        self.assertEqual([(r['resourceId'],r['state']) for r in usage['allocations']],[('shared','released')])
        self.assertEqual(len(usage['observations']),1)
        summary = experiment.graph(self.store)['runs'][0]['summary']['resources']
        self.assertEqual(summary['items'], [dict(resourceId='shared', name='Shared solver', mode='capacity',
                                                 unit='seat', released=True)])
        self.assertEqual(summary['elapsedSeconds'],usage['observations'][0]['amount'])
        ledger.delete('shared')
        self.assertEqual(experiment.graph(self.store)['runs'][0]['summary']['resources'], summary)
        # Reopening the project against a different registry must not invent a historical zero.
        with patch.dict(os.environ, {'OPTPILOT_RESOURCE_STORE': str(self.root / 'empty-registry')}):
            self.assertEqual(experiment.graph(self.store)['runs'][0]['summary']['resources'],
                             {'items':[], 'unavailable':True})
        self.register()
        preview = plans.preview_plan(self.store,study['id'])
        self.assertNotEqual(first_plan['planId'], preview['planId'])
        self.assertEqual(preview['durationEstimates'][0]['count'],1)
        self.assertEqual(completed,experiment.execute(self.store,study['id'],'r'))

    def test_concurrent_projects_compete_for_single_capacity_and_release_after_cancel(self):
        self.register()
        started = self.root / "started"
        release = self.root / "continue"
        (self.root / 'score.py').write_text(f"""
import time
from pathlib import Path
def score(candidate, ctx):
    assert ctx["resourceBindings"][0]["resourceId"] == "shared"
    Path({str(started)!r}).touch()
    while not Path({str(release)!r}).exists():
        time.sleep(.05)
    return {{"value": candidate["x"]}}
""")
        s1 = self.review()
        other = self.root / 'other'
        s2 = self.review(other)
        with ThreadPoolExecutor() as pool:
            live = pool.submit(experiment.execute,self.store,s1['id'],'same-id')
            for _ in range(80):
                if started.exists(): break
                time.sleep(.05)
            try:
                self.assertTrue(started.exists())
                r2 = experiment.execute(other,s2['id'],'same-id')
                self.assertEqual(r2['state'],'waiting')
                self.assertFalse(r2['trials'][0]['attempts'])
                self.assertEqual(experiment.graph(other)['runs'][0]['summary']['resources'], {'items': []})
                held = experiment.graph(self.store)['runs'][0]['summary']['resources']['items']
                self.assertEqual(held, [dict(resourceId='shared', name='Shared solver', mode='capacity',
                                             unit='seat', held=1)])
                experiment.cancel(self.store,'same-id')
                self.assertEqual(live.result(timeout=5)['state'],'cancelled')
            finally:
                release.touch()
        r2 = experiment.execute(other,s2['id'],'same-id')
        self.assertEqual(r2['state'],'succeeded',r2.get('error'))
        self.assertEqual(ledger.inventory()[0]['available'],1)

    def test_material_coverage_resolution_manual_receipt_and_no_automatic_consumption(self):
        self.data['tasks'][0].pop('evaluator')
        self.data['tasks'][0]['runner']='manual'
        self.data['candidates'][0]['resourceRequirements']=[{'capability':'resin','unit':'g','mode':'consumable','minimum':6,'maximum':9}]
        ledger.register({'id':'b1','name':'Stock','capability':'resin','unit':'g','mode':'consumable','capacity':22})
        study=self.review(repetitions=3)
        plan=plans.preview_plan(self.store,study['id'])
        self.assertEqual((plan['coverage'],plan['canStartNext'],plan['ready']),('partial',True,False))
        self.assertEqual([(x['minimum'],x['maximum'],x['available']) for x in plan['coverageTotals']],[(18,27,22)])
        first=experiment.execute(self.store,study['id'],'manual')
        self.assertEqual(first['state'],'waiting')
        t=experiment_manual.tasks(self.store,'manual')['tasks'][0]
        experiment_manual.claim(self.store,'manual',t['id'],'operator')
        experiment_manual.complete(self.store,'manual',t['id'],{'value':1},'receipt','m-1')
        data=plans.run_resources(self.store,'manual')
        pending=next(r for r in data['allocations'] if r['state']=='pending')
        def resource():
            return experiment.graph(self.store)['runs'][0]['summary']['resources']['items'][0]
        self.assertEqual(resource()['pending'], 9)
        self.assertNotIn('consumed', resource())
        ledger.record_usage(pending['owner'],pending['key'],6,'issued-1',complete=False)
        ledger.record_usage(pending['owner'],pending['key'],6,'issued-1',complete=False)
        self.assertEqual((resource()['pending'], resource()['consumed']), (3, 6))
        ledger.record_usage(pending['owner'],pending['key'],0,'issued-1-final')
        self.assertNotIn('pending', resource())
        self.assertEqual(resource()['consumed'], 6)
        stopped=experiment.cancel(self.store,'manual')
        self.assertEqual(stopped['state'],'cancelled')
        self.assertEqual(ledger.inventory()[0]['available'],16)

    def test_graph_keeps_reusable_capacity_peaks_separate_from_consumption(self):
        self.register()
        self.data['tasks'][0].pop('evaluator')
        self.data['tasks'][0]['runner'] = 'manual'
        study = self.review(repetitions=2)
        run_id = 'repeated-measurement'
        experiment.execute(self.store, study['id'], run_id)
        def summary():
            return experiment.graph(self.store)['runs'][0]['summary']['resources']['items'][0]
        for idx, reading in enumerate((1, 0.5)):
            task = next(t for t in experiment_manual.tasks(self.store,run_id)['tasks'] if t['state'] == 'offered')
            experiment_manual.claim(self.store, run_id, task['id'], 'operator')
            row = next(r for r in plans.run_resources(self.store,run_id)['allocations'] if r['state'] == 'held')
            if idx == 0:
                ledger.record_usage(row['owner'], row['key'], 0, 'zero-peak', complete=False)
                self.assertEqual(summary()['observedPeak'], 0)
            ledger.record_usage(row['owner'], row['key'], reading, f'peak-{idx}')
            experiment_manual.complete(self.store,run_id,task['id'],{'value':2},'sample',f'measured-{idx}')
        self.assertEqual(summary(), dict(resourceId='shared', name='Shared solver', mode='capacity',
                                         unit='seat', released=True, observedPeak=1))

    def test_dynamic_parameter_candidate_and_generator_recheck(self):
        self.data['candidates'][0]['resourceRequirements']=[]
        self.data['generators']=[{'id':'search','entrypoint':'generate.py:generate',
                                  'outputFormat':'candidate','resourceRequirements':[]}]
        (self.root/'generate.py').write_text("def generate(ctx):\n    return "+repr(
            {'format':'parameters','value':{'x':9},'resourceRequirements':[self.req]})+"\n")
        study=self.review(generatorId='search',generationBudget=1)
        waiting=experiment.execute(self.store,study['id'],'dynamic')
        self.assertEqual(waiting['state'],'waiting',waiting.get('error'))
        self.assertEqual(len(waiting['generations']),1)
        self.assertEqual(waiting['completedTrials'],1)
        self.register()
        done=experiment.execute(self.store,study['id'],'dynamic')
        self.assertEqual(done['state'],'succeeded',done.get('error'))
        self.assertEqual([t['metrics']['value'] for t in done['trials']],[7,9])
        self.assertEqual(len(done['generations']),1)

    def test_optional_retry_cost_and_actual_quota_spend(self):
        self.data['candidates'][0]['resourceRequirements'] = [
            {'capability': 'tokens', 'unit': 'token', 'mode': 'quota', 'minimum': 1, 'maximum': 4}
        ]
        ledger.register(dict(id='pool', name='Model quota', capability='tokens', unit='token',
                             mode='quota', capacity=6, windowSeconds=3600))
        budget = [{'capability': 'tokens', 'unit': 'token', 'limit': 6}]
        (self.root/'score.py').write_text("def score(c,ctx):\n    raise RuntimeError('invalid output')\n")
        study = self.review(retries=1, resourceBudgets=budget)
        preview = plans.preview_plan(self.store, study['id'])
        self.assertTrue(preview['canStartNext'])
        self.assertEqual(preview['coverage'], 'partial')
        self.assertTrue(preview['retryCoverageIssues'])
        self.assertEqual(preview['coverageTotals'][0]['maximumWithRetries'], 8)
        run = experiment.execute(self.store, study['id'], 'retry')
        self.assertEqual(run['state'], 'waiting')
        self.assertIn('Reconcile', run['error'])
        allocation = run['resourceUsage']['allocations'][0]
        ledger.record_usage(allocation['owner'], allocation['key'], 3, 'invoice')
        # Even when the quota pool now has room, the frozen run budget is cumulative.
        ledger.register(dict(id='pool', name='Model quota', capability='tokens', unit='token',
                             mode='quota', capacity=10, windowSeconds=3600))
        resumed = experiment.execute(self.store, study['id'], 'retry')
        self.assertIn('budget is 6', resumed['error'])
        self.assertEqual(len(resumed['trials'][0]['attempts']), 1)
        self.assertEqual(experiment.cancel(self.store,'retry')['state'], 'cancelled')

    def test_harbor_whole_job_holds_all_billable_retries_and_releases_unstarted_work(self):
        task_directory = Path(__file__).resolve().parents[1] / 'fixtures/harbor/help-topic'
        shutil.copytree(task_directory, self.root / 'help-topic')
        self.data['tasks'] = [dict(id='score', runner='harbor', harborTask='help-topic',
                                   decisionContract={'format':'agent'}, metrics=['value'])]
        self.data['candidates'] = [
            dict(id='base', format='agent', agent={'name':'oracle'}, resourceRequirements=[
                {'capability':'model','unit':'call','mode':'consumable','minimum':1,'maximum':2}])
        ]
        ledger.register(dict(id='pool', name='Model calls', capability='model',
                             mode='consumable', unit='call', capacity=12))
        study = self.review(repetitions=2, retries=2)
        preview = plans.preview_plan(self.store, study['id'])
        self.assertEqual(preview['placement']['evaluator'], 'harbor-docker')
        self.assertEqual([(r['stage'],r['amount']) for r in preview['items']], [('harbor',12)])
        with patch.dict(os.environ, {'OPTPILOT_HARBOR_EXECUTABLE':str(self.root/'unavailable')}):
            run = experiment.execute(self.store, study['id'], 'job')
        self.assertEqual(run['state'], 'failed')
        self.assertEqual(run['usedVersionIds'], [])
        self.assertEqual(run['resourceUsage']['allocations'][0]['state'], 'released')
        self.assertEqual(run['resourceUsage']['observations'], [])
        self.assertEqual(ledger.inventory()[0]['available'],12)

    def test_instance_derived_quantity_and_frozen_budget(self):
        self.data['tasks'][0]['parameters']={'cores':{'valueType':'int','default':2}}
        self.data['tasks'][0]['resourceRequirements']=[dict(self.req, parameter='cores')]
        self.data['tasks'][0]['resourceRequirements'][0].pop('minimum')
        self.data['candidates'][0]['resourceRequirements']=[]
        self.register(4)
        s=self.review(resourceBudgets=[{'capability':'solver','unit':'seat','limit':1}])
        pl=plans.preview_plan(self.store,s['id'])
        self.assertFalse(pl['ready'])
        self.assertEqual(pl['items'][0]['amount'],2)
        for _ in range(2):
            r=experiment.execute(self.store,s['id'],'budget')
            self.assertEqual(r['state'],'waiting')
            self.assertEqual(r['trials'][0]['attempts'],[])
        self.assertEqual(experiment.cancel(self.store,'budget')['state'],'cancelled')

if __name__ == '__main__':
    unittest.main()
