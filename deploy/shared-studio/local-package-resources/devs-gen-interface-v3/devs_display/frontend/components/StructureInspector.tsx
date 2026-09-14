import React, { useMemo } from 'react';
import { CheckCircle2, Circle, Code2, Layers3 } from 'lucide-react';
import { GraphLink, GraphNode } from '../types';
import {
  orderedStructureNodes,
  structureNodeRelations
} from '../services/structureLifecycleService.js';

type Lifecycle = 'proposed' | 'revising' | 'building' | 'finalizing' | 'stopped' | 'implemented';

interface Props {
  lifecycle: Lifecycle;
  nodes: GraphNode[];
  links: GraphLink[];
  rootModel: string;
  selectedNode: GraphNode | null;
  sourcePaths: Record<string, string | null>;
  onSelect: (node: GraphNode) => void;
  onOpenSource: (path: string) => void;
}

const PortList: React.FC<{ label: string; ports: string[] }> = ({ label, ports }) => (
  <div>
    <div className="text-[9px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
    <div className="mt-1 flex flex-wrap gap-1">
      {ports.length > 0
        ? ports.map(port => <span key={port} className="rounded bg-slate-100 px-1.5 py-0.5 text-[9px] text-slate-700">{port}</span>)
        : <span className="text-[10px] text-slate-400">None</span>}
    </div>
  </div>
);

export const StructureInspector: React.FC<Props> = ({
  lifecycle,
  nodes,
  links,
  rootModel,
  selectedNode,
  sourcePaths,
  onSelect,
  onOpenSource
}) => {
  const ordered = useMemo(() => orderedStructureNodes(nodes, rootModel), [nodes, rootModel]);
  const selected = (selectedNode && nodes.find(node => node.id === selectedNode.id)) || null;
  const relations = useMemo(
    () => structureNodeRelations(nodes, selected?.id),
    [nodes, selected?.id]
  );
  const sourcePath = selected ? sourcePaths[selected.id] : null;
  const showsImplementationProgress = lifecycle === 'building' || lifecycle === 'finalizing' || lifecycle === 'stopped';
  const implementedCount = showsImplementationProgress
    ? nodes.filter(node => Boolean(sourcePaths[node.id])).length
    : 0;
  const incoming = selected ? links.filter(link => link.target === selected.id).length : 0;
  const outgoing = selected ? links.filter(link => link.source === selected.id).length : 0;

  return (
    <aside className="flex h-full w-64 flex-shrink-0 flex-col border-l border-slate-200 bg-white xl:w-72" aria-label="Structure component inspector">
      <div className="border-b border-slate-200 px-3 py-2.5">
        <div className="flex items-center justify-between gap-2">
          <h2 className="flex items-center gap-1.5 text-xs font-semibold text-slate-800">
            <Layers3 size={14} /> {lifecycle === 'implemented' ? 'Model components' : 'Architecture components'}
          </h2>
          <span className="text-[10px] text-slate-400">{nodes.length}</span>
        </div>
        {showsImplementationProgress && (
          <p className="mt-1 text-[10px] leading-4 text-slate-500">
            {implementedCount} of {nodes.length} components have discoverable source so far.
          </p>
        )}
      </div>

      <div className="max-h-[42%] overflow-y-auto border-b border-slate-200 p-2">
        {ordered.map(({ node, depth }) => {
          const active = selected?.id === node.id;
          const hasSource = Boolean(sourcePaths[node.id]);
          return (
            <button
              type="button"
              key={node.id}
              onClick={() => onSelect(node)}
              title={node.description || node.name}
              className={`mb-1 flex w-full min-w-0 items-center gap-1.5 rounded px-2 py-1.5 text-left ${active ? 'bg-blue-50 text-blue-800' : 'text-slate-600 hover:bg-slate-50'}`}
              style={{ paddingLeft: `${8 + Math.min(depth, 5) * 12}px` }}
            >
              {showsImplementationProgress
                ? hasSource
                  ? <CheckCircle2 size={12} className="shrink-0 text-emerald-600" />
                  : <Circle size={11} className="shrink-0 text-slate-300" />
                : <Circle size={8} className={`shrink-0 ${node.type === 'coupled' ? 'fill-amber-100 text-amber-500' : 'fill-sky-100 text-sky-500'}`} />}
              <span className="min-w-0 flex-1 truncate text-[11px] font-medium">{node.name}</span>
            </button>
          );
        })}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {selected ? (
          <div className="space-y-3">
            <div>
              <div className="text-sm font-semibold text-slate-900">{selected.name}</div>
              <div className="mt-0.5 text-[10px] text-slate-500">{selected.className} · {selected.type}</div>
            </div>

            <div>
              <div className="text-[9px] font-semibold uppercase tracking-wide text-slate-400">Responsibility</div>
              <p className="mt-1 text-[11px] leading-4 text-slate-700">
                {selected.description || (lifecycle === 'implemented'
                  ? 'No responsibility description is recorded in the generated model metadata.'
                  : 'No responsibility was included in this architecture proposal.')}
              </p>
            </div>

            <dl className="grid grid-cols-[64px_1fr] gap-x-2 gap-y-1 text-[10px]">
              <dt className="text-slate-400">Parent</dt>
              <dd className="truncate font-medium text-slate-700">{relations.parent?.name || 'None — root model'}</dd>
              <dt className="text-slate-400">Contains</dt>
              <dd className="text-slate-700">{relations.children.length > 0 ? relations.children.map(node => node.name).join(', ') : 'No child components'}</dd>
            </dl>

            {lifecycle === 'implemented' ? (
              <>
                <div className="grid grid-cols-2 gap-2">
                  <PortList label="Input ports" ports={selected.ports.inputs} />
                  <PortList label="Output ports" ports={selected.ports.outputs} />
                </div>
                <div className="rounded bg-slate-50 px-2 py-1.5 text-[10px] text-slate-600">
                  {incoming} incoming · {outgoing} outgoing coupling{incoming + outgoing === 1 ? '' : 's'}
                </div>
                {sourcePath ? (
                  <button
                    type="button"
                    onClick={() => onOpenSource(sourcePath)}
                    className="flex w-full items-center justify-center gap-1.5 rounded border border-blue-200 bg-blue-50 px-2 py-1.5 text-[10px] font-semibold text-blue-700 hover:bg-blue-100"
                  >
                    <Code2 size={12} /> Open source file
                  </button>
                ) : (
                  <div className="text-[10px] text-slate-400">No source file could be matched to this component.</div>
                )}
              </>
            ) : showsImplementationProgress ? (
              <div className={`rounded px-2 py-1.5 text-[10px] ${sourcePath ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-50 text-slate-500'}`}>
                {sourcePath
                  ? `Source created: ${sourcePath}`
                  : lifecycle === 'stopped'
                    ? 'No generated source was completed for this component.'
                    : 'Waiting for generated source.'}
              </div>
            ) : (
              <div className="rounded bg-purple-50 px-2 py-1.5 text-[10px] leading-4 text-purple-700">
                Planned component. No implementation details exist at this checkpoint.
              </div>
            )}
          </div>
        ) : (
          <div className="flex h-full items-center justify-center text-center text-[11px] leading-5 text-slate-400">
            Select a component to inspect it.
          </div>
        )}
      </div>
    </aside>
  );
};
