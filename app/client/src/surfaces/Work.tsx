import { useEffect, useMemo, useRef, useState } from "react";
import { EngagementSubscription } from "../subscription";
import { useEngagement } from "../useEngagement";
import { glowingHypotheses } from "../work/glow";
import { api, ApiOptions } from "../api";

// Work — hypothesis tree, notebook and findings as one surface with three tabs (CLIENT_UI_DESIGN.md
// §6.2); they tear off together because they are one question looked at three ways. The tree gains
// exactly one live thing: a glow on the nodes a worker is on right now (pulses, moves nothing). The
// three read models come from the engagement's structured stores; the surface refetches the
// relevant one when its event fires (graph_node_changed / note_added / finding_recorded).
//
// Dragging on a live tree has the motion problem §6.2 names, solved rather than ignored: while a
// pointer is down the layout is frozen — incoming tree changes queue behind a "N new waiting"
// marker and land on release. The glow keeps pulsing throughout, because it moves nothing.

type Tab = "tree" | "notebook" | "findings";
const TAB_KEY = "oxpecker.work.tab";

// The glow is CSS, not JS: the browser runs the pulse on its own, so the tree re-renders only when
// the marking changes, never per frame. The color is the accent token from index.css, so a reskin
// changes it in one place. Reduced motion gets a still outline instead of the pulse.
const GLOW_CSS = `
@keyframes work-glow {
  0%, 100% { box-shadow: 0 0 0 0 var(--accent); }
  50% { box-shadow: 0 0 0 6px transparent; }
}
li[data-glowing="true"] { animation: work-glow 1.6s ease-in-out infinite; border-radius: 6px; }
@media (prefers-reduced-motion: reduce) {
  li[data-glowing="true"] { animation: none; outline: 1px solid var(--accent); }
}
`;

function rememberedTab(): Tab {
  try {
    const t = localStorage.getItem(TAB_KEY);
    if (t === "tree" || t === "notebook" || t === "findings") return t;
  } catch {
    /* private window / blocked storage: fall through to the default */
  }
  return "tree";
}

export function Work({
  subscription,
  engagementId,
  apiOptions = {},
}: {
  subscription: EngagementSubscription;
  engagementId: string;
  apiOptions?: ApiOptions;
}) {
  const projection = useEngagement(subscription);
  const [tab, setTab] = useState<Tab>(rememberedTab);
  const [graph, setGraph] = useState<any>({ exists: false, nodes: [], edges: [] });
  const [notebook, setNotebook] = useState<any>({ exists: false, notes: [] });
  const [findings, setFindings] = useState<any>({ findings: [], count: 0 });

  const dragging = useRef(false);
  const [pending, setPending] = useState(0);

  const refetchGraph = () => api.hypothesisGraph(engagementId, apiOptions).then(setGraph).catch(() => {});
  const refetchNotebook = () => api.notebook(engagementId, apiOptions).then(setNotebook).catch(() => {});
  const refetchFindings = () => api.findings(engagementId, apiOptions).then(setFindings).catch(() => {});

  // Initial load of all three read models.
  useEffect(() => {
    refetchGraph();
    refetchNotebook();
    refetchFindings();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engagementId]);

  // One signal per store, derived from the stream, so a refetch happens exactly when that store
  // changed rather than on every event.
  const graphSignal = useMemo(
    () => projection.events.filter((e) => e.kind === "graph_node_changed").length,
    [projection.events],
  );

  useEffect(() => {
    if (graphSignal === 0) return;
    if (dragging.current) {
      // Frozen layout: do not re-lay-out the tree under the operator's pointer. Count what is
      // waiting and apply it on release.
      setPending((n) => n + 1);
    } else {
      refetchGraph();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graphSignal]);

  useEffect(() => {
    if (projection.counts.note_added > 0) refetchNotebook();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projection.counts.note_added]);
  useEffect(() => {
    if (projection.counts.finding_recorded > 0) refetchFindings();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projection.counts.finding_recorded]);

  // The glow set: node ids a running worker is on right now (finished workers drop out).
  const glowing = glowingHypotheses(projection);

  function selectTab(t: Tab) {
    setTab(t);
    try {
      localStorage.setItem(TAB_KEY, t);
    } catch {
      /* ignore */
    }
  }

  return (
    <div className="surface" data-surface="work">
      <nav className="tabs" role="tablist">
        {(["tree", "notebook", "findings"] as Tab[]).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} data-testid={`tab-${t}`} onClick={() => selectTab(t)}>
            {t}
            {t === "notebook" ? ` (${notebook.notes?.length ?? 0})` : ""}
            {t === "findings" ? ` (${findings.count ?? 0})` : ""}
          </button>
        ))}
      </nav>

      {tab === "tree" && (
        <div
          data-testid="tree"
          onPointerDown={() => {
            dragging.current = true;
          }}
          onPointerUp={() => {
            dragging.current = false;
            if (pending > 0) {
              refetchGraph();
              setPending(0);
            }
          }}
        >
          <style>{GLOW_CSS}</style>
          {pending > 0 && <div data-testid="pending-marker">{pending} new waiting</div>}
          {/* Keyed by node identity, never position (§2.1 rule 4). */}
          <ul>
            {(graph.nodes ?? []).map((n: any) => {
              const id = String(n.ordinal ?? n.node ?? n.hypothesis_id);
              const isGlowing = glowing.has(id);
              return (
                <li key={id} data-node={id} data-glowing={isGlowing ? "true" : "false"} className={isGlowing ? "glow" : ""}>
                  {n.title ?? n.label ?? `H-${id}`} — {n.status ?? ""}
                </li>
              );
            })}
            {(graph.nodes ?? []).length === 0 && <li data-testid="tree-empty">no hypotheses yet</li>}
          </ul>
        </div>
      )}

      {tab === "notebook" && (
        <ul data-testid="notebook">
          {(notebook.notes ?? []).map((n: any) => (
            <li key={String(n.ordinal ?? n.note_id)}>
              [{n.category}] {n.note ?? n.text}
            </li>
          ))}
          {(notebook.notes ?? []).length === 0 && <li data-testid="notebook-empty">no notes yet</li>}
        </ul>
      )}

      {tab === "findings" && (
        <ul data-testid="findings">
          {(findings.findings ?? []).map((f: any) => (
            <li key={String(f.finding_id ?? f.id)}>
              {f.title ?? f.summary} {f.reviewed_by ? "✓ reviewed" : "· unreviewed"}
            </li>
          ))}
          {(findings.findings ?? []).length === 0 && <li data-testid="findings-empty">no findings yet</li>}
        </ul>
      )}
    </div>
  );
}
