import base64
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from .participant_identity import (
    COOKIE_IDENTITY_MODE,
    LAUNCH_IDENTITY_MODE,
    PARTICIPANT_COOKIE_NAME,
    ParticipantIdentityStore,
    launch_participant_id,
    participant_identity_mode,
)

from .schemas import (
    AuthLoginRequest,
    CancelRequest,
    ChatSubmitRequest,
    CloneProjectsRequest,
    CreateSessionRequest,
    GraphParseRequest,
    InteractionResolveRequest,
    LegacyChatRequest,
    LegacyUploadRequest,
    ParseModelRequest,
    SimulationRunRequest,
    EvaluationSubmitRequest,
    UpdateSessionRequest,
    TraceEventRequest,
    UploadProjectRequest,
)


AUTH_PASSWORD_ENV_NAMES = ("DEVS_DISPLAY_PASSWORD",)
AUTH_TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_ARCHIVE_UPLOAD_BYTES = 128 * 1024 * 1024


def _auth_password() -> str:
    for env_name in AUTH_PASSWORD_ENV_NAMES:
        value = os.getenv(env_name, "")
        if value:
            return value
    return ""


def _auth_required() -> bool:
    return bool(_auth_password())


def _auth_secret(password: str) -> bytes:
    explicit_secret = os.getenv("DEVS_DISPLAY_AUTH_SECRET", "")
    secret = explicit_secret or hashlib.sha256(password.encode("utf-8")).hexdigest()
    return secret.encode("utf-8")


def _token_ttl_seconds() -> int:
    raw = os.getenv("DEVS_DISPLAY_AUTH_TOKEN_TTL_SECONDS", "")
    if not raw:
        return AUTH_TOKEN_TTL_SECONDS
    try:
        return max(60, int(raw))
    except ValueError:
        return AUTH_TOKEN_TTL_SECONDS


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64_decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode((raw + padding).encode("ascii"))


