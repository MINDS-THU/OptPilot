/**
 * Compute hierarchy depth from explicit parent relationships. Component IDs
 * are opaque identifiers; their punctuation must not determine containment.
 * Missing parents and malformed cycles are treated as roots so layout remains
 * bounded instead of recursing forever.
 *
 * @param {{id: string, parent?: string | null}[]} nodes
 * @returns {Map<string, number>}
 */
export const hierarchyDepthById = nodes => {
  const parentById = new Map(nodes.map(node => [node.id, node.parent || null]));
  const depthById = new Map();

  const visit = (id, path = new Set()) => {
    if (depthById.has(id)) return depthById.get(id);
    if (path.has(id)) return 0;

    const parent = parentById.get(id);
    if (!parent || parent === id || !parentById.has(parent)) {
      depthById.set(id, 0);
      return 0;
    }

    const nextPath = new Set(path);
    nextPath.add(id);
    const depth = visit(parent, nextPath) + 1;
    depthById.set(id, depth);
    return depth;
  };

  nodes.forEach(node => visit(node.id));
  return depthById;
};
