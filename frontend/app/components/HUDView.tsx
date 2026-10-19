"use client";

import React from "react";
import { 
  Play, 
  Activity, 
  CheckCircle2,
  Eye,
  MoreVertical,
  Zap,
  Rocket,
  FileVideo,
  Bell,
  Search
} from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import {
  ApiError,
  fetchAllViolations,
  fetchJob,
  isRenderableDetection,
  retryAnalysis,
  startAnalysis,
  uploadReel,
  type Detection,
  type Job,
} from "../lib/api";

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

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
 * what is displayed. The UI no longer infers progress or completion - it was
 * previously unable to show completion at all, because no completion signal
 * existed.
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
  const [goal, setGoal] = React.useState("");
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
  const [activeTab, setActiveTab] = React.useState("Overview");

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
        time: "",
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
        time: "",
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
        time: "",
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
        time: "",
      },
    ];
  }, [auditPhase, uploadedVideoId, job]);

  // Track the backend job. Polls ONLY while the job is non-terminal, and
  // stops on completion, failure, unmount, or loss of authorization.
  // Derived outside the effect so the effect does not depend on the whole
  // `job` object: that identity changes on every poll, which would tear down
  // and restart polling in a loop.
  const jobIsTerminal = job?.is_terminal ?? false;

  React.useEffect(() => {
    if (!activeVideoId || auditPhase !== "tracking") return;
    if (jobIsTerminal) return;

    let cancelled = false;
    let failures = 0;
    let polls = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const loadResults = async (videoId: string) => {
      try {
        const data = await fetchAllViolations(videoId);
        if (cancelled) return;
        setDetections(data.detections);
      } catch (err) {
        if (!cancelled) console.error("Could not load results", err);
      }
    };

    const poll = async () => {
      if (cancelled) return;
      polls += 1;

      try {
        const next = await fetchJob(activeVideoId);
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
        if (cancelled) return;

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
        setPollNotice(
          "Still running after a long time. Reload to check again."
        );
        return;
      }

      // Back off while erroring, and never overlap requests: the next poll is
      // scheduled only after this one settles.
      const delay = failures > 0 ? POLL_BACKOFF_MS : POLL_INTERVAL_MS;
      timer = setTimeout(poll, delay);
    };

    void poll();

    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [activeVideoId, auditPhase, jobIsTerminal]);

  return (
    <motion.div 
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="space-y-12"
    >
      {/* Top Navigation / Tabs */}
      <div className="flex flex-wrap items-center gap-x-12 gap-y-4 border-b border-white/5">
        {["Overview", "Project Assets", "Telemetry"].map(tab => (
          <button 
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={cn(
              "pb-6 text-xs font-bold transition-all relative uppercase tracking-widest whitespace-nowrap",
              activeTab === tab ? "text-primary" : "text-white/20 hover:text-white/40"
            )}
          >
            {tab}
            {activeTab === tab && (
              <motion.div layoutId="tab-underline" className="absolute bottom-0 left-0 right-0 h-px bg-primary shadow-glow" />
            )}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-12 gap-10 items-start">
        {/* Left Column: Video & Goal */}
        <div className="xl:col-span-8 space-y-10">
          {/* Video Feed Area */}
          <section className="relative overflow-hidden rounded-[2rem] border border-white/5 bg-black/20 shadow-2xl group/video">
            <div className="aspect-video relative">
              <video 
                ref={videoRef}
                onTimeUpdate={(e) =>
                  setCurrentTime(e.currentTarget.currentTime)
                }
                src={videoSrc || "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"}
                className="w-full h-full object-cover"
                controls
              />
              
              {/* Real-Time Bounding Box Overlay */}
              <div className="absolute inset-0 pointer-events-none">
                {detections
                  // Skip malformed model output rather than rendering NaN.
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
                      top: `${d.bounding_box![0] / 10}%`,
                      left: `${d.bounding_box![1] / 10}%`,
                      height: `${(d.bounding_box![2] - d.bounding_box![0]) / 10}%`,
                      width: `${(d.bounding_box![3] - d.bounding_box![1]) / 10}%`,
                    }}
                  >
                    <div className="absolute -top-7 left-0 bg-critical text-white text-[10px] font-bold px-3 py-1 rounded-t-md whitespace-nowrap">
                      {d.category ?? "violation"}
                      {typeof d.confidence === "number"
                        ? ` • ${Math.round(d.confidence * 100)}%`
                        : ""}
                    </div>
                  </motion.div>
                ))}
              </div>
            </div>
          </section>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-10">
            {/* Submission Area */}
            <section className="bg-white/[0.02] border border-white/5 rounded-[2rem] p-10 space-y-8 flex flex-col">
              <div className="flex items-center space-x-4">
                <div className="w-12 h-12 bg-accent/5 rounded-2xl flex items-center justify-center border border-accent/20">
                  <Rocket className="w-6 h-6 text-accent" />
                </div>
                <div className="flex-1">
                  <h2 className="text-sm font-bold text-white uppercase tracking-widest leading-tight">Video Ingest</h2>
                  <p className="text-[10px] text-white/30 uppercase font-medium tracking-tight mt-1">Ready for Neural Sync</p>
                </div>
              </div>

              <div 
                onClick={() => videoInputRef.current?.click()}
                className={cn(
                  "border-2 border-dashed rounded-3xl p-10 flex flex-col items-center justify-center space-y-4 transition-all cursor-pointer flex-1 min-h-[160px]",
                  selectedFile ? "border-accent/40 bg-accent/5" : "border-white/5 hover:border-white/10 hover:bg-white/[0.01]"
                )}
              >
                <input 
                  type="file" 
                  ref={videoInputRef}
                  accept="video/*" 
                  className="hidden" 
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) setSelectedFile(file);
                  }}
                />
                <FileVideo className={cn("w-10 h-10", selectedFile ? "text-accent" : "text-white/10")} />
                <div className="text-center">
                  <p className="text-xs font-bold text-white/80">
                    {selectedFile ? selectedFile.name : "Select Video Payload"}
                  </p>
                  <p className="text-[10px] text-white/20 uppercase mt-2">Max 60MB / 60S</p>
                </div>
              </div>

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
                        <div>
                          Audit failed: {job.error_summary ?? "unknown error"}
                        </div>
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
                        err instanceof Error
                          ? err.message
                          : "Could not retry the analysis."
                      );
                    } finally {
                      setIsRetrying(false);
                    }
                  }}
                  className="w-full h-12 rounded-2xl border border-white/10 bg-white/5 hover:bg-white/10 text-white font-bold text-xs uppercase tracking-widest transition-all"
                >
                  {isRetrying ? "Retrying..." : "Retry Analysis"}
                </button>
              )}

              <button
                data-testid="launch-audit"
                disabled={
                  !selectedFile ||
                  auditPhase === "uploading" ||
                  auditPhase === "starting" ||
                  // Locked while the backend still owns a non-terminal job, so
                  // a second run cannot be started over the first.
                  (auditPhase === "tracking" && !job?.is_terminal)
                }
                onClick={async () => {
                  if (!selectedFile) return;
                  // Hard guard against duplicate submissions.
                  if (inFlightRef.current) return;
                  inFlightRef.current = true;
                  setAuditError(null);

                  try {
                    // Step 1: upload, unless a previous attempt already
                    // uploaded this file and only the trigger failed.
                    let videoId = uploadedVideoId;
                    if (!videoId) {
                      setAuditPhase("uploading");
                      const uploaded = await uploadReel(selectedFile);
                      videoId = uploaded.video_id;
                      // Bound to a non-nullable local so the list entry
                      // cannot be built with a null id.
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

                    // Step 2: actually trigger the analysis pipeline. This
                    // call was missing entirely, so no upload was ever
                    // analysed.
                    setAuditPhase("starting");
                    const started = await startAnalysis(videoId);
                    // Adopt the job the backend returned; its state - not a
                    // local guess - drives everything from here.
                    if (started.job) setJob(started.job);
                    setAuditPhase("tracking");
                  } catch (err) {
                    setAuditError(
                      err instanceof Error ? err.message : String(err)
                    );
                    setAuditPhase("failed");
                  } finally {
                    inFlightRef.current = false;
                  }
                }}
                className={cn(
                  "w-full h-14 rounded-2xl font-bold text-xs uppercase tracking-widest transition-all",
                  selectedFile &&
                    auditPhase !== "uploading" &&
                    auditPhase !== "starting" &&
                    !(auditPhase === "tracking" && !job?.is_terminal)
                    ? "bg-accent text-white shadow-xl hover:translate-y-[-2px]"
                    : "bg-white/5 text-white/10"
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

            {/* Prompt Area */}
            <section className="bg-white/[0.02] border border-white/5 rounded-[2rem] p-10 space-y-8 flex flex-col">
              <div className="flex items-center space-x-4">
                <div className="w-12 h-12 bg-primary/5 rounded-2xl flex items-center justify-center border border-primary/20">
                  <Activity className="w-6 h-6 text-primary" />
                </div>
                <div className="flex-1">
                  <h2 className="text-sm font-bold text-white uppercase tracking-widest leading-tight">Neural Command</h2>
                  <p className="text-[10px] text-white/30 uppercase font-medium tracking-tight mt-1">Task Definition Layer</p>
                </div>
              </div>

              <textarea
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
                placeholder="Enter audit parameters..."
                className="flex-1 w-full bg-white/[0.01] border border-white/5 rounded-2xl p-6 text-sm focus:outline-none focus:border-primary/20 transition-all placeholder:text-white/10 resize-none min-h-[160px]"
              />

              <button className="w-full h-14 rounded-2xl bg-white/5 border border-white/10 hover:bg-white/10 text-white font-bold text-xs uppercase tracking-widest transition-all mt-6">
                Pulse Protocol
              </button>
            </section>
          </div>
        </div>

        {/* Right Column: Feeds */}
        <div className="xl:col-span-4 space-y-10">
          <section className="bg-white/[0.02] border border-white/5 rounded-[2rem] p-10 space-y-10 flex flex-col h-full">
            <div>
              <h2 className="text-[10px] font-bold uppercase tracking-[0.3em] text-white/20 mb-8">System Telemetry</h2>
              <div className="space-y-6">
                {steps.map((step, i) => (
                  <div key={i} className="flex space-x-6 items-start">
                    <div className={cn(
                      "w-1.5 h-1.5 rounded-full mt-2 shrink-0",
                      step.status === 'success' ? "bg-primary shadow-glow" : 
                      step.status === 'error' ? "bg-critical shadow-glow-critical" :
                      step.status === 'active' ? "bg-accent animate-pulse" : "bg-white/10"
                    )} />
                    <div className="space-y-1">
                      <div className="flex items-center justify-between gap-4">
                        <h3 className="text-xs font-bold text-white/90">{step.title}</h3>
                        {step.time && <span className="text-[9px] font-bold text-primary">{step.time}</span>}
                      </div>
                      <p className="text-[10px] text-white/30 font-medium leading-relaxed">{step.desc}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="pt-10 border-t border-white/5 space-y-8 flex-1">
              <h3 className="text-[10px] font-bold uppercase tracking-[0.3em] text-white/20">Audit Pipeline</h3>
              <div className="space-y-4">
                {submissions.slice(0, 4).map((sub) => (
                  <div key={sub.id} className="flex items-center justify-between p-5 rounded-2xl bg-white/[0.01] border border-white/5 hover:bg-white/[0.03] transition-colors group">
                    <div className="flex items-center space-x-4 min-w-0">
                      <div className="w-10 h-10 rounded-xl bg-white/5 flex items-center justify-center shrink-0">
                        <Play className="w-4 h-4 text-white/20 group-hover:text-primary transition-colors" />
                      </div>
                      <div className="min-w-0">
                        <p className="text-xs font-bold text-white truncate">{sub.title}</p>
                        <p className="text-[9px] text-white/20 font-medium uppercase tracking-tighter mt-1">{sub.time}</p>
                      </div>
                    </div>
                    <div className="flex items-center space-x-3 ml-4">
                      <span className="text-[8px] font-bold text-primary/60 border border-primary/20 px-2 py-0.5 rounded uppercase">{sub.status}</span>
                      <button className="p-2 transition-colors">
                        <Eye className="w-4 h-4 text-white/10 group-hover:text-white" />
                      </button>
                    </div>
                  </div>
                ))}
                {submissions.length === 0 && (
                  <div className="text-center py-10 opacity-20 italic text-[10px] uppercase font-medium tracking-widest p-6 border border-dashed border-white/5 rounded-3xl">
                    Awaiting Signals...
                  </div>
                )}
              </div>
            </div>

            <div className="pt-10 border-t border-white/5 grid grid-cols-2 gap-10">
              {[{ label: "Network", val: 82 }, { label: "Nodes", val: 54 }].map((item, i) => (
                <div key={i} className="space-y-3">
                  <p className="text-[9px] font-bold uppercase text-white/20 tracking-widest">{item.label}</p>
                  <div className="h-1 bg-white/5 rounded-full overflow-hidden">
                    <motion.div initial={{ width: 0 }} animate={{ width: `${item.val}%` }} className="h-full bg-primary" />
                  </div>
                </div>
              ))}
            </div>
          </section>
        </div>
      </div>
    </motion.div>
  );
}
