// §11 test 5 (tier surfacing): each tier offers exactly the surfaces in the §8 table — the parity
// partner to the broker/audit/evidence tier-parity guard on the server side. Both directions are
// pinned: low tier hides the three graph/wave/worker surfaces (and says why), and medium/high
// offer every surface.

import { expect, test } from "vitest";
import { surfacesForTier, availableSurfaces } from "./surfacing";

test("medium and high offer every surface", () => {
  for (const tier of ["medium", "high"]) {
    expect(availableSurfaces(tier).sort()).toEqual(
      ["browser", "chat", "findings", "flow", "notebook", "tree", "worker"].sort(),
    );
  }
});

test("low hides exactly the tree, flow and worker rail — and each hidden one says why", () => {
  expect(availableSurfaces("low").sort()).toEqual(["browser", "chat", "findings", "notebook"].sort());
  const hidden = surfacesForTier("low").filter((s) => !s.available);
  expect(hidden.map((s) => s.name).sort()).toEqual(["flow", "tree", "worker"]);
  // "Say why", not vanish — every hidden surface carries a reason.
  expect(hidden.every((s) => (s.reason ?? "").length > 0)).toBe(true);
});

test("an unknown tier is treated as full rather than empty", () => {
  // A surface with nothing is worse than one not offered, but a surface wrongly withheld because
  // of a tier typo is worse still — so default to showing, matching the server's normalize.
  expect(availableSurfaces("high")).toContain("flow");
  expect(availableSurfaces("").sort()).toEqual(availableSurfaces("high").sort());
});
