import React from 'react';
import { Download, Play } from 'lucide-react';
import { SystemModelInfo } from '../types';

interface Props {
  modelInfo: SystemModelInfo | null;
  rootModelName: string;
  parseStatus: string;
  loading: boolean;
  hasNodes: boolean;
  onRefreshGraph: () => void;
  onExport: () => void;
}

export const VisualizationControls: React.FC<Props> = ({
  modelInfo,
  rootModelName,
  parseStatus,
  loading,
  hasNodes,
  onRefreshGraph,
  onExport
}) => {
  return (
    <div className="space-y-4 pt-4 border-t border-slate-100">
      <div className="space-y-2">
        <button
          onClick={onRefreshGraph}
          disabled={loading || !modelInfo || !rootModelName}
          className="w-full flex items-center justify-center gap-2 bg-blue-600 text-white py-2 rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          <Play size={16} /> {loading ? 'Parsing...' : 'Refresh Graph'}
        </button>
        {parseStatus && (
          <p className="text-xs text-slate-500">{parseStatus}</p>
        )}
      </div>

      <button
        onClick={onExport}
        disabled={!hasNodes}
        className="w-full flex items-center justify-center gap-2 bg-white text-slate-700 border border-slate-300 py-2 rounded hover:bg-slate-50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
      >
        <Download size={16} /> Export Image
      </button>
    </div>
  );
};
