import {
    ActivityFilePreview,
    BackendMessage,
    ChatRequestInfo,
    EventResponse,
    FileMap,
    FrontendConfig,
    GenerationMode,
    ProjectGraphResponse,
    ProjectInfo,
    SimulationResultPreview,
    SimulationRun,
    SimulationSpec,
    SessionInfo
} from '../types';

const defaultAgentApiUrl = (() => {
    if (typeof window === 'undefined') return 'http://localhost:8000';
    return `${window.location.protocol}//${window.location.hostname}:8000`;
})();

export const AGENT_API_URL = import.meta.env.VITE_AGENT_API_URL || defaultAgentApiUrl;
const AUTH_TOKEN_STORAGE_KEY = 'devs_display_auth_token';

export const getStoredAuthToken = (): string => {
    if (typeof window === 'undefined') return '';
    return window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY) || '';
};

export const clearStoredAuthToken = () => {
    if (typeof window !== 'undefined') {
        window.localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
    }
};

const storeAuthToken = (token: string) => {
    if (typeof window !== 'undefined') {
        if (token) window.localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, token);
        else window.localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
    }
};

export const isUnauthorizedError = (error: unknown): boolean => {
    return Boolean(error && typeof error === 'object' && (error as { status?: number }).status === 401);
};

const jsonFetch = async <T>(path: string, options?: RequestInit): Promise<T> => {
    const token = getStoredAuthToken();
    const response = await fetch(`${AGENT_API_URL}${path}`, {
        ...options,
        headers: {
            'Accept': 'application/json',
            ...(options?.body ? { 'Content-Type': 'application/json' } : {}),
            ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
            ...(options?.headers || {})
        }
    });
    if (!response.ok) {
        let detail = `${response.status} ${response.statusText}`;
        try {
            const data = await response.json();
            detail = data.detail || detail;
        } catch {
            // Keep HTTP status text.
        }
        if (response.status === 401) clearStoredAuthToken();
        const error = new Error(detail) as Error & { status?: number };
        error.status = response.status;
        throw error;
    }
    return response.json();
};

export const getAuthStatus = async (): Promise<{ auth_required: boolean }> => {
    return jsonFetch<{ auth_required: boolean }>('/auth/status');
};

export const loginWithPassword = async (
    password: string
): Promise<{ token: string; auth_required: boolean; expires_in: number | null }> => {
    const data = await jsonFetch<{ token: string; auth_required: boolean; expires_in: number | null }>('/auth/login', {
        method: 'POST',
        body: JSON.stringify({ password })
    });
    storeAuthToken(data.token || '');
    return data;
};

export const logoutAuth = () => {
    clearStoredAuthToken();
};

export const getSessions = async (): Promise<SessionInfo[]> => {
    const data = await jsonFetch<{ sessions: SessionInfo[] }>('/sessions');
    return data.sessions || [];
};

export const getFrontendConfig = async (): Promise<FrontendConfig> => {
    return jsonFetch<FrontendConfig>('/config/frontend');
};

export const createSession = async (
    title?: string,
    cloneProjects: Array<{
        source_session_id: string;
        source_project_id: string;
        source_version?: number;
        display_name?: string;
    }> = []
): Promise<{ session: SessionInfo; projects: ProjectInfo[] }> => {
    return jsonFetch('/sessions', {
        method: 'POST',
        body: JSON.stringify({ title, clone_projects: cloneProjects })
    });
};

export const renameSession = async (sessionId: string, title: string): Promise<SessionInfo> => {
    const data = await jsonFetch<{ session: SessionInfo }>(`/sessions/${encodeURIComponent(sessionId)}`, {
        method: 'PATCH',
        body: JSON.stringify({ title })
    });
    return data.session;
};

export const deleteSession = async (
    sessionId: string
): Promise<{ session_id: string; deleted: boolean; deleted_workspace: boolean; workspace_path: string }> => {
    return jsonFetch(`/sessions/${encodeURIComponent(sessionId)}`, {
        method: 'DELETE'
    });
};

