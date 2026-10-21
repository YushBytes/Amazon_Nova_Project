"use client";

import React from "react";
import { Activity, FileVideo, Rocket, ShieldCheck, VideoOff } from "lucide-react";
import { motion } from "framer-motion";
import { cn } from "../lib/utils";
import {
  ApiError,
  allowedVideoExtensions,
  describeError,
  fetchAllViolations,
  fetchJob,
  formatBytes,
  isAbortError,
  isRenderableDetection,
  maxUploadBytes,
  retryAnalysis,
  startAnalysis,
  uploadReel,
  validateVideoFile,
  type Detection,
  type Job,
} from "../lib/api";
import { formatTimestamp, verdictFor } from "../lib/jobs";
import { EmptyState, StatusBadge, UnavailablePanel } from "./ui/Feedback";
import ViolationList from "./ViolationList";

/**
 * Local phases for the parts of the flow the UI owns.
 *
 *   idle      nothing submitted yet
 *   uploading POST /upload-reel in flight
 *   starting  POST /analyze/{id} in flight
 *   tracking  the backend owns the job now; `job.state` is authoritative
 *   failed    the upload or the trigger failed client-side
 *
 * Phase 4: once a job exists, the backend's validated `job.state` decides
 * what is displayed. The UI never infers progress or completion.
 */
type AuditPhase = "idle" | "uploading" | "starting" | "tracking" | "failed";

/** Polling cadence while a job is active, and the bound on how long we wait. */
const POLL_INTERVAL_MS = 3000;
const POLL_BACKOFF_MS = 15000;
const MAX_POLL_FAILURES = 5;
// ~10 minutes at the normal cadence. A job still active after this is
// reported as untracked rather than polled forever.
const MAX_POLLS = 200;

/** The local record shown in this view's recent-uploads list. */
interface SubmissionSummary {
  id: string;
  title: string;
  time: string;
  status: string;
}