def _sign_token_payload(payload_b64: str, password: str) -> str:
    signature = hmac.new(_auth_secret(password), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64_encode(signature)


def _issue_auth_token(password: str) -> str:
    payload = {
        "exp": int(time.time()) + _token_ttl_seconds(),
        "iat": int(time.time()),
    }
    payload_b64 = _b64_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{payload_b64}.{_sign_token_payload(payload_b64, password)}"


def _verify_auth_token(token: str, password: str) -> bool:
    try:
        payload_b64, signature = token.split(".", 1)
        expected = _sign_token_payload(payload_b64, password)
        if not hmac.compare_digest(signature, expected):
            return False
        payload = json.loads(_b64_decode(payload_b64).decode("utf-8"))
        return int(payload.get("exp", 0)) >= int(time.time())
    except Exception:
        return False


def _extract_bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    prefix = "Bearer "
    if authorization.startswith(prefix):
        return authorization[len(prefix) :].strip()
    return ""


def _cors_configuration() -> tuple[list[str], Optional[str]]:
    configured_origins = [
        value.strip()
        for value in os.getenv("DEVS_DISPLAY_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    ]
    if configured_origins:
        return configured_origins, None
    return [], os.getenv(
        "DEVS_DISPLAY_ALLOWED_ORIGIN_REGEX",
        r"^https?://[^/]+$",
    )


def _secure_participant_cookie(request: Request) -> bool:
    configured = os.getenv("DEVS_DISPLAY_PARTICIPANT_COOKIE_SECURE", "").lower()
    if configured in {"1", "true", "yes", "on"}:
        return True
    if configured in {"0", "false", "no", "off"}:
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return (forwarded_proto.split(",", 1)[0].strip() or request.url.scheme) == "https"


_SESSION_PATH_RE = re.compile(r"^/sessions/([^/]+)(?:/|$)")


def create_app(service) -> FastAPI:
    app = FastAPI(title="xDEVS Agent API")
    allowed_origins, allowed_origin_regex = _cors_configuration()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_origin_regex=allowed_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    identity_mode = participant_identity_mode()
    identity_root = Path(service.registry_path).parent
    participant_store = (
        ParticipantIdentityStore(identity_root)
        if identity_mode == COOKIE_IDENTITY_MODE
        else None
    )
    launch_owner_id = (
        launch_participant_id(identity_root)
        if identity_mode == LAUNCH_IDENTITY_MODE
        else None
    )

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        if request.method == "OPTIONS" or request.url.path in {"/auth/status", "/auth/login"}:
            return await call_next(request)
        password = _auth_password()
        if not password:
            return await call_next(request)
        token = _extract_bearer_token(request)
        if token and _verify_auth_token(token, password):
            return await call_next(request)
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})

    @app.middleware("http")
    async def bind_participant_identity(request: Request, call_next):
        identity = None
        if participant_store is not None:
            identity = participant_store.resolve(
                request.cookies.get(PARTICIPANT_COOKIE_NAME)
            )
            participant_id = identity.participant_id
        else:
            participant_id = launch_owner_id
        request.state.participant_id = participant_id
        session_match = _SESSION_PATH_RE.match(request.url.path)
        if session_match and not service.session_belongs_to(
            session_match.group(1), participant_id
        ):
            response = JSONResponse(
                status_code=404,
                content={"detail": "Session not found"},
            )
        else:
            response = await call_next(request)
        if identity is not None and identity.set_cookie:
            response.set_cookie(
                key=PARTICIPANT_COOKIE_NAME,
                value=identity.token,
                max_age=participant_store.ttl_seconds,
                path="/",
                secure=_secure_participant_cookie(request),
                httponly=True,
                samesite="lax",
            )
        return response

    @app.get("/auth/status")
    def auth_status_route():
        return {"auth_required": _auth_required()}

    @app.post("/auth/login")
    def auth_login_route(request: AuthLoginRequest):
        password = _auth_password()
        if not password:
            return {"token": "", "auth_required": False, "expires_in": None}
        if not hmac.compare_digest(request.password, password):
            raise HTTPException(status_code=401, detail="Invalid password")
        return {
            "token": _issue_auth_token(password),
            "auth_required": True,
            "expires_in": _token_ttl_seconds(),
        }

    @app.get("/sessions")
    def list_sessions_route(
        http_request: Request,
        limit: int = 20,
        offset: int = 0,
    ):
        return {
            "sessions": service.list_sessions(
                limit=limit,
                offset=offset,
                client_id=http_request.state.participant_id,
            )
        }

    @app.get("/config/frontend")
    def frontend_config_route():
        return service.get_frontend_config()

    @app.get("/storage/status")
    def storage_status_route(http_request: Request):
        return {
            **service.storage_status(),
            **service.participant_storage_status(
                http_request.state.participant_id
            ),
        }

    @app.post("/visualizer/parse-model")
    def parse_model_route(request: ParseModelRequest):
        try:
            return {
                "parsed": service.parse_model_for_visualizer(
                    request.class_name,
                    request.code_content,
                    request.provider,
                    request.model,
                    request.api_key,
                )
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/sessions")
    def create_session_route(http_request: Request, request: CreateSessionRequest):
        try:
            participant_id = http_request.state.participant_id
            for clone in request.clone_projects:
                if not service.session_belongs_to(
                    clone.source_session_id, participant_id
                ):
                    raise HTTPException(status_code=404, detail="Source session not found")
            session, projects = service.create_session(
                request.title,
                request.clone_projects,
                participant_id,
            )
            return {"session": session, "projects": projects}
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/sessions/{session_id}")
    def get_session_route(session_id: str):
        try:
            return {"session": service.get_session(session_id)}
        except KeyError:
            raise HTTPException(status_code=404, detail="Session not found")

    @app.patch("/sessions/{session_id}")
    def update_session_route(session_id: str, request: UpdateSessionRequest):
        try:
            return {"session": service.update_session(session_id, request.title)}
        except KeyError:
            raise HTTPException(status_code=404, detail="Session not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.delete("/sessions/{session_id}")
    def delete_session_route(session_id: str):
        try:
            return service.delete_session(session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Session not found")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/sessions/{session_id}/projects")
    def list_projects_route(session_id: str):
        try:
            return {"projects": service.list_projects(session_id)}
        except KeyError:
            raise HTTPException(status_code=404, detail="Session not found")

    @app.post("/sessions/{session_id}/projects")
    def upload_project_route(session_id: str, request: UploadProjectRequest):
        try:
            return {"project": service.upload_project(session_id, request.display_name, request.files)}
        except KeyError:
            raise HTTPException(status_code=404, detail="Session not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/sessions/{session_id}/projects:upload-archive")
    async def upload_project_archive_route(
        session_id: str,
        http_request: Request,
        display_name: str = "Uploaded simulation",
    ):
        archive = bytearray()
        async for chunk in http_request.stream():
            archive.extend(chunk)
            if len(archive) > MAX_ARCHIVE_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="ZIP archive is too large.")
        try:
            return {
                "project": service.upload_project_archive(
                    session_id,
                    display_name,
                    bytes(archive),
                )
            }
        except KeyError:
            raise HTTPException(status_code=404, detail="Session not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/sessions/{session_id}/projects:clone")
    def clone_projects_route(
        session_id: str,
        http_request: Request,
        request: CloneProjectsRequest,
    ):
        try:
            participant_id = http_request.state.participant_id
            for clone in request.clone_projects:
                if not service.session_belongs_to(
                    clone.source_session_id, participant_id
                ):
                    raise HTTPException(status_code=404, detail="Source session not found")
            return {"projects": service.clone_projects(session_id, request.clone_projects)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Not found: {exc}")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/sessions/{session_id}/projects/{project_id}")
    def get_project_route(session_id: str, project_id: str):
        try:
            return {"project": service._project_by_id(session_id, project_id)}
        except KeyError:
            raise HTTPException(status_code=404, detail="Project not found")

    @app.get("/sessions/{session_id}/projects/{project_id}/files")
    def get_project_files_route(session_id: str, project_id: str):
        try:
            return service.get_project_files(session_id, project_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Project not found")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Project files not found")

    @app.get("/sessions/{session_id}/projects/{project_id}/archive")
    def download_project_archive_route(session_id: str, project_id: str):
        try:
            content, filename = service.download_project_archive(
                session_id, project_id
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Project not found")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Project files not found")
        except OverflowError as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        fallback_name = "".join(
            character
            if character.isascii() and (character.isalnum() or character in "._-")
            else "_"
            for character in filename
        ) or "simulation-project.zip"
        encoded_name = quote(filename, safe="")
        return Response(
            content=content,
            media_type="application/zip",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    f'attachment; filename="{fallback_name}"; '
                    f"filename*=UTF-8''{encoded_name}"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/sessions/{session_id}/projects/{project_id}/simulation")
    def get_project_simulation_route(session_id: str, project_id: str):
        try:
            return service.get_project_simulation(session_id, project_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Simulation not found")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/sessions/{session_id}/projects/{project_id}/simulation-runs")
    def start_project_simulation_route(
        session_id: str,
        project_id: str,
        request: SimulationRunRequest,
    ):
        try:
            return {
                "execution": service.start_simulation_run(
                    session_id,
                    project_id,
                    arguments=request.arguments,
                )
            }
        except KeyError:
            raise HTTPException(status_code=404, detail="Simulation not found")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/sessions/{session_id}/projects/{project_id}/simulation:validate")
    def validate_project_simulation_route(
        session_id: str,
        project_id: str,
        request: SimulationRunRequest,
    ):
        try:
            return {
                "execution": service.start_simulation_validation(
                    session_id,
                    project_id,
                    arguments=request.arguments,
                )
            }
        except KeyError:
            raise HTTPException(status_code=404, detail="Simulation not found")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get(
        "/sessions/{session_id}/projects/{project_id}/simulation-runs/{execution_id}"
    )
    def get_project_simulation_run_route(
        session_id: str, project_id: str, execution_id: str
    ):
        try:
            return {
                "execution": service.get_simulation_run(
                    session_id, project_id, execution_id
                )
            }
        except KeyError:
            raise HTTPException(status_code=404, detail="Simulation run not found")

    @app.get(
        "/sessions/{session_id}/projects/{project_id}/simulation-runs/"
        "{execution_id}/result-files/{file_path:path}"
    )
    def get_project_simulation_result_file_route(
        session_id: str,
        project_id: str,
        execution_id: str,
        file_path: str,
        download: bool = False,
    ):
        try:
            result = service.get_simulation_result_file(
                session_id,
                project_id,
                execution_id,
                file_path,
                download=download,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Simulation result not found")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Simulation result is unavailable")
        except OverflowError as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        except TypeError as exc:
            raise HTTPException(status_code=415, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        if not download:
            return JSONResponse(
                content=result,
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        filename = file_path.rsplit("/", 1)[-1]
        fallback_name = "".join(
            character
            if character.isascii() and (character.isalnum() or character in "._-")
            else "_"
            for character in filename
        ) or "simulation-result"
        encoded_name = quote(filename, safe="")
        return Response(
            content=result["content"],
            media_type=result["media_type"],
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    f'attachment; filename="{fallback_name}"; '
                    f"filename*=UTF-8''{encoded_name}"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post(
        "/sessions/{session_id}/projects/{project_id}/simulation-runs/{execution_id}/stop"
    )
    def stop_project_simulation_run_route(
        session_id: str, project_id: str, execution_id: str
    ):
        try:
            return {
                "execution": service.stop_simulation_run(
                    session_id, project_id, execution_id
                )
            }
        except KeyError:
            raise HTTPException(status_code=404, detail="Simulation run not found")

    @app.get("/sessions/{session_id}/projects/{project_id}/graph")
    def get_project_graph_route(session_id: str, project_id: str, start_if_missing: bool = True):
        try:
            return service.get_project_graph(session_id, project_id, start_if_missing=start_if_missing)
        except KeyError:
            raise HTTPException(status_code=404, detail="Session or project not found")

    @app.post("/sessions/{session_id}/projects/{project_id}/graph:parse")
    def parse_project_graph_route(session_id: str, project_id: str, request: GraphParseRequest):
        try:
            return service.start_project_graph_parse(
                session_id,
                project_id,
                provider=request.provider,
                model=request.model,
                api_key=request.api_key,
                force=request.force,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Session or project not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/sessions/{session_id}/messages")
    def get_messages_route(session_id: str, limit: int = 5, before: Optional[str] = None, order: str = "desc"):
        try:
            return service.get_messages(session_id, limit=limit, before=before, order=order)
        except KeyError:
            raise HTTPException(status_code=404, detail="Session not found")

    @app.post("/sessions/{session_id}/chat")
    def submit_chat_route(session_id: str, request: ChatSubmitRequest):
        try:
            chat_request, user_message = service.submit_chat(
                session_id,
                request.content,
                request.active_project_id,
                request.include_project_context,
                request.idempotency_key,
                request.generation_mode,
                request.auto_check,
            )
            return {"request": chat_request, "user_message": user_message}
        except KeyError:
            raise HTTPException(status_code=404, detail="Session or project not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/sessions/{session_id}/requests/{request_id}")
    def get_request_route(session_id: str, request_id: str):
        try:
            return {"request": service.get_request(session_id, request_id)}
        except KeyError:
            raise HTTPException(status_code=404, detail="Request not found")

    @app.post(
        "/sessions/{session_id}/requests/{request_id}/interactions/"
        "{interaction_id}:resolve"
    )
    def resolve_request_interaction_route(
        session_id: str,
        request_id: str,
        interaction_id: str,
        request: InteractionResolveRequest,
    ):
        try:
            chat_request, interaction = service.resolve_interaction(
                session_id=session_id,
                request_id=request_id,
                interaction_id=interaction_id,
                action=request.action,
                artifact_digest=request.artifact_digest,
                answers=request.answers,
                feedback=request.feedback,
                edited_intent=request.edited_intent,
                idempotency_key=request.idempotency_key,
            )
            return {"request": chat_request, "interaction": interaction}
        except KeyError:
            raise HTTPException(status_code=404, detail="Request or interaction not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get(
        "/sessions/{session_id}/requests/{request_id}/artifacts/{artifact_id}"
    )
    def get_request_artifact_route(
        session_id: str,
        request_id: str,
        artifact_id: str,
    ):
        try:
            return {
                "artifact": service.get_request_artifact(
                    session_id, request_id, artifact_id
                )
            }
        except KeyError:
            raise HTTPException(status_code=404, detail="Request artifact not found")

    @app.get("/sessions/{session_id}/events")
    def get_events_route(session_id: str, after: int = 0, request_id: Optional[str] = None, limit: int = 100):
        try:
            return service.get_events(session_id, after=after, request_id=request_id, limit=limit)
        except KeyError:
            raise HTTPException(status_code=404, detail="Session not found")

    @app.get("/sessions/{session_id}/trace")
    def get_trace_route(session_id: str, limit: int = 200):
        try:
            return service.list_trace(session_id, limit=limit)
        except KeyError:
            raise HTTPException(status_code=404, detail="Session not found")

    @app.post("/sessions/{session_id}/trace")
    def record_trace_route(session_id: str, request: TraceEventRequest):
        try:
            return {
                "trace": service.record_trace(
                    session_id,
                    request.type,
                    actor=request.actor,
                    request_id=request.request_id,
                    project_id=request.project_id,
                    payload=request.payload,
                )
            }
        except KeyError:
            raise HTTPException(status_code=404, detail="Session not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/sessions/{session_id}/evaluations")
    def get_evaluations_route(session_id: str, limit: int = 200):
        try:
            return service.list_evaluations(session_id, limit=limit)
        except KeyError:
            raise HTTPException(status_code=404, detail="Session not found")

    @app.post("/sessions/{session_id}/evaluations")
    def submit_evaluation_route(
        session_id: str,
        request: EvaluationSubmitRequest,
    ):
        try:
            return {
                "evaluation": service.submit_evaluation(
                    session_id,
                    stage=request.stage,
                    request_id=request.request_id,
                    project_id=request.project_id,
                    interaction_id=request.interaction_id,
                    scores=request.scores,
                    answers=request.answers,
                    comments=request.comments,
                    payload=request.payload,
                    idempotency_key=request.idempotency_key,
                )
            }
        except KeyError:
            raise HTTPException(status_code=404, detail="Session not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get(
        "/sessions/{session_id}/requests/{request_id}/activity-files/"
        "{file_path:path}"
    )
    def get_request_activity_file_route(
        session_id: str,
        request_id: str,
        file_path: str,
    ):
        try:
            return service.get_request_activity_file(
                session_id,
                request_id,
                file_path,
            )
        except (KeyError, FileNotFoundError):
            raise HTTPException(status_code=404, detail="Generated file not found")
        except OverflowError as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        except TypeError as exc:
            raise HTTPException(status_code=415, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/sessions/{session_id}/requests/{request_id}/cancel")
    def cancel_request_route(session_id: str, request_id: str, request: CancelRequest):
        if request.force:
            raise HTTPException(status_code=409, detail="Force-stopping running requests is not supported in this MVP")
        try:
            chat_request, user_message = service.cancel_request(
                session_id,
                request_id,
                withdraw_user_message=request.withdraw_user_message,
            )
            return {"request": chat_request, "user_message": user_message}
        except KeyError:
            raise HTTPException(status_code=404, detail="Request not found")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/projects")
    def legacy_list_projects_route():
        raise HTTPException(status_code=410, detail="Use the session-scoped API")

    @app.get("/projects/{project_name}/files")
    def legacy_get_files_route(project_name: str):
        raise HTTPException(status_code=410, detail="Use the session-scoped API")

    @app.post("/projects")
    def legacy_upload_project_route(request: LegacyUploadRequest):
        raise HTTPException(status_code=410, detail="Use the session-scoped API")

    @app.post("/chat")
    def legacy_chat_route(request: LegacyChatRequest):
        raise HTTPException(status_code=410, detail="Use /sessions/{session_id}/chat")

    return app
