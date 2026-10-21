"use client";

/**
 * Propose, review, decide, build and check a remediation for one finding.
 *
 * Phase 8 built the review half. Phase 9 added the rest: supplying the
 * replacement image, building a corrected video, and showing what the
 * re-analysis of THAT video found.
 *
 * WHAT THIS COMPONENT WILL NOT SAY
 * --------------------------------
 * In order of how damaging each would be:
 *
 * * "corrected" or "compliant" before the backend's own
 *   `is_compliance_claim` is true, which happens only in `verified`;
 * * "verified" from a state string - the verdict and its evidence come from
 *   `record.verification`, written server-side from the output's persisted
 *   analysis;
 * * anything resembling success for `applied`, which means a new video exists
 *   and NOTHING has checked it;
 * * "compliant" when the target is gone but other findings remain - those are
 *   separate facts and both are shown;
 * * a percentage. Progress is the backend's named stage for the output's
 *   analysis job, or nothing;
 * * a download link before a real authorized output exists. The URL is
 *   fetched on demand and 404s until there is one.
 *
 * The original video is never modified, and the panel says so at the point
 * where a reader would most want reassurance - while a new video is being
 * built.
 */

import React from "react";
import {
  AlertTriangle,
  Check,
  Clock,
  Film,
  Loader2,
  Upload,
  Wrench,
  X,
} from "lucide-react";
import { cn } from "../lib/utils";
import {
  ApiError,
  createRemediation,
  decideRemediation,
  describeError,
  fetchRemediationOutput,
  fetchRemediationStatus,
  isAbortError,
  startRemediationExecution,
  uploadRemediationAsset,
  validateReplacementImage,
  type Detection,
  type Remediation,
  type RemediationCapabilities,
  type RemediationOutput,
  type RemediationStatus,
} from "../lib/api";
import { formatDateTime } from "../lib/jobs";
import {
  canCancel,
  canExecute,
  canRetryExecution,
  claimsCompliance,
  gateLabel,
  hasOutput,
  isExecuting,
  needsAsset,
  otherFindings,
  remediationLabel,
  toneForGate,
  toneForRemediation,
  verdictSummary,
} from "../lib/remediation";
import { StatusBadge } from "./ui/Feedback";
import ViolationList from "./ViolationList";

type Busy =
  | "propose"
  | "approve"
  | "reject"
  | "cancel"
  | "asset"
  | "execute"
  | "output"
  | null;

/**
 * How often the status is re-read while work is in flight.
 *
 * Polled, not pushed. Each poll is scheduled only after the previous one
 * settles, so requests cannot overlap or stack up behind a slow response.
 */
