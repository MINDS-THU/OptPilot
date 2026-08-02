
export interface Port {
  name: string;
  type: 'input' | 'output';
  description?: string;
  dataType?: string;
}

export interface ModelMetadata {
  path: string;
  class_name: string;
  model_type?: 'atomic' | 'coupled';
  specification: {
    input_ports: Array<{ name: string; type: string; description: string }>;
    output_ports: Array<{ name: string; type: string; description: string }>;
    function?: { internal: string; external: string };
  };
}

export interface SystemModelInfo {
  [className: string]: ModelMetadata;
}

export interface Coupling {
  source_model: string; // 'self' or child instance name
  source_port: string;
  target_model: string; // 'self' or child instance name
  target_port: string;
}

export interface ParsedStructure {
  components: Array<{
    name: string; // instance name (e.g., 'triage_level1')
    className: string; // class name (e.g., 'TriageLevel1')
  }>;
  couplings: Coupling[];
}

export interface GraphNode {
  id: string; // unique path id (e.g., 'root/dept_0/doctor_1')
  name: string; // display name (instance name)
  className: string;
  description?: string; // Planned responsibility or generated model description
  type: 'atomic' | 'coupled';
  parent: string | null;
  expanded: boolean;
  fixed?: boolean; // New property: is the node pinned?
  x: number;
  y: number;
  width: number;
  height: number;
  ports: {
    inputs: string[];
    outputs: string[];
  };
  children: string[]; // IDs of children
  // Raw parsed data cached for expansion
  rawStructure?: ParsedStructure;
}

export interface GraphLink {
  id: string;
  source: string; // Node ID
  sourcePort: string;
  target: string; // Node ID
  targetPort: string;
  couplingType?: 'EIC' | 'IC' | 'EOC' | string;
  multiplicity?: number;
}

export interface FileMap {
  [path: string]: string; // path -> content
}

export interface ChatMessage {
    id: string;
    role: 'user' | 'assistant' | 'system';
    content: string;
    timestamp: number;
    isLoading?: boolean;
    status?: 'visible' | 'withdrawn';
}

export interface AgentResponse {
    response: string;
    // List of project names that were modified or created and need refreshing
    updated_project_names?: string[]; 
}

export interface SessionInfo {
    session_id: string;
    title: string;
    status: 'idle' | 'queued' | 'running' | 'waiting_for_user' | 'cancelling' | 'failed';
    active_request_id: string | null;
    created_at: string;
    updated_at: string;
    project_count: number;
    storage_session_id?: string;
    workspace_path?: string;
    is_current_workspace?: boolean;
}

export type GenerationMode = 'guided' | 'automatic';
export type GenerationPhase = 'interpret_intent' | 'plan_structure' | 'build' | 'verify' | 'complete';
export type ReviewInteractionKind = 'intent_review' | 'structure_review';

export interface ReviewChoice {
    value: string;
    label: string;
    description?: string;
    recommended?: boolean;
}

export interface ReviewQuestion {
    question_id: string;
    prompt: string;
    options?: ReviewChoice[];
    choices?: ReviewChoice[];
    recommended_value?: string | null;
    required?: boolean;
}

export interface SimulationBrief {
    title?: string;
    goal?: string;
    system_boundary?: string;
    entities?: string[];
    event_flow?: string[];
    parameters?: string[];
    metrics?: string[];
    assumptions?: string[];
    questions?: ReviewQuestion[];
    [key: string]: unknown;
}

export interface ProposedStructure {
    title?: string;
    root_model?: string;
    summary?: string;
    component_count?: number;
    hierarchy_depth?: number;
    graph?: ProjectGraph | null;
    [key: string]: unknown;
}

/**
 * A persisted pause in Guided generation. The backend owns the exact artifact
 * and its digest; the browser only presents it and sends an explicit decision.
 */
export interface PendingInteraction {
    interaction_id: string;
    kind: ReviewInteractionKind;
    phase?: GenerationPhase;
    status?: 'open' | 'resolved' | 'cancelled';
    revision?: number;
    artifact_id?: string;
    artifact_digest?: string;
    created_at?: string;
    prompt?: string;
    title?: string;
    description?: string;
    brief?: SimulationBrief | null;
    structure?: ProposedStructure | null;
    questions?: ReviewQuestion[];
    payload?: Record<string, unknown>;
    resolution?: {
        action?: 'confirm' | 'revise' | 'continue_automatically' | 'cancel';
        answers?: Record<string, unknown>;
        feedback?: string | null;
        automatic?: boolean;
        [key: string]: unknown;
    };
    artifact?: Record<string, unknown>;
    [key: string]: unknown;
}

