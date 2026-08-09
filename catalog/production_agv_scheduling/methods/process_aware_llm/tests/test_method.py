from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import traceback
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


METHOD_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = METHOD_ROOT.parents[3]
for import_root in (REPOSITORY_ROOT / "src", METHOD_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from method import (  # noqa: E402
    ProcessAwareLLMHeuristicMethod,
    _canonical_json_bytes,
    _query_trace_database,
    _read_bounded_http_body,
    _validate_policy_sources,
)


SCHEDULER_SOURCE = """\
class Scheduler:
    def run(self, snapshot):
        return []


def create_scheduler():
    return Scheduler()
"""
ESTIMATOR_SOURCE = "VALUE = 1.0\n"

POLICY_VALIDATION = {
    "entrypoint": {
        "file": "scheduler.py",
        "callable": "create_scheduler",
        "maxArguments": 0,
    },
    "forbiddenImports": [
        "builtins", "evaluator", "factory_sim", "importlib", "os",
        "pathlib", "replay", "simulation_runner", "socket", "subprocess",
        "sys",
    ],
    "forbiddenNames": ["create_controller"],
    "lints": [
        {
            "id": "agv-battery-field",
            "forbiddenConstant": "battery",
            "message": (
                "use 'battery_level' for AGV records; there is no 'battery' "
                "snapshot field."
            ),
        }
    ],
}

TARGET_FILES = ("scheduler.py", "param_estimator.py")


def _validate(sources):
    return _validate_policy_sources(
        sources,
        editable_files=TARGET_FILES,
        policy_validation=POLICY_VALIDATION,
    )


def _http_response(payload: object) -> io.BytesIO:
    encoded = json.dumps(payload).encode("utf-8")
    response = io.BytesIO(encoded)
    response.headers = {"Content-Length": str(len(encoded))}  # type: ignore[attr-defined]
    return response


def _completion(content: object, *, finish_reason: str = "stop") -> dict:
    return {
        "model": "deepseek/deepseek-v4-flash",
        "provider": "test-provider",
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
                "native_finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "cost": 0.0001,
            "completion_tokens_details": {"reasoning_tokens": 4},
        },
    }


