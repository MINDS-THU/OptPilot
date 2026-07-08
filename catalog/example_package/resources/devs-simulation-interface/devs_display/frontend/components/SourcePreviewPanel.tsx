import React, { useMemo } from 'react';
import { Code2 } from 'lucide-react';
import Prism from 'prismjs';
import 'prismjs/components/prism-python';
import { FileMap, GraphNode, SystemModelInfo } from '../types';

interface Props {
  selectedNode: GraphNode | null;
  modelInfo: SystemModelInfo | null;
  files: FileMap;
}

const normalizePath = (path: string) => path.replace(/\\/g, '/').replace(/^\.?\//, '').replace(/^\/+/, '');

const highlightPython = (content: string) => {
  return Prism.highlight(content, Prism.languages.python, 'python');
};

const resolveSourceFile = (
  className: string,
  modelInfo: SystemModelInfo | null,
  files: FileMap
): { path: string; content: string } | null => {
  const metadataPath = modelInfo?.[className]?.path;
  const fileEntries = Object.entries(files);

  if (metadataPath) {
    const normalizedMetadataPath = normalizePath(metadataPath);
    const exact = fileEntries.find(([path]) => normalizePath(path) === normalizedMetadataPath);
    if (exact) return { path: exact[0], content: exact[1] };

    const suffix = fileEntries.find(([path]) => {
      const normalizedPath = normalizePath(path);
      return normalizedPath.endsWith(`/${normalizedMetadataPath}`)
        || normalizedMetadataPath.endsWith(`/${normalizedPath}`)
        || normalizedPath === normalizedMetadataPath;
    });
    if (suffix) return { path: suffix[0], content: suffix[1] };

    const filename = normalizedMetadataPath.split('/').pop();
    if (filename) {
      const filenameMatch = fileEntries.find(([path]) => normalizePath(path).split('/').pop() === filename);
      if (filenameMatch) return { path: filenameMatch[0], content: filenameMatch[1] };
    }
  }

  const classRegex = new RegExp(`^class\\s+${className}\\b`, 'm');
  const classMatch = fileEntries.find(([path, content]) => path.endsWith('.py') && classRegex.test(content));
  if (classMatch) return { path: classMatch[0], content: classMatch[1] };

  return null;
};

export const SourcePreviewPanel: React.FC<Props> = ({ selectedNode, modelInfo, files }) => {
  const source = useMemo(() => {
    if (!selectedNode) return null;
    return resolveSourceFile(selectedNode.className, modelInfo, files);
  }, [selectedNode, modelInfo, files]);
  const highlightedSource = useMemo(() => {
    return source ? highlightPython(source.content) : '';
  }, [source]);

  return (
    <div className="space-y-3 border-t border-slate-100 pt-4">
      <div className="flex items-center gap-2 text-sm font-semibold text-slate-700">
        <Code2 size={16} />
        Source
      </div>

      {!selectedNode ? (
        <div className="rounded border border-slate-200 bg-slate-50 px-3 py-3 text-xs text-slate-500">
          Select a model node to view its source.
        </div>
      ) : !source ? (
        <div className="rounded border border-amber-200 bg-amber-50 px-3 py-3 text-xs text-amber-700">
          Source not found for {selectedNode.className}.
        </div>
      ) : (
        <div className="overflow-hidden rounded border border-slate-200 bg-white">
          <div className="space-y-1 border-b border-slate-100 bg-slate-50 px-3 py-2 text-xs">
            <div className="truncate font-semibold text-slate-800" title={selectedNode.id}>
              {selectedNode.name} <span className="font-normal text-slate-500">({selectedNode.className})</span>
            </div>
            <div className="truncate text-[11px] text-slate-500" title={source.path}>
              {source.path}
            </div>
          </div>
          <pre className="source-code max-h-96 overflow-auto bg-slate-950 p-3 text-[11px] leading-5 text-slate-100">
            <code
              className="language-python"
              dangerouslySetInnerHTML={{ __html: highlightedSource }}
            />
          </pre>
        </div>
      )}

      <style>{`
        .source-code .token.comment,
        .source-code .token.prolog,
        .source-code .token.doctype,
        .source-code .token.cdata { color: #64748b; }
        .source-code .token.punctuation,
        .source-code .token.operator { color: #cbd5e1; }
        .source-code .token.property,
        .source-code .token.tag,
        .source-code .token.constant,
        .source-code .token.symbol,
        .source-code .token.deleted,
        .source-code .token.number { color: #c4b5fd; }
        .source-code .token.boolean,
        .source-code .token.selector,
        .source-code .token.attr-name,
        .source-code .token.string,
        .source-code .token.char,
        .source-code .token.builtin,
        .source-code .token.inserted { color: #86efac; }
        .source-code .token.keyword { color: #7dd3fc; }
        .source-code .token.function,
        .source-code .token.class-name { color: #fde68a; }
        .source-code .token.decorator,
        .source-code .token.annotation { color: #f0abfc; }
        .source-code .token.variable,
        .source-code .token.parameter { color: #f8fafc; }
        .source-code .token.regex,
        .source-code .token.important { color: #fdba74; }
      `}</style>
    </div>
  );
};
