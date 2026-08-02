import React, { useEffect, useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, FileText, Folder, FolderOpen } from 'lucide-react';

type TreeNode = { name: string; path: string; type: 'folder' | 'file'; children: TreeNode[]; modelFile?: boolean };

interface Props {
  filePaths: string[];
  keyModuleFilePaths: string[];
  selectedFilePath: string | null;
  onFileSelect: (path: string) => void;
}

const normalize = (path: string) => path.replace(/\\/g, '/').replace(/^\.?\//, '');

export const FileTreeBrowser: React.FC<Props> = ({
  filePaths,
  keyModuleFilePaths,
  selectedFilePath,
  onFileSelect
}) => {
  const [expanded, setExpanded] = useState<Set<string>>(new Set(['']));
  const keyPaths = useMemo(() => new Set(keyModuleFilePaths.map(normalize)), [keyModuleFilePaths]);

  const tree = useMemo(() => {
    const root: TreeNode = { name: 'Files', path: '', type: 'folder', children: [] };
    const folders = new Map<string, TreeNode>([['', root]]);
    const ensureFolder = (path: string): TreeNode => {
      const existing = folders.get(path);
      if (existing) return existing;
      const parentPath = path.split('/').slice(0, -1).join('/');
      const node: TreeNode = { name: path.split('/').pop() || path, path, type: 'folder', children: [] };
      ensureFolder(parentPath).children.push(node);
      folders.set(path, node);
      return node;
    };
    filePaths.forEach(originalPath => {
      const path = normalize(originalPath);
      const parts = path.split('/').filter(Boolean);
      const parentPath = parts.slice(0, -1).join('/');
      ensureFolder(parentPath).children.push({
        name: parts[parts.length - 1] || path,
        path: originalPath,
        type: 'file',
        children: [],
        modelFile: keyPaths.has(path)
      });
    });
    const sort = (node: TreeNode) => {
      node.children.sort((a, b) => a.type.localeCompare(b.type) || Number(Boolean(b.modelFile)) - Number(Boolean(a.modelFile)) || a.name.localeCompare(b.name));
      node.children.forEach(sort);
    };
    sort(root);
    return root;
  }, [filePaths, keyPaths]);

  useEffect(() => {
    if (!selectedFilePath) return;
    const parts = normalize(selectedFilePath).split('/').slice(0, -1);
    setExpanded(previous => {
      const next = new Set(previous);
      let path = '';
      parts.forEach(part => { path = path ? `${path}/${part}` : part; next.add(path); });
      return next;
    });
  }, [selectedFilePath]);

  const renderNode = (node: TreeNode, depth: number): React.ReactNode => {
    if (node.type === 'folder') {
      const open = expanded.has(node.path);
      return (
        <div key={node.path || 'root'}>
          {node.path && (
            <button
              className="flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left text-xs font-medium text-slate-600 hover:bg-slate-100"
              style={{ paddingLeft: `${8 + depth * 12}px` }}
              onClick={() => setExpanded(previous => {
                const next = new Set(previous);
                open ? next.delete(node.path) : next.add(node.path);
                return next;
              })}
            >
              {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
              {open ? <FolderOpen size={14} /> : <Folder size={14} />}
              <span className="truncate">{node.name}</span>
            </button>
          )}
          {(open || !node.path) && node.children.map(child => renderNode(child, node.path ? depth + 1 : 0))}
        </div>
      );
    }
    const selected = selectedFilePath === node.path;
    return (
      <button
        key={node.path}
        onClick={() => onFileSelect(node.path)}
        className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs ${selected ? 'bg-blue-600 text-white' : node.modelFile ? 'bg-blue-50 text-blue-800 hover:bg-blue-100' : 'text-slate-600 hover:bg-slate-100'}`}
        style={{ paddingLeft: `${20 + depth * 12}px` }}
        title={normalize(node.path)}
      >
        <FileText size={13} className="flex-shrink-0" />
        <span className="min-w-0 flex-1 truncate">{node.name}</span>
        {node.modelFile && <span className={`rounded px-1.5 py-0.5 text-[9px] font-semibold ${selected ? 'bg-blue-500' : 'bg-white'}`}>Model</span>}
      </button>
    );
  };

  return filePaths.length ? (
    <div className="overflow-y-auto p-2">{tree.children.map(node => renderNode(node, 0))}</div>
  ) : (
    <div className="p-4 text-xs text-slate-500">Select a simulation to browse its files.</div>
  );
};
