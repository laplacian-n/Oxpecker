// Tier behaviour (CLIENT_UI_DESIGN.md §8). A surface with nothing in it is worse than one not
// offered, so in low tier — no strategist, no wave, no worker, no graph (§2.6) — the flow, the
// worker rail and the hypothesis tree are not shown. But a hidden surface SAYS WHY rather than
// vanishing, so the difference reads as a property of the tier, not a broken build. This is the
// §8 table as a pure function, which §11 test 5 asserts against.

export type SurfaceName = "chat" | "notebook" | "findings" | "tree" | "flow" | "worker" | "browser";

export interface SurfaceAvailability {
  name: SurfaceName;
  available: boolean;
  reason?: string; // present only when hidden — what the surface says instead of appearing
}

const LOW_ONLY_HIDDEN: Partial<Record<SurfaceName, string>> = {
  tree: "no hypothesis graph in low tier — one agent, driven by you, forms no graph (§2.6)",
  flow: "no flow in low tier — one agent is not a flow (§2.6)",
  worker: "no worker rail in low tier — there are no workers (§2.6)",
};

const ALL: SurfaceName[] = ["chat", "notebook", "findings", "tree", "flow", "worker", "browser"];

export function surfacesForTier(tier: string): SurfaceAvailability[] {
  const low = tier === "low";
  return ALL.map((name) => {
    const reason = low ? LOW_ONLY_HIDDEN[name] : undefined;
    return reason ? { name, available: false, reason } : { name, available: true };
  });
}

export function availableSurfaces(tier: string): SurfaceName[] {
  return surfacesForTier(tier)
    .filter((s) => s.available)
    .map((s) => s.name);
}
