from typing import Dict, List, Optional

from pydantic import BaseModel


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


class CloneProjectsRequest(BaseModel):
    clone_projects: List[CloneProjectSpec]


class ChatSubmitRequest(BaseModel):
    content: str
    active_project_id: Optional[str] = None
    include_project_context: bool = False
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
    model: str = "openrouter/openai/gpt-5.4-mini"
    api_key: Optional[str] = None
    force: bool = False


class LegacyUploadRequest(BaseModel):
    name: str
    files: Dict[str, str]
    path: str = "uploaded"


class LegacyChatRequest(BaseModel):
    message: str
    project_name: Optional[str] = None
