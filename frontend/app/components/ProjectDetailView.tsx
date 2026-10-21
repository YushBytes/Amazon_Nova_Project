"use client";

/**
 * One upload's real audit result.
 *
 * Phase 7 removed the most serious fabrication in the frontend. This view
 * rendered an unconditional success card:
 *
 *     ShieldCheck  "AUDIT PASSED"
 *     "The content was verified by NovaFlow Neural Agent #{id.slice(-3)}.
 *      Compliance & quality standards met. Frame consistency scan validated."
 *
 * It was shown for every video regardless of whether analysis had ever been
 * requested, was still running, or had failed. There is no neural agent, no
 * frame-consistency scan, and no endpoint that returns a pass. That directly
 * contradicts the project rule that an incomplete or failed analysis must
 * never be labelled compliant.
 *
 * Also removed: "Encoding: H.265 Neural" (nothing probes the codec),
 * "Sync Integrity 99.8%" (no such measurement exists), a "Turbo Boost
 * Optimization" toggle wired to nothing, and a "Post to Social" button for a
 * capability the backend does not have.
 *
 * What is shown instead comes from two real endpoints:
 *   GET /jobs/{video_id}       the authoritative state and counts
 *   GET /violations/{video_id} the persisted detections
 *
 * The verdict is computed by `verdictFor`, which refuses to call anything
 * clean unless the job state is "completed".
 */

import React from "react";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Clock,
  Database,
  HardDrive,
  Hourglass,
  Loader2,
  ShieldQuestion,
} from "lucide-react";
import { cn } from "../lib/utils";
import {
  describeError,
  fetchAllViolations,
  fetchJob,
  formatBytes,
  isAbortError,
  retryAnalysis,
  startAnalysis,
  type Detection,
  type Job,
  type Submission,
} from "../lib/api";
import {
  VERDICT_LABELS,
  formatDateTime,
  toneForVerdict,
  verdictFor,
  type Verdict,
} from "../lib/jobs";
import { EmptyState, ErrorBanner, LoadingPanel, StatusBadge } from "./ui/Feedback";
import ViolationList from "./ViolationList";

export type { Submission };

/** Icon per verdict. Paired with a word, never used as the only signal. */
const VERDICT_ICONS: Record<Verdict, React.ComponentType<{ className?: string }>> = {
  clean: CheckCircle2,
  flagged: AlertTriangle,
  failed: AlertTriangle,
  pending: Hourglass,
  unknown: ShieldQuestion,
};

