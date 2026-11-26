"""Exercise the process boundary with a deterministic local Harbor CLI test double."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from optpilot import experiment
from optpilot.harbor_runner import job_root

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/harbor"
FAKE_CLI = """import json, os, sys, time
from pathlib import Path
directory=Path.cwd()
cfg=json.loads((directory/'study.json').read_text())
job=Path(cfg['jobs_dir'])/cfg['job_name']
job.mkdir(parents=True,exist_ok=True)
with (directory/'starts').open('a') as f: f.write(' '.join(sys.argv[1:3])+'\\n')
(job/'config.json').write_text(json.dumps(cfg))
end=time.monotonic()+8
while not (directory/'release').exists() and time.monotonic()<end: time.sleep(.02)
records=[]
for agent in cfg['agents']:
    for task in cfg['tasks']:
        code=Path(task['path'])/'solution/solve.sh'
        assert os.access(code, os.X_OK)
        assert 'work card' in code.read_text().lower().replace('-', ' ')
        name=agent['name']+'-'+Path(task['path']).name
        path=job/name
        path.mkdir()
        row={'id':name,'task_name':Path(task['path']).name,'task_checksum':'checksum',
             'agent_info':{'name':agent['name'],'version':'0.24'},'config':{'task':task,'agent':agent,'job_id':'external-one'},
             'finished_at':'2026-01-01T12:00:00Z','verifier_result':{'rewards':{'reward':int(agent['name']=='oracle')}}}
        (path/'result.json').write_text(json.dumps(row))
        records.append(row)
(job/'result.json').write_text(json.dumps({'id':'external-one','n_total_trials':len(records),'stats':{},
                                          'finished_at':'2026-01-01T12:00:00Z'}))
