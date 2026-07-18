import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import {
  MAX_MEMORY_GRAPH_NODES,
  clampMemoryGraphCap,
  selectMemoryGraph
} from "../lib/memory-graph.mjs";

const nodes = Array.from({ length: 1305 }, (_, index) => ({
  id: `node-${index}`,
  label: index === 1304 ? "needle outside ordinary cap" : `node ${index}`,
  kind: "memory",
  degree: 1304 - index,
  tags: []
}));
const edges = [
  { source: "node-1304", target: "node-1303", kind: "mentions" },
  { source: "node-0", target: "node-1", kind: "mentions" }
];

const selected = selectMemoryGraph({
  nodes,
  edges,
  hiddenKinds: new Set(),
  renderCap: 300,
  search: "needle"
});
assert.equal(selected.nodes.length, 300);
assert.equal(selected.nodes.some((node) => node.id === "node-1304"), true);
assert.equal(selected.nodes.some((node) => node.id === "node-1303"), true);
assert.equal(selected.searchMatchIds.has("node-1304"), true);
assert.equal(selected.renderedSearchMatches, 1);
assert.equal(
  selected.edges.some((edge) => edge.source === "node-1304" && edge.target === "node-1303"),
  true
);

const hidden = selectMemoryGraph({
  nodes,
  edges,
  hiddenKinds: new Set(["memory"]),
  renderCap: 300,
  search: "needle"
});
assert.equal(hidden.nodes.length, 0);
assert.equal(hidden.searchMatchIds.size, 0);
assert.equal(clampMemoryGraphCap(100_000), MAX_MEMORY_GRAPH_NODES);
assert.equal(
  selectMemoryGraph({
    nodes,
    edges,
    hiddenKinds: new Set(),
    renderCap: 100_000,
    search: ""
  }).nodes.length,
  MAX_MEMORY_GRAPH_NODES
);

const componentSource = await readFile(new URL("../app/MemoryGraph.tsx", import.meta.url), "utf8");
assert.match(componentSource, /useLayoutEffect\(\(\) => \{/);
assert.match(componentSource, /nodeElsRef\.current\.get\(node\.id\)/);
assert.match(componentSource, /edgeElsRef\.current\.get\(topology\.edgeKeys\[index\]\)/);
assert.match(componentSource, /topologyRef\.current/);
assert.match(componentSource, /getScreenCTM\(\)/);
assert.match(componentSource, /screenMatrix\.inverse\(\)/);
assert.doesNotMatch(componentSource, /value=\{?100000\}?/);
assert.doesNotMatch(componentSource, /все узлы/);

const pageSource = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
const refreshStart = pageSource.indexOf("const refresh = useCallback");
const refreshEnd = pageSource.indexOf("const refreshVitals = useCallback", refreshStart);
assert.notEqual(refreshStart, -1);
assert.notEqual(refreshEnd, -1);
assert.doesNotMatch(pageSource.slice(refreshStart, refreshEnd), /\/api\/memory\/vault/);
assert.match(pageSource, /if \(activeTab !== "memory"\) return;/);
assert.match(pageSource, /const refresh = useCallback\(async \(includeMemoryVault = false\)/);
assert.match(pageSource, /includeMemoryVault \? \[refreshMemoryVault\(\)\] : \[\]/);
assert.match(pageSource, /await loadMemoryVault\(true\);/);
assert.doesNotMatch(
  pageSource.slice(
    pageSource.indexOf('if (activeTab !== "memory") return;'),
    pageSource.indexOf("// Chat persistence", pageSource.indexOf('if (activeTab !== "memory") return;'))
  ),
  /memoryVaultRef\.current && !force/
);

console.log("memory-graph-ok");
