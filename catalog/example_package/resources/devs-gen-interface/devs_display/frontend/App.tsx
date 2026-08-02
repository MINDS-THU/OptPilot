import React, { useState, useMemo, useRef, useEffect, useCallback } from 'react';
import { AlertCircle, ChevronDown, Code2, Cpu, MessageSquare, Network, PanelLeftClose, PanelLeftOpen, Play, RefreshCw, Upload } from 'lucide-react';
import { GraphVisualizer, GraphVisualizerHandle } from './components/GraphVisualizer';
import { ChatInterface } from './components/ChatInterface';
import { SessionSelectorPanel } from './components/SessionSelectorPanel';
import { SourcePreviewPanel } from './components/SourcePreviewPanel';
import { FileTreeBrowser } from './components/FileTreeBrowser';
import { SimulationRunPanel } from './components/SimulationRunPanel';
import { StructureInspector } from './components/StructureInspector';
import { parseModelCode } from './services/graphParseService';
import { architectureTerminalHandoff, reviewComponentResponsibility, shouldClearArchitectureProjection } from './services/reviewPresentationService.js';
import {
  architectureOnlyGraph,
  canRefreshStructure,
  rootStructureNode,
  structureLifecyclePresentation
} from './services/structureLifecycleService.js';
import { getKeyModuleFilePaths, isDisplayableSourceFile, isKnownNoiseFile, normalizeFilePath, resolveClassSourcePath, sortSourceFiles } from './services/sourceFileService';
import { activityPreviewFileState, projectToFollowDuringGeneration, projectToOpenAfterGeneration, resolveActivityPreviewPath, selectedFileAfterProjectRefresh, shouldFocusFilesForProjectRefresh } from './services/projectSelectionService.js';
import {
  createSession,
  deleteSession,
  getFrontendConfig,
  getAuthStatus,
  getSessionProjectGraph,
  getSessionProjects,
  getSessionRequest,
  getSessions,
  getSessionProjectFiles,
  getRequestActivityFile,
  getStoredAuthToken,
  isUnauthorizedError,
  loginWithPassword,
  renameSession,
  uploadSessionProject
} from './services/agentService';
import { ActivityFilePreview, SystemModelInfo, FileMap, FrontendConfig, GraphNode, GraphLink, ParsedStructure, PendingInteraction, ProjectInfo, SessionInfo, ProjectGraph, ProjectGraphResponse } from './types';

// Default dimension constants
const NODE_WIDTH = 180;
const NODE_HEIGHT = 100;
const PANEL_BOUNDS = {
  conversation: { min: 280, max: 480, default: 336, collapseBelow: 220 }
};

type PanelName = keyof typeof PANEL_BOUNDS;
type AuthState = 'checking' | 'authenticated' | 'required';
type ConversationMode = 'history' | 'chat';
type MainTab = 'structure' | 'run' | 'files';
type StructureReviewState = 'awaiting_review' | 'revising' | 'approved_building' | 'finalizing' | 'build_stopped';

const PROJECT_STATUS_LABEL: Record<ProjectInfo['status'], string> = {
  ready: 'Ready',
  updating: 'Building',
  error: 'Needs attention'
};

const sortSessionsByRecentActivity = (sessionList: SessionInfo[]): SessionInfo[] => {
  return [...sessionList].sort((a, b) => {
    const bTime = Date.parse(b.updated_at || b.created_at || '') || 0;
    const aTime = Date.parse(a.updated_at || a.created_at || '') || 0;
    return bTime - aTime;
  });
};

const SESSION_REFRESH_INTERVAL_MS = 15000;
const PROJECT_REFRESH_INTERVAL_MS = 3000;
const ARCHITECTURE_REQUEST_POLL_INTERVAL_MS = 2000;
const GRAPH_PARSE_POLL_INTERVAL_MS = 2000;
const GRAPH_PARSE_MAX_POLL_ATTEMPTS = 300;
const GRAPH_PARSE_PROVIDER = 'openai';
const GRAPH_PARSE_MODEL = import.meta.env.VITE_DEVS_DISPLAY_MODEL_ID || 'deepseek/deepseek-v4-pro';

const emptySpec = () => ({
  input_ports: [] as Array<{ name: string; type: string; description: string }>,
  output_ports: [] as Array<{ name: string; type: string; description: string }>
});

const normalizePorts = (ports: any[] | undefined) => {
  return (ports || []).map(port => ({
    name: String(port.name || ''),
    type: String(port.type || ''),
    description: String(port.description || port.structure || '')
  })).filter(port => port.name);
};

const graphFromStructureReview = (interaction: PendingInteraction): ProjectGraph | null => {
  if (interaction.kind !== 'structure_review') return null;
  const payload = (interaction.payload || interaction.artifact || {}) as Record<string, unknown>;
  const embeddedGraph = payload.graph;
  if (
    embeddedGraph
    && typeof embeddedGraph === 'object'
    && Array.isArray((embeddedGraph as ProjectGraph).nodes)
    && Array.isArray((embeddedGraph as ProjectGraph).links)
  ) {
    return architectureOnlyGraph(embeddedGraph) as ProjectGraph;
  }

  const rawComponents = Array.isArray(payload.components)
    ? payload.components.filter(component => component && typeof component === 'object') as Array<Record<string, unknown>>
    : [];
  if (rawComponents.length === 0) return null;

  const rootModel = String(payload.root_model_name || payload.title || rawComponents[0]?.name || 'Simulation');
  const proposedRootId = String(payload.root_node_id || rootModel);
  const componentIds = new Set(rawComponents.map(component => String(component.id || component.name || '')).filter(Boolean));
  const rootComponent = rawComponents.find(component => (
    String(component.id || component.name || '') === proposedRootId
    || String(component.id || component.name || '') === rootModel
    || component.parent_id === null
  ));
  const rootId = String(rootComponent?.id || rootComponent?.name || proposedRootId);
  if (!componentIds.has(rootId)) componentIds.add(rootId);

  const normalizedComponents = rootComponent
    ? rawComponents
    : [{ id: rootId, name: rootModel, model_type: 'coupled', parent_id: null }, ...rawComponents];
  const aliases = new Map<string, string>();
  normalizedComponents.forEach(component => {
    const id = String(component.id || component.name || '');
    if (!id) return;
    aliases.set(id, id);
    aliases.set(String(component.name || id), id);
  });
  const componentParentId = (component: Record<string, unknown>): string | null => {
    const id = String(component.id || component.name || '');
    if (id === rootId) return null;
    return aliases.get(String(component.parent_id || '')) || rootId;
  };

  const componentById = new Map<string, Record<string, unknown>>();
  const parentById = new Map<string, string | null>();
  const childrenById = new Map<string, string[]>();
  normalizedComponents.forEach((component, index) => {
    const id = String(component.id || component.name || `component-${index}`);
    componentById.set(id, component);
    const parent = componentParentId(component);
    parentById.set(id, parent);
    if (parent) childrenById.set(parent, [...(childrenById.get(parent) || []), id]);
  });

  // Give every subtree its own vertical band. A per-parent sibling index makes
  // grandchildren under different branches overlap at exactly the same point.
  const layout = new Map<string, { x: number; y: number }>();
  const visiting = new Set<string>();
  let nextLeaf = 0;
  const placeSubtree = (id: string, depth: number): number => {
    if (layout.has(id)) return layout.get(id)!.y;
    if (visiting.has(id)) {
      const y = nextLeaf++ * 150 + 80;
      layout.set(id, { x: depth * 260 + 80, y });
      return y;
    }
    visiting.add(id);
    const children = (childrenById.get(id) || []).filter(child => child !== id);
    const childYs = children.map(child => placeSubtree(child, depth + 1));
    const y = childYs.length > 0
      ? childYs.reduce((sum, childY) => sum + childY, 0) / childYs.length
      : nextLeaf++ * 150 + 80;
    visiting.delete(id);
    layout.set(id, { x: depth * 260 + 80, y });
    return y;
  };
  placeSubtree(rootId, 0);
  componentById.forEach((_component, id) => {
    if (!layout.has(id)) placeSubtree(id, 1);
  });

  const nodes: GraphNode[] = normalizedComponents.map((component, index) => {
    const id = String(component.id || component.name || `component-${index}`);
    const isRoot = id === rootId;
    const parent = parentById.get(id) ?? null;
    const position = layout.get(id) || { x: isRoot ? 80 : 340, y: nextLeaf++ * 150 + 80 };
    return {
      id,
      name: String(component.name || id),
      className: String(component.class_name || component.name || id),
      description: reviewComponentResponsibility(component),
      type: String(component.model_type || (isRoot ? 'coupled' : 'atomic')) === 'coupled' ? 'coupled' : 'atomic',
      parent,
      expanded: true,
      fixed: false,
      x: position.x,
      y: position.y,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      ports: {
        inputs: [],
        outputs: []
      },
      children: childrenById.get(id) || []
    };
  });

  return { root_model: rootId, nodes, links: [] };
};

interface PanelToolbarProps {
  title: string;
  collapsed: boolean;
  onToggle: () => void;
}