"""


@unittest.skipUnless(os.name == "posix", "POSIX process and file-mode integration")
class HarborStudyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.root = self.directory / "project"
        self.store = self.directory / "store"
        shutil.copytree(FIXTURE, self.root)
        self.fake = self.directory / "harbor"
        self.fake.write_text("#!" + sys.executable + "\n" + FAKE_CLI)
        self.fake.chmod(0o755)
        self.study = experiment.review(self.root, self.store, {
            "suiteIds": ["terminology"], "candidateIds": ["oracle", "nop"], "repetitions": 2,
        })
        self.env = {**os.environ, "OPTPILOT_HARBOR_EXECUTABLE": str(self.fake),
                    "PYTHONPATH": str(REPO / "src")}

    def test_codex_transport_configuration_is_frozen_and_rejects_credentials(self):
        import yaml
        path = self.root / "optpilot.experiment.yaml"
        manifest = yaml.safe_load(path.read_text())
        provider = {"name": "Local API", "base_url": "http://host.docker.internal:18765/v1",
                    "wire_api": "responses", "env_key": "OPENAI_API_KEY", "supports_websockets": False, "http_headers": {"Host": "127.0.0.1:18765"}}
        manifest["candidates"][0]["agent"] = {"name": "codex", "model_name": "test-model",
            "kwargs": {"config": {"model_provider": "local", "model_providers": {"local": provider}}}}
        path.write_text(yaml.safe_dump(manifest))
        reviewed = experiment.review(self.root, self.store, {"suiteIds": ["terminology"], "candidateIds": ["oracle"]})
        from optpilot._experiment_store import database, version_spec
        from optpilot.harbor_runner import stage
        with database(self.store) as db:
            frozen = version_spec(db, reviewed["candidateVersionIds"][0])["agent"]
            stage(db, self.directory / "staged", reviewed)
        launched = json.loads((self.directory / "staged/study.json").read_text())["agents"][0]
        self.assertEqual(launched, frozen)
        self.assertFalse(launched["kwargs"]["config"]["model_providers"]["local"]["supports_websockets"])
        provider["api_key"] = "synthetic-secret"
        path.write_text(yaml.safe_dump(manifest))
        with self.assertRaises(ValueError):
            experiment.review(self.root, self.store, {"suiteIds": ["terminology"], "candidateIds": ["oracle"]})

    def start_worker(self, run):
        p = subprocess.Popen([sys.executable, "-m", "optpilot", "experiment", "run", "--store", str(self.store),
                              "--review", self.study["id"], "--run-id", run],
                             env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (p.terminate(), p.wait()) if p.poll() is None else None)
        record = job_root(self.store, run)
        end = time.monotonic() + 5
        while not (record / "starts").exists() and time.monotonic() < end:
            time.sleep(.02)
        self.assertTrue((record / "starts").exists())
        return p, record

    def test_reconnect_after_client_termination_keeps_one_external_job(self):
        run = "one-run"
        (self.root / "help-topic/solution/solve.sh").write_text("edited after review")
        p, record = self.start_worker(run)
        p.terminate()
        p.wait(timeout=3)
        (record / "release").touch()
        with patch.dict(os.environ, {"OPTPILOT_HARBOR_EXECUTABLE": str(self.fake)}):
            completed = experiment.execute(self.store, self.study["id"], run)
        self.assertEqual(completed["state"], "succeeded", completed.get("error"))
        self.assertEqual((record / "starts").read_text().splitlines(), ["run --config"])
        self.assertEqual(completed["external"]["jobId"], "external-one")
        self.assertEqual(completed["external"]["importedTrials"], 4)
        self.assertEqual(completed["external"]["coverage"], "trials")
        self.assertTrue(Path(completed["runnerDirectory"]).is_dir())
        self.assertEqual([a["metrics"]["reward"]["mean"] for a in completed["aggregates"]], [1, 0])
        self.assertEqual(len({t["externalTrialId"] for t in completed["trials"]}), 4)
        self.assertTrue(all("seed" not in t and t["attempts"] == [] for t in completed["trials"]))
        self.assertEqual(set(completed["usedVersionIds"]), set(self.study["usedVersionIds"]))

    def test_refresh_preserves_receipts_and_excludes_failed_agent_rewards(self):
        p, record = self.start_worker("refresh")
        (record / "release").touch()
        p.wait(timeout=5)
        before = experiment.detail(self.store, "refresh")
        path = next((record / "jobs/evaluation").glob("oracle-*/result.json"))
        raw = json.loads(path.read_text())
        raw.update(reward_valid=True, exception_info={"exception_type": "AgentTimeoutError", "exception_message": "timed out"},
                   agent_result={"n_input_tokens": 42, "n_output_tokens": 7, "metadata": {"private": "excluded"}})
        path.write_text(json.dumps(raw))
        receipt = path.read_bytes()
        after = experiment.refresh_results(self.store, "refresh")
        self.assertEqual(after["state"], "failed")
        self.assertEqual(after["finishedAt"], before["finishedAt"])
        failed = next(t for t in after["trials"] if t["externalTrialId"] == raw["id"])
        self.assertEqual(failed["metrics"], {})
        self.assertEqual(failed["evidence"]["reportedRewards"], {"reward": 1})
        self.assertEqual(failed["evidence"]["agentExecution"], {"n_input_tokens": 42, "n_output_tokens": 7})
        self.assertEqual(experiment.graph(self.store)["runs"][0]["summary"]["resources"]["harbor"],
                         {"inputTokens":{"amount":42,"source":"trials","reportedTrials":1},
                          "outputTokens":{"amount":7,"source":"trials","reportedTrials":1}, "totalTrials":4})
        self.assertEqual(experiment.refresh_results(self.store, "refresh"), after)
        self.assertEqual(path.read_bytes(), receipt)
        self.assertEqual(sum(a["validTrials"] for a in after["aggregates"]), 3)

    def test_stop_reaches_external_worker_and_preserves_unstarted_inputs(self):
        p, _ = self.start_worker("cancel")
        experiment.cancel(self.store, "cancel")
        p.wait(timeout=5)
        record = experiment.detail(self.store, "cancel")
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(record["usedVersionIds"], [])
        self.assertTrue(all(t["state"] == "cancelled" for t in record["trials"]))

    def test_unavailable_executable_explains_dependency_without_using_native_runner(self):
        with patch.dict(os.environ, {"OPTPILOT_HARBOR_EXECUTABLE": str(self.directory / "unavailable")}):
            out = experiment.execute(self.store, self.study["id"], "missing")
        self.assertEqual(out["state"], "failed")
        self.assertIn("Install Harbor", out["error"])
        self.assertEqual(out["usedVersionIds"], [])

class HarborAuthenticationTest(unittest.TestCase):
    def test_selected_local_auth_stays_out_of_specs_and_maps_loopback(self):
        from optpilot._harbor_auth import environment
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'.codex').mkdir()
            (root/'.claude').mkdir()
            (root/'.codex/config.toml').write_text('model_provider="local"\n[model_providers.local]\nbase_url="http://127.0.0.1:1234/v1"\nenv_key="TEST_LOCAL_TOKEN"\n')
            (root/'.claude/settings.json').write_text(json.dumps({'env':{'ANTHROPIC_AUTH_TOKEN':'synthetic-claude','ANTHROPIC_BASE_URL':'http://localhost:2345','UNRELATED_SECRET':'not-exported'}}))
            with patch('optpilot._harbor_auth.Path.home',return_value=root), patch.dict(os.environ,{'OPTPILOT_HARBOR_CODEX_LOGIN':'1','OPTPILOT_HARBOR_CLAUDE_SETTINGS':'1','TEST_LOCAL_TOKEN':'synthetic-codex'},clear=True):
                result = environment()
                self.assertEqual(result['OPENAI_API_KEY'],'synthetic-codex')
                self.assertEqual(result['OPENAI_BASE_URL'],'http://host.docker.internal:1234/v1')
                self.assertEqual(result['ANTHROPIC_AUTH_TOKEN'],'synthetic-claude')
                self.assertEqual(result['ANTHROPIC_BASE_URL'],'http://host.docker.internal:2345')
                self.assertNotIn('UNRELATED_SECRET',result)
            with patch('optpilot._harbor_auth.Path.home',side_effect=AssertionError('No opt-in')),patch.dict(os.environ,{},clear=True):
                self.assertEqual(environment(),{})
