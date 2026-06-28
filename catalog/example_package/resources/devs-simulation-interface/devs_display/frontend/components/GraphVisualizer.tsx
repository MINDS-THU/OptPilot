
import React, { useEffect, useRef, useImperativeHandle, forwardRef } from 'react';
import * as d3 from 'd3';
import { GraphNode, GraphLink } from '../types';
import { ZoomIn, ZoomOut, Move } from 'lucide-react';

interface Props {
  nodes: GraphNode[];
  links: GraphLink[];
  physicsEnabled: boolean; // Ignored
  selectedNodeId?: string | null;
  onExpand: (nodeId: string) => void;
  onCollapse: (nodeId: string) => void;
  onToggleFixed: (nodeId: string, isFixed: boolean, x?: number, y?: number) => void;
  onNodeMove: (nodeId: string, x: number, y: number) => void; 
  onNodeSelect?: (node: GraphNode) => void;
}

export interface GraphVisualizerHandle {
    exportImage: () => void;
}

// Dimensions configuration
const ATOMIC_WIDTH = 140;
const ATOMIC_HEIGHT = 80;
const DEFAULT_EXPANDED_WIDTH = 400;
const DEFAULT_EXPANDED_HEIGHT = 300;
const PORT_RADIUS = 5;

// Asymmetric Padding
const PADDING_TOP = 40;    
const PADDING_SIDE = 30;   
const PADDING_BOTTOM = 20; 

const COLORS = {
  atomic: '#e0f2fe', 
  coupled: '#fef3c7', 
  coupledExpanded: 'rgba(254, 243, 199, 0.4)', 
  stroke: '#64748b', 
  strokeSelected: '#334155',
  portInput: '#22c55e', 
  portOutput: '#ef4444', 
  link: '#475569', 
  text: '#1e293b'
};

const getDepth = (id: string) => id.split('/').length;

