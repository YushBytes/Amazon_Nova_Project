"use client";

/**
 * Propose, review and decide a remediation for one finding.
 *
 * Phase 8. Rendered inside `ViolationList` under the finding it addresses, so
 * a reviewer sees the evidence and the proposed change together rather than
 * on separate screens.
 *
 * WHAT THIS COMPONENT WILL NOT DO
 * -------------------------------
 * It never describes a video as corrected, fixed or compliant. The backend
 * answers that question in `is_compliance_claim` and it is false for every
 * state this build can reach, so the only honest rendering of an approved
 * proposal is "approved, nothing generated".
 *
 * Generation is offered only when `capabilities.generation_available` is true,
 * which it is not on any current deployment. When it is false the panel says
 * so, with the backend's own reason, instead of showing a button that would
 * 503. Disabling that control is a courtesy, NOT the security boundary: the
 * backend refuses an unapproved or unavailable execution on its own, and the
 * route tests assert that directly.
 */

import React from "react";
import { AlertTriangle, Check, Loader2, Wrench, X } from "lucide-react";
import { cn } from "../lib/utils";
import {
  ApiError,
  createRemediation,
  decideRemediation,
  describeError,
  executeRemediation,
  isAbortError,
  type Detection,
  type Remediation,
  type RemediationCapabilities,
} from "../lib/api";
import { formatDateTime } from "../lib/jobs";
import {
  canCancel,
  canExecute,
  claimsCompliance,
  gateLabel,
  remediationLabel,
  toneForGate,
  toneForRemediation,
} from "../lib/remediation";
import { StatusBadge } from "./ui/Feedback";

type Busy = "propose" | "approve" | "reject" | "cancel" | "execute" | null;

