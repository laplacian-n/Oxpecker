import { EngagementEvent } from "../projection";

// RunActivity — the autonomous run, narrated in the chat.
//
// In autonomous mode the conversation transcript is empty: the operator does not type and the
// agent does not reply in the session stream. All the work lands on the ENGAGEMENT event stream
// (projection.events) — phases entered, the surface enumerated, workers spawned and finished,
// tools called, findings recorded — and until now that only showed as a raw "#seq kind — detail"
// feed in the tools drawer. So the chat had no role in a run; you watched a log, not an assistant.
//
// This turns those same events into a readable, assistant-style activity narration, rendered in the
// chat itself: "Entered RECON", "Spawned worker w0: verify missing CSP", "Worker w0 finished". It
// reads the one data path everything else reads (§6.5) — no new stream — and an unknown kind is
// still shown (humanised) rather than dropped, so a newly added event never silently vanishes.

interface Line {
  icon: string;
  text: string;
}

function humanize(kind: string): string {
  const s = kind.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// These carry no operator-facing meaning on their own (they drive the flow/worker views, or they
// are the closing half of a start/finish pair already narrated), so narrating them is just noise.
const SKIP = new Set([
  "tool_call_finished", "note_added", "graph_node_changed", "context", "steer_applied",
  "model_call", "compacted", "heartbeat",
]);

/** One event → one narration line, or null to skip it. Exported for the test: the mapping is the
 *  whole behaviour here, so it is asserted directly rather than through the rendered list. */
export function narrate(event: EngagementEvent): Line | null {
  if (SKIP.has(event.kind)) return null;
  const p = (event.payload ?? {}) as Record<string, any>;
  const d = typeof p.detail === "string" ? p.detail : "";
  const worker = p.worker_id ?? p.worker ?? "";
  switch (event.kind) {
    case "engagement_bootstrapped":
      return { icon: "·", text: d || "Prepared the engagement" };
    case "phase_entered":
      return { icon: "◆", text: `Entered ${p.phase ?? "a phase"}${d ? ` — ${d}` : ""}` };
    case "phase_advanced":
      return { icon: "→", text: d || `Advanced to ${p.to_phase ?? p.phase ?? "the next phase"}` };
    case "recon_enumerated":
      return { icon: "🔍", text: d || "Recon enumerated the attack surface" };
    case "worker_spawned": {
      const what = p.task ?? p.title ?? d;
      return { icon: "🤖", text: `Spawned worker ${worker}${what ? `: ${what}` : ""}`.trim() };
    }
    case "worker_finished":
      return { icon: "✓", text: `Worker ${worker} finished${d ? `: ${d}` : ""}`.trim() };
    case "tool_call_started":
      return { icon: "↳", text: `${worker ? `${worker} → ` : ""}${p.tool ?? "tool"}${p.target ? ` ${p.target}` : ""}` };
    case "finding_recorded":
      return { icon: "🔺", text: d || "Recorded a finding" };
    case "findings_promoted":
      return { icon: "🔺", text: d || "Promoted candidate findings" };
    case "finding_gate_done":
      return { icon: "✓", text: d || "Verified the findings" };
    case "finding_gate_skipped":
      return { icon: "·", text: d || "Finding gate skipped" };
    case "artifact_stored":
      return { icon: "📄", text: `Saved ${p.artifact ?? "output"}${worker ? ` (${worker})` : ""}` };
    case "approval_required":
      return { icon: "⚠", text: `Needs your approval${d ? `: ${d}` : ""}` };
    case "approval_resolved":
      return { icon: "✓", text: d || "Approval resolved" };
    case "run_complete":
    case "autonomous_done":
      return { icon: "✅", text: d || "Run complete" };
    case "already_closed_out":
      return { icon: "ℹ", text: d || "This engagement is already closed out" };
    case "blocked":
    case "stopped_by_operator":
    case "run_error":
    case "wall_clock_exceeded":
      return { icon: "⛔", text: d || humanize(event.kind) };
    default:
      // Unknown kinds still show, humanised, so a newly added event is visible rather than dropped.
      return { icon: "·", text: `${humanize(event.kind)}${d ? ` — ${d}` : ""}` };
  }
}

export function RunActivity({ events }: { events: EngagementEvent[] }) {
  const lines = events
    .map((e) => ({ seq: e.seq, line: narrate(e) }))
    .filter((x): x is { seq: number; line: Line } => x.line !== null);
  if (lines.length === 0) return null;
  return (
    <ol className="run-activity" data-testid="run-activity">
      {lines.map(({ seq, line }) => (
        <li key={seq} className="activity-row">
          <span className="activity-icon" aria-hidden="true">{line.icon}</span>
          <span className="activity-text">{line.text}</span>
        </li>
      ))}
    </ol>
  );
}