export interface ProjectInfo {
    project_id: string;
    display_name: string;
    status: 'ready' | 'updating' | 'error';
    version: number;
    created_at: string;
    updated_at: string;
    path: string;
    source?: Record<string, unknown>;
}

export interface BackendMessage {
    message_id: string;
    session_id: string;
    request_id: string;
    role: 'user' | 'assistant' | 'system';
    status: 'visible' | 'withdrawn';
    content: string;
    created_at: string;
    withdrawn_at?: string | null;
}

export interface ChatRequestInfo {
    request_id: string;
    session_id: string;
    status: 'queued' | 'running' | 'waiting_for_user' | 'cancelling' | 'completed' | 'failed' | 'cancelled';
    user_message_id: string;
    assistant_message_id: string | null;
    active_project_id: string | null;
    include_project_context?: boolean;
    updated_project_ids: string[];
    updated_project_names?: string[];
    started_at: string | null;
    completed_at: string | null;
    cancel_requested_at: string | null;
    error: string | null;
    generation_mode?: GenerationMode;
    phase?: GenerationPhase;
    pending_interaction?: PendingInteraction | null;
    interactions?: PendingInteraction[];
}

export type ProgressActivityState = 'started' | 'progress' | 'completed' | 'failed';

export interface ProgressFileChange {
    path: string;
    change: 'added' | 'modified';
}

/**
 * One persisted, request-scoped activity record from the generator.
 *
 * Older backends only provide `type` and `content`. Newer activity records use
 * the optional structured fields so the UI can present useful progress without
 * exposing prompts, generated source, or raw agent reasoning.
 */
export interface ProgressEvent {
    event_id: number;
    session_id: string;
    request_id: string;
    type: string;
    content: string;
    created_at: string;
    activity_key?: string;
    activity_state?: ProgressActivityState;
    title?: string;
    detail?: string | null;
    current?: number | null;
    total?: number | null;
    technical_name?: string | null;
    file_changes?: ProgressFileChange[];
}

export interface ActivityFilePreview {
    path: string;
    content: string;
    size: number;
    root_path?: string;
    selected_path?: string;
    files?: FileMap;
    files_truncated?: boolean;
}

export interface EventResponse {
    events: ProgressEvent[];
    next_after: number;
    request_status: ChatRequestInfo['status'] | SessionInfo['status'];
}

export interface ModelPreset {
    provider: 'openai';
    label: string;
    model: string;
}

export interface FrontendConfig {
    default_provider: 'openai';
    default_model: string;
    api_key_available: Record<string, boolean>;
    model_presets: ModelPreset[];
    default_generation_mode?: GenerationMode;
}

export interface ProjectGraph {
    root_model: string;
    nodes: GraphNode[];
    links: GraphLink[];
}

export interface GraphParseState {
    status: 'missing' | 'running' | 'completed' | 'failed';
    started_at?: string | null;
    completed_at?: string | null;
    error?: string | null;
    provider?: string;
    model?: string;
    root_model?: string;
    node_count?: number;
    link_count?: number;
}

export interface ProjectGraphResponse {
    parse: GraphParseState;
    graph: ProjectGraph | null;
}

export type SimulationParameterType = 'string' | 'integer' | 'number' | 'boolean' | 'choice';

export interface SimulationParameter {
    name: string;
    label?: string;
    description?: string;
    type: SimulationParameterType;
    default?: string | number | boolean;
    minimum?: number;
    maximum?: number;
    choices?: Array<string | number>;
    required?: boolean;
}

export interface SimulationSpec {
    available: boolean;
    entrypoint?: string;
    description?: string;
    parameters: SimulationParameter[];
    validation_status?: 'unverified' | 'validating' | 'ready' | 'failed' | 'stale';
    validation_message?: string | null;
}

export type SimulationRunStatus = 'queued' | 'running' | 'finalizing' | 'succeeded' | 'completed' | 'failed' | 'timed_out' | 'stopping' | 'stopped' | 'cancelled';

export interface SimulationResultFile {
    path: string;
    size?: number;
    sha256?: string;
    media_type?: string;
    previewable?: boolean;
    downloadable?: boolean;
}

export interface SimulationResultPreview extends SimulationResultFile {
    content: string;
    media_type: string;
    previewable: true;
}

export interface SimulationRun {
    run_id: string;
    status: SimulationRunStatus;
    execution_status?: Exclude<SimulationRunStatus, 'finalizing'>;
    created_at?: string;
    started_at?: string | null;
    completed_at?: string | null;
    exit_code?: number | null;
    stdout?: string;
    stderr?: string;
    metrics?: Record<string, string | number | boolean | null>;
    result_files?: SimulationResultFile[];
    stdout_truncated?: boolean;
    stderr_truncated?: boolean;
    duration_seconds?: number | null;
    failure_kind?: string | null;
    error?: string | null;
}