export const getSessionProjects = async (sessionId: string): Promise<ProjectInfo[]> => {
    const data = await jsonFetch<{ projects: ProjectInfo[] }>(`/sessions/${encodeURIComponent(sessionId)}/projects`);
    return data.projects || [];
};

export const getSessionProjectFiles = async (sessionId: string, projectId: string): Promise<FileMap> => {
    const data = await jsonFetch<{ files: FileMap }>(
        `/sessions/${encodeURIComponent(sessionId)}/projects/${encodeURIComponent(projectId)}/files`
    );
    return data.files || {};
};

export const getRequestActivityFile = async (
    sessionId: string,
    requestId: string,
    filePath: string
): Promise<ActivityFilePreview> => {
    const encodedPath = filePath
        .split('/')
        .map(part => encodeURIComponent(part))
        .join('/');
    return jsonFetch<ActivityFilePreview>(
        `/sessions/${encodeURIComponent(sessionId)}/requests/${encodeURIComponent(requestId)}/activity-files/${encodedPath}`
    );
};

export const getSessionProjectGraph = async (
    sessionId: string,
    projectId: string,
    startIfMissing = true
): Promise<ProjectGraphResponse> => {
    return jsonFetch<ProjectGraphResponse>(
        `/sessions/${encodeURIComponent(sessionId)}/projects/${encodeURIComponent(projectId)}/graph?start_if_missing=${startIfMissing ? 'true' : 'false'}`
    );
};

export const getSimulationSpec = async (
    sessionId: string,
    projectId: string
): Promise<SimulationSpec> => {
    const data = await jsonFetch<{ simulation?: Partial<SimulationSpec> & { arguments?: SimulationSpec['parameters'] } } & Partial<SimulationSpec> & { arguments?: SimulationSpec['parameters'] }>(
        `/sessions/${encodeURIComponent(sessionId)}/projects/${encodeURIComponent(projectId)}/simulation`
    );
    const raw = data.simulation || data;
    return {
        available: raw.available !== false,
        entrypoint: raw.entrypoint,
        description: raw.description,
        parameters: Array.isArray(raw.parameters) ? raw.parameters : (Array.isArray(raw.arguments) ? raw.arguments : []),
        validation_status: raw.validation_status,
        validation_message: raw.validation_message
    };
};

type RawSimulationRun = Partial<SimulationRun> & {
    execution_id?: string;
    finished_at?: string | null;
    message?: string | null;
};

const normalizeSimulationRun = (raw: RawSimulationRun): SimulationRun => ({
    ...raw,
    run_id: raw.run_id || raw.execution_id || '',
    status: raw.status || 'queued',
    completed_at: raw.completed_at ?? raw.finished_at,
    error: raw.error ?? raw.message
});

export const startSimulationRun = async (
    sessionId: string,
    projectId: string,
    parameters: Record<string, string | number | boolean>
): Promise<SimulationRun> => {
    const data = await jsonFetch<{ run?: RawSimulationRun; execution?: RawSimulationRun } & RawSimulationRun>(
        `/sessions/${encodeURIComponent(sessionId)}/projects/${encodeURIComponent(projectId)}/simulation-runs`,
        {
            method: 'POST',
            body: JSON.stringify({
                arguments: parameters
            })
        }
    );
    return normalizeSimulationRun(data.run || data.execution || data);
};

export const validateSimulation = async (
    sessionId: string,
    projectId: string,
    parameters: Record<string, string | number | boolean>
): Promise<SimulationRun> => {
    const data = await jsonFetch<{ run?: RawSimulationRun; execution?: RawSimulationRun } & RawSimulationRun>(
        `/sessions/${encodeURIComponent(sessionId)}/projects/${encodeURIComponent(projectId)}/simulation:validate`,
        {
            method: 'POST',
            body: JSON.stringify({ arguments: parameters })
        }
    );
    return normalizeSimulationRun(data.run || data.execution || data);
};