class ProcessAwareMethodTest(unittest.TestCase):
    def test_generated_policy_contract_accepts_snapshot_scheduler(self) -> None:
        _validate(
            {
                "scheduler.py": SCHEDULER_SOURCE,
                "param_estimator.py": ESTIMATOR_SOURCE,
            }
        )

    def test_generated_policy_contract_rejects_controller_bindings(self) -> None:
        invalid_sources = (
            SCHEDULER_SOURCE
            + "\ndef create_controller(simulation, settings):\n    return Scheduler()\n",
            SCHEDULER_SOURCE
            + "\ncreate_controller = lambda simulation, settings: Scheduler()\n",
        )
        for scheduler_source in invalid_sources:
            with self.subTest(source=scheduler_source):
                with self.assertRaisesRegex(ValueError, "forbidden identifier .create_controller"):
                    _validate(
                        {
                            "scheduler.py": scheduler_source,
                            "param_estimator.py": ESTIMATOR_SOURCE,
                        }
                    )

    def test_generated_policy_contract_rejects_ambiguous_scheduler_factory(self) -> None:
        invalid_sources = (
            SCHEDULER_SOURCE + "\ncreate_scheduler = None\n",
            SCHEDULER_SOURCE.replace(
                "def create_scheduler():", "def create_scheduler(value):"
            ),
            SCHEDULER_SOURCE.replace(
                "def create_scheduler():", "async def create_scheduler():"
            ),
        )
        for scheduler_source in invalid_sources:
            with self.subTest(source=scheduler_source):
                with self.assertRaisesRegex(
                    ValueError,
                    "create_scheduler",
                ):
                    _validate(
                        {
                            "scheduler.py": scheduler_source,
                            "param_estimator.py": ESTIMATOR_SOURCE,
                        }
                    )

    def test_generated_policy_contract_rejects_runtime_imports(self) -> None:
        for import_line in (
            "import factory_sim\n",
            "from simulation_runner import run_policy_once\n",
            "import os\n",
        ):
            with self.subTest(import_line=import_line):
                with self.assertRaisesRegex(ValueError, "imports forbidden module"):
                    _validate(
                        {
                            "scheduler.py": import_line + SCHEDULER_SOURCE,
                            "param_estimator.py": ESTIMATOR_SOURCE,
                        }
                    )

    def test_generated_policy_contract_names_battery_level_exactly(self) -> None:
        wrong_field = SCHEDULER_SOURCE.replace(
            "return []", "return snapshot['lines']['line1']['agvs']['AGV_1']['battery']"
        )
        with self.assertRaisesRegex(ValueError, "use 'battery_level'"):
            _validate(
                {
                    "scheduler.py": wrong_field,
                    "param_estimator.py": ESTIMATOR_SOURCE,
                }
            )

        correct_field = wrong_field.replace("['battery']", "['battery_level']")
        _validate(
            {
                "scheduler.py": correct_field,
                "param_estimator.py": ESTIMATOR_SOURCE,
            }
        )

    def test_editor_retries_contract_failure_before_returning_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            method = self._method(Path(temporary))
            self.addCleanup(method.close)
            method._elite_sources = {
                "scheduler.py": SCHEDULER_SOURCE,
                "param_estimator.py": ESTIMATOR_SOURCE,
            }
            invalid = {
                "summary": "wrong field",
                "files": [
                    {
                        "path": "scheduler.py",
                        "content": SCHEDULER_SOURCE.replace(
                            "return []", "return snapshot['battery']"
                        ),
                    },
                    {"path": "param_estimator.py", "content": ESTIMATOR_SOURCE},
                ],
            }
            valid = {
                "summary": "corrected field",
                "files": [
                    {"path": "scheduler.py", "content": SCHEDULER_SOURCE},
                    {"path": "param_estimator.py", "content": ESTIMATOR_SOURCE},
                ],
            }
            with patch.object(method, "_chat_json", side_effect=[invalid, valid]) as chat:
                summary, sources = method._run_editor(
                    {"title": "battery-aware"},
                    [{"role": "user", "content": "Return JSON."}],
                )

            self.assertEqual(summary, "corrected field")
            self.assertEqual(sources["scheduler.py"], SCHEDULER_SOURCE)
            self.assertEqual(chat.call_count, 2)
            correction = chat.call_args_list[1].args[0][-1]["content"]
            self.assertIn("battery_level", correction)

    def test_factory_prompt_publishes_exact_battery_level_field(self) -> None:
        prompt_path = (
            METHOD_ROOT.parents[1]
            / "environments"
            / "production_agv_scheduling"
            / "prompts"
            / "factory_description.md"
        )
        prompt = prompt_path.read_text(encoding="utf-8")
        self.assertIn('"battery_level": 87.5', prompt)
        self.assertIn("not `battery`", prompt)

        policy_prompt = (prompt_path.parent / "policy_system.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Do not define `create_controller`", policy_prompt)

    def test_baseline_then_parallel_editor_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            method = self._method(root)
            self.addCleanup(method.close)
            baseline = method.propose(
                4,
                {"runtime_context": {"candidate_staging_dir": str(root / "stage-0")}},
            )
            self.assertEqual(len(baseline), 1)
            self.assertEqual(
                baseline[0]["candidate_id"], "process-aware-test-baseline"
            )
            self.assertEqual(
                {item["path"] for item in baseline[0]["spec"]["files"]},
                {"scheduler.py", "param_estimator.py"},
            )

            # Environment replay is tested in the package integration smoke;
            # this unit test isolates the method lifecycle.
            method._replay_incumbent = lambda observation: None
            method.observe(
                [
                    {
                        "candidate_id": baseline[0]["candidate_id"],
                        "status": "success",
                        "metric_values": {
                            "mean_total_score": 60.0,
                            "worst_seed": 123,
                            "worst_total_score": 58.0,
                        },
                    }
                ]
            )

            editor_barrier = threading.Barrier(2, timeout=5)

            def fake_chat(messages):
                if messages[0]["content"].startswith("You are the manager"):
                    return {
                        "queries": [],
                        "plans": [
                            {
                                "title": "distance aware",
                                "rationale": "empty travel is high",
                                "changes": ["rank nearby vehicles first"],
                            },
                            {
                                "title": "blocking aware",
                                "rationale": "downstream queues are full",
                                "changes": ["penalize blocked destinations"],
                            },
                        ],
                    }
                editor_barrier.wait()
                return {
                    "summary": "valid complete policy",
                    "files": [
                        {"path": "scheduler.py", "content": SCHEDULER_SOURCE},
                        {"path": "param_estimator.py", "content": ESTIMATOR_SOURCE},
                    ],
                }

            method._chat_json = fake_chat
            method._record_llm_exchange(
                {"model": "test-model", "messages": [{"role": "user", "content": "x"}]},
                '{"plans":[]}',
                {"plans": []},
            )
            candidates = method.propose(
                2,
                {"runtime_context": {"candidate_staging_dir": str(root / "stage-1")}},
            )
            self.assertEqual(
                [candidate["candidate_id"] for candidate in candidates],
                [
                    "process-aware-test-iteration-01-plan-01",
                    "process-aware-test-iteration-01-plan-02",
                ],
            )
            self.assertTrue(
                all(candidate["lineage"]["parents"] == [baseline[0]["candidate_id"]] for candidate in candidates)
            )
            for candidate in candidates:
                generator = candidate["generator"]
                self.assertNotIn("recorded_at", json.dumps(generator))
                self.assertRegex(
                    generator["prompt_record_id"], r"^prompt-[0-9a-f]{24}$"
                )
                self.assertNotIn("contentRef", generator["prompt_record"])
                self.assertEqual(
                    {item["path"] for item in candidate["spec"]["files"]},
                    {
                        "scheduler.py",
                        "param_estimator.py",
                        "provenance/llm_exchanges.json",
                    },
                )
                provenance = Path(candidate["spec"]["bundleRef"]) / generator[
                    "provenance_file"
                ]
                self.assertEqual(
                    hashlib.sha256(provenance.read_bytes()).hexdigest(),
                    generator["provenance_sha256"],
                )
                provenance_payload = json.loads(provenance.read_text(encoding="utf-8"))
                self.assertEqual(
                    provenance_payload["exchanges"][0]["response_content"],
                    '{"plans":[]}',
                )

    def test_trace_queries_are_read_only_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "trace.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE events (timestamp REAL, status TEXT)")
                connection.executemany(
                    "INSERT INTO events VALUES (?, ?)",
                    [(float(index), "idle") for index in range(5)],
                )
                connection.commit()
            finally:
                connection.close()

            result = _query_trace_database(
                database_path,
                "SELECT timestamp, status FROM events ORDER BY timestamp",
                max_rows=2,
            )
            self.assertEqual(result["columns"], ["timestamp", "status"])
            self.assertEqual(len(result["rows"]), 2)
            self.assertTrue(result["truncated"])
            with self.assertRaisesRegex(ValueError, "read-only"):
                _query_trace_database(
                    database_path, "DELETE FROM events", max_rows=2
                )

    def test_trace_query_bounds_cells_and_complete_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "trace.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE events (payload TEXT)")
                connection.execute("INSERT INTO events VALUES (?)", ("x" * 10000,))
                connection.commit()
            finally:
                connection.close()
            result = _query_trace_database(
                database_path,
                "SELECT payload FROM events",
                max_rows=10,
                max_cell_bytes=64,
                max_result_bytes=512,
                sqlite_length_limit_bytes=20000,
            )
            self.assertLessEqual(len(result["rows"][0][0].encode("utf-8")), 64)
            self.assertLessEqual(len(_canonical_json_bytes(result)), 512)

    def test_http_body_read_is_bounded(self) -> None:
        response = io.BytesIO(b"x" * 33)
        response.headers = {}  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ValueError, "32-byte limit"):
            _read_bounded_http_body(response, max_bytes=32, label="test response")

    def test_invalid_content_length_cannot_escape_provider_secret(self) -> None:
        api_key = "actual-openrouter-secret"
        malformed_length = f"exceeds {api_key}"
        with tempfile.TemporaryDirectory() as temporary:
            method = self._method(Path(temporary))
            self.addCleanup(method.close)

            success = _http_response(_completion('{"plans": []}'))
            success.headers["Content-Length"] = malformed_length  # type: ignore[index]
            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": api_key}),
                patch("method.urllib.request.urlopen", return_value=success),
            ):
                self.assertEqual(
                    method._chat_json(
                        [{"role": "user", "content": "Return JSON."}]
                    ),
                    {"plans": []},
                )
            self.assertNotIn(api_key, method._encode_provenance().decode("utf-8"))

            unauthorized = urllib.error.HTTPError(
                "https://openrouter.ai/api/v1/chat/completions",
                401,
                "unauthorized",
                {"Content-Length": malformed_length},
                io.BytesIO(b'{"error":{"message":"invalid key"}}'),
            )
            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": api_key}),
                patch("method.urllib.request.urlopen", side_effect=unauthorized),
                self.assertRaises(RuntimeError) as raised,
            ):
                method._chat_json(
                    [{"role": "user", "content": "Return JSON."}]
                )
            self.assertIn("HTTP 401", str(raised.exception))
            self.assertNotIn(api_key, str(raised.exception))

    def test_provider_exceptions_do_not_retain_secrets_in_traceback_chains(self) -> None:
        api_key = "traceback-openrouter-secret"
        with tempfile.TemporaryDirectory() as temporary:
            method = self._method(Path(temporary))
            self.addCleanup(method.close)

            failure_factories = {
                "url-error": lambda: urllib.error.URLError(
                    f"provider transport echoed {api_key}"
                ),
                "http-error": lambda: urllib.error.HTTPError(
                    "https://openrouter.ai/api/v1/chat/completions",
                    401,
                    f"provider reason echoed {api_key}",
                    {},
                    io.BytesIO(
                        json.dumps(
                            {"error": {"message": f"api_key={api_key}"}}
                        ).encode("utf-8")
                    ),
                ),
            }
            for label, factory in failure_factories.items():
                with self.subTest(label=label):
                    with (
                        patch.dict(os.environ, {"OPENROUTER_API_KEY": api_key}),
                        patch(
                            "method.urllib.request.urlopen",
                            side_effect=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                                factory()
                            ),
                        ),
                        self.assertRaises(RuntimeError) as raised,
                    ):
                        method._chat_json(
                            [{"role": "user", "content": "Return JSON."}]
                        )
                    self._assert_exception_chain_excludes(
                        raised.exception, api_key
                    )

            malformed_responses = []
            for _ in range(method.request_retries + 1):
                response = io.BytesIO(
                    f'{{"provider_echo":"{api_key}"'.encode("utf-8")
                )
                response.headers = {}  # type: ignore[attr-defined]
                malformed_responses.append(response)
            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": api_key}),
                patch(
                    "method.urllib.request.urlopen",
                    side_effect=malformed_responses,
                ),
                self.assertRaises(RuntimeError) as raised,
            ):
                method._chat_json(
                    [{"role": "user", "content": "Return JSON."}]
                )
            self._assert_exception_chain_excludes(raised.exception, api_key)

            class LeakyReadResponse(io.BytesIO):
                headers = {}

                def read(self, *_args, **_kwargs):
                    raise RuntimeError(f"provider read echoed {api_key}")

            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": api_key}),
                patch(
                    "method.urllib.request.urlopen",
                    side_effect=lambda *_args, **_kwargs: LeakyReadResponse(),
                ),
                self.assertRaises(RuntimeError) as raised,
            ):
                method._chat_json(
                    [{"role": "user", "content": "Return JSON."}]
                )
            self._assert_exception_chain_excludes(raised.exception, api_key)

    def test_chat_retries_transient_http_and_records_safe_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            method = self._method(Path(temporary))
            self.addCleanup(method.close)
            transient = urllib.error.HTTPError(
                "https://openrouter.ai/api/v1/chat/completions",
                429,
                "rate limited",
                {"Retry-After": "0"},
                io.BytesIO(b'{"error":{"message":"busy"}}'),
            )
            success = _http_response(
                _completion("```json\n{\"plans\": []}\n```")
            )
            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}),
                patch(
                    "method.urllib.request.urlopen",
                    side_effect=[transient, success],
                ) as urlopen,
                patch("method.time.sleep") as sleep,
            ):
                result = method._chat_json(
                    [{"role": "user", "content": "Return JSON."}]
                )

            self.assertEqual(result, {"plans": []})
            self.assertEqual(urlopen.call_count, 2)
            sleep.assert_not_called()
            request = urlopen.call_args_list[-1].args[0]
            request_payload = json.loads(request.data.decode("utf-8"))
            self.assertEqual(
                request_payload["model"], "deepseek/deepseek-v4-flash"
            )
            self.assertEqual(
                request_payload["provider"],
                {"allow_fallbacks": True, "require_parameters": True},
            )
            self.assertNotIn("test-secret", json.dumps(request_payload))
            exchange = next(iter(method._llm_exchanges.values()))
            self.assertEqual(
                exchange["response_metadata"]["model"],
                "deepseek/deepseek-v4-flash",
            )
            self.assertEqual(
                exchange["response_metadata"]["usage"]["reasoning_tokens"], 4
            )
            self.assertNotIn("test-secret", method._encode_provenance().decode("utf-8"))

    def test_editor_does_not_retry_nontransient_http_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            method = self._method(Path(temporary))
            self.addCleanup(method.close)
            unauthorized = urllib.error.HTTPError(
                "https://openrouter.ai/api/v1/chat/completions",
                401,
                "unauthorized",
                {},
                io.BytesIO(b'{"error":{"message":"invalid key"}}'),
            )
            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}),
                patch(
                    "method.urllib.request.urlopen", side_effect=unauthorized
                ) as urlopen,
            ):
                with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                    method._run_editor(
                        {"title": "unreachable plan"},
                        [{"role": "user", "content": "Return JSON."}]
                    )
            urlopen.assert_called_once()

    def test_provider_http_error_redacts_exact_and_pattern_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            method = self._method(Path(temporary))
            self.addCleanup(method.close)
            api_key = "actual-openrouter-secret"
            leaked_password = "provider-password"
            leaked_bearer = "provider-bearer-token"
            error_body = json.dumps(
                {
                    "error": {
                        "message": (
                            f"key={api_key}; password={leaked_password}; "
                            f"Authorization: Bearer {leaked_bearer}"
                        )
                    }
                }
            ).encode("utf-8")
            unauthorized = urllib.error.HTTPError(
                "https://openrouter.ai/api/v1/chat/completions",
                401,
                "unauthorized",
                {},
                io.BytesIO(error_body),
            )
            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": api_key}),
                patch(
                    "method.urllib.request.urlopen", side_effect=unauthorized
                ),
                self.assertRaises(RuntimeError) as raised,
            ):
                method._run_editor(
                    {"title": "unreachable plan"},
                    [{"role": "user", "content": "Return JSON."}],
                )

            diagnostic = str(raised.exception)
            self.assertIn("[REDACTED]", diagnostic)
            for secret in (api_key, leaked_password, leaked_bearer):
                self.assertNotIn(secret, diagnostic)
                self.assertNotIn(secret, method._encode_provenance().decode("utf-8"))

    def test_provider_envelope_and_success_content_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            method = self._method(Path(temporary))
            self.addCleanup(method.close)
            api_key = "envelope-api-secret"
            envelope_password = "envelope-password"
            provider_error = {
                "error": {
                    "code": 401,
                    "message": (
                        f"api_key={api_key}; password={envelope_password}"
                    ),
                }
            }
            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": api_key}),
                patch(
                    "method.urllib.request.urlopen",
                    return_value=_http_response(provider_error),
                ),
                self.assertRaises(RuntimeError) as raised,
            ):
                method._chat_json([{"role": "user", "content": "Return JSON."}])
            diagnostic = str(raised.exception)
            self.assertNotIn(api_key, diagnostic)
            self.assertNotIn(envelope_password, diagnostic)
            self.assertIn("[REDACTED]", diagnostic)

            completion_password = "completion-password"
            completion = _completion(
                json.dumps(
                    {
                        "plans": [],
                        "note": api_key,
                        "password": completion_password,
                    }
                )
            )
            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": api_key}),
                patch(
                    "method.urllib.request.urlopen",
                    return_value=_http_response(completion),
                ),
            ):
                result = method._chat_json(
                    [{"role": "user", "content": "Return JSON."}]
                )
            encoded = method._encode_provenance().decode("utf-8")
            self.assertEqual(result["note"], "[REDACTED]")
            self.assertEqual(result["password"], "[REDACTED]")
            self.assertNotIn(api_key, encoded)
            self.assertNotIn(completion_password, encoded)

            key_password = "provider-key-password"
            structured_completion = _completion(
                [
                    {
                        "type": "text",
                        "text": '{"plans": []}',
                        api_key: "echoed exact key",
                        f"password={key_password}": "echoed credential key",
                        "[REDACTED]": "pre-existing redacted key",
                    }
                ]
            )
            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": api_key}),
                patch(
                    "method.urllib.request.urlopen",
                    return_value=_http_response(structured_completion),
                ),
            ):
                self.assertEqual(
                    method._chat_json(
                        [{"role": "user", "content": "Return JSON."}]
                    ),
                    {"plans": []},
                )
            encoded = method._encode_provenance().decode("utf-8")
            self.assertNotIn(api_key, encoded)
            self.assertNotIn(key_password, encoded)
            self.assertIn('"[REDACTED]#2"', encoded)

    def test_success_redaction_preserves_ordinary_token_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            method = self._method(Path(temporary))
            self.addCleanup(method.close)
            api_key = "source-redaction-api-secret"
            source = (
                "class Scheduler:\n"
                "    def run(self, snapshot):\n"
                "        token = snapshot.get(\"token\")\n"
                "        return [] if token is None else []\n"
            )
            completion = _completion(
                json.dumps(
                    {
                        "summary": "Preserve an ordinary token variable.",
                        "files": [
                            {"path": "scheduler.py", "content": source},
                            {
                                "path": "param_estimator.py",
                                "content": ESTIMATOR_SOURCE,
                            },
                        ],
                        "api_key": api_key,
                    }
                )
            )
            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": api_key}),
                patch(
                    "method.urllib.request.urlopen",
                    return_value=_http_response(completion),
                ),
            ):
                result = method._chat_json(
                    [{"role": "user", "content": "Return JSON."}]
                )
            self.assertEqual(result["files"][0]["content"], source)
            self.assertEqual(result["api_key"], "[REDACTED]")
            self.assertNotIn(api_key, method._encode_provenance().decode("utf-8"))

    def test_chat_retries_incomplete_and_provider_error_envelopes(self) -> None:
        invalid_envelopes = [
            {
                "error": {
                    "code": 503,
                    "message": "provider busy",
                    "metadata": {"error_type": "provider_overloaded"},
                }
            },
            _completion('{"plans":', finish_reason="length"),
            {"choices": []},
            _completion(""),
            _completion("not JSON"),
            {
                "choices": [
                    {
                        "error": {
                            "code": 502,
                            "message": "provider disconnected",
                            "metadata": {"error_type": "provider_unavailable"},
                        },
                        "message": {"content": ""},
                        "finish_reason": "error",
                    }
                ]
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            method = self._method(Path(temporary))
            self.addCleanup(method.close)
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}):
                for invalid in invalid_envelopes:
                    with self.subTest(envelope=invalid):
                        with (
                            patch(
                                "method.urllib.request.urlopen",
                                side_effect=[
                                    _http_response(invalid),
                                    _http_response(_completion('{"plans": []}')),
                                ],
                            ) as urlopen,
                            patch("method.time.sleep"),
                        ):
                            self.assertEqual(
                                method._chat_json(
                                    [{"role": "user", "content": "Return JSON."}]
                                ),
                                {"plans": []},
                            )
                        self.assertEqual(urlopen.call_count, 2)

    def test_provider_routing_preferences_are_openrouter_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            method = self._method(Path(temporary))
            self.addCleanup(method.close)
            method.config["provider"] = "compatible-service"
            with (
                patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-secret"}),
                patch(
                    "method.urllib.request.urlopen",
                    return_value=_http_response(_completion('{"plans": []}')),
                ) as urlopen,
            ):
                method._chat_json(
                    [{"role": "user", "content": "Return JSON."}]
                )
            request = urlopen.call_args.args[0]
            request_payload = json.loads(request.data.decode("utf-8"))
            self.assertNotIn("provider", request_payload)

    def test_manager_corrects_json_valid_but_missing_plans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            method = self._method(Path(temporary))
            self.addCleanup(method.close)
            valid = {
                "queries": [],
                "plans": [
                    {
                        "title": "distance",
                        "rationale": "empty travel is high",
                        "changes": ["rank nearer AGVs"],
                    },
                    {
                        "title": "blocking",
                        "rationale": "queues are congested",
                        "changes": ["penalize blocked destinations"],
                    },
                ],
            }
            with patch.object(
                method,
                "_chat_json",
                side_effect=[{"queries": [], "plans": []}, valid],
            ) as chat:
                result = method._run_manager_query_loop(
                    [{"role": "system", "content": "manager"}], 2
                )
            self.assertEqual(result, valid)
            self.assertEqual(chat.call_count, 2)
            self.assertIn(
                "exactly 2 distinct plans",
                chat.call_args_list[-1].args[0][-1]["content"],
            )

    def test_durable_cache_restages_baseline_after_method_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "method-state"
            first = self._method(root)
            first_result = first.propose(
                1,
                {
                    "runtime_context": {
                        "candidate_staging_dir": str(root / "stage-first"),
                        "method_state_dir": str(state_dir),
                    }
                },
            )
            first.close()

            restarted = self._method(root)
            self.addCleanup(restarted.close)
            second_result = restarted.propose(
                1,
                {
                    "runtime_context": {
                        "candidate_staging_dir": str(root / "stage-second"),
                        "method_state_dir": str(state_dir),
                    }
                },
            )
            self.assertEqual(
                first_result[0]["candidate_id"], second_result[0]["candidate_id"]
            )
            self.assertEqual(
                first_result[0]["generator"], second_result[0]["generator"]
            )
            self.assertNotEqual(
                first_result[0]["spec"]["bundleRef"],
                second_result[0]["spec"]["bundleRef"],
            )

    def test_replay_executes_candidate_only_in_secret_scrubbed_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment_root = root / "environment"
            candidate_root = root / "candidate"
            environment_root.mkdir()
            candidate_root.mkdir()
            (candidate_root / "scheduler.py").write_text(
                "import os\n"
                "PID = os.getpid()\n"
                "SECRET_PRESENT = 'OPENROUTER_API_KEY' in os.environ\n",
                encoding="utf-8",
            )
            (environment_root / "evaluator.py").write_text(
                "import importlib.util\n"
                "import sqlite3\n"
                "def replay_candidate(*, candidate_dir, settings, seed, database_path):\n"
                "    spec = importlib.util.spec_from_file_location('isolated_candidate', candidate_dir / 'scheduler.py')\n"
                "    module = importlib.util.module_from_spec(spec)\n"
                "    spec.loader.exec_module(module)\n"
                "    connection = sqlite3.connect(database_path)\n"
                "    connection.execute('CREATE TABLE events (seed INTEGER)')\n"
                "    connection.execute('INSERT INTO events VALUES (?)', (seed,))\n"
                "    connection.commit()\n"
                "    connection.close()\n"
                "    return {'total_score': settings['score'], 'candidate_pid': module.PID, 'secret_present': module.SECRET_PRESENT}\n",
                encoding="utf-8",
            )
            method = self._method(root)
            self.addCleanup(method.close)
            sys.path.insert(0, str(environment_root))
            previous_secret = os.environ.get("OPENROUTER_API_KEY")
            os.environ["OPENROUTER_API_KEY"] = "must-not-cross-replay-boundary"
            try:
                result = method._run_replay_subprocess(
                    candidate_dir=candidate_root,
                    settings={"score": 71.5},
                    seed=123,
                    database_path=root / "isolated.db",
                )
            finally:
                sys.path.remove(str(environment_root))
                if previous_secret is None:
                    os.environ.pop("OPENROUTER_API_KEY", None)
                else:
                    os.environ["OPENROUTER_API_KEY"] = previous_secret
            self.assertEqual(result["total_score"], 71.5)
            self.assertNotEqual(result["candidate_pid"], os.getpid())
            self.assertFalse(result["secret_present"])

    @staticmethod
    def _method(root: Path) -> ProcessAwareLLMHeuristicMethod:
        (root / "scheduler.py").write_text(SCHEDULER_SOURCE, encoding="utf-8")
        (root / "param_estimator.py").write_text(ESTIMATOR_SOURCE, encoding="utf-8")
        (root / "instructions.md").write_text("Snapshot contract.", encoding="utf-8")
        (root / "replay_settings.json").write_text(
            json.dumps({"simulationMinutes": 10}), encoding="utf-8"
        )
        references = [
            {
                "name": path,
                "type": "candidate_template",
                "path": str(root / path),
            }
            for path in ("scheduler.py", "param_estimator.py")
        ]
        references.append(
            {
                "name": "replay_settings",
                "type": "json",
                "path": str(root / "replay_settings.json"),
            }
        )
        study = SimpleNamespace(
            candidate={
                "context": {
                    "files": {
                        "editable": [
                            {"path": "scheduler.py"},
                            {"path": "param_estimator.py"},
                        ],
                        "allow": [
                            "scheduler.py",
                            "param_estimator.py",
                            "provenance/**",
                        ],
                    },
                    "methodContext": {
                        "instructions": [str(root / "instructions.md")],
                        "references": references,
                    },
                    "description": "a test discrete-event system",
                    "policyValidation": POLICY_VALIDATION,
                    "capabilities": [
                        {
                            "id": "exact_seed_replay",
                            "callable": "evaluator:replay_candidate",
                        }
                    ],
                }
            },
            objective={
                "primaryMetric": {
                    "name": "mean_total_score",
                    "direction": "maximize",
                }
            },
        )
        definition = {
            "id": "process-aware-test",
            "config": {
                "candidatesPerIteration": 2,
                "maxIterations": 2,
                "targetScore": 99,
                "requestRetries": 2,
                "retryBaseSeconds": 0,
                "retryMaxSeconds": 0,
            },
        }
        return ProcessAwareLLMHeuristicMethod(definition, study, rng=None)

    def _assert_exception_chain_excludes(
        self, error: BaseException, secret: str
    ) -> None:
        formatted = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        self.assertNotIn(secret, formatted)
        pending = [error]
        seen = set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            self.assertNotIn(secret, str(current))
            self.assertNotIn(secret, repr(current))
            if current.__cause__ is not None:
                pending.append(current.__cause__)
            if current.__context__ is not None:
                pending.append(current.__context__)


if __name__ == "__main__":
    unittest.main()
