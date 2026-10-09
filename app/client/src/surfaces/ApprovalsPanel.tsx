import { useState } from "react";
import { Projection } from "../projection";
import { api, ApiOptions } from "../api";

// Approvals, one request → one state → three renderings (CLIENT_UI_DESIGN.md §7): this one
// component is mounted in chat, in the approvals drawer, and (as a count) on the flow node, so the
// operator sees a pending request wherever they are looking. It reads the pending approvals out of
// the projection — the same state every surface holds — and the approve/deny buttons send the
// decision over REST to the resolve endpoint keyed by the request id the event carried.
//
// Read-and-command, never read-and-write-the-stream: the resolution comes back as an
// approval_resolved event and the button's row disappears when the projection folds it, rather
// than the component mutating its own state. So a decision made in the chat rendering clears the
// drawer rendering too, with no cross-wiring.
export function ApprovalsPanel({
  projection,
  apiOptions = {},
  compact = false,
}: {
  projection: Projection;
  apiOptions?: ApiOptions;
  compact?: boolean;
}) {
  const pending = Object.values(projection.approvals).filter((a) => a.status === "pending");
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  async function resolve(requestId: string, approved: boolean) {
    setBusy((b) => ({ ...b, [requestId]: true }));
    try {
      await api.resolveApproval(requestId, approved, apiOptions);
      // Nothing else to do: the approval_resolved event will fold the row out of `pending`.
    } catch {
      setBusy((b) => ({ ...b, [requestId]: false })); // let the operator try again
    }
  }

  if (compact) {
    // The flow-node rendering: just how many are blocked, which is what decides urgency (§7).
    return (
      <span data-testid="approvals-badge" className="approvals-badge">
        {pending.length > 0 ? `⚠ ${pending.length} awaiting approval` : ""}
      </span>
    );
  }

  return (
    <div className="approvals" data-testid="approvals-panel">
      <h2>Approvals {pending.length > 0 ? `(${pending.length})` : ""}</h2>
      {pending.length === 0 && <div data-testid="approvals-empty">nothing awaiting approval</div>}
      <ul>
        {pending.map((a) => (
          <li key={String(a.request_id)} data-request={String(a.request_id)}>
            <span className="approval-what">
              {String(a.tool ?? "action")}
              {a.note ? ` ${a.note}` : ""}
              {a.argument_digest ? ` · ${a.argument_digest}` : ""}
            </span>
            <button
              data-testid={`approve-${a.request_id}`}
              disabled={busy[String(a.request_id)]}
              onClick={() => resolve(String(a.request_id), true)}
            >
              Approve
            </button>
            <button
              data-testid={`deny-${a.request_id}`}
              disabled={busy[String(a.request_id)]}
              onClick={() => resolve(String(a.request_id), false)}
            >
              Deny
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