export default function HUDView() {
  const [submissions, setSubmissions] = React.useState<SubmissionSummary[]>([]);
  const [activeVideoId, setActiveVideoId] = React.useState<string | null>(null);
  const [detections, setDetections] = React.useState<Detection[]>([]);
  const [currentTime, setCurrentTime] = React.useState(0);

  // Real audit workflow state.
  const [auditPhase, setAuditPhase] = React.useState<AuditPhase>("idle");
  const [auditError, setAuditError] = React.useState<string | null>(null);
  // The backend's authoritative job record. Null until a job exists.
  const [job, setJob] = React.useState<Job | null>(null);
  const [pollNotice, setPollNotice] = React.useState<string | null>(null);
  const [isRetrying, setIsRetrying] = React.useState(false);
  // Set once the upload succeeds, so a retry after a failed trigger does
  // not re-upload the same file.
  const [uploadedVideoId, setUploadedVideoId] = React.useState<string | null>(null);
  // Guards against double submission from rapid clicks, independently of
  // the disabled attribute.
  const inFlightRef = React.useRef(false);
  const videoInputRef = React.useRef<HTMLInputElement>(null);
  const videoRef = React.useRef<HTMLVideoElement>(null);

  const [videoSrc, setVideoSrc] = React.useState<string | null>(null);
  const [selectedFile, setSelectedFile] = React.useState<File | null>(null);
  // Inline validation for the file field, replacing a silent accept of a
  // file the backend was always going to reject.
  const [fileError, setFileError] = React.useState<string | null>(null);

  // The recent-uploads list is session state only. It was previously
  // hydrated from a "novaflow_submissions" localStorage key that nothing ever
  // wrote, so the read was dead code. It is removed rather than implemented:
  // submission URLs carry a short-lived media token, and persisting them in
  // browser storage would outlive the token's purpose. /submissions is the
  // authoritative, owner-scoped source.

  // Real pipeline telemetry, derived from actual workflow state. This
  // previously rendered a fixed, fabricated list ('4096 clusters synced') that
  // reported success regardless of what the backend had done.
  const steps = React.useMemo(() => {
    return [
      {
        title: "Payload Upload",
        status:
          auditPhase === "uploading"
            ? "active"
            : uploadedVideoId
              ? "success"
              : auditPhase === "failed"
                ? "error"
                : "pending",
        desc: uploadedVideoId
          ? `Stored as ${uploadedVideoId}.`
          : auditPhase === "uploading"
            ? "Transferring payload to backend..."
            : "Awaiting a selected video.",
      },
      {
        title: "Analysis Trigger",
        status:
          auditPhase === "starting"
            ? "active"
            : job
              ? "success"
              : auditPhase === "failed" && uploadedVideoId
                ? "error"
                : "pending",
        desc: job
          ? "Backend accepted the analysis job."
          : auditPhase === "starting"
            ? "Requesting frame extraction and audit..."
            : "Not yet requested.",
      },
      {
        // Stages and counts come from the backend's job record. Nothing here
        // is inferred, and no percentage is shown, because the frame total is
        // unknown until extraction finishes.
        title: "Frame Extraction",
        status:
          job?.state === "extracting"
            ? "active"
            : job?.frames_extracted
              ? "success"
              : job?.state === "failed" && job?.frames_extracted === null
                ? "error"
                : "pending",
        desc:
          job?.frames_extracted != null
            ? `${job.frames_extracted} frame(s) extracted.`
            : job?.state === "extracting"
              ? "Decoding and sampling frames..."
              : "Not started.",
      },
      {
        title: "Frame Audit",
        status:
          job?.state === "analyzing"
            ? "active"
            : job?.state === "completed"
              ? "success"
              : job?.state === "failed" && job?.frames_extracted != null
                ? "error"
                : "pending",
        desc:
          job?.state === "completed"
            ? `${job.frames_audited ?? 0} frame(s) audited, ` +
              `${job.detections_recorded ?? 0} violation(s) recorded.`
            : job?.state === "analyzing"
              ? "Sending frames to the model..."
              : job?.state === "failed"
                ? job.error_summary ?? "Analysis failed."
                : "No frames audited yet.",
      },
    ];
  }, [auditPhase, uploadedVideoId, job]);

  // Track the backend job. Polls ONLY while the job is non-terminal, and
  // stops on completion, failure, unmount, or loss of authorization.
  // Derived outside the effect so the effect does not depend on the whole
  // `job` object: that identity changes on every poll, which would tear down
  // and restart polling in a loop.
  const jobIsTerminal = job?.is_terminal ?? false;

  // Aborts in-flight requests when the view goes away. Deliberately NOT tied
  // to the polling effect's cleanup: that cleanup also runs when the job
  // turns terminal, which is exactly the moment the results fetch starts, so
  // aborting there would cancel the fetch that loads the findings.
  const abortRef = React.useRef<AbortController | null>(null);
  React.useEffect(() => {
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, []);

  React.useEffect(() => {
    if (!activeVideoId || auditPhase !== "tracking") return;
    if (jobIsTerminal) return;

    let cancelled = false;
    let failures = 0;
    let polls = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const controller = new AbortController();
    abortRef.current = controller;

    const loadResults = async (videoId: string) => {
      try {
        const data = await fetchAllViolations(videoId, { signal: controller.signal });
        if (cancelled) return;
        setDetections(data.detections);
      } catch (err) {
        // A cancellation is not a failure to report, and the URL is never
        // logged: a media token can appear in a query string.
        if (!cancelled && !isAbortError(err)) {
          setPollNotice(
            describeError(err, "The audit finished but its results could not be loaded.")
          );
        }
      }
    };

    const poll = async () => {
      if (cancelled) return;
      polls += 1;

      try {
        const next = await fetchJob(activeVideoId, { signal: controller.signal });
        if (cancelled) return;

        failures = 0;
        setPollNotice(null);
        setJob(next);

        if (next.is_terminal) {
          // Results are only read once the backend says the job finished, so
          // a partial read can never be shown as a final verdict.
          if (next.state === "completed") await loadResults(activeVideoId);
          return; // terminal: stop polling
        }
      } catch (err) {
        if (cancelled || isAbortError(err)) return;

        if (err instanceof ApiError && err.isUnauthorized) {
          // Session gone: stop rather than hammering a route that will keep
          // rejecting us.
          setPollNotice("Your session expired. Please sign in again.");
          return;
        }
        if (err instanceof ApiError && (err.status === 403 || err.status === 404)) {
          setPollNotice("This video is no longer available.");
          return;
        }
        if (err instanceof ApiError && err.status === 429) {
          // Throttled: slow down rather than giving up or spinning.
          failures += 1;
          setPollNotice("Server is busy; checking less often.");
          timer = setTimeout(poll, POLL_BACKOFF_MS);
          return;
        }

        failures += 1;
        if (failures >= MAX_POLL_FAILURES) {
          // A network problem is NOT an audit failure: the job may well be
          // running. Say exactly that instead of inventing an outcome.
          setPollNotice(
            "Lost contact with the server. The analysis may still be running."
          );
          return;
        }
        setPollNotice("Reconnecting...");
      }

      if (polls >= MAX_POLLS) {
        setPollNotice("Still running after a long time. Reload to check again.");
        return;
      }

      // Back off while erroring, and never overlap requests: the next poll is
      // scheduled only after this one settles.
      const delay = failures > 0 ? POLL_BACKOFF_MS : POLL_INTERVAL_MS;
      timer = setTimeout(poll, delay);
    };

    void poll();

    return () => {
      // `cancelled` is what prevents a late response from overwriting newer
      // UI state; the abort itself happens on unmount (see above).
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [activeVideoId, auditPhase, jobIsTerminal]);

  /** Seek the player to a finding's timestamp. */
  const seekTo = React.useCallback((seconds: number) => {
    const player = videoRef.current;
    if (!player) return;
    try {
      player.currentTime = seconds;
      setCurrentTime(seconds);
    } catch {
      // Seeking can throw before metadata has loaded; nothing to report.
    }
  }, []);

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;

    // Mirrors the backend's own checks so the user is told immediately
    // rather than after transferring the whole file. The backend validates
    // independently and remains the authority.
    const problem = validateVideoFile(file);
    if (problem) {
      setFileError(problem);
      setSelectedFile(null);
      return;
    }
    setFileError(null);
    setSelectedFile(file);
  };

  const launchAudit = async () => {
    if (!selectedFile) return;
    // Hard guard against duplicate submissions.
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setAuditError(null);

    try {
      // Step 1: upload, unless a previous attempt already uploaded this file
      // and only the trigger failed.
      let videoId = uploadedVideoId;
      if (!videoId) {
        setAuditPhase("uploading");
        const uploaded = await uploadReel(selectedFile);
        videoId = uploaded.video_id;
        // Bound to a non-nullable local so the list entry cannot be built
        // with a null id.
        const newVideoId: string = videoId;
        setUploadedVideoId(newVideoId);
        setActiveVideoId(newVideoId);
        setVideoSrc(uploaded.video_url);
        setSubmissions((prev) => [
          {
            id: newVideoId,
            title: selectedFile.name,
            time: new Date().toLocaleTimeString([], {
              hour: "2-digit",
              minute: "2-digit",
            }),
            // Reflects the real backend state after upload.
            status: "UPLOADED",
          },
          ...prev,
        ]);
      }

      // Step 2: actually trigger the analysis pipeline.
      setAuditPhase("starting");
      const started = await startAnalysis(videoId);
      // Adopt the job the backend returned; its state - not a local guess -
      // drives everything from here.
      if (started.job) setJob(started.job);
      setAuditPhase("tracking");
    } catch (err) {
      setAuditError(describeError(err, "The audit could not be started."));
      setAuditPhase("failed");
    } finally {
      inFlightRef.current = false;
    }
  };

  const launchDisabled =
    !selectedFile ||
    auditPhase === "uploading" ||
    auditPhase === "starting" ||
    // Locked while the backend still owns a non-terminal job, so a second
    // run cannot be started over the first.
    (auditPhase === "tracking" && !job?.is_terminal);

  // The compliance verdict, which is NOT the job state: only a completed job
  // can be described as clean, and only the server's recorded total decides.
  const verdict = verdictFor(job, detections.length);
  const extensions = allowedVideoExtensions();

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="space-y-12"
    >
      <div className="grid grid-cols-1 xl:grid-cols-12 gap-10 items-start">
        {/* Left Column: Video, ingest, findings */}
        <div className="xl:col-span-8 space-y-10">
          {/* Video Feed Area */}
          <section
            aria-label="Video preview"
            className="relative overflow-hidden rounded-[2rem] border border-white/5 bg-black/20 shadow-2xl"
          >
            <div className="aspect-video relative">
              {videoSrc ? (
                <video
                  ref={videoRef}
                  onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)}
                  src={videoSrc}
                  className="w-full h-full object-contain"
                  controls
                />
              ) : (
                /* No stand-in clip. This panel previously played a sample
                   video hosted by MDN, which looked like a live feed from
                   the user's own content and was not theirs at all. */
                <div
                  data-testid="no-video"
                  className="flex h-full w-full flex-col items-center justify-center gap-4 px-8 text-center"
                >
                  <VideoOff className="h-8 w-8 text-white/15" aria-hidden="true" />
                  <p className="text-[11px] font-bold uppercase tracking-widest text-white/40">
                    No video loaded
                  </p>
                  <p className="max-w-sm text-[11px] leading-relaxed text-white/25">
                    Select a video below and launch an audit. The uploaded clip
                    plays here with any detected violations drawn over it.
                  </p>
                </div>
              )}

              {/* Violation overlay, drawn only for findings near the current
                  playback position and only from a validated bounding box. */}
              {videoSrc && (
                <div className="absolute inset-0 pointer-events-none">
                  {detections
                    // Type predicate: skips malformed model output, and
                    // narrows the box so no assertion is needed below.
                    .filter(isRenderableDetection)
                    .filter(
                      (d) =>
                        typeof d.timestamp === "number" &&
                        Math.abs(d.timestamp - currentTime) < 0.5
                    )
                    .map((d, idx) => (
                      <motion.div
                        key={idx}
                        initial={{ opacity: 0 }}
                        animate={{ opacity: 1 }}
                        className="absolute border-2 border-critical shadow-glow-critical rounded-md"
                        style={{
                          top: `${d.bounding_box[0] / 10}%`,
                          left: `${d.bounding_box[1] / 10}%`,
                          height: `${(d.bounding_box[2] - d.bounding_box[0]) / 10}%`,
                          width: `${(d.bounding_box[3] - d.bounding_box[1]) / 10}%`,
                        }}
                      >
                        <div className="absolute -top-7 left-0 bg-critical text-white text-[10px] font-bold px-3 py-1 rounded-t-md whitespace-nowrap">
                          {d.category ?? "violation"}
                        </div>
                      </motion.div>
                    ))}
                </div>
              )}
            </div>
          </section>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-10">
            {/* Submission Area */}
            <section className="bg-white/[0.02] border border-white/5 rounded-[2rem] p-10 space-y-8 flex flex-col">
              <div className="flex items-center space-x-4">
                <div className="w-12 h-12 bg-accent/5 rounded-2xl flex items-center justify-center border border-accent/20">
                  <Rocket className="w-6 h-6 text-accent" aria-hidden="true" />
                </div>
                <div className="flex-1">
                  <h2 className="text-sm font-bold text-white uppercase tracking-widest leading-tight">
                    Video Ingest
                  </h2>
                  <p className="text-[10px] text-white/30 uppercase font-medium tracking-tight mt-1">
                    Upload, then audit
                  </p>
                </div>
              </div>

              {/* A real button, so it is reachable by keyboard and announced
                  as a control. It was a click-only <div>. */}
              <button
                type="button"
                onClick={() => videoInputRef.current?.click()}
                aria-describedby="file-constraints"
                className={cn(
                  "border-2 border-dashed rounded-3xl p-10 flex flex-col items-center justify-center",
                  "space-y-4 transition-all flex-1 min-h-[160px] w-full",
                  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
                  fileError
                    ? "border-critical/40 bg-critical/5"
                    : selectedFile
                      ? "border-accent/40 bg-accent/5"
                      : "border-white/5 hover:border-white/10 hover:bg-white/[0.01]"
                )}
              >
                <FileVideo
                  className={cn("w-10 h-10", selectedFile ? "text-accent" : "text-white/10")}
                  aria-hidden="true"
                />
                <span className="text-center">
                  <span className="block text-xs font-bold text-white/80">
                    {selectedFile ? selectedFile.name : "Select a video file"}
                  </span>
                  <span className="block text-[10px] text-white/20 uppercase mt-2">
                    {selectedFile
                      ? formatBytes(selectedFile.size)
                      : "Click to browse"}
                  </span>
                </span>
              </button>

              <input
                type="file"
                id="video-file"
                ref={videoInputRef}
                accept={extensions.map((ext) => `.${ext}`).join(",")}
                className="sr-only"
                aria-label="Video file to audit"
                onChange={handleFileChange}
              />

              {/* The real configured limits, not a guess. These mirror the
                  backend's MAX_UPLOAD_BYTES and ALLOWED_VIDEO_EXTENSIONS;
                  the server validates independently and wins. */}
              <p
                id="file-constraints"
                className="text-[10px] uppercase tracking-widest text-white/25"
              >
                Up to {formatBytes(maxUploadBytes())} · {extensions.join(", ")}
              </p>

              {fileError && (
                <p
                  role="alert"
                  data-testid="file-error"
                  className="rounded-2xl border border-critical/40 bg-critical/10 px-5 py-4 text-[10px] font-bold uppercase tracking-widest text-critical"
                >
                  {fileError}
                </p>
              )}

              {/* Workflow status. Every value below comes from the backend's
                  validated job record: "Audit complete" appears only when the
                  server reports state === "completed", which it sets solely
                  after analysis finished AND its results were persisted. */}
              {(auditPhase !== "idle" || job) && (
                <div
                  role="status"
                  aria-live="polite"
                  data-testid="audit-status"
                  className={cn(
                    "rounded-2xl border px-5 py-4 text-[10px] font-bold uppercase tracking-widest space-y-1",
                    auditPhase === "failed" || job?.state === "failed"
                      ? "border-critical/40 bg-critical/10 text-critical"
                      : job?.state === "completed"
                        ? "border-success/40 bg-success/10 text-success"
                        : "border-accent/30 bg-accent/5 text-accent"
                  )}
                >
                  {auditPhase === "uploading" && <div>Uploading payload...</div>}
                  {auditPhase === "starting" && <div>Starting analysis...</div>}

                  {auditPhase === "failed" && (
                    <div>Audit failed: {auditError ?? "unknown error"}</div>
                  )}

                  {job && auditPhase === "tracking" && (
                    <>
                      {job.state === "completed" ? (
                        <div>
                          Audit complete — {job.frames_audited ?? 0} frame(s)
                          analyzed, {job.detections_recorded ?? 0} violation(s)
                          found
                        </div>
                      ) : job.state === "failed" ? (
                        <div>Audit failed: {job.error_summary ?? "unknown error"}</div>
                      ) : (
                        <div>
                          {job.stage}
                          {job.frames_extracted != null && (
                            <> — {job.frames_extracted} frame(s) extracted</>
                          )}
                        </div>
                      )}
                      {job.attempt > 1 && (
                        <div className="opacity-60">
                          Attempt {job.attempt} of {job.max_attempts}
                        </div>
                      )}
                      {/* The backend exposes this precisely so an
                          automatically restarted run is not invisible. */}
                      {(job.recovered_count ?? 0) > 0 && (
                        <div className="opacity-60">
                          Automatically restarted {job.recovered_count} time(s)
                          after an interrupted run.
                        </div>
                      )}
                      {job.frames_failed != null && job.frames_failed > 0 && (
                        <div className="opacity-60">
                          {job.frames_failed} frame(s) could not be analyzed.
                        </div>
                      )}
                    </>
                  )}

                  {pollNotice && (
                    <div data-testid="poll-notice" className="opacity-70">
                      {pollNotice}
                    </div>
                  )}
                </div>
              )}

              {/* Retry is offered only for a job the backend says has
                  finished unsuccessfully, and only while attempts remain. */}
              {job?.state === "failed" && job.attempt < job.max_attempts && (
                <button
                  type="button"
                  data-testid="retry-audit"
                  disabled={isRetrying}
                  onClick={async () => {
                    if (isRetrying || !activeVideoId) return;
                    setIsRetrying(true);
                    setPollNotice(null);
                    try {
                      const next = await retryAnalysis(activeVideoId);
                      setDetections([]);
                      setJob(next);
                      setAuditPhase("tracking");
                    } catch (err) {
                      setPollNotice(
                        describeError(err, "Could not retry the analysis.")
                      );
                    } finally {
                      setIsRetrying(false);
                    }
                  }}
                  className={cn(
                    "w-full h-12 rounded-2xl border border-white/10 bg-white/5 text-white",
                    "font-bold text-xs uppercase tracking-widest transition-all",
                    "hover:bg-white/10 focus-visible:outline-2 focus-visible:outline-offset-2",
                    "focus-visible:outline-primary"
                  )}
                >
                  {isRetrying ? "Retrying..." : "Retry Analysis"}
                </button>
              )}

              <button
                type="button"
                data-testid="launch-audit"
                disabled={launchDisabled}
                onClick={launchAudit}
                className={cn(
                  "w-full h-14 rounded-2xl font-bold text-xs uppercase tracking-widest transition-all",
                  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
                  !launchDisabled
                    ? "bg-accent text-white shadow-xl hover:translate-y-[-2px]"
                    : "bg-white/5 text-white/30"
                )}
              >
                {auditPhase === "uploading" && "Uploading..."}
                {auditPhase === "starting" && "Starting..."}
                {auditPhase === "tracking" &&
                  (job?.is_terminal ? "Launch Audit" : "Analyzing...")}
                {auditPhase === "failed" && "Retry Audit"}
                {auditPhase === "idle" && "Launch Audit"}
              </button>
            </section>

            {/* Pipeline telemetry */}
            <section className="bg-white/[0.02] border border-white/5 rounded-[2rem] p-10 space-y-8 flex flex-col">
              <div className="flex items-center space-x-4">
                <div className="w-12 h-12 bg-primary/5 rounded-2xl flex items-center justify-center border border-primary/20">
                  <Activity className="w-6 h-6 text-primary" aria-hidden="true" />
                </div>
                <div className="flex-1">
                  <h2 className="text-sm font-bold text-white uppercase tracking-widest leading-tight">
                    Pipeline
                  </h2>
                  <p className="text-[10px] text-white/30 uppercase font-medium tracking-tight mt-1">
                    Stages reported by the backend
                  </p>
                </div>
              </div>

              <ol className="space-y-6">
                {steps.map((step) => (
                  <li key={step.title} className="flex space-x-6 items-start">
                    <span
                      aria-hidden="true"
                      className={cn(
                        "w-1.5 h-1.5 rounded-full mt-2 shrink-0",
                        step.status === "success"
                          ? "bg-primary shadow-glow"
                          : step.status === "error"
                            ? "bg-critical shadow-glow-critical"
                            : step.status === "active"
                              ? "bg-accent animate-pulse"
                              : "bg-white/10"
                      )}
                    />
                    <div className="space-y-1">
                      <h3 className="text-xs font-bold text-white/90">
                        {step.title}
                        {/* The dot is decorative; the state is also a word. */}
                        <span className="sr-only">: {step.status}</span>
                      </h3>
                      <p className="text-[10px] text-white/30 font-medium leading-relaxed">
                        {step.desc}
                      </p>
                    </div>
                  </li>
                ))}
              </ol>

              <div className="pt-8 border-t border-white/5 space-y-4">
                <h3 className="text-[10px] font-bold uppercase tracking-[0.3em] text-white/20">
                  This Session
                </h3>
                {submissions.length === 0 ? (
                  <p className="text-[10px] uppercase tracking-widest text-white/20">
                    No uploads yet in this session.
                  </p>
                ) : (
                  <ul className="space-y-3">
                    {submissions.slice(0, 4).map((sub) => (
                      <li
                        key={sub.id}
                        className="flex items-center justify-between gap-4 rounded-2xl bg-white/[0.01] border border-white/5 p-4"
                      >
                        <div className="min-w-0">
                          <p className="truncate text-xs font-bold text-white">
                            {sub.title}
                          </p>
                          <p className="mt-1 text-[9px] uppercase tracking-tighter text-white/20">
                            {sub.time} · {sub.id}
                          </p>
                        </div>
                        <StatusBadge label={sub.status} tone="neutral" />
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </section>
          </div>

          {/* Findings. Rendered only from persisted results, and only once
              the backend has reported a terminal state. */}
          <section
            aria-label="Audit findings"
            className="bg-white/[0.02] border border-white/5 rounded-[2rem] p-10 space-y-8"
          >
            <div className="flex flex-wrap items-center justify-between gap-6 border-b border-white/5 pb-6">
              <div className="flex items-center gap-4">
                <ShieldCheck className="h-6 w-6 text-primary" aria-hidden="true" />
                <h2 className="text-sm font-bold uppercase tracking-widest text-white">
                  Findings
                </h2>
              </div>
              {job && (
                <StatusBadge
                  label={
                    verdict === "clean"
                      ? "No violations found"
                      : verdict === "flagged"
                        ? `${job.detections_recorded ?? detections.length} violation(s)`
                        : verdict === "failed"
                          ? "Analysis failed"
                          : "Analysis incomplete"
                  }
                  tone={
                    verdict === "clean"
                      ? "success"
                      : verdict === "flagged" || verdict === "failed"
                        ? "critical"
                        : "active"
                  }
                />
              )}
            </div>

            {!job ? (
              <EmptyState
                title="No audit yet"
                hint="Upload a video and launch an audit. Findings appear here once the backend reports the analysis finished."
              />
            ) : job.state === "failed" ? (
              /* A failed analysis is never shown as compliant. There may be
                 partial detections from frames that were audited before the
                 failure; they are not a verdict. */
              <EmptyState
                title="Analysis failed — no verdict"
                hint={
                  job.error_summary ??
                  "The analysis did not finish, so nothing can be concluded about this video."
                }
              />
            ) : !job.is_terminal ? (
              <EmptyState
                title="Analysis in progress"
                hint={`${job.stage}. Results are loaded only after the backend reports the analysis finished and its results were persisted.`}
              />
            ) : detections.length === 0 ? (
              <EmptyState
                title="No violations found"
                hint={`The analysis completed and recorded no violations across ${
                  job.frames_audited ?? 0
                } audited frame(s). Frames are sampled at 1 fps, so a violation visible for under about a second can be missed.`}
              />
            ) : (
              <>
                <ViolationList detections={detections} onSeek={seekTo} />
                {detections.length > 0 && (
                  <p className="text-[10px] uppercase tracking-widest text-white/25">
                    Playback position {formatTimestamp(currentTime) ?? "0:00"} ·
                    {" "}
                    {job.frames_audited ?? 0} frame(s) audited
                  </p>
                )}
              </>
            )}
          </section>
        </div>

        {/* Right Column */}
        <div className="xl:col-span-4 space-y-10">
          {/* Previously a free-text "Neural Command" box with a "Pulse
              Protocol" button that was wired to nothing. The backend route
              behind that idea (POST /execute) returns placeholder text from
              an unimplemented orchestrator, so offering the control would
              present a stub as a working feature. */}
          <UnavailablePanel
            title="Custom audit instructions"
            explanation="Audits currently run against one fixed brand-safety prompt. Per-client rules, custom instructions and agent automation are not implemented in this build, so there is no control here to pretend otherwise."
          />

          <UnavailablePanel
            title="What this build verifies"
            explanation="Frames are sampled at 1 fps and sent to Amazon Bedrock for analysis. Detection accuracy has not been measured, and no part of this pipeline has been validated against live AWS infrastructure. Treat findings as assistance for a human reviewer, not as a compliance sign-off."
          />
        </div>
      </div>
    </motion.div>
  );
}