const PanelToolbar: React.FC<PanelToolbarProps> = ({
  title,
  collapsed,
  onToggle
}) => (
  <div className="flex items-center gap-1">
    <button
      onClick={onToggle}
      className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
      title={collapsed ? `Show ${title}` : `Hide ${title}`}
    >
      {collapsed ? <PanelLeftOpen size={15} /> : <PanelLeftClose size={15} />}
    </button>
  </div>
);

const LoginScreen: React.FC<{
  error: string | null;
  loading: boolean;
  onSubmit: (password: string) => void;
}> = ({ error, loading, onSubmit }) => {
  const [password, setPassword] = useState('');

  return (
    <div className="flex h-full min-h-0 w-full items-center justify-center overflow-hidden bg-slate-100 px-4">
      <form
        className="w-full max-w-sm rounded border border-slate-200 bg-white p-6 shadow-sm"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit(password);
        }}
      >
        <div className="mb-5">
          <h1 className="flex items-center gap-2 text-xl font-bold text-slate-800">
            <Cpu className="text-blue-600" />
            DEVS Generator
          </h1>
        </div>
        <label className="mb-2 block text-sm font-medium text-slate-700" htmlFor="hamlet-password">
          Password
        </label>
        <input
          id="hamlet-password"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoFocus
          className="mb-4 w-full rounded border border-slate-300 px-3 py-2 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
        />
        {error && (
          <div className="mb-4 flex items-center gap-2 text-sm text-red-600">
            <AlertCircle size={14} />
            {error}
          </div>
        )}
        <button
          type="submit"
          disabled={loading || !password}
          className="w-full rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? 'Checking...' : 'Enter'}
        </button>
      </form>
    </div>
  );
};

interface PanelResizeHandleProps {
  title: string;
  panel: PanelName;
  onResizeStart: (panel: PanelName, event: React.PointerEvent<HTMLDivElement>) => void;
}

const PanelResizeHandle: React.FC<PanelResizeHandleProps> = ({ title, panel, onResizeStart }) => (
  <div
    role="separator"
    aria-orientation="vertical"
    aria-label={`Resize ${title}`}
    title={`Drag to resize ${title}; drag past minimum to collapse`}
    onPointerDown={(event) => onResizeStart(panel, event)}
    className="absolute inset-y-0 -right-1 z-40 w-2 cursor-col-resize bg-transparent transition-colors hover:bg-blue-300/70"
  />
);

interface CollapsedPanelButtonProps {
  title: string;
  icon: React.ReactNode;
  onClick: () => void;
}

const CollapsedPanelButton: React.FC<CollapsedPanelButtonProps> = ({ title, icon, onClick }) => (
  <button
    onClick={onClick}
    className="flex h-full w-10 flex-col items-center justify-start gap-2 border-r border-slate-200 bg-white px-2 py-4 text-slate-500 hover:bg-slate-50 hover:text-blue-600"
    title={`Show ${title}`}
  >
    {icon}
    <span className="vertical-rl text-[10px] font-semibold uppercase tracking-wide" style={{ writingMode: 'vertical-rl' }}>
      {title}
    </span>
  </button>
);

// Helper function to detect root model based on path depth and JSON order
const detectRootModel = (info: SystemModelInfo): string => {
  const keys = Object.keys(info);
  if (keys.length === 0) return '';

  const coupledTopLevel = keys
    .filter(key => info[key].model_type === 'coupled')
    .sort((a, b) => {
      const aPath = info[a].path || '';
      const bPath = info[b].path || '';
      return aPath.split(/[/\\]/).length - bPath.split(/[/\\]/).length;
    });
  if (coupledTopLevel.length > 0) return coupledTopLevel[0];

  let bestKey = '';
  let minDepth = Infinity;

  keys.forEach(key => {
    const path = info[key].path || '';
    const depth = path.split(/[/\\]/).length;
    if (depth <= minDepth) {
      minDepth = depth;
      bestKey = key;
    }
  });

  return bestKey;
};

const parseRegistryModelInfo = (rawFiles: FileMap): SystemModelInfo | null => {
  const registryKey = Object.keys(rawFiles).find(key =>
    key.endsWith('system_registry_v1_post_build.json') || key.endsWith('system_registry.json')
  );
  if (!registryKey) return null;

  try {
    const registry = JSON.parse(rawFiles[registryKey]);
    if (!Array.isArray(registry)) return null;

    const info: SystemModelInfo = {};
    registry.forEach((entry: any) => {
      const className = entry.class_name;
      if (!className) return;
      const spec = entry.specification || {};
      const path = entry.relative_file_path || entry.file_path || `${className}.py`;
      const functionText = String(spec.function || '').toLowerCase();
      info[className] = {
        path,
        class_name: className,
        model_type: functionText.includes('coupled') ? 'coupled' : 'atomic',
        specification: {
          ...emptySpec(),
          ...spec,
          input_ports: normalizePorts(spec.input_ports),
          output_ports: normalizePorts(spec.output_ports)
        }
      };
    });
    return Object.keys(info).length > 0 ? info : null;
  } catch (err) {
    console.warn('Failed to parse system registry metadata.', err);
    return null;
  }
};

