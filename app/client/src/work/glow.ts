import type { Projection } from "../projection";

// The one thing the tree gains (CLIENT_UI_DESIGN.md §6.2): a glow on the nodes a worker is working
// on right now. It pulses; it moves nothing — live marking is information, live motion on a tree
// someone is reading is noise. The set is derived purely from the running workers' hypotheses, so
// it is testable and the tree only has to ask "is this node in the glow set".
export function glowingHypotheses(projection: Projection): Set<string> {
  const glowing = new Set<string>();
  for (const w of Object.values(projection.workers)) {
    if (w.status !== "finished" && w.hypothesis != null) {
      glowing.add(String(w.hypothesis));
    }
  }
  return glowing;
}
