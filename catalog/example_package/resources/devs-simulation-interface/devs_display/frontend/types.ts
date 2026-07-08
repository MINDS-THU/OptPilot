
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
    status: 'idle' | 'queued' | 'running' | 'cancelling' | 'failed';
    active_request_id: string | null;
    created_at: string;
    updated_at: string;
    project_count: number;
    storage_session_id?: string;
    workspace_path?: string;
    is_current_workspace?: boolean;
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
    status: 'queued' | 'running' | 'cancelling' | 'completed' | 'failed' | 'cancelled';
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
}

export interface ModelPreset {
    provider: 'gemini' | 'openai';
    label: string;
    model: string;
}

export interface FrontendConfig {
    default_provider: 'gemini' | 'openai';
    default_model: string;
    api_key_available: Record<'gemini' | 'openai', boolean>;
    model_presets: ModelPreset[];
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