const POLL_INTERVAL_MS = 4000;
// ~10 minutes. A record still active after this is left alone rather than
// polled forever; a reload picks it up again.
const MAX_POLLS = 150;

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
  const [fileError, setFileError] = React.useState<string | null>(null);
  const [output, setOutput] = React.useState<RemediationOutput | null>(null);
  const [analysis, setAnalysis] = React.useState<
    RemediationStatus["output_analysis"]
  >(null);
  const fileInputRef = React.useRef<HTMLInputElement>(null);

  // Aborts in-flight work when the panel goes away.
  const abortRef = React.useRef<AbortController | null>(null);
  // Belt and braces alongside the abort: an abort depends on the transport
  // honouring the signal, and a late response must not reach `onChanged`.
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
  const addressable =
    typeof frameNumber === "number" && typeof detectionIndex === "number";

  const remediationId = record?.remediation_id;
  const shouldPoll = Boolean(record && isExecuting(record));

  // ------------------------------------------------------------------ poll
  React.useEffect(() => {
    if (!shouldPoll || !remediationId) {
      setAnalysis(null);
      return;
    }

    let cancelled = false;
    let polls = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const controller = new AbortController();

    const poll = async () => {
      if (cancelled) return;
      polls += 1;
      try {
        const status = await fetchRemediationStatus(remediationId, {
          signal: controller.signal,
        });
        if (cancelled) return;
        setAnalysis(status.output_analysis);
        onChanged(status);
        // Terminal, or no longer executing: stop. The record's own state is
        // the authority on that, never a local guess.
        if (status.is_terminal || !isExecuting(status)) return;
      } catch (err) {
        if (cancelled || isAbortError(err)) return;
        if (err instanceof ApiError && (err.isUnauthorized || err.isNotFound)) {
          // Nothing more to poll for; stop rather than hammering a route that
          // will keep refusing.
          return;
        }
        // A transient failure is not a failed correction. Keep waiting.
      }

      if (polls >= MAX_POLLS) return;
      timer = setTimeout(poll, POLL_INTERVAL_MS);
    };

    void poll();

    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
      controller.abort();
    };
  }, [shouldPoll, remediationId, onChanged]);

  // Fetch the playback URL only once an output really exists.
  React.useEffect(() => {
    if (!record || !hasOutput(record) || !remediationId) {
      setOutput(null);
      return;
    }
    let cancelled = false;
    const controller = new AbortController();

    (async () => {
      try {
        const result = await fetchRemediationOutput(remediationId, {
          signal: controller.signal,
        });
        if (!cancelled) setOutput(result);
      } catch (err) {
        // No placeholder: if the URL cannot be had, no player is offered.
        if (!cancelled && !isAbortError(err)) setOutput(null);
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [record, remediationId]);

  // ------------------------------------------------------------------ run
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

  const onFileChosen = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file || !remediationId) return;

    const problem = validateReplacementImage(file, capabilities);
    if (problem) {
      setFileError(problem);
      return;
    }
    setFileError(null);
    void run("asset", (signal) =>
      uploadRemediationAsset(remediationId, file, { signal })
    );
  };

  if (!capabilities) return null;

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
          className={actionClass}
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
  const compliant = claimsCompliance(record);
  const verdict = verdictSummary(record);
  const others = otherFindings(record);
  const reconstruction = record.reconstruction;

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
        <StatusBadge
          label={remediationLabel(record.state)}
          tone={toneForRemediation(record.state)}
        />
        {/* The original is never modified by any phase; said plainly at every
            stage, because this is where a reader would worry. */}
        <StatusBadge label="Original unchanged" tone="neutral" />
        {!compliant && record.state !== "proposed" && (
          <StatusBadge
            label="Not a compliance sign-off"
            tone="neutral"
            title="Only a completed check of the new video can establish that, and only for the finding it targeted."
          />
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

      {/* Decisions */}
      {record.state === "proposed" && (
        <div className="flex flex-wrap items-center gap-3">
          <ActionButton
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
          <ActionButton
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

      {/* ------------------------------------------------ supply an asset */}
      {needsAsset(record) && (
        <div className="space-y-3 rounded-xl border border-white/10 bg-white/[0.02] p-4">
          <p className="text-[10px] font-bold uppercase tracking-widest text-white/40">
            Replacement image
          </p>
          {capabilities.generation_available ? (
            <p className="text-[11px] leading-relaxed text-white/40">
              An image can be generated for this finding, or you can supply
              your own.
            </p>
          ) : (
            /* Honest about why there is an upload here rather than a
               "generate" button. */
            <p
              data-testid="generation-unavailable"
              className="text-[11px] leading-relaxed text-white/40"
            >
              <span className="font-bold uppercase tracking-widest text-white/30">
                Automatic generation unavailable:{" "}
              </span>
              {capabilities.generation_unavailable_reason ??
                "No image provider is configured on this deployment."}{" "}
              Supply the replacement image yourself to continue.
            </p>
          )}

          {capabilities.reconstruction_available === false ? (
            <p
              data-testid="reconstruction-unavailable"
              className="text-[11px] leading-relaxed text-white/40"
            >
              {capabilities.reconstruction_unavailable_reason ??
                "Building corrected videos is not enabled on this deployment."}
            </p>
          ) : (
            <>
              <label htmlFor={`asset-${record.remediation_id}`} className="sr-only">
                Replacement image for this finding
              </label>
              <input
                id={`asset-${record.remediation_id}`}
                ref={fileInputRef}
                type="file"
                accept={(capabilities.accepted_asset_types ?? []).join(",")}
                className="sr-only"
                onChange={onFileChosen}
              />
              <button
                type="button"
                data-testid={`upload-asset-${record.remediation_id}`}
                disabled={busy !== null}
                onClick={() => fileInputRef.current?.click()}
                className={actionClass}
              >
                {busy === "asset" ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                ) : (
                  <Upload className="h-3.5 w-3.5" aria-hidden="true" />
                )}
                <span>
                  {busy === "asset"
                    ? "Uploading..."
                    : "Choose a replacement image"}
                </span>
              </button>
              <p className="text-[10px] uppercase tracking-widest text-white/25">
                PNG or JPEG, up to{" "}
                {Math.floor((capabilities.max_asset_bytes ?? 0) / (1024 * 1024))}{" "}
                MB
              </p>
            </>
          )}

          {fileError && (
            <p
              role="alert"
              data-testid="asset-file-error"
              className="text-[10px] font-bold uppercase tracking-widest text-critical"
            >
              {fileError}
            </p>
          )}
        </div>
      )}

      {record.has_asset && (
        <p className="text-[10px] uppercase tracking-widest text-white/30">
          Replacement image attached
          {record.asset_source === "upload" ? " by you" : ""}
          {record.asset_supplied_at
            ? ` · ${formatDateTime(record.asset_supplied_at)}`
            : ""}
        </p>
      )}

      {/* --------------------------------------------- build the new video */}
      {canExecute(record) && capabilities.reconstruction_available !== false && (
        <div className="space-y-3">
          <ActionButton
            testId={`execute-${record.remediation_id}`}
            label="Build the new video"
            busyLabel="Starting..."
            busy={busy === "execute"}
            disabled={busy !== null}
            icon={Film}
            onClick={() =>
              run("execute", (signal) =>
                startRemediationExecution(record.remediation_id, { signal })
              )
            }
          />
          <p className="text-[10px] leading-relaxed text-white/30">
            This creates a NEW video. Your original is left exactly as
            uploaded. The new video is then analysed in its own right, and only
            that analysis can say whether the finding is gone.
          </p>
        </div>
      )}

      {/* ------------------------------------------------------- progress */}
      {isExecuting(record) && (
        <div
          role="status"
          aria-live="polite"
          data-testid={`progress-${record.remediation_id}`}
          className="space-y-2 rounded-xl border border-accent/30 bg-accent/5 p-4"
        >
          <p className="flex items-center gap-3 text-[10px] font-bold uppercase tracking-widest text-accent">
            <Clock className="h-3.5 w-3.5" aria-hidden="true" />
            {record.stage}
          </p>
          {/* The backend's named stage for the OUTPUT's analysis job. Never a
              percentage: the frame total is unknown until extraction ends. */}
          {analysis && (
            <p className="text-[10px] uppercase tracking-widest text-accent/70">
              New video analysis: {analysis.stage}
              {analysis.frames_extracted != null && (
                <> · {analysis.frames_extracted} frame(s) extracted</>
              )}
            </p>
          )}
          {(record.recovered_count ?? 0) > 0 && (
            <p className="text-[10px] uppercase tracking-widest text-accent/70">
              Automatically restarted {record.recovered_count} time(s) after an
              interrupted run.
            </p>
          )}
        </div>
      )}

      {/* --------------------------------------------------- the new video */}
      {hasOutput(record) && (
        <div className="space-y-3 rounded-xl border border-white/10 bg-white/[0.02] p-4">
          <p className="text-[10px] font-bold uppercase tracking-widest text-white/40">
            The new video
          </p>
          {output ? (
            <video
              data-testid={`output-video-${record.remediation_id}`}
              src={output.playback_url}
              controls
              preload="metadata"
              aria-label="The new video built for this remediation"
              className="w-full rounded-lg"
            />
          ) : (
            /* No placeholder player and no dead link. */
            <p className="text-[10px] uppercase tracking-widest text-white/25">
              Preparing playback...
            </p>
          )}
          {reconstruction && (
            <dl className="flex flex-wrap gap-x-6 gap-y-1 text-[10px] uppercase tracking-widest text-white/30">
              <div className="flex gap-2">
                <dt className="font-bold text-white/25">Method</dt>
                {/* Named plainly. An overlay conceals a region; it does not
                    inpaint, and the word "seamless" never appears. */}
                <dd>
                  {reconstruction.method === "scaled_overlay"
                    ? "Scaled overlay over the approved region"
                    : reconstruction.method}
                </dd>
              </div>
              {reconstruction.window_seconds && (
                <div className="flex gap-2">
                  <dt className="font-bold text-white/25">Applied</dt>
                  <dd>
                    {reconstruction.window_seconds[0]}s–
                    {reconstruction.window_seconds[1]}s
                  </dd>
                </div>
              )}
              <div className="flex gap-2">
                <dt className="font-bold text-white/25">Audio</dt>
                <dd>
                  {reconstruction.audio_preserved
                    ? "Preserved"
                    : reconstruction.source_had_audio === false
                      ? "None in the original"
                      : "Not preserved"}
                </dd>
              </div>
            </dl>
          )}
        </div>
      )}

      {/* ----------------------------------------------------- the verdict */}
      {verdict && (
        <div
          data-testid={`verdict-${record.remediation_id}`}
          className={cn(
            "space-y-3 rounded-xl border p-4",
            compliant
              ? "border-success/30 bg-success/5"
              : record.state === "verification_failed"
                ? "border-critical/30 bg-critical/5"
                : "border-white/10 bg-white/[0.02]"
          )}
        >
          <p
            className={cn(
              "text-[10px] font-bold uppercase tracking-widest",
              compliant
                ? "text-success"
                : record.state === "verification_failed"
                  ? "text-critical"
                  : "text-white/40"
            )}
          >
            {compliant
              ? "The targeted finding is gone"
              : record.state === "verification_failed"
                ? "The targeted finding is still there"
                : "No conclusion"}
          </p>
          <p className="text-[11px] leading-relaxed text-white/50">{verdict}</p>

          {record.verification?.frames_audited != null && (
            <p className="text-[10px] uppercase tracking-widest text-white/25">
              Based on {record.verification.frames_audited} audited frame(s) of
              the new video
            </p>
          )}

          {/* Separate from the verdict, deliberately: "the logo is gone" and
              "the video is fine" are different statements, and the second is
              not implied by the first. */}
          {others.length > 0 && (
            <div className="space-y-3 border-t border-white/10 pt-3">
              <p
                data-testid={`other-findings-${record.remediation_id}`}
                className="text-[10px] font-bold uppercase tracking-widest text-white/40"
              >
                {others.length} other finding(s) in the new video
              </p>
              <p className="text-[10px] leading-relaxed text-white/30">
                These were reported by the same analysis. They are not what this
                remediation targeted, and they are not resolved.
              </p>
              <ViolationList detections={others} />
            </div>
          )}
        </div>
      )}

      {record.error_summary && !verdict && (
        <p
          role="status"
          data-testid={`error-summary-${record.remediation_id}`}
          className="rounded-xl border border-white/10 bg-white/[0.02] p-4 text-[11px] leading-relaxed text-white/50"
        >
          {record.error_summary}
        </p>
      )}

      {/* A retry is offered only for a build that failed, never for a check
          that came back negative - the same asset would build the same video. */}
      {canRetryExecution(record) && (
        <ActionButton
          testId={`retry-${record.remediation_id}`}
          label="Try building it again"
          busyLabel="Starting..."
          busy={busy === "execute"}
          disabled={busy !== null}
          icon={Film}
          onClick={() =>
            run("execute", (signal) =>
              startRemediationExecution(record.remediation_id, { signal })
            )
          }
        />
      )}

      {canCancel(record) && record.state !== "proposed" && (
        <ActionButton
          testId={`cancel-${record.remediation_id}`}
          label="Withdraw"
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

const actionClass = cn(
  "inline-flex items-center gap-3 rounded-xl border border-white/10 bg-white/5",
  "px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-white/70",
  "transition-colors hover:bg-white/10 hover:text-white disabled:opacity-50",
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
);

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

function ActionButton({
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
      className={actionClass}
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
