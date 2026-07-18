export const MAX_MEMORY_GRAPH_NODES = 1200;
export const DEFAULT_MEMORY_GRAPH_NODES = 600;

const HUB_KINDS = new Set(["namespace", "folder", "daybucket"]);
const SEARCH_NEIGHBOURS_PER_MATCH = 12;

/**
 * @typedef {{
 *   id: string,
 *   label: string,
 *   kind: string,
 *   degree?: number | null,
 *   tags?: string[],
 *   namespace?: string | null,
 *   path?: string | null
 * }} GraphNode
 *
 * @typedef {{source: string, target: string, kind: string}} GraphEdge
 */

/** Keep externally supplied/select values inside the simulation's safe budget. */
export function clampMemoryGraphCap(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return DEFAULT_MEMORY_GRAPH_NODES;
  return Math.max(1, Math.min(Math.trunc(numeric), MAX_MEMORY_GRAPH_NODES));
}

/** @param {GraphNode} node @param {string} normalizedSearch */
export function memoryGraphNodeMatches(node, normalizedSearch) {
  if (!normalizedSearch) return false;
  const haystack = [
    node.label,
    node.id,
    node.path ?? "",
    node.namespace ?? "",
    ...(node.tags ?? [])
  ]
    .join(" ")
    .toLocaleLowerCase();
  return haystack.includes(normalizedSearch);
}

/** @param {GraphNode} node */
function nodePriority(node) {
  return HUB_KINDS.has(node.kind) ? 3 : node.kind === "document" ? 2 : 1;
}

/** @param {GraphNode} a @param {GraphNode} b */
function compareNodes(a, b) {
  const priorityDelta = nodePriority(b) - nodePriority(a);
  if (priorityDelta) return priorityDelta;
  const degreeDelta = (b.degree ?? 0) - (a.degree ?? 0);
  if (degreeDelta) return degreeDelta;
  return a.id.localeCompare(b.id);
}

/**
 * Select the bounded topology rendered by the O(N^2) SVG simulation.
 * Search is evaluated against every visible node before the cap: matches are
 * inserted first, followed by a bounded round-robin sample of their neighbours.
 *
 * @template {GraphNode} TNode
 * @template {GraphEdge} TEdge
 * @param {{
 *   nodes: readonly TNode[],
 *   edges: readonly TEdge[],
 *   hiddenKinds: ReadonlySet<string>,
 *   renderCap: number,
 *   search: string
 * }} input
 */
export function selectMemoryGraph(input) {
  const cap = clampMemoryGraphCap(input.renderCap);
  const normalizedSearch = input.search.trim().toLocaleLowerCase();
  const visibleNodes = input.nodes.filter((node) => !input.hiddenKinds.has(node.kind));
  const visibleIds = new Set(visibleNodes.map((node) => node.id));
  const rankedNodes = [...visibleNodes].sort(compareNodes);
  const rankById = new Map(rankedNodes.map((node, index) => [node.id, index]));
  const searchMatchIds = new Set(
    normalizedSearch
      ? visibleNodes
          .filter((node) => memoryGraphNodeMatches(node, normalizedSearch))
          .map((node) => node.id)
      : []
  );

  const adjacency = new Map();
  if (searchMatchIds.size) {
    for (const edge of input.edges) {
      if (!visibleIds.has(edge.source) || !visibleIds.has(edge.target)) continue;
      if (!adjacency.has(edge.source)) adjacency.set(edge.source, new Set());
      if (!adjacency.has(edge.target)) adjacency.set(edge.target, new Set());
      adjacency.get(edge.source).add(edge.target);
      adjacency.get(edge.target).add(edge.source);
    }
  }

  /** @type {TNode[]} */
  const keptNodes = [];
  const keptIds = new Set();
  /** @param {TNode | undefined} node */
  const addNode = (node) => {
    if (!node || keptNodes.length >= cap || keptIds.has(node.id)) return false;
    keptIds.add(node.id);
    keptNodes.push(node);
    return true;
  };

  const matchingNodes = rankedNodes.filter((node) => searchMatchIds.has(node.id));
  const promotedMatches = matchingNodes.slice(0, cap);
  for (const node of promotedMatches) addNode(node);

  // A single high-degree match must not consume the entire graph. Round-robin
  // keeps context balanced when several search results are present.
  const neighbourQueues = promotedMatches.map((node) =>
    [...(adjacency.get(node.id) ?? [])]
      .sort((a, b) => (rankById.get(a) ?? Infinity) - (rankById.get(b) ?? Infinity))
      .slice(0, SEARCH_NEIGHBOURS_PER_MATCH)
  );
  for (let offset = 0; keptNodes.length < cap; offset += 1) {
    let foundCandidate = false;
    for (const queue of neighbourQueues) {
      const neighbourId = queue[offset];
      if (!neighbourId) continue;
      foundCandidate = true;
      addNode(rankedNodes[rankById.get(neighbourId)]);
      if (keptNodes.length >= cap) break;
    }
    if (!foundCandidate) break;
  }

  for (const node of rankedNodes) addNode(node);

  const keptEdges = input.edges.filter(
    (edge) => keptIds.has(edge.source) && keptIds.has(edge.target)
  );
  return {
    nodes: keptNodes,
    edges: keptEdges,
    totalNodes: visibleNodes.length,
    truncated: keptNodes.length < visibleNodes.length,
    searchMatchIds,
    renderedSearchMatches: keptNodes.filter((node) => searchMatchIds.has(node.id)).length,
    cap
  };
}
