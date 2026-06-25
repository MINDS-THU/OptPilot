import React, { useState, useMemo, useRef, useEffect, useCallback } from 'react';
import { AlertCircle, Cpu, Folder, MessageSquare, PanelLeftClose, PanelLeftOpen, Rows3 } from 'lucide-react';
import { GraphVisualizer, GraphVisualizerHandle } from './components/GraphVisualizer';
import { ChatInterface } from './components/ChatInterface';
import { SessionSelectorPanel } from './components/SessionSelectorPanel';
import { ProjectPanel } from './components/ProjectPanel';
import { VisualizationControls } from './components/VisualizationControls';
import { SourcePreviewPanel } from './components/SourcePreviewPanel';
import { parseModelCode } from './services/geminiService';
import {
  createSession,
  deleteSession,
  getAuthStatus,
  getSessionProjectGraph,
  getSessionProjects,
  getSessions,
  getSessionProjectFiles,
  getStoredAuthToken,
  isUnauthorizedError,
  loginWithPassword,
  renameSession,
  uploadSessionProject
} from './services/agentService';
import { SystemModelInfo, FileMap, GraphNode, GraphLink, ParsedStructure, ProjectInfo, SessionInfo, ProjectGraphResponse } from './types';

// Default dimension constants
const NODE_WIDTH = 180;
const NODE_HEIGHT = 100;
const PANEL_BOUNDS = {
  sessions: { min: 220, max: 420, default: 288, collapseBelow: 170 },
  chat: { min: 300, max: 640, default: 416, collapseBelow: 240 },
  project: { min: 240, max: 460, default: 320, collapseBelow: 190 }
};

type PanelName = keyof typeof PANEL_BOUNDS;
type AuthState = 'checking' | 'authenticated' | 'required';

const sortSessionsByRecentActivity = (sessionList: SessionInfo[]): SessionInfo[] => {
  return [...sessionList].sort((a, b) => {
    const bTime = Date.parse(b.updated_at || b.created_at || '') || 0;
    const aTime = Date.parse(a.updated_at || a.created_at || '') || 0;
    return bTime - aTime;
  });
};

const SESSION_REFRESH_INTERVAL_MS = 15000;
const GRAPH_PARSE_POLL_INTERVAL_MS = 2000;
const GRAPH_PARSE_MAX_POLL_ATTEMPTS = 300;
const GRAPH_PARSE_PROVIDER = 'openai';
const GRAPH_PARSE_MODEL = 'openrouter/openai/gpt-5.4-mini';

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
    <div className="flex h-screen w-full items-center justify-center bg-slate-100 px-4">
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
            HAMLET Workspace
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
    if (paths.length === 0) return { name: 'Empty Project', files: rawFiles };

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

    const inferredName = commonPrefix ? commonPrefix.slice(0, -1) : 'Uploaded Project';
    return { name: inferredName, files: cleanedFiles };
};

