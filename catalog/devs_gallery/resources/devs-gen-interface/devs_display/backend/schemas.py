from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from devs_settings import visualizer_model_id


class AuthLoginRequest(BaseModel):
    password: str


class CloneProjectSpec(BaseModel):
    source_session_id: str
    source_project_id: str
    source_version: Optional[int] = None
    display_name: Optional[str] = None


class CreateSessionRequest(BaseModel):
    title: Optional[str] = None
    clone_projects: List[CloneProjectSpec] = []


class UpdateSessionRequest(BaseModel):
    title: str


class UploadProjectRequest(BaseModel):
    display_name: str
    files: Dict[str, str]


class SimulationRunRequest(BaseModel):
    arguments: Dict[str, Any] = Field(default_factory=dict)


class CloneProjectsRequest(BaseModel):
    clone_projects: List[CloneProjectSpec]


class ChatSubmitRequest(BaseModel):
    content: str
    active_project_id: Optional[str] = None
    include_project_context: bool = False
    idempotency_key: Optional[str] = None
    generation_mode: Literal["automatic", "guided"] = "automatic"


class InteractionResolveRequest(BaseModel):
    action: Literal[
        "confirm",
        "revise",
        "continue_automatically",
        "cancel",
    ]
    artifact_digest: Optional[str] = None
    answers: Dict[str, Any] = Field(default_factory=dict)
    feedback: Optional[str] = None
    edited_intent: Optional[Dict[str, Any]] = None
    idempotency_key: Optional[str] = None


class CancelRequest(BaseModel):
    force: bool = False
    withdraw_user_message: bool = True


class ParseModelRequest(BaseModel):
    class_name: str
    code_content: str
    provider: str = "openai"
    model: str
    api_key: Optional[str] = None


class GraphParseRequest(BaseModel):
    provider: str = "openai"
    model: str = visualizer_model_id()
    api_key: Optional[str] = None
    force: bool = False


class LegacyUploadRequest(BaseModel):
    name: str
    files: Dict[str, str]
    path: str = "uploaded"


class LegacyChatRequest(BaseModel):
    message: str
    project_name: Optional[str] = None
