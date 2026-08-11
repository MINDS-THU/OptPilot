"""Stable Studio contracts for local coordination-storage failures."""

from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from optpilot_studio.ui.coordination_store import CoordinationStorageUnavailable
from optpilot_studio.ui.server import _handler_factory


def _mock_ui_state() -> mock.Mock:
    """A permissive UiState double with the real Run-list cache fields.

    Every mutating request invalidates the Run-list response cache in a
    ``finally`` hook, so even failure-path handler tests need a real lock and
    counter rather than auto-created Mock attributes.
    """

    state = mock.Mock()
    state._runs_response_cache_lock = threading.Lock()
    state._runs_response_cache = None
    state._runs_mutation_generation = 0
    return state


_APP_JS = (
    Path(__file__).resolve().parents[2]
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "app.js"
)


def _async_function_source(source: str, name: str, next_name: str) -> str:
    start = source.index(f"async function {name}(")
    end = source.index(f"function {next_name}(", start)
    return source[start:end]


def _function_source(source: str, name: str, next_name: str) -> str:
    start = source.index(f"function {name}(")
    end = source.index(f"function {next_name}(", start)
    return source[start:end]


class StudioStorageUnavailableHttpTest(unittest.TestCase):
    def _request_with_failure(
        self,
        *,
        path: str,
        patched_callable: str,
        error: BaseException,
    ) -> tuple[dict, HTTPStatus]:
        handler = object.__new__(_handler_factory(_mock_ui_state()))
        responses: list[tuple[dict, HTTPStatus]] = []
        handler.path = path
        handler._read_json_body = lambda: {}  # type: ignore[method-assign]
        handler._send_json = (  # type: ignore[method-assign]
            lambda payload, status=HTTPStatus.OK: responses.append(
                (payload, status)
            )
        )

        with mock.patch(
            patched_callable,
            side_effect=error,
        ):
            handler.do_POST()

        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_study_storage_failures_return_stable_service_unavailable_payload(
        self,
    ) -> None:
        cases = (
            ("/api/studies/draft", "optpilot_studio.ui.server._draft_study"),
            (
                "/api/studies/launch",
                "optpilot_studio.ui.server._submit_study_launch_request",
            ),
        )
        failures = (
            sqlite3.OperationalError(
                "disk I/O error while opening /private/realm.sqlite3"
            ),
            CoordinationStorageUnavailable(
                "coordination database unavailable at /private/realm.sqlite3"
            ),
        )
        for path, patched_callable in cases:
            for failure in failures:
                with self.subTest(path=path, failure=type(failure).__name__):
                    payload, status = self._request_with_failure(
                        path=path,
                        patched_callable=patched_callable,
                        error=failure,
                    )

                    self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
                    self.assertEqual(
                        payload.get("code"), "studio_storage_unavailable"
                    )
                    self.assertEqual(
                        payload.get("error"),
                        "Studio local storage is temporarily unavailable. "
                        "Your Study was not changed; try again.",
                    )
                    public_payload = json.dumps(payload, sort_keys=True).lower()
                    self.assertNotIn("sqlite", public_payload)
                    self.assertNotIn("disk i/o", public_payload)
                    self.assertNotIn("coordination database", public_payload)
                    self.assertNotIn("/private/", public_payload)

    def test_workspace_index_failures_use_the_same_retryable_boundary(self) -> None:
        cases = (
            (
                "GET",
                "/api/workspaces",
                "optpilot_studio.ui.server._list_ui_workspaces",
            ),
            (
                "DELETE",
                "/api/workspaces/workspace-a",
                "optpilot_studio.ui.server._delete_ui_workspace",
            ),
        )
        for method, path, patched_callable in cases:
            with self.subTest(method=method):
                handler = object.__new__(_handler_factory(_mock_ui_state()))
                responses: list[tuple[dict, HTTPStatus]] = []
                handler.path = path
                handler._send_json = (  # type: ignore[method-assign]
                    lambda payload, status=HTTPStatus.OK: responses.append(
                        (payload, status)
                    )
                )
                with mock.patch(
                    patched_callable,
                    side_effect=sqlite3.OperationalError(
                        "disk I/O error at /private/workspace-index.json"
                    ),
                ):
                    getattr(handler, f"do_{method}")()

                self.assertEqual(len(responses), 1)
                payload, status = responses[0]
                self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(payload.get("code"), "studio_storage_unavailable")
                self.assertTrue(payload.get("retryable"))
                self.assertEqual(
                    payload.get("error"),
                    "Studio local storage is temporarily unavailable. Try again.",
                )
                public_payload = json.dumps(payload, sort_keys=True).lower()
                self.assertNotIn("disk i/o", public_payload)
                self.assertNotIn("/private/", public_payload)


class StudioStorageUnavailableStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP_JS.read_text(encoding="utf-8")

    def test_storage_failure_has_specific_title_and_preserves_draft_validation(
        self,
    ) -> None:
        save = _async_function_source(
            self.source,
            "savePlanDraft",
            "reconcileStudyDraftAfterSave",
        )
        storage_start = save.index(
            'const storageUnavailable = result.code === "studio_storage_unavailable";'
        )
        generic_failure_start = save.index("plan.draft = {", storage_start)
        storage_branch = save[storage_start:generic_failure_start]

        self.assertIn("if (storageUnavailable)", storage_branch)
        self.assertIn(
            '"Studio local storage is temporarily unavailable"',
            storage_branch,
        )
        self.assertIn("code: result.code", storage_branch)
        self.assertIn("Your Run setup was not rejected or changed.", storage_branch)
        self.assertIn("return null;", storage_branch)
        self.assertNotIn("plan.draft =", storage_branch)
        self.assertNotIn("plan.validation =", storage_branch)
        self.assertNotIn("plan.status =", storage_branch)
        self.assertNotIn("valid: false", storage_branch)

    def test_storage_guidance_replaces_configuration_correction_copy(self) -> None:
        render = _function_source(
            self.source,
            "renderStudyActionStatus",
            "renderPlanDetail",
        )

        self.assertIn("error.guidance ||", render)
        self.assertIn("Correct the Run setup if needed", render)
        self.assertIn("escapeHtml(error.guidance", render)


if __name__ == "__main__":
    unittest.main()
