const PRESENTATION = {
  proposed: {
    label: 'Proposed architecture',
    instruction: 'Select each component to review its responsibility and place in the hierarchy. Approve it or request changes in Conversation.',
    scope: 'This checkpoint covers components, responsibilities, and parent/child hierarchy. Ports and couplings are refined during implementation and are intentionally not shown yet.'
  },
  building: {
    label: 'Approved architecture · Building',
    instruction: 'The approved whole-model architecture stays visible while the generator implements it.',
    scope: 'Component source status appears as files become available. Ports and couplings remain hidden until the implemented model can be parsed from source.'
  },
  revising: {
    label: 'Previous proposal · Revision in progress',
    instruction: 'The generator is preparing a replacement from your requested changes.',
    scope: 'This is the previous proposal for reference. It is no longer awaiting approval and will be replaced when the revised architecture is ready.'
  },
  finalizing: {
    label: 'Approved architecture · Loading implemented model',
    instruction: 'Generation finished. The approved architecture remains visible while the implemented hierarchy, ports, and couplings are loaded.',
    scope: 'The view changes to the implemented model only after its final graph has loaded successfully.'
  },
  stopped: {
    label: 'Approved architecture · Build stopped',
    instruction: 'The build stopped before a complete implemented model could replace this approved architecture.',
    scope: 'The approved whole-model design is retained for diagnosis. Generated files that were completed remain available in Files.'
  },
  implemented: {
    label: 'Implemented model',
    instruction: 'Explore the hierarchy, ports, and couplings parsed from the generated source. Select a component to open its source file.',
    scope: ''
  }
};

/**
 * @param {'awaiting_review' | 'approved_building' | 'revising' | 'finalizing' | 'build_stopped' | null | undefined} reviewState
 * @param {boolean} hasImplementedProject
 */
export const structureLifecyclePresentation = (reviewState, hasImplementedProject) => {
  if (reviewState === 'awaiting_review') return PRESENTATION.proposed;
  if (reviewState === 'approved_building') return PRESENTATION.building;
  if (reviewState === 'revising') return PRESENTATION.revising;
  if (reviewState === 'finalizing') return PRESENTATION.finalizing;
  if (reviewState === 'build_stopped') return PRESENTATION.stopped;
  return hasImplementedProject ? PRESENTATION.implemented : null;
};

/**
 * The implemented graph can be refreshed normally, and the same action must
 * remain available while the approved architecture is waiting for the final
 * source-derived graph.  Otherwise a transient parse or transport failure
 * leaves the handoff stuck with no recovery control.
 *
 * @param {'proposed' | 'revising' | 'building' | 'finalizing' | 'stopped' | 'implemented' | null | undefined} lifecycle
 */
export const canRefreshStructure = lifecycle => (
  lifecycle === 'implemented' || lifecycle === 'finalizing'
);

/**
 * A structure checkpoint is deliberately architectural. Strip implementation
 * details even when an older or richer producer embeds them in its graph so
 * the UI never presents speculative ports or couplings as generated facts.
 *
 * @param {{root_model?: string, nodes?: unknown[], links?: unknown[]} | null | undefined} graph
 */
export const architectureOnlyGraph = graph => {
  if (!graph || !Array.isArray(graph.nodes)) return null;
  return {
    ...graph,
    nodes: graph.nodes.map(node => ({
      ...node,
      ports: { inputs: [], outputs: [] }
    })),
    links: []
  };
};

/**
 * @param {Array<{id?: string, name?: string, className?: string, parent?: string | null}>} nodes
 * @param {string | null | undefined} rootModel
 */
export const rootStructureNode = (nodes, rootModel) => (
  nodes.find(node => node.id === rootModel)
  || nodes.find(node => node.className === rootModel)
  || nodes.find(node => node.name === rootModel)
  || nodes.find(node => node.parent == null)
  || nodes[0]
  || null
);

/**
 * @param {Array<{id: string, parent?: string | null}>} nodes
 * @param {string | null | undefined} selectedId
 */
export const structureNodeRelations = (nodes, selectedId) => {
  const selected = nodes.find(node => node.id === selectedId) || null;
  if (!selected) return { parent: null, children: [] };
  return {
    parent: selected.parent ? nodes.find(node => node.id === selected.parent) || null : null,
    children: nodes.filter(node => node.parent === selected.id)
  };
};

/**
 * Produce a stable, hierarchy-first component list without trusting slash
 * characters in opaque ids.
 *
 * @param {Array<{id: string, parent?: string | null}>} nodes
 * @param {string | null | undefined} rootModel
 */
export const orderedStructureNodes = (nodes, rootModel) => {
  const root = rootStructureNode(nodes, rootModel);
  const children = new Map();
  nodes.forEach(node => {
    if (!node.parent) return;
    children.set(node.parent, [...(children.get(node.parent) || []), node]);
  });
  const result = [];
  const visited = new Set();
  const visit = (node, depth) => {
    if (!node || visited.has(node.id)) return;
    visited.add(node.id);
    result.push({ node, depth });
    (children.get(node.id) || []).forEach(child => visit(child, depth + 1));
  };
  visit(root, 0);
  nodes.forEach(node => visit(node, node.parent ? 1 : 0));
  return result;
};