export const getSimulationRun = async (
    sessionId: string,
    projectId: string,
    runId: string
): Promise<SimulationRun> => {
    const data = await jsonFetch<{ run?: RawSimulationRun; execution?: RawSimulationRun } & RawSimulationRun>(
        `/sessions/${encodeURIComponent(sessionId)}/projects/${encodeURIComponent(projectId)}/simulation-runs/${encodeURIComponent(runId)}`
    );
    return normalizeSimulationRun(data.run || data.execution || data);
};

export const stopSimulationRun = async (
    sessionId: string,
    projectId: string,
    runId: string
): Promise<SimulationRun> => {
    const data = await jsonFetch<{ run?: RawSimulationRun; execution?: RawSimulationRun } & RawSimulationRun>(
        `/sessions/${encodeURIComponent(sessionId)}/projects/${encodeURIComponent(projectId)}/simulation-runs/${encodeURIComponent(runId)}/stop`,
        { method: 'POST', body: JSON.stringify({}) }
    );
    return normalizeSimulationRun(data.run || data.execution || data);
};

const simulationResultFilePath = (
    sessionId: string,
    projectId: string,
    runId: string,
    filePath: string
): string => {
    const encodedFilePath = filePath.split('/').map(segment => encodeURIComponent(segment)).join('/');
    return `/sessions/${encodeURIComponent(sessionId)}/projects/${encodeURIComponent(projectId)}`
        + `/simulation-runs/${encodeURIComponent(runId)}/result-files/${encodedFilePath}`;
};

export const getSimulationResultPreview = async (
    sessionId: string,
    projectId: string,
    runId: string,
    filePath: string
): Promise<SimulationResultPreview> => {
    return jsonFetch<SimulationResultPreview>(
        simulationResultFilePath(sessionId, projectId, runId, filePath)
    );
};

export const downloadSimulationResultFile = async (
    sessionId: string,
    projectId: string,
    runId: string,
    filePath: string
): Promise<Blob> => {
    const token = getStoredAuthToken();
    const response = await fetch(
        `${AGENT_API_URL}${simulationResultFilePath(sessionId, projectId, runId, filePath)}?download=true`,
        {
            headers: {
                ...(token ? { 'Authorization': `Bearer ${token}` } : {})
            }
        }
    );
    if (!response.ok) {
        let detail = `${response.status} ${response.statusText}`;
        try {
            const data = await response.json();
            detail = data.detail || detail;
        } catch {
            // Keep HTTP status text.
        }
        if (response.status === 401) clearStoredAuthToken();
        const error = new Error(detail) as Error & { status?: number };
        error.status = response.status;
        throw error;
    }
    return response.blob();
};

export const parseSessionProjectGraph = async (
    sessionId: string,
    projectId: string,
    provider: string,
    model: string,
    apiKey: string | null,
    force = true
): Promise<ProjectGraphResponse> => {
    return jsonFetch<ProjectGraphResponse>(
        `/sessions/${encodeURIComponent(sessionId)}/projects/${encodeURIComponent(projectId)}/graph:parse`,
        {
            method: 'POST',
            body: JSON.stringify({ provider, model, api_key: apiKey, force })
        }
    );
};

export const uploadSessionProject = async (
    sessionId: string,
    displayName: string,
    files: FileMap
): Promise<ProjectInfo> => {
    const data = await jsonFetch<{ project: ProjectInfo }>(`/sessions/${encodeURIComponent(sessionId)}/projects`, {
        method: 'POST',
        body: JSON.stringify({ display_name: displayName, files })
    });
    return data.project;
};

export const getSessionMessages = async (sessionId: string, limit = 20): Promise<BackendMessage[]> => {
    const data = await jsonFetch<{ messages: BackendMessage[] }>(
        `/sessions/${encodeURIComponent(sessionId)}/messages?limit=${limit}&order=asc`
    );
    return data.messages || [];
};