export default function ProjectDetailView({
  submission,
  onBack,
}: {
  submission: Submission;
  onBack: () => void;
}) {
  const [job, setJob] = React.useState<Job | null>(null);
  const [detections, setDetections] = React.useState<Detection[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [actionNotice, setActionNotice] = React.useState<string | null>(null);
  const [actionBusy, setActionBusy] = React.useState(false);
  const [reloadToken, setReloadToken] = React.useState(0);
  const videoRef = React.useRef<HTMLVideoElement>(null);

  const videoId = submission.id;

  React.useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    const load = async () => {
      try {
        const nextJob = await fetchJob(videoId, { signal: controller.signal });
        if (cancelled) return;
        setJob(nextJob);
        setError(null);

        // Results are read only once the backend reports the analysis
        // finished and persisted them. For any other state a detections list
        // would be partial, and a partial list is not a verdict.
        if (nextJob.state === "completed") {
          const results = await fetchAllViolations(videoId, {
            signal: controller.signal,
          });
          if (cancelled) return;
          setDetections(results.detections);
        } else {
          setDetections([]);
        }
      } catch (err) {
        if (cancelled || isAbortError(err)) return;
        setError(describeError(err, "Could not load this audit."));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void load();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [videoId, reloadToken]);

  const reload = () => {
    setLoading(true);
    setReloadToken((n) => n + 1);
  };

  const verdict = verdictFor(job, detections.length);
  const VerdictIcon = VERDICT_ICONS[verdict];
  const tone = toneForVerdict(verdict);

  /** Jump playback to a finding. */
  const seekTo = React.useCallback((seconds: number) => {
    const player = videoRef.current;
    if (!player) return;
    try {
      player.currentTime = seconds;
    } catch {
      // Seeking before metadata loads can throw; nothing to report.
    }
  }, []);

  /**
   * Start, or re-run, the analysis for this video.
   *
   * Both are explicit user actions and neither is ever automatic: each run
   * bills the model per frame. The button is offered only for the state the
   * backend actually accepts - `/analyze` for a video never analysed,
   * `/analyze/{id}/retry` for one that finished - so a 409 is the exception
   * rather than the normal path.
   */
  const runAnalysis = async (mode: "start" | "retry") => {
    if (actionBusy) return;
    setActionBusy(true);
    setActionNotice(null);
    try {
      if (mode === "retry") {
        setJob(await retryAnalysis(videoId));
      } else {
        const started = await startAnalysis(videoId);
        if (started.job) setJob(started.job);
      }
      setDetections([]);
      setActionNotice("Analysis queued. Reload to follow its progress.");
    } catch (err) {
      setActionNotice(describeError(err, "Could not queue the analysis."));
    } finally {
      setActionBusy(false);
    }
  };

  const canStart = job?.state === "uploaded";
  const canRetry =
    job != null && job.is_terminal && job.attempt < job.max_attempts;

  const title = submission.title || submission.original_filename || submission.id;

  return (
    <div className="space-y-8">
      <button
        type="button"
        onClick={onBack}
        className={cn(
          "group flex items-center space-x-3 text-white/40 transition-colors hover:text-white",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
        )}
      >
        <ArrowLeft
          className="h-4 w-4 transition-transform group-hover:-translate-x-1"
          aria-hidden="true"
        />
        <span className="text-[10px] font-black uppercase tracking-[0.3em]">
          Back to your videos
        </span>
      </button>

      <div className="grid grid-cols-1 gap-10 xl:grid-cols-12">
        {/* Left: player and findings */}
        <div className="space-y-8 xl:col-span-8">
          <div className="glass-card overflow-hidden rounded-3xl border-white/10 bg-surface/40 p-2 shadow-2xl">
            {/* No autoplay: it starts a download and plays audio the user did
                not ask for. The URL carries a media token and is never logged. */}
            <video
              ref={videoRef}
              src={submission.url}
              controls
              preload="metadata"
              aria-label={`Video ${title}`}
              className="w-full rounded-2xl"
            />
          </div>

          {loading && !job && <LoadingPanel label="Loading this audit" />}

          {error && <ErrorBanner message={error} onRetry={reload} />}

          {/* The verdict. Every branch states what the backend reported, and
              no branch can produce a pass without state === "completed". */}
          {job && (
            <section
              aria-label="Audit verdict"
              className={cn(
                "glass-card flex items-start gap-8 p-8",
                tone === "success"
                  ? "border-success/20 bg-success/[0.02]"
                  : tone === "critical"
                    ? "border-critical/20 bg-critical/[0.02]"
                    : "border-white/10 bg-white/[0.01]"
              )}
            >
              <div
                className={cn(
                  "flex h-16 w-16 shrink-0 items-center justify-center rounded-2xl border",
                  tone === "success"
                    ? "border-success/30 bg-success/10"
                    : tone === "critical"
                      ? "border-critical/30 bg-critical/10"
                      : "border-white/10 bg-white/5"
                )}
              >
                <VerdictIcon
                  className={cn(
                    "h-8 w-8",
                    tone === "success"
                      ? "text-success"
                      : tone === "critical"
                        ? "text-critical"
                        : "text-white/40"
                  )}
                />
              </div>
              <div className="space-y-3">
                <h2
                  data-testid="verdict-title"
                  className={cn(
                    "text-lg font-black uppercase tracking-widest",
                    tone === "success"
                      ? "text-success"
                      : tone === "critical"
                        ? "text-critical"
                        : "text-white/70"
                  )}
                >
                  {VERDICT_LABELS[verdict]}
                </h2>
                <p className="max-w-xl text-xs font-medium leading-relaxed text-white/50">
                  {verdict === "clean" &&
                    `Analysis completed and recorded no violations across ${
                      job.frames_audited ?? 0
                    } audited frame(s). Frames are sampled at 1 fps, so a violation visible for under about a second can be missed. This is model output, not a compliance sign-off.`}
                  {verdict === "flagged" &&
                    `Analysis completed and recorded ${
                      job.detections_recorded ?? detections.length
                    } violation(s) across ${
                      job.frames_audited ?? 0
                    } audited frame(s). Each finding is model output and needs a human review.`}
                  {verdict === "failed" &&
                    (job.error_summary ??
                      "The analysis did not finish, so nothing can be concluded about this video.")}
                  {verdict === "pending" &&
                    `${job.stage}. No verdict exists until the backend reports the analysis finished and its results were persisted.`}
                  {verdict === "unknown" &&
                    "The server did not report a usable job state for this video."}
                </p>
                <div className="flex flex-wrap items-center gap-3 pt-1">
                  <StatusBadge label={job.stage} tone="neutral" />
                  {job.attempt > 1 && (
                    <StatusBadge
                      label={`Attempt ${job.attempt} of ${job.max_attempts}`}
                      tone="neutral"
                    />
                  )}
                  {(job.recovered_count ?? 0) > 0 && (
                    <StatusBadge
                      label={`Auto-restarted ${job.recovered_count}x`}
                      tone="neutral"
                      title="An interrupted run was automatically requeued."
                    />
                  )}
                  {job.frames_failed != null && job.frames_failed > 0 && (
                    <StatusBadge
                      label={`${job.frames_failed} frame(s) not analyzed`}
                      tone="active"
                    />
                  )}
                </div>
              </div>
            </section>
          )}

          {/* Analysis actions, offered only where the backend accepts them. */}
          {(canStart || canRetry) && (
            <div className="flex flex-wrap items-center gap-4">
              {canStart && (
                <button
                  type="button"
                  data-testid="start-analysis"
                  disabled={actionBusy}
                  onClick={() => runAnalysis("start")}
                  className={cn(
                    "flex items-center gap-3 rounded-2xl bg-accent px-8 py-4 text-xs font-bold",
                    "uppercase tracking-widest text-white transition-all hover:brightness-110",
                    "disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2",
                    "focus-visible:outline-accent"
                  )}
                >
                  {actionBusy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
                  <span>{actionBusy ? "Queueing..." : "Start analysis"}</span>
                </button>
              )}
              {canRetry && (
                <button
                  type="button"
                  data-testid="retry-analysis"
                  disabled={actionBusy}
                  onClick={() => runAnalysis("retry")}
                  className={cn(
                    "flex items-center gap-3 rounded-2xl border border-white/10 bg-white/5 px-8 py-4",
                    "text-xs font-bold uppercase tracking-widest text-white transition-all",
                    "hover:bg-white/10 disabled:opacity-50 focus-visible:outline-2",
                    "focus-visible:outline-offset-2 focus-visible:outline-primary"
                  )}
                >
                  {actionBusy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
                  <span>{actionBusy ? "Queueing..." : "Re-run analysis"}</span>
                </button>
              )}
            </div>
          )}

          {actionNotice && (
            <p
              role="status"
              aria-live="polite"
              data-testid="action-notice"
              className="rounded-2xl border border-white/10 bg-white/[0.02] px-6 py-4 text-[11px] font-semibold uppercase tracking-widest text-white/60"
            >
              {actionNotice}
            </p>
          )}

          {/* Findings */}
          <section
            aria-label="Findings"
            className="glass-card space-y-6 border-white/5 bg-white/[0.01] p-8"
          >
            <h3 className="border-b border-white/5 pb-4 text-xs font-black uppercase tracking-[0.4em] text-white/20">
              Findings
            </h3>

            {verdict === "flagged" && detections.length > 0 ? (
              <ViolationList detections={detections} onSeek={seekTo} />
            ) : verdict === "clean" ? (
              <EmptyState
                title="No violations recorded"
                hint="The completed analysis found nothing to flag in the frames it audited."
              />
            ) : verdict === "flagged" ? (
              /* The job recorded violations but this client could not page
                 them in. Saying "none" here would be wrong. */
              <EmptyState
                title="Findings could not be loaded"
                hint={`The job recorded ${
                  job?.detections_recorded ?? 0
                } violation(s), but the detail could not be retrieved.`}
              />
            ) : (
              <EmptyState
                title="No findings to show"
                hint={
                  verdict === "failed"
                    ? "The analysis failed, so no results were persisted."
                    : verdict === "pending"
                      ? "Findings appear once the analysis finishes."
                      : "No job record is available for this video."
                }
              />
            )}
          </section>
        </div>

        {/* Right: metadata, all of it from the record */}
        <div className="space-y-6 xl:col-span-4">
          <div className="glass-card space-y-8 border-white/5 bg-white/[0.01] p-8">
            <h3 className="border-b border-white/5 pb-4 text-xs font-black uppercase tracking-[0.4em] text-white/20">
              Upload details
            </h3>

            <dl className="space-y-6">
              {[
                {
                  icon: Database,
                  label: "Video id",
                  value: submission.id,
                },
                {
                  icon: Database,
                  label: "Original filename",
                  value: submission.original_filename || "--",
                },
                {
                  icon: Clock,
                  label: "Uploaded",
                  value: formatDateTime(submission.created_at),
                },
                {
                  icon: HardDrive,
                  label: "Size",
                  value: formatBytes(submission.size_bytes),
                },
              ].map((item) => (
                <div key={item.label} className="flex items-center space-x-5">
                  <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-white/5">
                    <item.icon className="h-5 w-5 text-white/20" aria-hidden="true" />
                  </div>
                  <div className="min-w-0">
                    <dt className="mb-1 text-[9px] font-black uppercase tracking-widest text-white/20">
                      {item.label}
                    </dt>
                    <dd className="truncate text-xs font-bold tracking-wider text-white">
                      {item.value}
                    </dd>
                  </div>
                </div>
              ))}
            </dl>
          </div>

          {/* Analysis provenance, where the backend recorded it. No codec,
              bitrate or integrity figure: nothing measures those. */}
          {job && (
            <div className="glass-card space-y-6 border-white/5 bg-white/[0.01] p-8">
              <h3 className="border-b border-white/5 pb-4 text-xs font-black uppercase tracking-[0.4em] text-white/20">
                Analysis record
              </h3>
              <dl className="space-y-4 text-[10px] uppercase tracking-widest">
                {[
                  ["Frames extracted", job.frames_extracted ?? "--"],
                  ["Frames audited", job.frames_audited ?? "--"],
                  ["Frames failed", job.frames_failed ?? "--"],
                  ["Violations recorded", job.detections_recorded ?? "--"],
                  ["Model", job.model_id ?? "Not recorded"],
                  ["Pipeline", job.pipeline_version ?? "Not recorded"],
                  ["Completed", formatDateTime(job.completed_at)],
                ].map(([label, value]) => (
                  <div key={String(label)} className="flex justify-between gap-4">
                    <dt className="font-bold text-white/25">{label}</dt>
                    <dd className="truncate font-bold text-white/70">{String(value)}</dd>
                  </div>
                ))}
              </dl>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
