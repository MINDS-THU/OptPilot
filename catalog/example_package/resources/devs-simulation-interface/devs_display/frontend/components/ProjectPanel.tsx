import React from 'react';
import { Cloud, CloudOff, Folder, RefreshCw, Upload } from 'lucide-react';
import { ProjectInfo } from '../types';

interface Props {
  currentSessionId: string;
  currentProjectId: string | null;
  currentProjectName: string | null;
  projects: ProjectInfo[];
  filesLoaded: number;
  onProjectSelect: (event: React.ChangeEvent<HTMLSelectElement>) => void;
  onRefreshProjects: () => void;
  onFileUpload: (event: React.ChangeEvent<HTMLInputElement>) => void;
}

export const ProjectPanel: React.FC<Props> = ({
  currentSessionId,
  currentProjectId,
  currentProjectName,
  projects,
  filesLoaded,
  onProjectSelect,
  onRefreshProjects,
  onFileUpload
}) => {
  return (
    <div className="space-y-3 border-b border-slate-100 pb-4">
      <div className="flex items-center justify-between">
        <label className="flex items-center gap-2 text-sm font-semibold text-slate-700">
          <Folder size={16} />
          Projects
        </label>
        <button
          onClick={onRefreshProjects}
          disabled={!currentSessionId}
          className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-blue-600 disabled:cursor-not-allowed disabled:opacity-40"
          title="Refresh Project List"
        >
          <RefreshCw size={14} />
        </button>
      </div>

      {!currentSessionId && (
        <p className="text-xs text-slate-500">
          Select or create a session before choosing projects.
        </p>
      )}

      <select
        value={currentProjectId || ''}
        onChange={onProjectSelect}
        disabled={!currentSessionId}
        className="w-full rounded border border-slate-300 bg-white px-2 py-2 text-sm outline-none focus:ring-2 focus:ring-blue-200 disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-400"
      >
        <option value="">-- Select Project --</option>
        {projects.map(project => (
          <option key={project.project_id} value={project.project_id}>
            {project.display_name} {project.project_id.startsWith('local-') ? '(Local)' : ''}
          </option>
        ))}
      </select>

      <div>
        <p className="mb-1 text-[10px] text-slate-400">Upload folder to this session:</p>
        <label
          htmlFor="file-upload"
          className={`flex h-16 w-full flex-col items-center justify-center rounded border-2 border-dashed border-slate-200 bg-slate-50 ${currentSessionId ? 'cursor-pointer hover:bg-slate-100' : 'cursor-not-allowed opacity-60'}`}
        >
          <div className="flex items-center justify-center gap-2">
            <Upload className="h-4 w-4 text-slate-400" />
            <span className="text-xs text-slate-500">Upload Folder</span>
          </div>
          <input
            id="file-upload"
            type="file"
            multiple
            disabled={!currentSessionId}
            {...({ webkitdirectory: '', directory: '' } as any)}
            className="hidden"
            onChange={onFileUpload}
          />
        </label>
      </div>

      <div className="flex justify-between text-xs text-slate-500">
        <span>Files Loaded: {filesLoaded}</span>
        {currentProjectName && (
          currentProjectId?.startsWith('local-')
            ? <span className="flex items-center gap-1 font-medium text-amber-600"><CloudOff size={10} /> Local Only</span>
            : <span className="flex items-center gap-1 font-medium text-blue-600"><Cloud size={10} /> Synced</span>
        )}
      </div>
    </div>
  );
};