export const submitSessionChat = async (
    sessionId: string,
    content: string,
    activeProjectId: string | null,
    includeProjectContext = false,
    generationMode: GenerationMode = 'guided',
    idempotencyKey?: string
): Promise<{ request: ChatRequestInfo; user_message: BackendMessage }> => {
    return jsonFetch(`/sessions/${encodeURIComponent(sessionId)}/chat`, {
        method: 'POST',
        body: JSON.stringify({
            content,
            active_project_id: activeProjectId,
            include_project_context: includeProjectContext,
            generation_mode: generationMode,
            idempotency_key: idempotencyKey || `${Date.now()}-${Math.random().toString(16).slice(2)}`
        })
    });
};

export type ResolveInteractionAction = 'confirm' | 'revise' | 'continue_automatically' | 'cancel';

export const resolveGenerationInteraction = async (
    sessionId: string,
    requestId: string,
    interactionId: string,
    input: {
        action: ResolveInteractionAction;
        artifact_digest?: string;
        answers?: Record<string, string>;
        feedback?: string | null;
        edited_intent?: Record<string, unknown> | null;
    },
    idempotencyKey?: string
): Promise<{ request: ChatRequestInfo }> => {
    return jsonFetch(
        `/sessions/${encodeURIComponent(sessionId)}/requests/${encodeURIComponent(requestId)}`
        + `/interactions/${encodeURIComponent(interactionId)}:resolve`,
        {
            method: 'POST',
            body: JSON.stringify({
                ...input,
                idempotency_key: idempotencyKey
                    || `resolve-${Date.now()}-${Math.random().toString(16).slice(2)}`
            })
        }
    );
};

export const getSessionRequest = async (sessionId: string, requestId: string): Promise<ChatRequestInfo> => {
    const data = await jsonFetch<{ request: ChatRequestInfo }>(
        `/sessions/${encodeURIComponent(sessionId)}/requests/${encodeURIComponent(requestId)}`
    );
    return data.request;
};

export const getSessionEvents = async (
    sessionId: string,
    requestId: string,
    after = 0
): Promise<EventResponse> => {
    const query = new URLSearchParams({
        request_id: requestId,
        after: String(Math.max(0, Math.floor(after)))
    });
    return jsonFetch<EventResponse>(
        `/sessions/${encodeURIComponent(sessionId)}/events?${query.toString()}`
    );
};

export const cancelQueuedRequest = async (
    sessionId: string,
    requestId: string
): Promise<{ request: ChatRequestInfo; user_message: BackendMessage | null }> => {
    return jsonFetch(`/sessions/${encodeURIComponent(sessionId)}/requests/${encodeURIComponent(requestId)}/cancel`, {
        method: 'POST',
        body: JSON.stringify({ force: false, withdraw_user_message: true })
    });
};

// Transitional wrappers used by older App code paths.
const getDefaultSessionId = async (): Promise<string> => {
    const sessions = await getSessions();
    const sessionId = sessions[0]?.session_id;
    if (!sessionId) throw new Error('No session found');
    return sessionId;
};

export const getProjectList = async (): Promise<string[]> => {
    try {
        const projects = await getSessionProjects(await getDefaultSessionId());
        return projects.map(p => p.display_name);
    } catch (error) {
        console.warn("Backend offline or unreachable, using local mode.");
        return [];
    }
};

export const getProjectFiles = async (projectName: string): Promise<FileMap> => {
    const sessionId = await getDefaultSessionId();
    const projects = await getSessionProjects(sessionId);
    const project = projects.find(p => p.display_name === projectName);
    if (!project) throw new Error(`Project not found: ${projectName}`);
    return getSessionProjectFiles(sessionId, project.project_id);
};

export const uploadProject = async (projectName: string, files: FileMap): Promise<boolean> => {
    try {
        await uploadSessionProject(await getDefaultSessionId(), projectName, files);
        return true;
    } catch (error) {
        console.warn("Failed to sync project to backend (Offline mode):", error);
        return false;
    }
};