export const GraphVisualizer = forwardRef<GraphVisualizerHandle, Props>(({ 
    nodes, 
    links, 
    selectedNodeId,
    onExpand, 
    onCollapse, 
    onNodeMove,
    onNodeSelect
}, ref) => {
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  
  const svgSelection = useRef<d3.Selection<SVGSVGElement, unknown, null, undefined> | null>(null);
  const gSelection = useRef<d3.Selection<SVGGElement, unknown, null, undefined> | null>(null);
  const zoomBehavior = useRef<d3.ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  
  // Ref to hold the current working set of nodes (with mutable x,y)
  const visualNodesRef = useRef<any[]>([]);

  // Expose Export Function
  useImperativeHandle(ref, () => ({
      exportImage: () => {
          if (!gSelection.current) return;
          const node = gSelection.current.node() as SVGGElement;
          const bbox = node.getBBox();
          if (bbox.width === 0 || bbox.height === 0) return;

          const padding = 50;
          const width = bbox.width + padding * 2;
          const height = bbox.height + padding * 2;
          const x = bbox.x - padding;
          const y = bbox.y - padding;

          const clone = node.cloneNode(true) as SVGGElement;
          clone.setAttribute('transform', '');

          const svgString = `
            <svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="${x} ${y} ${width} ${height}">
              <style>text { font-family: ui-sans-serif, system-ui, sans-serif; }</style>
              ${clone.outerHTML}
            </svg>
          `;
          const blob = new Blob([svgString], {type: 'image/svg+xml;charset=utf-8'});
          const url = URL.createObjectURL(blob);
          const img = new Image();
          img.onload = () => {
              const canvas = document.createElement('canvas');
              const scale = 2;
              canvas.width = width * scale;
              canvas.height = height * scale;
              const ctx = canvas.getContext('2d');
              if (ctx) {
                  ctx.fillStyle = '#ffffff';
                  ctx.fillRect(0, 0, canvas.width, canvas.height);
                  ctx.scale(scale, scale);
                  ctx.drawImage(img, 0, 0);
                  const downloadLink = document.createElement('a');
                  downloadLink.href = canvas.toDataURL('image/png');
                  downloadLink.download = 'xdevs-structure.png';
                  document.body.appendChild(downloadLink);
                  downloadLink.click();
                  document.body.removeChild(downloadLink);
              }
              URL.revokeObjectURL(url);
          };
          img.src = url;
      }
  }));

  // Initialize Zoom
  useEffect(() => {
    if (!svgRef.current) return;
    svgSelection.current = d3.select(svgRef.current);
    gSelection.current = svgSelection.current.select('g.main-group') as d3.Selection<SVGGElement, unknown, null, undefined>;
    if (gSelection.current.empty()) {
        gSelection.current = svgSelection.current.append('g').attr('class', 'main-group') as d3.Selection<SVGGElement, unknown, null, undefined>;
    }
    zoomBehavior.current = d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.1, 4])
      .on('zoom', (event) => gSelection.current?.attr('transform', event.transform));
    svgSelection.current.call(zoomBehavior.current);
  }, []);

  // Main Render Logic
  useEffect(() => {
    if (!gSelection.current || !svgRef.current) return;
    const width = containerRef.current?.clientWidth || 800;
    const height = containerRef.current?.clientHeight || 600;
    
    // 1. Data Prep
    // Convert props to mutable D3 objects.
    // Important: We perform the sort HERE (Parents -> Children) so DOM order is correct (Children on top).
    // This avoids needing to call .sort() or .raise() during drag interactions.
    const simNodes = nodes.map(n => ({
          ...n,
          x: n.x ?? width/2,
          y: n.y ?? height/2,
          visualWidth: n.expanded ? DEFAULT_EXPANDED_WIDTH : ATOMIC_WIDTH, 
          visualHeight: n.expanded ? DEFAULT_EXPANDED_HEIGHT : ATOMIC_HEIGHT
    })).sort((a, b) => getDepth(a.id) - getDepth(b.id));

    visualNodesRef.current = simNodes;
    const simLinks = links.map(l => ({ ...l }));
    
    // 2. Initial Geometry Calculation
    updateGeometry(); 
    
    // 3. Render
    render();

    function updateGeometry() {
        // Build hierarchy map
        const childrenMap = new Map<string, any[]>();
        visualNodesRef.current.forEach((n: any) => {
            if (n.parent) {
                if (!childrenMap.has(n.parent)) childrenMap.set(n.parent, []);
                childrenMap.get(n.parent)!.push(n);
            }
        });

        // Bottom-up traversal for sizing (Deepest first)
        // We use a separate sorted array for calculation logic
        const calcOrder = [...visualNodesRef.current].sort((a: any, b: any) => getDepth(b.id) - getDepth(a.id));

        calcOrder.forEach((n: any) => {
            if (n.expanded && n.type === 'coupled') {
                const children = childrenMap.get(n.id);
                
                if (children && children.length > 0) {
                    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
                    let validKids = 0;

                    children.forEach(c => {
                         if (!isNaN(c.x) && !isNaN(c.y)) {
                             const hw = c.visualWidth / 2;
                             const hh = c.visualHeight / 2;
                             if (c.x - hw < minX) minX = c.x - hw;
                             if (c.x + hw > maxX) maxX = c.x + hw;
                             if (c.y - hh < minY) minY = c.y - hh;
                             if (c.y + hh > maxY) maxY = c.y + hh;
                             validKids++;
                         }
                    });

                    if (validKids > 0 && minX !== Infinity) {
                        const contentW = maxX - minX;
                        const contentH = maxY - minY;
                        n.visualWidth = contentW + (PADDING_SIDE * 2);
                        n.visualHeight = contentH + PADDING_TOP + PADDING_BOTTOM;
                        const newLeft = minX - PADDING_SIDE;
                        const newTop = minY - PADDING_TOP;
                        n.x = newLeft + (n.visualWidth / 2);
                        n.y = newTop + (n.visualHeight / 2);
                    }
                } else {
                     n.visualWidth = DEFAULT_EXPANDED_WIDTH;
                     n.visualHeight = DEFAULT_EXPANDED_HEIGHT;
                }
            } else {
                n.visualWidth = ATOMIC_WIDTH;
                n.visualHeight = ATOMIC_HEIGHT;
            }
        });
    }

    function render() {
        renderLinks();
        renderNodes();
    }

    function getPortPosition(node: any, portName: string, isSource: boolean) {
          if (!node || isNaN(node.x) || isNaN(node.y)) return { x: 0, y: 0, dir: 1 };

          let side: 'left' | 'right' = isSource ? 'right' : 'left';
          let portIndex = -1;
          let totalPorts = 1;

          const inputs = node.ports?.inputs || [];
          const outputs = node.ports?.outputs || [];
          const inputIdx = inputs.indexOf(portName);
          const outputIdx = outputs.indexOf(portName);

          if (isSource) {
             if (outputIdx !== -1) { side = 'right'; portIndex = outputIdx; totalPorts = outputs.length; } 
             else if (inputIdx !== -1) { side = 'left'; portIndex = inputIdx; totalPorts = inputs.length; }
          } else {
             if (inputIdx !== -1) { side = 'left'; portIndex = inputIdx; totalPorts = inputs.length; } 
             else if (outputIdx !== -1) { side = 'right'; portIndex = outputIdx; totalPorts = outputs.length; }
          }
          if (portIndex === -1) { portIndex = 0; totalPorts = 1; }

          const w = node.visualWidth;
          const h = node.visualHeight;
          const xOffset = side === 'right' ? w / 2 : -w / 2;
          
          const effectiveTop = -h/2 + (node.expanded ? PADDING_TOP : 0);
          const effectiveH = h - (node.expanded ? PADDING_TOP : 0);
          const yOffset = effectiveTop + ((portIndex + 1) * (effectiveH / (totalPorts + 1)));
          
          return { x: node.x + xOffset, y: node.y + yOffset, dir: side === 'right' ? 1 : -1 };
    }

    function renderLinks() {
      const link = gSelection.current!.selectAll('.link').data(simLinks, (d: any) => d.id);
      
      const linkEnter = link.enter().insert('path', '.node') 
        .attr('class', 'link')
        .attr('stroke', COLORS.link).attr('stroke-width', 2).attr('fill', 'none')
        .attr('marker-end', 'url(#arrowhead)');
      
      linkEnter.merge(link as any).attr('d', (d: any) => {
             const s = visualNodesRef.current.find(n => n.id === d.source.id || n.id === d.source);
             const t = visualNodesRef.current.find(n => n.id === d.target.id || n.id === d.target);
             if (!s || !t || isNaN(s.x) || isNaN(t.x)) return '';

             let p1 = getPortPosition(s, d.sourcePort, true);
             let p2 = getPortPosition(t, d.targetPort, false);
             
             if (t.parent === s.id) p1.dir = -p1.dir;
             if (s.parent === t.id) p2.dir = -p2.dir;

             const dx = p2.x - p1.x;
             const dy = p2.y - p1.y;
             const dist = Math.sqrt(dx*dx + dy*dy);
             const strength = Math.min(dist * 0.5, 100); 
             return `M${p1.x},${p1.y} C${p1.x + p1.dir*strength},${p1.y} ${p2.x + p2.dir*strength},${p2.y} ${p2.x},${p2.y}`;
        });
      link.exit().remove();
    }

    function renderNodes() {
      // Data bind
      const node = gSelection.current!.selectAll<SVGGElement, any>('.node')
            .data(visualNodesRef.current, (d: any) => d.id);
      
      // ENTER
      const nodeEnter = node.enter().append('g').attr('class', 'node')
        .call(d3.drag<SVGGElement, any>().on("start", dragstarted).on("drag", dragged).on("end", dragended));

      nodeEnter.append('rect').attr('rx', 6).attr('ry', 6).attr('stroke-width', 2);
      
      const labels = nodeEnter.append('g').attr('class', 'labels');
      labels.append('text').attr('class', 'label-instance').attr('text-anchor', 'middle').style('font-weight', 'bold').style('pointer-events', 'none');
      labels.append('text').attr('class', 'label-class').attr('text-anchor', 'middle').style('font-style', 'italic').style('pointer-events', 'none');
      
      const controls = nodeEnter.append('g').attr('class', 'controls');
      const expandBtn = controls.append('g').attr('class', 'btn-expand cursor-pointer');
      expandBtn.append('circle').attr('r', 8).attr('fill', 'white').attr('stroke', COLORS.stroke);
      expandBtn.append('text').attr('class', 'expand-icon').attr('text-anchor', 'middle').attr('dy', 4).attr('font-size', 10);
      
      // Update Selection
      const nodeUpdate = nodeEnter.merge(node);
      
      // Events
      nodeUpdate.on('click', (event, d) => {
        event.stopPropagation();
        onNodeSelect?.(d as GraphNode);
      });
      nodeUpdate.select('.btn-expand')
        .on('mousedown', (event) => event.stopPropagation())
        .on('pointerdown', (event) => event.stopPropagation())
        .on('touchstart', (event) => event.stopPropagation())
        .on('click', (e, d) => {
          e.stopPropagation();
          if (d.type === 'coupled') {
            d.expanded ? onCollapse(d.id) : onExpand(d.id);
          }
        });

      // --- CRITICAL: Position Update ---
      // We do NOT use sort() here, relying on the initial data sort order for Z-index.
      nodeUpdate.attr('transform', (d: any) => (!isNaN(d.x) && !isNaN(d.y)) ? `translate(${d.x},${d.y})` : 'scale(0)');

      // Visual Styles
      nodeUpdate.select('rect')
        .attr('width', (d: any) => d.visualWidth)
        .attr('height', (d: any) => d.visualHeight)
        .attr('x', (d: any) => -d.visualWidth / 2)
        .attr('y', (d: any) => -d.visualHeight / 2)
        .attr('stroke', (d: any) => d.id === selectedNodeId ? COLORS.strokeSelected : (d.expanded ? '#9ca3af' : COLORS.stroke))
        .attr('stroke-width', (d: any) => d.id === selectedNodeId ? 3 : 2)
        .attr('stroke-dasharray', (d: any) => d.expanded ? '4,4' : 'none')
        .attr('fill', (d: any) => d.type === 'atomic' ? COLORS.atomic : (d.expanded ? COLORS.coupledExpanded : COLORS.coupled));

      nodeUpdate.select('.labels').attr('transform', (d: any) => {
           const headerCenterY = -d.visualHeight/2 + (d.expanded ? PADDING_TOP/2 : d.visualHeight/2); 
           return `translate(0, ${headerCenterY})`;
      });
      nodeUpdate.select('.label-instance').text((d: any) => d.name).attr('fill', COLORS.text).attr('dy', (d:any) => d.expanded ? 0 : -5);
      nodeUpdate.select('.label-class').text((d: any) => `(${d.className})`).attr('dy', (d:any) => d.expanded ? 14 : 10).attr('fill', '#64748b').attr('font-size', 10);

      nodeUpdate.select('.btn-expand').style('display', (d: any) => d.type === 'coupled' ? 'block' : 'none')
        .attr('transform', (d: any) => `translate(${-d.visualWidth/2 + 12}, ${-d.visualHeight/2 + 12})`)
        .select('.expand-icon').text((d: any) => d.expanded ? '-' : '+');

      // Re-render ports (kept simple for robustness, though slightly expensive)
      nodeUpdate.selectAll('.port-group').remove();
      const portGroups = nodeUpdate.append('g').attr('class', 'port-group');
      
      nodeUpdate.each(function(d: any) {
          const g = d3.select(this).select('.port-group');
          const inputs = (d.ports?.inputs || []).map((p:string, i:number) => ({name:p, i, total: d.ports.inputs.length, type:'in'}));
          const outputs = (d.ports?.outputs || []).map((p:string, i:number) => ({name:p, i, total: d.ports.outputs.length, type:'out'}));
          
          [...inputs, ...outputs].forEach(p => {
               const w = d.visualWidth;
               const h = d.visualHeight;
               const x = p.type === 'out' ? w/2 : -w/2;
               
               const headerOffset = d.expanded ? PADDING_TOP : 0;
               const availableHeight = h - headerOffset; 
               const y = (-h/2 + headerOffset) + ((p.i + 1) * (availableHeight / (p.total + 1)));

               const pg = g.append('g').attr('transform', `translate(${x}, ${y})`);
               pg.append('circle').attr('r', 4)
                 .attr('fill', p.type === 'in' ? COLORS.portInput : COLORS.portOutput)
                 .attr('stroke', 'white').attr('stroke-width', 1);
               
               if (!d.expanded || availableHeight > 20) {
                   pg.append('text').text(p.name)
                     .attr('x', p.type === 'in' ? -6 : 6).attr('y', 3)
                     .attr('text-anchor', p.type === 'in' ? 'end' : 'start')
                     .attr('font-size', 9).attr('fill', '#64748b')
                     .style('pointer-events', 'none');
               }
          });
      });
      node.exit().remove();
    }

    function moveHierarchy(node: any, dx: number, dy: number, visited = new Set<string>()) {
        if (visited.has(node.id)) return;
        visited.add(node.id);
        
        node.x += dx;
        node.y += dy;

        const children = visualNodesRef.current.filter((n: any) => n.parent === node.id);
        children.forEach((child: any) => moveHierarchy(child, dx, dy, visited));
    }

    function dragstarted(event: any, d: any) {
        // NO raise() here to prevent Z-index fighting
        d3.select(this).attr("stroke", "black");
    }
    
    function dragged(event: any, d: any) {
        if (isNaN(event.dx) || isNaN(event.dy)) return;
        moveHierarchy(d, event.dx, event.dy);
        updateGeometry();
        render(); // This updates all nodes including children/parents affected by moveHierarchy
    }

    function dragended(event: any, d: any) {
        d3.select(this).attr("stroke", null);
        // Sync final position back to React
        visualNodesRef.current.forEach((n: any) => {
            onNodeMove(n.id, n.x, n.y);
        });
    }

  }, [nodes, links, selectedNodeId, onExpand, onCollapse, onNodeMove, onNodeSelect]);

  return (
    <div ref={containerRef} className="w-full h-full relative overflow-hidden bg-slate-50 border border-slate-200 rounded-lg shadow-inner">
      <div className="absolute top-4 right-4 flex flex-col gap-2 z-10 bg-white p-2 rounded shadow">
         <button onClick={() => svgSelection.current?.transition().duration(750).call(zoomBehavior.current!.scaleBy, 1.2)} className="p-2 hover:bg-slate-100 rounded"><ZoomIn size={20} /></button>
         <button onClick={() => svgSelection.current?.transition().duration(750).call(zoomBehavior.current!.scaleBy, 0.8)} className="p-2 hover:bg-slate-100 rounded"><ZoomOut size={20} /></button>
         <button onClick={() => svgSelection.current?.transition().duration(750).call(zoomBehavior.current!.transform, d3.zoomIdentity)} className="p-2 hover:bg-slate-100 rounded"><Move size={20} /></button>
      </div>
      <svg ref={svgRef} className="w-full h-full cursor-grab active:cursor-grabbing">
        <defs>
            <marker id="arrowhead" viewBox="0 -5 10 10" refX="10" refY="0" markerWidth="6" markerHeight="6" orient="auto">
                <path d="M0,-5L10,0L0,5" fill={COLORS.link} />
            </marker>
        </defs>
        <g className="main-group"></g>
      </svg>
      {nodes.length === 0 && <div className="absolute inset-0 flex items-center justify-center text-slate-400 pointer-events-none">Waiting for data...</div>}
    </div>
  );
});