const inferModelInfoFromPython = (rawFiles: FileMap): SystemModelInfo | null => {
  const info: SystemModelInfo = {};
  Object.entries(rawFiles).forEach(([path, content]) => {
    if (!path.endsWith('.py') || path.includes('/_analysis_logs/') || path.includes('/devs_utils/')) return;

    for (const match of content.matchAll(/^class\s+(\w+)\s*\(([^)]*)\):/gm)) {
      const className = match[1];
      const bases = match[2];
      if (!bases.includes('Coupled') && !bases.includes('Atomic')) continue;

      const bodyStart = match.index || 0;
      const nextClass = /^class\s+\w+\s*\([^)]*\):/gm;
      nextClass.lastIndex = bodyStart + match[0].length;
      const nextMatch = nextClass.exec(content);
      const body = content.slice(bodyStart, nextMatch ? nextMatch.index : content.length);
      const spec = emptySpec();

      for (const portMatch of body.matchAll(/add_in_port\(\s*Port\([^,]+,\s*["']([^"']+)["']/g)) {
        spec.input_ports.push({ name: portMatch[1], type: '', description: '' });
      }
      for (const portMatch of body.matchAll(/add_out_port\(\s*Port\([^,]+,\s*["']([^"']+)["']/g)) {
        spec.output_ports.push({ name: portMatch[1], type: '', description: '' });
      }

      info[className] = {
        path,
        class_name: className,
        model_type: bases.includes('Coupled') ? 'coupled' : 'atomic',
        specification: spec
      };
    }
  });

  return Object.keys(info).length > 0 ? info : null;
};

const getModelInfoFromFiles = (rawFiles: FileMap): SystemModelInfo | null => {
  const jsonKey = Object.keys(rawFiles).find(key => key.endsWith('system_model_info.json'));
  if (jsonKey) {
    try {
      const parsed = JSON.parse(rawFiles[jsonKey]);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed as SystemModelInfo;
    } catch (err) {
      console.warn('Invalid system_model_info.json.', err);
    }
  }

  return parseRegistryModelInfo(rawFiles) || inferModelInfoFromPython(rawFiles);
};

// Helper: Strip common root folder if exists to standardize paths
const standardizeFiles = (rawFiles: FileMap): { name: string, files: FileMap } => {
    const paths = Object.keys(rawFiles);
    if (paths.length === 0) return { name: 'Empty simulation', files: rawFiles };

    const firstPathParts = paths[0].split('/');
    let commonPrefix = '';

    // Check if the first part is a directory present in ALL files (e.g. "ProjectA/file1", "ProjectA/file2")
    if (firstPathParts.length > 1) {
        const potentialRoot = firstPathParts[0] + '/';
        const allMatch = paths.every(p => p.startsWith(potentialRoot));
        if (allMatch) {
            commonPrefix = potentialRoot;
        }
    }

    const cleanedFiles: FileMap = {};
    paths.forEach(p => {
        cleanedFiles[p.replace(commonPrefix, '')] = rawFiles[p];
    });

    const inferredName = commonPrefix ? commonPrefix.slice(0, -1) : 'Uploaded simulation';
    return { name: inferredName, files: cleanedFiles };
};

const App: React.FC = () => {
  // Backend calls these generated bundles "projects". The student UI consistently calls them simulations.
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<string>('');
  const [remoteProjects, setRemoteProjects] = useState<ProjectInfo[]>([]);
  const [localProjects, setLocalProjects] = useState<ProjectInfo[]>([]); // Track manually uploaded projects
  const [currentProjectId, setCurrentProjectId] = useState<string | null>(null);
  const [currentProjectName, setCurrentProjectName] = useState<string | null>(null);
  const [projectCache, setProjectCache] = useState<Record<string, FileMap>>({});
  const [collapsedPanels, setCollapsedPanels] = useState<Record<PanelName, boolean>>({
      conversation: typeof window !== 'undefined' && window.innerWidth < 900
  });
  const [panelWidths, setPanelWidths] = useState<Record<PanelName, number>>({
      conversation: PANEL_BOUNDS.conversation.default
  });
  const [conversationMode, setConversationMode] = useState<ConversationMode>('history');
  const [mainTab, setMainTab] = useState<MainTab>('files');
  const dragStateRef = useRef<{
      panel: PanelName;
      startX: number;
      startWidth: number;
      pointerId: number;
  } | null>(null);

  // --- Current Project Data ---
  const [modelInfo, setModelInfo] = useState<SystemModelInfo | null>(null);
  const [files, setFiles] = useState<FileMap>({});
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(null);
  const [activityFilePreview, setActivityFilePreview] = useState<ActivityFilePreview | null>(null);
  const [rootModelName, setRootModelName] = useState<string>('');
  
  // Settings
  const [physicsEnabled, setPhysicsEnabled] = useState<boolean>(true);

  // Graph State
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [links, setLinks] = useState<GraphLink[]>([]);
  const [proposedGraph, setProposedGraph] = useState<ProjectGraph | null>(null);
  const [structureReviewState, setStructureReviewState] = useState<StructureReviewState | null>(null);
  const [architectureProjectionOwner, setArchitectureProjectionOwner] = useState<{ sessionId: string; requestId: string } | null>(null);
  const [selectedSourceNode, setSelectedSourceNode] = useState<GraphNode | null>(null);
  const [graphSource, setGraphSource] = useState<'backend' | 'local' | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [parseStatus, setParseStatus] = useState<string>('');
  const [authState, setAuthState] = useState<AuthState>('checking');
  const [authLoading, setAuthLoading] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [frontendConfig, setFrontendConfig] = useState<FrontendConfig | null>(null);

  // Refs
  const nodesRef = useRef<GraphNode[]>([]);
  useEffect(() => { nodesRef.current = nodes; }, [nodes]);

  const shouldAutoRefreshRef = useRef(false);
  const manualProjectSelectionRef = useRef(false);
  const projectLoadRequestRef = useRef(0);
  const activityFilePreviewRef = useRef<ActivityFilePreview | null>(null);
  const activityFileRequestRef = useRef(0);
  const lastActiveRequestIdRef = useRef<string | null>(null);
  const architectureProjectionOwnerRef = useRef<{ sessionId: string; requestId: string } | null>(null);
  const architectureTerminalPollInFlightRef = useRef(false);
  const handledArchitectureTerminalRequestsRef = useRef<Set<string>>(new Set());
  const handleProjectsUpdatedRef = useRef<(updatedIdsOrNames: string[]) => Promise<void>>(async () => {});
  const graphVisualizerRef = useRef<GraphVisualizerHandle>(null);
  const [parsedCache, setParsedCache] = useState<Record<string, ParsedStructure>>({});

  // --- Initialization ---
  useEffect(() => {
      initializeAuthAndBackendState();
  }, []);

  const initializeAuthAndBackendState = async () => {
      setAuthState('checking');
      setAuthError(null);
      try {
          const status = await getAuthStatus();
          if (status.auth_required && !getStoredAuthToken()) {
              setAuthState('required');
              return;
          }
          setAuthState('authenticated');
          await initializeBackendState();
      } catch (err) {
          if (isUnauthorizedError(err)) {
              setAuthState('required');
              setAuthError('Password required.');
              return;
          }
          setAuthState('authenticated');
          await initializeBackendState();
      }
  };

  const handleLogin = async (password: string) => {
      setAuthLoading(true);
      setAuthError(null);
      try {
          await loginWithPassword(password);
          setAuthState('authenticated');
          await initializeBackendState();
      } catch (err: any) {
          setAuthError(isUnauthorizedError(err) ? 'Invalid password.' : (err.message || 'Login failed.'));
          setAuthState('required');
      } finally {
          setAuthLoading(false);
      }
  };

  const initializeBackendState = async () => {
      try {
          const [sessionRows, config] = await Promise.all([
              getSessions(),
              getFrontendConfig()
          ]);
          setFrontendConfig(config);
          const sessionList = sortSessionsByRecentActivity(sessionRows);
          setSessions(sessionList);
          const nextSessionId = sessionList[0]?.session_id || '';
          setCurrentSessionId(nextSessionId);
          if (nextSessionId) {
              await refreshProjectList(nextSessionId);
          } else {
              setRemoteProjects([]);
          }
      } catch (err) {
          if (isUnauthorizedError(err)) {
              setAuthState('required');
              setAuthError('Password required.');
              return;
          }
          console.warn("Backend sessions unavailable, using local mode.", err);
          setCurrentSessionId('');
          setRemoteProjects([]);
      }
  };

  const refreshSessionList = async (): Promise<SessionInfo[]> => {
      try {
          const sessionList = sortSessionsByRecentActivity(await getSessions());
          setSessions(sessionList);
          return sessionList;
      } catch (err) {
          if (isUnauthorizedError(err)) {
              setAuthState('required');
              setAuthError('Password required.');
              return [];
          }
          console.warn("Failed to refresh sessions.", err);
          return [];
      }
  };

  useEffect(() => {
      if (authState !== 'authenticated') return;
      const interval = window.setInterval(() => {
          refreshSessionList();
      }, SESSION_REFRESH_INTERVAL_MS);
      return () => window.clearInterval(interval);
  }, [authState]);

  const refreshProjectList = async (
      sessionId = currentSessionId
  ): Promise<ProjectInfo[]> => {
      if (!sessionId) {
          setRemoteProjects([]);
          return [];
      }
      try {
          const projs = await getSessionProjects(sessionId);
          setRemoteProjects(projs);
          return projs;
      } catch (err) {
          if (isUnauthorizedError(err)) {
              setAuthState('required');
              setAuthError('Password required.');
              return [];
          }
          console.warn("Backend offline or unreachable, using local mode.", err);
          setRemoteProjects([]);
          return [];
      }
  };

  // Merge lists, removing duplicates
  const allProjects = useMemo(() => {
      const byId = new Map<string, ProjectInfo>();
      [...remoteProjects, ...localProjects].forEach(project => byId.set(project.project_id, project));
      return Array.from(byId.values());
  }, [remoteProjects, localProjects]);

  // --- File Loading Logic ---

  const loadFilesIntoState = (
    newFiles: FileMap,
    project?: ProjectInfo,
    options: {
      preserveActivityPreview?: boolean;
      preserveSelectionForSameProject?: boolean;
    } = {}
  ) => {
    const info = getModelInfoFromFiles(newFiles);
    const newRoot = info ? detectRootModel(info) : '';

    setFiles(newFiles);
    setModelInfo(info);
    setRootModelName(newRoot);
    if (!options.preserveActivityPreview) {
        activityFilePreviewRef.current = null;
        setActivityFilePreview(null);
        setSelectedSourceNode(null);
        if (options.preserveSelectionForSameProject) {
            setSelectedFilePath(previousPath => selectedFileAfterProjectRefresh(
                previousPath,
                currentProjectId,
                project?.project_id || null,
                Object.keys(newFiles)
            ));
        } else {
            setSelectedFilePath(null);
        }
    }
    setParsedCache({}); // Clear parse cache when files change
    setParseStatus(info
      ? `Loaded ${Object.keys(info).length} model definitions. Root: ${newRoot || 'unknown'}.`
      : 'No model metadata or xDEVS model classes were detected.');
    if (project) {
        setCurrentProjectName(project.display_name);
        setCurrentProjectId(project.project_id);
    }
    
    // Auto refresh graph if we found a root
    if (newRoot) {
        shouldAutoRefreshRef.current = true;
    }
  };

  const handleProjectSelect = async (e: React.ChangeEvent<HTMLSelectElement>) => {
      const projectId = e.target.value;
      manualProjectSelectionRef.current = true;
      if (!projectId) {
          setCurrentProjectId(null);
          setCurrentProjectName(null);
          handleClearProject();
          return;
      }
      
      const project = allProjects.find(p => p.project_id === projectId);
      if (project) await fetchAndLoadProject(project);
  };

  const selectSessionById = async (sessionId: string) => {
      manualProjectSelectionRef.current = false;
      projectLoadRequestRef.current += 1;
      setCurrentSessionId(sessionId);
      setLocalProjects([]);
      setProjectCache({});
      setCurrentProjectId(null);
      setCurrentProjectName(null);
      handleClearProject();
      await refreshProjectList(sessionId);
      setConversationMode('chat');
  };

  const handleCreateSession = async () => {
      try {
          const title = `New simulation ${new Date().toLocaleString()}`;
          const result = await createSession(title);
          const refreshedSessions = await refreshSessionList();
          if (!refreshedSessions.some(session => session.session_id === result.session.session_id)) {
              setSessions(prev => [result.session, ...prev.filter(session => session.session_id !== result.session.session_id)]);
          }
          setCurrentSessionId(result.session.session_id);
          manualProjectSelectionRef.current = false;
          projectLoadRequestRef.current += 1;
          setRemoteProjects(result.projects || []);
          setLocalProjects([]);
          setProjectCache({});
          setCurrentProjectId(null);
          setCurrentProjectName(null);
          handleClearProject();
          setConversationMode('chat');
      } catch (err: any) {
          setError(err.message || "Failed to start a new simulation design.");
      }
  };

  const handleRenameSession = async (sessionId: string, title: string) => {
      try {
          const updatedSession = await renameSession(sessionId, title);
          setSessions(prev => sortSessionsByRecentActivity(
              prev.map(session => session.session_id === sessionId ? updatedSession : session)
          ));
          setError(null);
      } catch (err: any) {
          setError(err.message || "Failed to rename the design.");
      }
  };

  const handleDeleteSession = async (session: SessionInfo) => {
      const title = session.title || session.session_id;
      const confirmed = window.confirm(
          `Delete design "${title}"?\n\nThis removes its conversation and temporary generated simulations from DEVS Generator.`
      );
      if (!confirmed) return;

      try {
          await deleteSession(session.session_id);
          const remainingSessions = sortSessionsByRecentActivity(
              sessions.filter(item => item.session_id !== session.session_id)
          );
          setSessions(remainingSessions);

          if (session.session_id === currentSessionId) {
              const nextSessionId = remainingSessions[0]?.session_id || '';
              setCurrentSessionId(nextSessionId);
              manualProjectSelectionRef.current = false;
              projectLoadRequestRef.current += 1;
              setLocalProjects([]);
              setProjectCache({});
              setCurrentProjectId(null);
              setCurrentProjectName(null);
              handleClearProject();
              if (nextSessionId) {
                  await refreshProjectList(nextSessionId);
              } else {
                  setRemoteProjects([]);
              }
          }
          setError(null);
      } catch (err: any) {
          setError(err.message || "Failed to delete the design.");
      }
  };

  const fetchAndLoadProject = async (
      project: ProjectInfo,
      forceRefresh = false
  ): Promise<boolean> => {
      if (!currentSessionId && !project.project_id.startsWith('local-')) {
          setError("Start or select a design before loading simulations.");
          return false;
      }
      const loadRequest = ++projectLoadRequestRef.current;
      setLoading(true);
      setError(null);
      try {
          // 1. Check Cache (Priority for Local/Offline)
          if (!forceRefresh && projectCache[project.project_id]) {
              console.log("Loading from cache:", project.display_name);
              if (loadRequest === projectLoadRequestRef.current) {
                  loadFilesIntoState(projectCache[project.project_id], project);
              } else {
                  return false;
              }
          } else {
              // 2. Fetch from backend
              console.log("Fetching from backend:", project.display_name);
              try {
                const projectFiles = await getSessionProjectFiles(currentSessionId, project.project_id);
                if (loadRequest !== projectLoadRequestRef.current) return false;
                setProjectCache(prev => ({ ...prev, [project.project_id]: projectFiles }));
                loadFilesIntoState(projectFiles, project);
              } catch (fetchErr) {
                // Backend failed and no cache
                throw new Error("Could not load the simulation. The service is offline and there is no local copy.");
              }
          }
          return true;
      } catch (err: any) {
          if (loadRequest === projectLoadRequestRef.current) {
              setError(err.message || `Failed to load simulation: ${project.display_name}`);
          }
          return false;
      } finally {
          if (loadRequest === projectLoadRequestRef.current) setLoading(false);
      }
  };

  // Handle updates from Agent Chat
  const handleProjectsUpdated = async (updatedIdsOrNames: string[]) => {
      // 1. Refresh list of projects available
      const refreshedProjects = await refreshProjectList();

      // 2. Invalidate caches for updated projects
      setProjectCache(prev => {
          const newCache = { ...prev };
          updatedIdsOrNames.forEach(id => delete newCache[id]);
          return newCache;
      });

      // 3. Open the simulation the agent just created or revised. Use the
      // refreshed records so the graph and files cannot lag one revision.
      const projectToOpen = projectToOpenAfterGeneration(
          refreshedProjects,
          updatedIdsOrNames,
          currentProjectId,
          manualProjectSelectionRef.current
      );
      let implementedGraphReady = false;
      if (projectToOpen) {
          setMainTab('structure');
          const projectLoaded = await fetchAndLoadProject(projectToOpen, true);
          if (projectLoaded) {
              if (projectToOpen.project_id.startsWith('local-')) {
                  implementedGraphReady = true;
              } else {
                  try {
                      const response = await getSessionProjectGraph(
                          currentSessionId,
                          projectToOpen.project_id,
                          true
                      );
                      implementedGraphReady = applyBackendGraphResponse(response)
                          || await waitForBackendGraph(currentSessionId, projectToOpen.project_id);
                  } catch (graphError: any) {
                      setError(graphError.message || 'The implemented model is ready, but its Structure could not be loaded yet. Choose Refresh in Structure to retry.');
                  }
              }
          }
      }
      // Replace the approved plan atomically. If loading fails, keep the
      // whole approved architecture instead of exposing a partial hierarchy.
      if (implementedGraphReady) {
          handlePendingStructureChange(null, 'clear');
      } else if (architectureProjectionOwnerRef.current) {
          setStructureReviewState('finalizing');
      }
  };

  // Keep the simulation menu live while the agent writes a new bundle.  The
  // backend exposes discovered bundles as `updating`; following that record
  // lets students inspect files before the full generation request completes.
  // A direct dropdown choice disables auto-follow for the rest of this design.
  useEffect(() => {
      if (authState !== 'authenticated' || !currentSessionId) return;

      let cancelled = false;
      let inFlight = false;
      const refreshAndFollow = async () => {
          if (inFlight) return;
          inFlight = true;
          try {
              const projects = await getSessionProjects(currentSessionId);
              if (cancelled) return;
              setRemoteProjects(projects);
              const projectToFollow = projectToFollowDuringGeneration(
                  projects,
                  currentProjectId,
                  manualProjectSelectionRef.current
              );
              if (!projectToFollow) return;

              const loadRequest = ++projectLoadRequestRef.current;
              const projectFiles = await getSessionProjectFiles(
                  currentSessionId,
                  projectToFollow.project_id
              );
              if (
                  cancelled
                  || loadRequest !== projectLoadRequestRef.current
              ) return;
              setProjectCache(prev => ({
                  ...prev,
                  [projectToFollow.project_id]: projectFiles
              }));
              const shouldFocusFiles = shouldFocusFilesForProjectRefresh(
                  currentProjectId,
                  projectToFollow.project_id
              );
              // Keep the explicit architecture checkpoint/build lifecycle on
              // screen. Students may still choose Files themselves, but
              // background project discovery must not pull them away from the
              // approved whole-model view.
              if (shouldFocusFiles && !structureReviewState) setMainTab('files');
              loadFilesIntoState(projectFiles, projectToFollow, {
                  preserveActivityPreview: Boolean(activityFilePreviewRef.current),
                  preserveSelectionForSameProject: !shouldFocusFiles
              });
          } catch (err) {
              if (isUnauthorizedError(err)) {
                  setAuthState('required');
                  setAuthError('Password required.');
              } else {
                  console.warn('Failed to refresh simulations during generation.', err);
              }
          } finally {
              inFlight = false;
          }
      };

      refreshAndFollow();
      const interval = window.setInterval(refreshAndFollow, PROJECT_REFRESH_INTERVAL_MS);
      return () => {
          cancelled = true;
          window.clearInterval(interval);
      };
  }, [authState, currentSessionId, currentProjectId, structureReviewState]);

  // Auto-refresh graph
  useEffect(() => {
      if (shouldAutoRefreshRef.current && rootModelName && modelInfo) {
          shouldAutoRefreshRef.current = false;
          initializeGraph();
      }
  }, [rootModelName, modelInfo]);


  // Enhanced File Upload
  const handleFileUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const fileList = event.target.files;
    if (!fileList) return;

    if (!currentSessionId) {
      setError("Start or select a design before uploading a simulation.");
      event.target.value = '';
      return;
    }

    setLoading(true);
    const rawFiles: FileMap = {};
    for (let i = 0; i < fileList.length; i++) {
      const file = fileList[i];
      if (file.name.startsWith('.')) continue;
      const text = await file.text();
      const filePath = file.webkitRelativePath || file.name;
      rawFiles[filePath] = text;
    }

    // Standardize paths (strip root folder) and guess name
    const { name: inferredName, files: cleanFiles } = standardizeFiles(rawFiles);
    
    // Attempt to sync to backend first; fall back to local-only if it fails.
    try {
        const project = await uploadSessionProject(currentSessionId, inferredName, cleanFiles);
        setRemoteProjects(prev => Array.from(new Map([...prev, project].map(p => [p.project_id, p])).values()));
        setProjectCache(prev => ({ ...prev, [project.project_id]: cleanFiles }));
        loadFilesIntoState(cleanFiles, project);
        // If sync success, refresh remote list to ensure consistency
        await refreshProjectList();
    } catch (err) {
        // If sync fails, we are in "Local Mode" for this project
        const localProject: ProjectInfo = {
            project_id: `local-${Date.now()}`,
            display_name: inferredName,
            status: 'ready',
            version: 1,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            path: inferredName,
            source: { type: 'local' }
        };
        setLocalProjects(prev => Array.from(new Map([...prev, localProject].map(p => [p.project_id, p])).values()));
        setProjectCache(prev => ({ ...prev, [localProject.project_id]: cleanFiles }));
        loadFilesIntoState(cleanFiles, localProject);
        setError(`Simulation "${inferredName}" was loaded only in this browser because synchronization failed.`);
    }
    setLoading(false);
  };

  const handleClearProject = () => {
      projectLoadRequestRef.current += 1;
      activityFileRequestRef.current += 1;
      activityFilePreviewRef.current = null;
      setActivityFilePreview(null);
      setFiles({});
      setModelInfo(null);
      setRootModelName('');
      setNodes([]);
      setLinks([]);
      setSelectedSourceNode(null);
      setSelectedFilePath(null);
      setGraphSource(null);
      setParsedCache({});
      setParseStatus('');
      setError(null);
  };

  const findFileContent = (pathInJson: string): string | undefined => {
      if (files[pathInJson]) return files[pathInJson];

      const allFileKeys = Object.keys(files);
      
      const suffixMatch = allFileKeys.find(key => 
          key.endsWith(pathInJson) || (pathInJson.length > key.length && pathInJson.endsWith(key))
      );
      if (suffixMatch) return files[suffixMatch];

      const filename = pathInJson.split(/[/\\]/).pop();
      if (filename) {
          const fileMatch = allFileKeys.find(k => k.endsWith('/' + filename) || k === filename);
          if (fileMatch) return files[fileMatch];
      }
      return undefined;
  };

  const getPorts = (className: string) => {
      if (!modelInfo || !modelInfo[className]) return { inputs: [], outputs: [] };
      const spec = modelInfo[className].specification;
      return {
          inputs: spec.input_ports.map(p => p.name),
          outputs: spec.output_ports.map(p => p.name)
      };
  };

  const applyBackendGraphResponse = (response: ProjectGraphResponse): boolean => {
    const parse = response.parse;
    if (parse.status === 'completed' && response.graph) {
      setRootModelName(response.graph.root_model);
      setNodes(response.graph.nodes);
      setLinks(response.graph.links);
      setGraphSource('backend');
      setParseStatus(`Graph ready: ${response.graph.nodes.length} nodes, ${response.graph.links.length} links.`);
      return true;
    }
    if (parse.status === 'failed') {
      throw new Error(parse.error || 'Backend graph parse failed.');
    }
    setParseStatus(`Backend graph parse is ${parse.status}...`);
    return false;
  };

  const waitForBackendGraph = async (sessionId: string, projectId: string): Promise<boolean> => {
    for (let attempt = 0; attempt < GRAPH_PARSE_MAX_POLL_ATTEMPTS; attempt += 1) {
      const response = await getSessionProjectGraph(sessionId, projectId, false);
      if (applyBackendGraphResponse(response)) return true;
      await new Promise(resolve => setTimeout(resolve, GRAPH_PARSE_POLL_INTERVAL_MS));
    }
    setParseStatus('Backend graph parse is still running. You can refresh the graph again later to load the completed cache.');
    return false;
  };

  const initializeGraph = async () => {
    if (!modelInfo || !rootModelName) {
      setError("Please select a simulation.");
      return;
    }
    
    setLoading(true);
    setError(null);
    setParseStatus(`Parsing ${rootModelName} from source code...`);
    setNodes([]); 
    setLinks([]);
    setSelectedSourceNode(null);
    setGraphSource(null);

    try {
      if (currentSessionId && currentProjectId && !currentProjectId.startsWith('local-')) {
          setParseStatus(`Loading backend graph for ${currentProjectName || currentProjectId}...`);
          const response = await getSessionProjectGraph(
            currentSessionId,
            currentProjectId,
            true
          );
          if (!applyBackendGraphResponse(response)) {
            await waitForBackendGraph(currentSessionId, currentProjectId);
          }
          return;
      }

      let parsed = parsedCache[rootModelName];
      
      if (!parsed) {
          const rootMeta = modelInfo[rootModelName];
          if (!rootMeta) throw new Error(`Root model '${rootModelName}' not found in metadata.`);
          
          const code = findFileContent(rootMeta.path);
          if (!code) throw new Error(`Source code for ${rootModelName} not found.`);
          
          setParseStatus(`Parsing ${rootModelName}. Local parser will run first; backend ${GRAPH_PARSE_MODEL} is used only as fallback.`);
          parsed = await parseModelCode(rootModelName, code, {
            apiKey: '',
            provider: GRAPH_PARSE_PROVIDER,
            model: GRAPH_PARSE_MODEL
          });
          setParsedCache(prev => ({ ...prev, [rootModelName]: parsed }));
      }
      
      const rootNode: GraphNode = {
        id: 'root',
        name: rootModelName,
        className: rootModelName,
        type: modelInfo[rootModelName]?.model_type === 'atomic' ? 'atomic' : 'coupled',
        parent: null,
        expanded: true,
        fixed: false, 
        x: 0,
        y: 0,
        width: 800,
        height: 600,
        ports: getPorts(rootModelName),
        children: [],
        rawStructure: parsed
      };

      const initialNodes: GraphNode[] = [rootNode];
      const initialLinks: GraphLink[] = [];
      const cols = 3; 

      parsed.components.forEach((comp, idx) => {
         const compId = `root/${comp.name}`;
         rootNode.children.push(compId);
         const col = idx % cols;
         const row = Math.floor(idx / cols);
         const offsetX = (col - 1) * 250; 
         const offsetY = (row - 1) * 200;

         initialNodes.push({
             id: compId,
             name: comp.name,
             className: comp.className,
             type: modelInfo[comp.className]?.model_type === 'atomic' ? 'atomic' : 'coupled',
             parent: 'root',
             expanded: false,
             x: offsetX,
             y: offsetY,
             width: NODE_WIDTH,
             height: NODE_HEIGHT,
             ports: getPorts(comp.className),
             children: []
         });
      });

      parsed.couplings.forEach((c, idx) => {
          let source = c.source_model === 'self' ? 'root' : `root/${c.source_model}`;
          let target = c.target_model === 'self' ? 'root' : `root/${c.target_model}`;
          initialLinks.push({
              id: `link-root-${idx}`,
              source,
              sourcePort: c.source_port,
              target,
              targetPort: c.target_port
          });
      });

      setNodes(initialNodes);
      setLinks(initialLinks);
      setGraphSource('local');
      setParseStatus(`Graph ready: ${initialNodes.length} nodes, ${initialLinks.length} links.`);

    } catch (err: any) {
      setError(err.message || "Failed to parse model.");
      setParseStatus(`Failed to parse ${rootModelName}.`);
    } finally {
      setLoading(false);
    }
  };

  const handleExpand = async (nodeId: string) => {
    const currentNode = nodesRef.current.find(n => n.id === nodeId);
    if (!currentNode) return;
    
    setNodes(prev => prev.map(n => n.id === nodeId ? { ...n, expanded: true } : n));

    const knownChildCount = currentNode.children.length
        || nodesRef.current.filter(node => node.parent === nodeId).length;
    if (knownChildCount > 0) {
        setParseStatus(`Expanded ${currentNode.className}: showing ${knownChildCount} cached child nodes.`);
        return;
    }

    if (graphSource === 'backend') {
        setParseStatus(`Expanded ${currentNode.className}: no cached child nodes are available. Refresh Graph to re-parse.`);
        return;
    }

    setLoading(true);
    try {
        let parsed = parsedCache[currentNode.className];

        if (!parsed) {
            const meta = modelInfo![currentNode.className];
            if (!meta) throw new Error(`No metadata for ${currentNode.className}`);
            const code = findFileContent(meta.path);
            if (!code) throw new Error(`No code for ${currentNode.className}`);
            
            setParseStatus(`Parsing ${currentNode.className}. Local parser will run first; backend ${GRAPH_PARSE_MODEL} is used only as fallback.`);
            parsed = await parseModelCode(currentNode.className, code, {
              apiKey: '',
              provider: GRAPH_PARSE_PROVIDER,
              model: GRAPH_PARSE_MODEL
            });
            setParsedCache(prev => ({ ...prev, [currentNode.className]: parsed }));
        }

        const newNodes: GraphNode[] = [];
        const newLinks: GraphLink[] = [];
        const cols = 2; 

        parsed.components.forEach((comp, idx) => {
            const childId = `${currentNode.id}/${comp.name}`;
            const col = idx % cols;
            const row = Math.floor(idx / cols);
            // Spawn relative to CURRENT position of the parent node
            const startX = currentNode.x + (col * 220) - 100; 
            const startY = currentNode.y + (row * 150) - 50;

            newNodes.push({
                id: childId,
                name: comp.name,
                className: comp.className,
                type: modelInfo[comp.className]?.model_type === 'atomic' ? 'atomic' : 'coupled',
                parent: currentNode.id,
                expanded: false,
                x: startX,
                y: startY,
                width: NODE_WIDTH,
                height: NODE_HEIGHT,
                ports: getPorts(comp.className),
                children: []
            });
        });

        parsed.couplings.forEach((c, idx) => {
            const source = c.source_model === 'self' ? currentNode.id : `${currentNode.id}/${c.source_model}`;
            const target = c.target_model === 'self' ? currentNode.id : `${currentNode.id}/${c.target_model}`;
            newLinks.push({
                id: `link-${currentNode.id}-${idx}`,
                source,
                sourcePort: c.source_port,
                target,
                targetPort: c.target_port
            });
        });

        setNodes(prev => {
             return prev.map(n => {
                 if (n.id === nodeId) {
                     return { ...n, children: newNodes.map(child => child.id) };
                 }
                 return n;
             }).concat(newNodes);
        });
        setLinks(prev => [...prev, ...newLinks]);
        setParseStatus(`Expanded ${currentNode.className}: +${newNodes.length} nodes, +${newLinks.length} links.`);

    } catch (err: any) {
        setError(err.message);
        setParseStatus(`Failed to parse ${currentNode.className}.`);
        setNodes(prev => prev.map(n => n.id === nodeId ? { ...n, expanded: false } : n));
    } finally {
        setLoading(false);
    }
  };

  const handleCollapse = (nodeId: string) => {
      setNodes(prev => prev.map(n => n.id === nodeId ? { ...n, expanded: false } : n));
  };

  const handleToggleFixed = (nodeId: string, isFixed: boolean, currentX?: number, currentY?: number) => {
      setNodes(prev => prev.map(n => {
          if (n.id === nodeId) return { ...n, fixed: isFixed, x: currentX ?? n.x, y: currentY ?? n.y };
          return n;
      }));
  };

  // NEW: Sync function to keep state updated with D3 drag
  const handleNodeMove = (nodeId: string, x: number, y: number) => {
      setNodes(prev => prev.map(n => {
          if (n.id === nodeId) {
              return { ...n, x, y };
          }
          return n;
      }));
  };

  const handleExport = () => {
      if (graphVisualizerRef.current) graphVisualizerRef.current.exportImage();
  };

  const togglePanel = (panel: PanelName) => {
      setCollapsedPanels(prev => ({ ...prev, [panel]: !prev[panel] }));
  };

  const handlePanelResizeStart = useCallback((panel: PanelName, event: React.PointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      dragStateRef.current = {
          panel,
          startX: event.clientX,
          startWidth: panelWidths[panel],
          pointerId: event.pointerId
      };
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
  }, [panelWidths]);

  useEffect(() => {
      const handlePointerMove = (event: PointerEvent) => {
          const dragState = dragStateRef.current;
          if (!dragState) return;
          const bounds = PANEL_BOUNDS[dragState.panel];
          const rawWidth = dragState.startWidth + event.clientX - dragState.startX;
          const clampedWidth = Math.min(bounds.max, Math.max(bounds.min, rawWidth));
          setPanelWidths(prev => ({ ...prev, [dragState.panel]: clampedWidth }));
      };

      const finishDrag = (event: PointerEvent) => {
          const dragState = dragStateRef.current;
          if (!dragState) return;
          const bounds = PANEL_BOUNDS[dragState.panel];
          const rawWidth = dragState.startWidth + event.clientX - dragState.startX;
          if (rawWidth < bounds.collapseBelow) {
              setCollapsedPanels(prev => ({ ...prev, [dragState.panel]: true }));
              setPanelWidths(prev => ({ ...prev, [dragState.panel]: bounds.min }));
          }
          dragStateRef.current = null;
          document.body.style.cursor = '';
          document.body.style.userSelect = '';
      };

      window.addEventListener('pointermove', handlePointerMove);
      window.addEventListener('pointerup', finishDrag);
      window.addEventListener('pointercancel', finishDrag);
      return () => {
          window.removeEventListener('pointermove', handlePointerMove);
          window.removeEventListener('pointerup', finishDrag);
          window.removeEventListener('pointercancel', finishDrag);
          document.body.style.cursor = '';
          document.body.style.userSelect = '';
      };
  }, []);

  const visibleNodes = useMemo(() => {
      return nodes.filter(n => {
          if (n.id === 'root') return true; 
          const parent = nodes.find(p => p.id === n.parent);
          return parent && parent.expanded;
      });
  }, [nodes]);

  const visibleLinks = useMemo(() => {
      return links.filter(l => {
         const sourceVisible = visibleNodes.find(n => n.id === l.source);
         const targetVisible = visibleNodes.find(n => n.id === l.target);
         return sourceVisible && targetVisible;
      });
  }, [links, visibleNodes]);
  const structureNodes = proposedGraph?.nodes || visibleNodes;
  const structureLinks = proposedGraph?.links || visibleLinks;
  const structureRootModel = proposedGraph?.root_model || rootModelName;
  const structureLifecycle = proposedGraph
      ? structureReviewState === 'approved_building'
        ? 'building'
        : structureReviewState === 'revising'
          ? 'revising'
          : structureReviewState === 'finalizing'
            ? 'finalizing'
            : structureReviewState === 'build_stopped'
              ? 'stopped'
              : 'proposed'
      : currentProjectId
        ? 'implemented'
        : null;
  const structurePresentation = structureLifecyclePresentation(
      structureReviewState,
      Boolean(currentProjectId)
  );
  const selectedStructureNode = selectedSourceNode
      ? structureNodes.find(node => node.id === selectedSourceNode.id) || null
      : null;
  const structureSourcePaths = useMemo<Record<string, string | null>>(() => (
      Object.fromEntries(structureNodes.map(node => [
          node.id,
          resolveClassSourcePath(node.className, modelInfo, files)
      ]))
  ), [structureNodes, modelInfo, files]);

  useEffect(() => {
      if (mainTab !== 'structure' || structureNodes.length === 0 || selectedStructureNode) return;
      const root = rootStructureNode(structureNodes, structureRootModel) as GraphNode | null;
      if (root) setSelectedSourceNode(root);
  }, [mainTab, structureNodes, structureRootModel, selectedStructureNode]);

  const activityPreviewDisplayPath = useMemo(() => {
      if (!activityFilePreview) return null;
      return resolveActivityPreviewPath(activityFilePreview.path, Object.keys(files));
  }, [activityFilePreview, files]);

  const displayedFiles = useMemo<FileMap>(() => {
      if (!activityFilePreview || !activityPreviewDisplayPath) return files;
      return {
          ...files,
          [activityPreviewDisplayPath]: activityFilePreview.content
      };
  }, [activityFilePreview, activityPreviewDisplayPath, files]);

  useEffect(() => {
      if (!activityFilePreview || !activityPreviewDisplayPath) return;
      setSelectedSourceNode(null);
      setSelectedFilePath(activityPreviewDisplayPath);
  }, [activityFilePreview, activityPreviewDisplayPath]);

  const keyModuleFilePaths = useMemo(() => getKeyModuleFilePaths(modelInfo, files), [modelInfo, files]);
  const selectableFiles = useMemo(() => sortSourceFiles(
      Object.keys(displayedFiles).filter(path => isDisplayableSourceFile(path) && !isKnownNoiseFile(path)),
      keyModuleFilePaths
  ), [displayedFiles, keyModuleFilePaths]);

  const handleGraphNodeSelect = useCallback((node: GraphNode) => {
      setSelectedSourceNode(node);
      if (proposedGraph) {
          setSelectedFilePath(null);
          return;
      }
      const sourcePath = resolveClassSourcePath(node.className, modelInfo, files);
      setSelectedFilePath(sourcePath || null);
  }, [files, modelInfo, proposedGraph]);

  const handleOpenStructureSource = useCallback((path: string) => {
      setSelectedFilePath(path);
      setMainTab('files');
  }, []);

  const handleFileSelect = useCallback((path: string) => {
      if (activityFilePreviewRef.current && path !== activityPreviewDisplayPath) {
          activityFilePreviewRef.current = null;
          setActivityFilePreview(null);
      }
      setSelectedFilePath(path);
      const normalizedPath = normalizeFilePath(path);
      setSelectedSourceNode(visibleNodes.find(node => {
          const sourcePath = resolveClassSourcePath(node.className, modelInfo, files);
          return sourcePath && normalizeFilePath(sourcePath) === normalizedPath;
      }) || null);
  }, [activityPreviewDisplayPath, files, modelInfo, visibleNodes]);

  const handleActivityFileSelect = useCallback(async (requestId: string, filePath: string) => {
      if (!currentSessionId) return;
      const previewRequest = ++activityFileRequestRef.current;
      setMainTab('files');
      setError(null);
      try {
          const preview = await getRequestActivityFile(currentSessionId, requestId, filePath);
          if (previewRequest !== activityFileRequestRef.current) return;
          const previewState = activityPreviewFileState(preview);
          activityFilePreviewRef.current = preview;
          setActivityFilePreview(preview);
          loadFilesIntoState(previewState.files, undefined, {
              preserveActivityPreview: true
          });
          setSelectedSourceNode(null);
          setSelectedFilePath(previewState.selectedPath);
      } catch (err: any) {
          if (previewRequest !== activityFileRequestRef.current) return;
          setError(err.message || 'This generated file is no longer available to preview.');
      }
  }, [currentSessionId]);

  const handlePendingStructureChange = useCallback((
      interaction: PendingInteraction | null,
      state: StructureReviewState | 'clear' = interaction ? 'awaiting_review' : 'clear',
      owner?: { sessionId: string; requestId: string }
  ) => {
      if (state === 'clear' || !interaction) {
          const currentOwner = architectureProjectionOwnerRef.current;
          if (
              owner
              && currentOwner
              && (
                  owner.sessionId !== currentOwner.sessionId
                  || owner.requestId !== currentOwner.requestId
              )
          ) return;
          architectureProjectionOwnerRef.current = null;
          setArchitectureProjectionOwner(null);
          setProposedGraph(null);
          setStructureReviewState(null);
          setSelectedSourceNode(null);
          return;
      }
      if (owner) {
          architectureProjectionOwnerRef.current = owner;
          setArchitectureProjectionOwner(owner);
          // Chat may learn about a newly submitted request before the slower
          // session-list poll. Record that ownership locally so a later
          // terminal active_request_id transition is observable even if the
          // Conversation view is hidden in the meantime.
          setSessions(current => current.map(session => (
              session.session_id === owner.sessionId
                  && session.active_request_id !== owner.requestId
                ? { ...session, active_request_id: owner.requestId }
                : session
          )));
      }
      const graph = interaction ? graphFromStructureReview(interaction) : null;
      setProposedGraph(graph);
      setStructureReviewState(state);
      setSelectedSourceNode(
          graph ? rootStructureNode(graph.nodes, graph.root_model) as GraphNode | null : null
      );
      if (graph) {
          setSelectedFilePath(null);
          setMainTab('structure');
      }
  }, []);

  const retryFinalImplementedGraph = async () => {
      if (
          !currentSessionId
          || !currentProjectId
          || currentProjectId.startsWith('local-')
      ) {
          setError('The generated simulation is not available for final Structure loading.');
          return;
      }

      setLoading(true);
      setError(null);
      setParseStatus(`Retrying the implemented Structure for ${currentProjectName || currentProjectId}...`);
      try {
          const response = await getSessionProjectGraph(
              currentSessionId,
              currentProjectId,
              true
          );
          const implementedGraphReady = applyBackendGraphResponse(response)
              || await waitForBackendGraph(currentSessionId, currentProjectId);
          if (implementedGraphReady) {
              // Keep the approved projection visible until the complete
              // source-derived graph has been applied successfully.
              handlePendingStructureChange(null, 'clear');
          } else {
              setError('The implemented Structure is still being prepared. Choose Refresh to try again.');
          }
      } catch (graphError: any) {
          setError(graphError.message || 'The implemented Structure could not be loaded yet. Choose Refresh to try again.');
      } finally {
          setLoading(false);
      }
  };

  const handleProposedNodeMove = useCallback((nodeId: string, x: number, y: number) => {
      setProposedGraph(current => current ? {
          ...current,
          nodes: current.nodes.map(node => node.id === nodeId ? { ...node, x, y } : node)
      } : current);
  }, []);

  const handleProposedToggleFixed = useCallback((nodeId: string, isFixed: boolean, currentX?: number, currentY?: number) => {
      setProposedGraph(current => current ? {
          ...current,
          nodes: current.nodes.map(node => node.id === nodeId
            ? { ...node, fixed: isFixed, x: currentX ?? node.x, y: currentY ?? node.y }
            : node)
      } : current);
  }, []);

  const currentSession = useMemo(
      () => sessions.find(session => session.session_id === currentSessionId),
      [sessions, currentSessionId]
  );

  // Keep the latest project handoff callback behind a ref so terminal polling
  // is tied only to request ownership, not to unrelated App renders.
  useEffect(() => {
      handleProjectsUpdatedRef.current = handleProjectsUpdated;
  });

  // Terminal build ownership belongs to App, which remains mounted when the
  // Conversation panel is collapsed. ChatInterface continues to poll messages
  // and review state while visible, but it no longer owns project/graph side
  // effects. The handled set makes this exact-request transition idempotent.
  useEffect(() => {
      const owner = architectureProjectionOwner;
      if (!owner) return;

      let cancelled = false;
      const handledKey = `${owner.sessionId}:${owner.requestId}`;
      const pollOwnerRequest = async () => {
          if (
              cancelled
              || architectureTerminalPollInFlightRef.current
              || handledArchitectureTerminalRequestsRef.current.has(handledKey)
          ) return;
          architectureTerminalPollInFlightRef.current = true;
          try {
              const request = await getSessionRequest(owner.sessionId, owner.requestId);
              if (
                  cancelled
                  || architectureProjectionOwnerRef.current?.sessionId !== owner.sessionId
                  || architectureProjectionOwnerRef.current?.requestId !== owner.requestId
              ) return;

              const handoff = architectureTerminalHandoff(owner, request);
              if (!handoff) return;
              if (handoff.state === 'build_stopped') {
                  setStructureReviewState('build_stopped');
                  handledArchitectureTerminalRequestsRef.current.add(handledKey);
                  return;
              }
              if (handoff.state === 'clear') {
                  handledArchitectureTerminalRequestsRef.current.add(handledKey);
                  handlePendingStructureChange(null, 'clear', owner);
                  return;
              }

              setStructureReviewState('finalizing');
              await handleProjectsUpdatedRef.current(handoff.updatedProjects);
              if (!cancelled) {
                  handledArchitectureTerminalRequestsRef.current.add(handledKey);
              }
          } catch (pollError) {
              // A transient request/graph failure must remain retryable. The
              // visible approved architecture is intentionally left intact.
              console.warn('Could not complete the generated Structure handoff yet.', pollError);
          } finally {
              architectureTerminalPollInFlightRef.current = false;
          }
      };

      void pollOwnerRequest();
      const interval = window.setInterval(
          pollOwnerRequest,
          ARCHITECTURE_REQUEST_POLL_INTERVAL_MS
      );
      return () => {
          cancelled = true;
          window.clearInterval(interval);
      };
  }, [architectureProjectionOwner, handlePendingStructureChange]);

  useEffect(() => {
      if (!shouldClearArchitectureProjection(
          architectureProjectionOwnerRef.current,
          {
              sessionId: currentSessionId,
              activeRequestId: currentSession?.active_request_id
          }
      )) return;
      handlePendingStructureChange(null, 'clear');
  }, [currentSessionId, currentSession?.active_request_id, handlePendingStructureChange]);

  useEffect(() => {
      const activeRequestId = currentSession?.active_request_id || null;
      if (activeRequestId && activeRequestId !== lastActiveRequestIdRef.current) {
          // A new generation should follow its own simulation by default, even
          // if the student manually inspected an older simulation beforehand.
          manualProjectSelectionRef.current = false;
      }
      lastActiveRequestIdRef.current = activeRequestId;
  }, [currentSessionId, currentSession?.active_request_id]);

  const designLabel = currentSessionId
      ? currentSession?.title || currentSessionId
      : 'Start a new simulation to begin';
  const conversationPanelWidth = panelWidths.conversation;
  const currentProject = allProjects.find(project => project.project_id === currentProjectId) || null;

  if (authState === 'checking') {
    return (
      <div className="flex h-full min-h-0 w-full items-center justify-center overflow-hidden bg-slate-100 text-sm text-slate-500">
        Loading...
      </div>
    );
  }

  if (authState === 'required') {
    return <LoginScreen error={authError} loading={authLoading} onSubmit={handleLogin} />;
  }

  return (
    <div className="flex h-full min-h-0 w-full overflow-hidden bg-slate-100">
      {collapsedPanels.conversation ? (
        <CollapsedPanelButton
          title="Conversation"
          icon={<MessageSquare size={16} />}
          onClick={() => togglePanel('conversation')}
        />
      ) : (
        <aside className="relative flex-shrink-0 border-r border-slate-200 bg-white max-[800px]:absolute max-[800px]:inset-y-0 max-[800px]:left-0 max-[800px]:z-50 max-[800px]:shadow-xl" style={{ width: conversationPanelWidth }}>
          <div className="absolute right-2 top-2 z-10">
            <PanelToolbar
              title="Conversation"
              collapsed={false}
              onToggle={() => togglePanel('conversation')}
            />
          </div>
          <PanelResizeHandle title="Conversation" panel="conversation" onResizeStart={handlePanelResizeStart} />
          <div className={conversationMode === 'chat' && currentSessionId ? 'h-full' : 'hidden'}>
            {currentSessionId && (
              <ChatInterface
                sessionId={currentSessionId}
                activeRequestId={currentSession?.active_request_id}
                activeProjectId={currentProjectId}
                currentProjectName={currentProjectName}
                currentSessionTitle={currentSession?.title || currentSessionId}
                onActivityFileSelect={handleActivityFileSelect}
                onPendingStructureChange={handlePendingStructureChange}
                onReviewStructure={() => setMainTab('structure')}
                defaultGenerationMode={frontendConfig?.default_generation_mode || 'guided'}
                isOpen={true}
                onBack={() => setConversationMode('history')}
              />
            )}
          </div>
          <div className={conversationMode === 'chat' && currentSessionId ? 'hidden' : 'h-full'}>
            <SessionSelectorPanel
              sessions={sessions}
              currentSessionId={currentSessionId}
              onCreateSession={handleCreateSession}
              onRefreshSessions={refreshSessionList}
              onSelectSessionId={selectSessionById}
              onRenameSession={handleRenameSession}
              onDeleteSession={handleDeleteSession}
            />
          </div>
        </aside>
      )}

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-10 min-h-10 shrink-0 items-center justify-between gap-3 border-b border-slate-200 bg-white px-3">
          <div className="flex min-w-0 items-center gap-2">
            <Cpu size={16} aria-hidden="true" className="shrink-0 text-blue-600" />
            <h1 className="shrink-0 text-sm font-semibold text-slate-800">DEVS Generator</h1>
            <span
              className="hidden min-w-0 truncate border-l border-slate-200 pl-2 text-xs text-slate-500 min-[560px]:block"
              title={`Current design: ${designLabel}`}
            >
              {designLabel}
            </span>
          </div>
          {error && (
            <div role="status" aria-live="polite" className="flex min-w-0 items-center gap-1 text-xs text-red-600" title={error}>
              <AlertCircle size={14} aria-hidden="true" />
              <span className="max-w-72 truncate">{error}</span>
            </div>
          )}
        </header>

        <section className="flex min-h-0 flex-1 flex-col bg-slate-50">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 bg-white px-3 py-2">
            <div className="flex min-w-0 flex-1 items-center gap-2">
              <label className="hidden text-[10px] font-semibold uppercase tracking-wide text-slate-500 sm:block">Simulation</label>
              <div className="relative min-w-[180px] max-w-md flex-1">
                <select
                  value={proposedGraph ? '__active_architecture__' : currentProjectId || ''}
                  onChange={handleProjectSelect}
                  disabled={!currentSessionId || Boolean(proposedGraph)}
                  title={proposedGraph
                    ? 'The Structure view belongs to the simulation currently being generated.'
                    : 'Choose a generated simulation'}
                  className="h-9 w-full appearance-none rounded border border-slate-300 bg-white py-0 pl-3 pr-8 text-sm font-medium text-slate-800 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100 disabled:bg-slate-100 disabled:text-slate-400"
                >
                  {proposedGraph && <option value="__active_architecture__">Current generation architecture</option>}
                  <option value="">{currentSessionId ? 'Choose a generated simulation' : 'Start a design first'}</option>
                  {allProjects.map(project => <option key={project.project_id} value={project.project_id}>{project.display_name}{project.project_id.startsWith('local-') ? ' (local)' : ''}</option>)}
                </select>
                <ChevronDown size={14} className="pointer-events-none absolute right-2 top-2.5 text-slate-400" />
              </div>
              <button onClick={() => refreshProjectList()} disabled={!currentSessionId} className="rounded border border-slate-200 p-2 text-slate-500 hover:bg-slate-50 hover:text-blue-600 disabled:opacity-40" title="Refresh simulations"><RefreshCw size={15} /></button>
              <label className={`flex h-9 cursor-pointer items-center gap-2 rounded border border-slate-200 px-3 text-xs font-medium text-slate-600 hover:bg-slate-50 ${!currentSessionId ? 'pointer-events-none opacity-40' : ''}`} title="Upload an existing simulation folder">
                <Upload size={14} /><span className="hidden md:inline">Upload</span>
                <input type="file" multiple {...({ webkitdirectory: '', directory: '' } as any)} className="hidden" onChange={handleFileUpload} disabled={!currentSessionId} />
              </label>
              {!proposedGraph && currentProject && <span className={`hidden rounded-full px-2 py-1 text-[10px] font-semibold sm:inline ${currentProject.status === 'ready' ? 'bg-emerald-50 text-emerald-700' : currentProject.status === 'error' ? 'bg-red-50 text-red-700' : 'bg-blue-50 text-blue-700'}`}>{PROJECT_STATUS_LABEL[currentProject.status]}</span>}
            </div>
          </div>

          <div className="flex min-h-[46px] items-center justify-between gap-3 border-b border-slate-200 bg-white px-3">
            <nav className="flex h-full items-end gap-1" aria-label="Simulation views">
              {([
                ['files', 'Files', <Code2 key="files-icon" size={14} />],
                ['structure', 'Structure', <Network key="structure-icon" size={14} />],
                ['run', 'Run', <Play key="run-icon" size={14} />]
              ] as const).map(([tab, label, icon]) => (
                <button key={tab} onClick={() => setMainTab(tab)} className={`flex h-10 items-center gap-2 border-b-2 px-3 text-xs font-semibold ${mainTab === tab ? 'border-blue-600 text-blue-700' : 'border-transparent text-slate-500 hover:text-slate-800'}`}>{icon}{label}</button>
              ))}
            </nav>
            <div className="min-w-0 truncate text-right text-[11px] text-slate-500">
              {structurePresentation
                ? structurePresentation.label
                : currentProjectName || 'No simulation selected'}
            </div>
          </div>

          <div className="relative min-h-0 flex-1 overflow-hidden">
            {mainTab === 'structure' && (
              (currentProjectId || proposedGraph) && structureLifecycle && structurePresentation ? (
                <div className="flex h-full min-h-0 flex-col bg-slate-50">
                  <header className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-200 bg-white px-4 py-2.5">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={`flex items-center gap-1.5 rounded px-2 py-1 text-xs font-semibold ${structureLifecycle === 'implemented' ? 'bg-emerald-50 text-emerald-800' : structureLifecycle === 'stopped' ? 'bg-amber-50 text-amber-800' : structureLifecycle === 'building' || structureLifecycle === 'finalizing' ? 'bg-blue-50 text-blue-800' : 'bg-purple-50 text-purple-800'}`}>
                          <Network size={13} /> {structurePresentation.label}
                        </span>
                        <span className="text-[11px] leading-5 text-slate-600">{structurePresentation.instruction}</span>
                      </div>
                      {structurePresentation.scope && <p className="mt-1 text-[10px] leading-4 text-slate-500">{structurePresentation.scope}</p>}
                      {structureLifecycle === 'implemented' && parseStatus && <p className="mt-1 truncate text-[10px] text-slate-400" title={parseStatus}>{parseStatus}</p>}
                    </div>
                    <div className="flex shrink-0 items-center gap-1.5">
                      {canRefreshStructure(structureLifecycle) && (
                        <button
                          onClick={structureLifecycle === 'finalizing' ? retryFinalImplementedGraph : initializeGraph}
                          disabled={loading || (structureLifecycle === 'implemented' && !modelInfo)}
                          className="flex items-center gap-1.5 rounded border border-slate-200 px-2 py-1.5 text-[10px] font-medium text-slate-600 hover:bg-slate-50 disabled:opacity-40"
                        >
                          <RefreshCw size={12} className={loading ? 'animate-spin' : ''} /> Refresh
                        </button>
                      )}
                      <button onClick={handleExport} disabled={!structureNodes.length} className="rounded border border-slate-200 px-2 py-1.5 text-[10px] font-medium text-slate-600 hover:bg-slate-50 disabled:opacity-40">Export image</button>
                    </div>
                  </header>
                  <div className="flex min-h-0 flex-1">
                    <section className="min-w-0 flex-1 bg-slate-50 p-3">
                      <GraphVisualizer
                        ref={graphVisualizerRef}
                        nodes={structureNodes}
                        links={structureLinks}
                        physicsEnabled={physicsEnabled}
                        selectedNodeId={selectedStructureNode?.id || null}
                        onExpand={proposedGraph ? () => {} : handleExpand}
                        onCollapse={proposedGraph ? () => {} : handleCollapse}
                        onToggleFixed={proposedGraph ? handleProposedToggleFixed : handleToggleFixed}
                        onNodeMove={proposedGraph ? handleProposedNodeMove : handleNodeMove}
                        onNodeSelect={handleGraphNodeSelect}
                      />
                    </section>
                    <StructureInspector
                      lifecycle={structureLifecycle}
                      nodes={structureNodes}
                      links={structureLinks}
                      rootModel={structureRootModel}
                      selectedNode={selectedStructureNode}
                      sourcePaths={structureSourcePaths}
                      onSelect={handleGraphNodeSelect}
                      onOpenSource={handleOpenStructureSource}
                    />
                  </div>
                </div>
              ) : <div className="flex h-full items-center justify-center p-6"><div className="max-w-md rounded-xl border border-dashed border-slate-300 bg-white p-8 text-center"><Network className="mx-auto mb-3 text-slate-400" size={28} /><h2 className="font-semibold text-slate-800">No simulation selected</h2><p className="mt-2 text-sm leading-6 text-slate-500">Ask the agent to generate one, then choose it above to explore its structure.</p></div></div>
            )}

            {mainTab === 'run' && <SimulationRunPanel sessionId={currentSessionId} simulationId={currentProjectId} simulationName={currentProjectName} />}

            {mainTab === 'files' && (
              <div className="flex h-full min-h-0 flex-col bg-white md:flex-row">
                <aside className="h-48 flex-shrink-0 overflow-hidden border-b border-slate-200 bg-slate-50 md:h-full md:w-64 md:border-b-0 md:border-r">
                  <div className="border-b border-slate-200 bg-white px-3 py-3 text-xs font-semibold text-slate-700">Simulation files <span className="ml-1 font-normal text-slate-400">{selectableFiles.length}</span></div>
                  <div className="h-[calc(100%-42px)] overflow-y-auto"><FileTreeBrowser filePaths={selectableFiles} keyModuleFilePaths={keyModuleFilePaths} selectedFilePath={selectedFilePath} onFileSelect={handleFileSelect} /></div>
                </aside>
                <section className="min-h-0 min-w-0 flex-1">
                  <SourcePreviewPanel selectedNode={selectedSourceNode} selectedFilePath={selectedFilePath} modelInfo={modelInfo} files={displayedFiles} workspace />
                </section>
              </div>
            )}
          </div>
        </section>
      </main>
    </div>
  );
};

export default App;
