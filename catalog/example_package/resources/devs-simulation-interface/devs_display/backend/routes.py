import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .schemas import (
    AuthLoginRequest,
    CancelRequest,
    ChatSubmitRequest,
    CloneProjectsRequest,
    CreateSessionRequest,
    GraphParseRequest,
    LegacyChatRequest,
    LegacyUploadRequest,
    ParseModelRequest,
    UpdateSessionRequest,
    UploadProjectRequest,
)


AUTH_PASSWORD_ENV_NAMES = ("DEVS_DISPLAY_PASSWORD", "HAMLET_DISPLAY_PASSWORD")
AUTH_TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60


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


def create_app(service) -> FastAPI:
    app = FastAPI(title="xDEVS Agent API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
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
    def list_sessions_route(limit: int = 20, offset: int = 0):
        return {"sessions": service.list_sessions(limit=limit, offset=offset)}

    @app.get("/config/frontend")
    def frontend_config_route():
        return service.get_frontend_config()

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
    def create_session_route(request: CreateSessionRequest):
        session, projects = service.create_session(request.title, request.clone_projects)
        return {"session": session, "projects": projects}

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

    @app.post("/sessions/{session_id}/projects:clone")
    def clone_projects_route(session_id: str, request: CloneProjectsRequest):
        try:
            return {"projects": service.clone_projects(session_id, request.clone_projects)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Not found: {exc}")

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
            )
            return {"request": chat_request, "user_message": user_message}
        except KeyError:
            raise HTTPException(status_code=404, detail="Session or project not found")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/sessions/{session_id}/requests/{request_id}")
    def get_request_route(session_id: str, request_id: str):
        try:
            return {"request": service.get_request(session_id, request_id)}
        except KeyError:
            raise HTTPException(status_code=404, detail="Request not found")

    @app.get("/sessions/{session_id}/events")
    def get_events_route(session_id: str, after: int = 0, request_id: Optional[str] = None, limit: int = 100):
        try:
            return service.get_events(session_id, after=after, request_id=request_id, limit=limit)
        except KeyError:
            raise HTTPException(status_code=404, detail="Session not found")

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
        return {"projects": service.scan_projects()}

    @app.get("/projects/{project_name}/files")
    def legacy_get_files_route(project_name: str):
        try:
            return {"files": service.legacy_get_project_files(project_name)}
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Project not found")

    @app.post("/projects")
    def legacy_upload_project_route(request: LegacyUploadRequest):
        try:
            project = service.upload_project(service.default_session_id(), request.name, request.files)
        except KeyError:
            raise HTTPException(status_code=404, detail="No session found")
        return {"status": "success", "project": project}

    @app.post("/chat")
    def legacy_chat_route(request: LegacyChatRequest):
        raise HTTPException(status_code=410, detail="Use /sessions/{session_id}/chat")

    return app