export default function RemediationPanel({
  videoId,
  detection,
  capabilities,
  record,
  onChanged,
}: {
  videoId: string;
  detection: Detection;
  /** Null while capabilities are still loading; undefined if unreadable. */
  capabilities: RemediationCapabilities | null | undefined;
  /** The existing record for this finding, if one has been created. */
  record: Remediation | null;
  /** Hand the updated record up so one owner holds the list. */
  onChanged: (record: Remediation) => void;
}) {
  const [busy, setBusy] = React.useState<Busy>(null);
  const [error, setError] = React.useState<string | null>(null);

  // Aborts an in-flight decision when the panel goes away, so a late response
  // cannot update a gone component.
  const abortRef = React.useRef<AbortController | null>(null);
  // Belt and braces alongside the abort, and the same guard the other views
  // use: an abort depends on the transport honouring the signal, and a late
  // response must not reach `onChanged` even if it does not.
  const mountedRef = React.useRef(true);
  React.useEffect(
    () => () => {
      mountedRef.current = false;
      abortRef.current?.abort();
      abortRef.current = null;
    },
    []
  );

  const frameNumber = detection.frame_number;
  const detectionIndex = detection.detection_index;

  // Without both, the finding has no address the remediation API can take.
  // This is a real state for a legacy response rather than something to
  // paper over, so the panel says so and offers nothing.
  const addressable =
    typeof frameNumber === "number" && typeof detectionIndex === "number";

  const run = async (
    action: Busy,
    operation: (signal: AbortSignal) => Promise<Remediation>
  ) => {
    if (busy) return;
    setBusy(action);
    setError(null);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const updated = await operation(controller.signal);
      if (!mountedRef.current) return;
      onChanged(updated);
    } catch (err) {
      if (isAbortError(err) || !mountedRef.current) return;
      // A 409 is the backend telling us the record moved underneath us, which
      // is information rather than a fault; its detail says what happened.
      setError(
        describeError(
          err,
          err instanceof ApiError && err.status === 409
            ? "This proposal changed; reload to see its current state."
            : "That action could not be completed."
        )
      );
    } finally {
      if (mountedRef.current) setBusy(null);
    }
  };

  if (!capabilities) {
    // Null while loading, undefined when the capability read failed. Either
    // way, offering a control would be guessing at what the server supports.
    return null;
  }

  if (!addressable) {
    return (
      <p className="text-[10px] uppercase tracking-widest text-white/25">
        This finding predates remediation support and cannot be addressed.
      </p>
    );
  }

  // ---------------------------------------------------------------- propose
  if (!record) {
    return (
      <div className="space-y-3">
        <button
          type="button"
          data-testid={`propose-${frameNumber}-${detectionIndex}`}
          disabled={busy !== null}
          onClick={() =>
            run("propose", (signal) =>
              createRemediation(videoId, frameNumber, detectionIndex, { signal })
            )
          }
          className={cn(
            "inline-flex items-center gap-3 rounded-xl border border-white/10 bg-white/5",
            "px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-white/70",
            "transition-colors hover:bg-white/10 hover:text-white disabled:opacity-50",
            "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
          )}
        >
          {busy === "propose" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <Wrench className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          <span>
            {busy === "propose" ? "Preparing..." : "Propose a remediation"}
          </span>
        </button>
        {error && <PanelError message={error} />}
      </div>
    );
  }

  // ----------------------------------------------------------------- review
  const proposal = record.proposal;
  const tone = toneForRemediation(record.state);
  // Defensive: no state this build can reach sets it, and if a future one
  // does the claim must come from the server, not from this component.
  const compliant = claimsCompliance(record);

  return (
    <section
      aria-label={`Remediation for frame ${frameNumber}`}
      data-testid={`remediation-${record.remediation_id}`}
      className="space-y-4 rounded-2xl border border-white/10 bg-white/[0.02] p-5"
    >
      <div className="flex flex-wrap items-center gap-3">
        <Wrench className="h-3.5 w-3.5 shrink-0 text-primary" aria-hidden="true" />
        <h5 className="text-[10px] font-bold uppercase tracking-widest text-white/50">
          Remediation
        </h5>
        <StatusBadge label={remediationLabel(record.state)} tone={tone} />
        {!compliant && (
          /* Stated, not implied. A reader who sees "Approved" must not take
             it for "resolved". */
          <StatusBadge label="Video unchanged" tone="neutral" />
        )}
      </div>

      <p className="text-[11px] font-semibold leading-relaxed text-white/70">
        {proposal.summary}
      </p>
      <p className="text-[11px] leading-relaxed text-white/40">
        {proposal.rationale}
      </p>
      <p className="text-[11px] leading-relaxed text-white/40">
        <span className="font-bold text-white/30">Target: </span>
        {proposal.target_description}
      </p>

      <div className="flex flex-wrap items-center gap-3">
        <StatusBadge
          label={gateLabel(proposal.confidence_gate)}
          tone={toneForGate(proposal.confidence_gate)}
        />
        <span className="text-[10px] text-white/30">{record.stage}</span>
      </div>

      {/* The model's own words, attributed and kept separate from the
          rationale - an explanation is not evidence that the finding is real. */}
      <blockquote className="border-l-2 border-white/10 pl-4 text-[11px] italic leading-relaxed text-white/35">
        <span className="not-italic font-bold uppercase tracking-widest text-white/25">
          Model said:{" "}
        </span>
        {proposal.model_claim}
      </blockquote>

      <p className="rounded-xl border border-white/5 bg-white/[0.02] p-4 text-[10px] leading-relaxed text-white/35">
        {proposal.limitations}
      </p>

      {record.approved_by && (
        <p className="text-[10px] uppercase tracking-widest text-white/30">
          Approved by {record.approved_by} · {formatDateTime(record.approved_at)}
        </p>
      )}
      {record.decision_note && (
        <p className="text-[11px] text-white/40">
          <span className="font-bold text-white/30">Note: </span>
          {record.decision_note}
        </p>
      )}
      {record.error_summary && (
        <p
          role="status"
          className="rounded-xl border border-white/10 bg-white/[0.02] p-4 text-[11px] leading-relaxed text-white/50"
        >
          {record.error_summary}
        </p>
      )}

      {/* Decisions */}
      {record.state === "proposed" && (
        <div className="flex flex-wrap items-center gap-3">
          <DecisionButton
            testId={`approve-${record.remediation_id}`}
            label="Approve"
            busyLabel="Approving..."
            busy={busy === "approve"}
            disabled={busy !== null}
            icon={Check}
            onClick={() =>
              run("approve", (signal) =>
                decideRemediation(record.remediation_id, "approve", undefined, {
                  signal,
                })
              )
            }
          />
          <DecisionButton
            testId={`reject-${record.remediation_id}`}
            label="Reject"
            busyLabel="Rejecting..."
            busy={busy === "reject"}
            disabled={busy !== null}
            icon={X}
            onClick={() =>
              run("reject", (signal) =>
                decideRemediation(record.remediation_id, "reject", undefined, {
                  signal,
                })
              )
            }
          />
        </div>
      )}

      {canExecute(record) && (
        <div className="space-y-3">
          {capabilities.generation_available ? (
            <DecisionButton
              testId={`execute-${record.remediation_id}`}
              label="Generate the corrected frame"
              busyLabel="Starting..."
              busy={busy === "execute"}
              disabled={busy !== null}
              icon={Wrench}
              onClick={() =>
                run("execute", (signal) =>
                  executeRemediation(record.remediation_id, { signal })
                )
              }
            />
          ) : (
            /* No control at all, rather than one that would 503. The reason
               is the backend's own words. */
            <p
              data-testid="generation-unavailable"
              className="rounded-xl border border-white/10 bg-white/[0.02] p-4 text-[10px] leading-relaxed text-white/40"
            >
              <span className="font-bold uppercase tracking-widest text-white/30">
                Not available:{" "}
              </span>
              {capabilities.generation_unavailable_reason ??
                "Automated image generation is not configured on this deployment."}
            </p>
          )}
        </div>
      )}

      {canCancel(record) && record.state === "approved" && (
        <DecisionButton
          testId={`cancel-${record.remediation_id}`}
          label="Withdraw approval"
          busyLabel="Withdrawing..."
          busy={busy === "cancel"}
          disabled={busy !== null}
          icon={X}
          onClick={() =>
            run("cancel", (signal) =>
              decideRemediation(record.remediation_id, "cancel", undefined, {
                signal,
              })
            )
          }
        />
      )}

      {error && <PanelError message={error} />}
    </section>
  );
}

function PanelError({ message }: { message: string }) {
  return (
    <p
      role="alert"
      className="flex items-start gap-3 rounded-xl border border-critical/30 bg-critical/5 p-4 text-[11px] font-semibold leading-relaxed text-critical"
    >
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span>{message}</span>
    </p>
  );
}

function DecisionButton({
  testId,
  label,
  busyLabel,
  busy,
  disabled,
  icon: Icon,
  onClick,
}: {
  testId: string;
  label: string;
  busyLabel: string;
  busy: boolean;
  disabled: boolean;
  icon: React.ComponentType<{ className?: string }>;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-3 rounded-xl border border-white/10 bg-white/5",
        "px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-white/70",
        "transition-colors hover:bg-white/10 hover:text-white disabled:opacity-50",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
      )}
    >
      {busy ? (
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
      ) : (
        <Icon className="h-3.5 w-3.5" />
      )}
      <span>{busy ? busyLabel : label}</span>
    </button>
  );
}
