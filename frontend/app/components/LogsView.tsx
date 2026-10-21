"use client";

/**
 * Compliance log: the caller's own audits, from GET /submissions.
 *
 * Phase 7 replaced this screen wholesale. It previously listed three
 * hardcoded "incidents" — "Unauthorized Access Attempt", "Tailgating
 * Detected", "After-hours Activity" — with stock photographs, a "LIVE AI"
 * badge and a "Live Anomaly Detection" subtitle. None of it came from any
 * endpoint, and none of it was even this product: those are physical-security
 * events, not brand-compliance findings.
 *
 * What the backend actually has is the caller's own uploads and the state of
 * each one's analysis job. `/submissions` returns exactly that, scoped to the
 * signed-in user by an ownership check, and its `status` field is the real
 * job state from backend/job_state.py.
 *
 * Deliberately NOT done here: fetching GET /jobs/{id} per row. That would be
 * one request per upload on every visit to this screen. A row shows what
 * `/submissions` carries, and opening it loads the full job record and
 * findings once.
 */

import React from "react";
import { FileVideo, History } from "lucide-react";
import { motion } from "framer-motion";
import { cn } from "../lib/utils";
import {
  describeError,
  fetchSubmissions,
  formatBytes,
  isAbortError,
  type Submission,
} from "../lib/api";
import {
  JOB_STAGE_LABELS,
  JOB_STATE_LABELS,
  formatDateTime,
  isActiveState,
  jobStateOf,
  toneForState,
} from "../lib/jobs";
import { EmptyState, ErrorBanner, LoadingPanel, StatusBadge } from "./ui/Feedback";

/** Row filters, each defined by real job states rather than invented labels. */
const FILTERS = [
  { id: "all", label: "All audits" },
  { id: "analyzed", label: "Analyzed" },
  { id: "in-progress", label: "In progress" },
  { id: "failed", label: "Failed" },
  { id: "not-analyzed", label: "Not analyzed" },
] as const;

type FilterId = (typeof FILTERS)[number]["id"];

function matchesFilter(submission: Submission, filter: FilterId): boolean {
  const state = jobStateOf(submission.status);
  switch (filter) {
    case "analyzed":
      return state === "completed";
    case "failed":
      return state === "failed";
    case "in-progress":
      return isActiveState(state);
    case "not-analyzed":
      return state === "uploaded";
    default:
      return true;
  }
}

export default function LogsView({
  onSelect,
}: {
  /** Open one audit. Omitted when this view is rendered without a detail pane. */
  onSelect?: (submission: Submission) => void;
}) {
  const [submissions, setSubmissions] = React.useState<Submission[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [filter, setFilter] = React.useState<FilterId>("all");
  const [reloadToken, setReloadToken] = React.useState(0);

  React.useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    const load = async () => {
      try {
        const data = await fetchSubmissions({ signal: controller.signal });
        if (cancelled) return;
        setSubmissions(data);
        setError(null);
      } catch (err) {
        if (cancelled || isAbortError(err)) return;
        setError(describeError(err, "Could not load your audit history."));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void load();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [reloadToken]);

  const visible = React.useMemo(
    () => submissions.filter((s) => matchesFilter(s, filter)),
    [submissions, filter]
  );

  const reload = () => {
    setLoading(true);
    setReloadToken((n) => n + 1);
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="space-y-10"
    >
      <div className="flex flex-wrap items-center justify-between gap-6 border-b border-white/5 pb-10">
        <div className="flex items-center space-x-4">
          <div className="w-12 h-12 bg-primary/5 rounded-2xl flex items-center justify-center border border-primary/20">
            <History className="text-primary w-6 h-6" aria-hidden="true" />
          </div>
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-white">
              Compliance Log
            </h1>
            <p className="mt-1 text-[11px] font-semibold uppercase tracking-[0.3em] text-primary/60">
              Your own audits · {submissions.length} upload(s)
            </p>
          </div>
        </div>
      </div>

      {/* Filters operate on real job states, client-side over the list
          already fetched — no request per tab. */}
      <div
        role="group"
        aria-label="Filter audits by state"
        className="flex flex-wrap items-center gap-x-10 gap-y-4 border-b border-white/5"
      >
        {FILTERS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            aria-pressed={filter === entry.id}
            onClick={() => setFilter(entry.id)}
            className={cn(
              "relative whitespace-nowrap pb-6 text-xs font-bold uppercase tracking-widest transition-all",
              "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary",
              filter === entry.id ? "text-primary" : "text-white/20 hover:text-white/40"
            )}
          >
            {entry.label}
            {filter === entry.id && (
              <motion.div
                layoutId="tab-underline-logs"
                className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary shadow-glow"
              />
            )}
          </button>
        ))}
      </div>

      {loading && submissions.length === 0 && (
        <LoadingPanel label="Loading your audit history" />
      )}

      {error && <ErrorBanner message={error} onRetry={reload} />}

      {!loading && !error && submissions.length === 0 && (
        <EmptyState
          title="No audits yet"
          hint="Upload a video from the dashboard and launch an audit. Every upload you make appears here with the state of its analysis."
        />
      )}

      {!error && submissions.length > 0 && visible.length === 0 && (
        <EmptyState
          title="Nothing matches this filter"
          hint="None of your uploads are in this state right now."
        />
      )}

      {visible.length > 0 && (
        <ul className="space-y-4">
          {visible.map((submission) => {
            const state = jobStateOf(submission.status);
            const title = submission.title || submission.original_filename || submission.id;

            return (
              <li
                key={submission.id}
                className="rounded-[1.5rem] border border-white/5 bg-white/[0.02] p-8 transition-colors hover:border-primary/20"
              >
                <div className="flex flex-wrap items-start justify-between gap-6">
                  <div className="flex min-w-0 items-start gap-5">
                    <div className="mt-1 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-white/5">
                      <FileVideo className="h-4 w-4 text-white/30" aria-hidden="true" />
                    </div>
                    <div className="min-w-0 space-y-2">
                      <h3 className="truncate text-sm font-bold text-white">{title}</h3>
                      <p className="text-[10px] uppercase tracking-widest text-white/25">
                        {submission.id} · {formatDateTime(submission.created_at)} ·{" "}
                        {formatBytes(submission.size_bytes)}
                      </p>
                      <p className="text-[11px] text-white/40">
                        {JOB_STAGE_LABELS[state]}
                      </p>
                    </div>
                  </div>

                  <div className="flex shrink-0 items-center gap-4">
                    <StatusBadge
                      label={JOB_STATE_LABELS[state]}
                      tone={toneForState(state)}
                    />
                    {onSelect && (
                      <button
                        type="button"
                        onClick={() => onSelect(submission)}
                        className={cn(
                          "rounded-xl border border-white/5 bg-white/5 px-6 py-3",
                          "text-[10px] font-bold uppercase tracking-widest text-white/60",
                          "transition-colors hover:bg-primary hover:text-white",
                          "focus-visible:outline-2 focus-visible:outline-offset-2",
                          "focus-visible:outline-primary"
                        )}
                      >
                        Open audit
                      </button>
                    )}
                  </div>
                </div>

                {/* No verdict is shown in the list. `/submissions` carries the
                    job state but not the recorded violation count, and a
                    verdict without that count would be a guess. Opening the
                    audit loads the real figures. */}
              </li>
            );
          })}
        </ul>
      )}

      <p className="text-[10px] leading-relaxed text-white/25">
        This log lists uploads you own. It is not a security event feed, an
        alert stream, or an audit trail of account activity — none of those
        exist in this build.
      </p>
    </motion.div>
  );
}