const App: React.FC = () => {
  // --- Project Management State ---
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<string>('');
  const [remoteProjects, setRemoteProjects] = useState<ProjectInfo[]>([]);
  const [localProjects, setLocalProjects] = useState<ProjectInfo[]>([]); // Track manually uploaded projects
  const [currentProjectId, setCurrentProjectId] = useState<string | null>(null);
  const [currentProjectName, setCurrentProjectName] = useState<string | null>(null);
  const [projectCache, setProjectCache] = useState<Record<string, FileMap>>({});
  const [collapsedPanels, setCollapsedPanels] = useState<Record<PanelName, boolean>>({
      sessions: false,
      chat: false,
      project: false
  });
  const [panelWidths, setPanelWidths] = useState<Record<PanelName, number>>({
      sessions: PANEL_BOUNDS.sessions.default,
      chat: PANEL_BOUNDS.chat.default,
      project: PANEL_BOUNDS.project.default
  });
  const dragStateRef = useRef<{
      panel: PanelName;
      startX: number;
      startWidth: number;
      pointerId: number;
  } | null>(null);

  // --- Current Project Data ---
  const [modelInfo, setModelInfo] = useState<SystemModelInfo | null>(null);
  const [files, setFiles] = useState<FileMap>({});
  const [rootModelName, setRootModelName] = useState<string>('');
  
  // Settings
  const [physicsEnabled, setPhysicsEnabled] = useState<boolean>(true);

  // Graph State
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [links, setLinks] = useState<GraphLink[]>([]);
  const [selectedSourceNode, setSelectedSourceNode] = useState<GraphNode | null>(null);
  const [graphSource, setGraphSource] = useState<'backend' | 'local' | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [parseStatus, setParseStatus] = useState<string>('');
  const [authState, setAuthState] = useState<AuthState>('checking');
  const [authLoading, setAuthLoading] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);

  // Refs
  const nodesRef = useRef<GraphNode[]>([]);
  useEffect(() => { nodesRef.current = nodes; }, [nodes]);

  const shouldAutoRefreshRef = useRef(false);
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
          const sessionList = sortSessionsByRecentActivity(await getSessions());
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

  const refreshProjectList = async (sessionId = currentSessionId) => {
      if (!sessionId) {
          setRemoteProjects([]);
          return;
      }
      try {
          const projs = await getSessionProjects(sessionId);
          setRemoteProjects(projs);
      } catch (err) {
          if (isUnauthorizedError(err)) {
              setAuthState('required');
              setAuthError('Password required.');
              return;
          }
          console.warn("Backend offline or unreachable, using local mode.", err);
          setRemoteProjects([]);
      }
  };

  // Merge lists, removing duplicates
  const allProjects = useMemo(() => {
      const byId = new Map<string, ProjectInfo>();
      [...remoteProjects, ...localProjects].forEach(project => byId.set(project.project_id, project));
      return Array.from(byId.values());
  }, [remoteProjects, localProjects]);

  // --- File Loading Logic ---

  const loadFilesIntoState = (newFiles: FileMap, project?: ProjectInfo) => {
    const info = getModelInfoFromFiles(newFiles);
    const newRoot = info ? detectRootModel(info) : '';

    setFiles(newFiles);
    setModelInfo(info);
    setRootModelName(newRoot);
    setSelectedSourceNode(null);
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
      setCurrentSessionId(sessionId);
      setLocalProjects([]);
      setProjectCache({});
      setCurrentProjectId(null);
      setCurrentProjectName(null);
      handleClearProject();
      await refreshProjectList(sessionId);
  };

  const handleCreateSession = async () => {
      try {
          const title = `Session ${new Date().toLocaleString()}`;
          const result = await createSession(title);
          const refreshedSessions = await refreshSessionList();
          if (!refreshedSessions.some(session => session.session_id === result.session.session_id)) {
              setSessions(prev => [result.session, ...prev.filter(session => session.session_id !== result.session.session_id)]);
          }
          setCurrentSessionId(result.session.session_id);
          setRemoteProjects(result.projects || []);
          setLocalProjects([]);
          setProjectCache({});
          setCurrentProjectId(null);
          setCurrentProjectName(null);
          handleClearProject();
      } catch (err: any) {
          setError(err.message || "Failed to create session.");
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
          setError(err.message || "Failed to rename session.");
      }
  };

  const handleDeleteSession = async (session: SessionInfo) => {
      const title = session.title || session.session_id;
      const confirmed = window.confirm(
          `Delete session "${title}"?\n\nThis removes the session from HAMLET. Automatically-created session workspaces will also be deleted.`
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
          setError(err.message || "Failed to delete session.");
      }
  };

  const fetchAndLoadProject = async (project: ProjectInfo) => {
      if (!currentSessionId && !project.project_id.startsWith('local-')) {
          setError("Create or select a session before loading backend projects.");
          return;
      }
      setLoading(true);
      setError(null);
      try {
          // 1. Check Cache (Priority for Local/Offline)
          if (projectCache[project.project_id]) {
              console.log("Loading from cache:", project.display_name);
              loadFilesIntoState(projectCache[project.project_id], project);
          } else {
              // 2. Fetch from backend
              console.log("Fetching from backend:", project.display_name);
              try {
                const projectFiles = await getSessionProjectFiles(currentSessionId, project.project_id);
                setProjectCache(prev => ({ ...prev, [project.project_id]: projectFiles }));
                loadFilesIntoState(projectFiles, project);
              } catch (fetchErr) {
                // Backend failed and no cache
                throw new Error("Could not load project. Backend offline and no local copy.");
              }
          }
      } catch (err: any) {
          setError(err.message || `Failed to load project: ${project.display_name}`);
      } finally {
          setLoading(false);
      }
  };

  // Handle updates from Agent Chat
  const handleProjectsUpdated = async (updatedIdsOrNames: string[]) => {
      // 1. Refresh list of projects available
      await refreshProjectList();

      // 2. Invalidate caches for updated projects
      setProjectCache(prev => {
          const newCache = { ...prev };
          updatedIdsOrNames.forEach(id => delete newCache[id]);
          return newCache;
      });

      // 3. If current project was updated, force reload
      if (currentProjectId && (updatedIdsOrNames.includes(currentProjectId) || (currentProjectName && updatedIdsOrNames.includes(currentProjectName)))) {
          const project = allProjects.find(p => p.project_id === currentProjectId);
          if (project) await fetchAndLoadProject(project);
      }
  };

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
      setError("Create or select a session before uploading a project.");
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
        setError(`Project "${inferredName}" loaded locally. (Backend sync failed)`);
    }
    setLoading(false);
  };

  const handleClearProject = () => {
      setFiles({});
      setModelInfo(null);
      setRootModelName('');
      setNodes([]);
      setLinks([]);
      setSelectedSourceNode(null);
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
      setError("Please load a project.");
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

  const currentSession = useMemo(
      () => sessions.find(session => session.session_id === currentSessionId),
      [sessions, currentSessionId]
  );

  const workspaceLabel = currentSessionId
      ? `Session: ${currentSession?.title || currentSessionId}${currentProjectName ? ` / Project: ${currentProjectName}` : ''}`
      : 'Create or select a session to begin';
  const sessionPanelWidth = panelWidths.sessions;
  const chatPanelWidth = panelWidths.chat;
  const projectPanelWidth = panelWidths.project;

  if (authState === 'checking') {
    return (
      <div className="flex h-screen w-full items-center justify-center bg-slate-100 text-sm text-slate-500">
        Loading...
      </div>
    );
  }

  if (authState === 'required') {
    return <LoginScreen error={authError} loading={authLoading} onSubmit={handleLogin} />;
  }

  return (
    <div className="flex h-screen w-full overflow-hidden bg-slate-100">
      {collapsedPanels.sessions ? (
        <CollapsedPanelButton
          title="Sessions"
          icon={<Rows3 size={16} />}
          onClick={() => togglePanel('sessions')}
        />
      ) : (
        <div className="relative flex-shrink-0" style={{ width: sessionPanelWidth }}>
          <div className="absolute right-2 top-2 z-10">
            <PanelToolbar
              title="Sessions"
              collapsed={false}
              onToggle={() => togglePanel('sessions')}
            />
          </div>
          <PanelResizeHandle title="Sessions" panel="sessions" onResizeStart={handlePanelResizeStart} />
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
      )}

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-4 shadow-sm">
          <div>
            <h1 className="flex items-center gap-2 text-xl font-bold text-slate-800">
              <Cpu className="text-blue-600" />
              HAMLET Workspace
            </h1>
            <p className="text-sm text-slate-500">{workspaceLabel}</p>
          </div>
          <div className="flex items-center gap-4">
            {error && (
              <div className="flex items-center gap-1 text-sm text-red-600">
                <AlertCircle size={14} />
                {error}
              </div>
            )}
            <a href="#help" className="text-sm text-blue-600 hover:underline">Help</a>
          </div>
        </header>

        <div className="flex min-h-0 flex-1 overflow-hidden">
          {collapsedPanels.chat ? (
            <CollapsedPanelButton
              title="Chat"
              icon={<MessageSquare size={16} />}
              onClick={() => togglePanel('chat')}
            />
          ) : (
            <section className="relative flex-shrink-0 border-r border-slate-200 bg-white" style={{ width: chatPanelWidth }}>
              <div className="absolute right-2 top-2 z-10">
                <PanelToolbar
                  title="Chat"
                  collapsed={false}
                  onToggle={() => togglePanel('chat')}
                />
              </div>
              <PanelResizeHandle title="Chat" panel="chat" onResizeStart={handlePanelResizeStart} />
              <ChatInterface
                sessionId={currentSessionId}
                activeRequestId={currentSession?.active_request_id}
                activeProjectId={currentProjectId}
                currentProjectName={currentProjectName}
                onProjectsUpdated={handleProjectsUpdated}
                isOpen={true}
              />
            </section>
          )}

          <section className="flex min-w-0 flex-1 flex-col bg-slate-50">
            <div className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3">
              <div>
                <h2 className="text-sm font-semibold text-slate-800">Visualizer</h2>
                <p className="text-xs text-slate-500">
                  {currentProjectName ? `Project: ${currentProjectName}` : 'Select a project or upload one to visualize'}
                </p>
              </div>
            </div>

            <div className="flex min-h-0 flex-1 overflow-hidden">
              {collapsedPanels.project ? (
                <CollapsedPanelButton
                  title="Project"
                  icon={<Folder size={16} />}
                  onClick={() => togglePanel('project')}
                />
              ) : (
                <aside className="relative flex flex-shrink-0 flex-col border-r border-slate-200 bg-white" style={{ width: projectPanelWidth }}>
                  <div className="absolute right-2 top-2 z-10">
                    <PanelToolbar
                      title="Project"
                      collapsed={false}
                      onToggle={() => togglePanel('project')}
                    />
                  </div>
                  <PanelResizeHandle title="Project" panel="project" onResizeStart={handlePanelResizeStart} />
                  <div className="mr-3 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4 pt-11">
                    <ProjectPanel
                      currentSessionId={currentSessionId}
                      currentProjectId={currentProjectId}
                      currentProjectName={currentProjectName}
                      projects={allProjects}
                      filesLoaded={Object.keys(files).length}
                      onProjectSelect={handleProjectSelect}
                      onRefreshProjects={() => refreshProjectList()}
                      onFileUpload={handleFileUpload}
                    />

                    <VisualizationControls
                      modelInfo={modelInfo}
                      rootModelName={rootModelName}
                      parseStatus={parseStatus}
                      loading={loading}
                      hasNodes={nodes.length > 0}
                      onRefreshGraph={initializeGraph}
                      onExport={handleExport}
                    />

                    <SourcePreviewPanel
                      selectedNode={selectedSourceNode}
                      modelInfo={modelInfo}
                      files={files}
                    />
                  </div>
                </aside>
              )}

              <section className="relative flex-1 overflow-hidden bg-slate-50 p-4">
                <GraphVisualizer
                  ref={graphVisualizerRef}
                  nodes={visibleNodes}
                  links={visibleLinks}
                  physicsEnabled={physicsEnabled}
                  selectedNodeId={selectedSourceNode?.id || null}
                  onExpand={handleExpand}
                  onCollapse={handleCollapse}
                  onToggleFixed={handleToggleFixed}
                  onNodeMove={handleNodeMove}
                  onNodeSelect={setSelectedSourceNode}
                />
              </section>
            </div>
          </section>
        </div>
      </main>
    </div>
  );
};

export default App;
