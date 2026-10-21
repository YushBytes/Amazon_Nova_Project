"use client";

/**
 * Analysis infrastructure, from GET /health/worker.
 *
 * Phase 7 replaced this screen wholesale. It previously showed two invented
 * "operational units" ("Core-Act-01", "Nova-Vision-7") with stock photographs
 * and ONLINE/SYNCING badges, alongside fabricated metrics — "Logic Load 42%",
 * "Vision Latency 68ms", "99.9% uptime", "2.1ms sync" — and an "Expand
 * Cluster" button wired to nothing. No backend endpoint has ever produced any
 * of it; there are no agents, and no telemetry source for latency or uptime.
 *
 * What the backend genuinely exposes is the analysis queue. `/health/worker`
 * reports job counts by state, how many leases have lapsed, and the
 * configured execution mode. Read its docstring in backend/app.py before
 * changing anything here: it does NOT contact the worker process. It infers
 * whether work is being consumed from the queue, and that caveat is shown to
 * the user rather than dropped, because "the worker is fine" is not something
 * this data can prove.
 *
 * Counts only — the endpoint returns no ids and no owners.
 */

import React from "react";
import { Boxes, RefreshCw, ServerCog } from "lucide-react";
import { motion } from "framer-motion";
import { cn } from "../lib/utils";
import {
  describeError,
  fetchWorkerHealth,
  isAbortError,
  type WorkerHealth,
} from "../lib/api";
import {
  JOB_STATES,
  JOB_STATE_LABELS,
  isActiveState,
  toneForState,
} from "../lib/jobs";
import { ErrorBanner, LoadingPanel, StatusBadge, UnavailablePanel } from "./ui/Feedback";

/** Health is polled rarely: it is an operational read, not a live feed. */
const REFRESH_INTERVAL_MS = 30000;

export default function AgentsView() {
  const [health, setHealth] = React.useState<WorkerHealth | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [checkedAt, setCheckedAt] = React.useState<Date | null>(null);
  // Bumping this re-runs the load effect, which is also how the retry
  // button works — so there is exactly one request path.
  const [reloadToken, setReloadToken] = React.useState(0);

  React.useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;

    const load = async () => {
      try {
        const data = await fetchWorkerHealth({ signal: controller.signal });
        if (cancelled) return;
        setHealth(data);
        setError(null);
        setCheckedAt(new Date());
      } catch (err) {
        if (cancelled || isAbortError(err)) return;
        setError(describeError(err, "Could not read analysis infrastructure health."));
      } finally {
        if (!cancelled) setLoading(false);
      }
      // Scheduled only after the previous request settles, so requests
      // cannot overlap or stack up behind a slow response.
      if (!cancelled) timer = setTimeout(load, REFRESH_INTERVAL_MS);
    };

    void load();

    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
      controller.abort();
    };
  }, [reloadToken]);

  const counts = health?.counts ?? {};
  const total = Object.values(counts).reduce((sum, n) => sum + (Number(n) || 0), 0);
  const abandoned = typeof health?.abandoned === "number" ? health.abandoned : null;
  const active = typeof health?.active === "number" ? health.active : null;
  const queued = Number(counts["queued"] ?? 0);

  // The backend's own signature for "no worker is running": work waiting with
  // nothing in flight. Stated as a possibility, because this endpoint cannot
  // see the worker process to be sure.
  const possiblyNoWorker = queued > 0 && active === 0 && abandoned === 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="space-y-10"
    >
      <div className="flex flex-wrap items-center justify-between gap-6 border-b border-white/5 pb-10">
        <div className="flex items-center space-x-4">
          <div className="w-12 h-12 bg-primary/5 rounded-2xl flex items-center justify-center border border-primary/20">
            <ServerCog className="text-primary w-6 h-6" aria-hidden="true" />
          </div>
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-white">
              Analysis Infrastructure
            </h1>
            <p className="mt-1 text-[11px] font-semibold uppercase tracking-[0.3em] text-primary/60">
              Job queue · inferred from GET /health/worker
            </p>
          </div>
        </div>

        <div className="flex items-center gap-4">
          {checkedAt && (
            <span className="text-[10px] uppercase tracking-widest text-white/25">
              Checked {checkedAt.toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
              })}
            </span>
          )}
          <button
            type="button"
            onClick={() => {
              setLoading(true);
              setReloadToken((n) => n + 1);
            }}
            className={cn(
              "flex items-center gap-3 rounded-xl border border-white/5 bg-white/[0.02] px-6 py-3",
              "text-[10px] font-bold uppercase tracking-widest text-white/40 transition-colors",
              "hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2",
              "focus-visible:outline-primary"
            )}
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Refresh</span>
          </button>
        </div>
      </div>

      {loading && !health && <LoadingPanel label="Reading queue health" />}

      {error && (
        <ErrorBanner
          message={error}
          onRetry={() => {
            setLoading(true);
            setReloadToken((n) => n + 1);
          }}
        />
      )}

      {health && (
        <>
          {/* Overall state. Never colour alone: each tile states a word. */}
          <section
            aria-label="Queue summary"
            className="grid grid-cols-1 gap-8 md:grid-cols-3"
          >
            <div className="space-y-4 rounded-[2rem] border border-white/5 bg-white/[0.02] p-8">
              <p className="text-[10px] font-bold uppercase tracking-widest text-white/25">
                Reported status
              </p>
              <StatusBadge
                label={
                  health.status === "ok"
                    ? "Healthy"
                    : health.status === "degraded"
                      ? "Degraded"
                      : "Unknown"
                }
                tone={
                  health.status === "ok"
                    ? "success"
                    : health.status === "degraded"
                      ? "critical"
                      : "neutral"
                }
              />
              {health.detail && (
                <p className="text-[11px] leading-relaxed text-white/40">{health.detail}</p>
              )}
            </div>

            <div className="space-y-4 rounded-[2rem] border border-white/5 bg-white/[0.02] p-8">
              <p className="text-[10px] font-bold uppercase tracking-widest text-white/25">
                Execution mode
              </p>
              <p className="text-2xl font-bold tracking-tight text-white">
                {health.execution_mode ?? "unknown"}
              </p>
              <p className="text-[11px] leading-relaxed text-white/40">
                {health.execution_mode === "worker"
                  ? "A separate worker process claims and runs jobs."
                  : health.execution_mode === "inline"
                    ? "Jobs run inside the API process (development mode)."
                    : "The server did not report an execution mode."}
              </p>
            </div>

            <div className="space-y-4 rounded-[2rem] border border-white/5 bg-white/[0.02] p-8">
              <p className="text-[10px] font-bold uppercase tracking-widest text-white/25">
                Abandoned jobs
              </p>
              <p
                className={cn(
                  "text-2xl font-bold tracking-tight",
                  abandoned && abandoned > 0 ? "text-critical" : "text-white"
                )}
              >
                {abandoned ?? "--"}
              </p>
              <p className="text-[11px] leading-relaxed text-white/40">
                Jobs whose worker lease lapsed. A healthy deployment returns
                these to the queue within one recovery interval.
              </p>
            </div>
          </section>

          {/* Counts by job state, straight from the endpoint. */}
          <section aria-label="Jobs by state" className="space-y-6">
            <div className="flex items-center justify-between border-b border-white/5 pb-4">
              <h2 className="flex items-center gap-3 text-[11px] font-bold uppercase tracking-[0.3em] text-white/20">
                <Boxes className="h-4 w-4" aria-hidden="true" />
                Jobs by state
              </h2>
              <span className="text-[10px] font-bold uppercase tracking-widest text-white/25">
                {total} total
              </span>
            </div>

            {total === 0 ? (
              <p className="rounded-2xl border border-dashed border-white/10 bg-white/[0.01] px-8 py-10 text-center text-[11px] uppercase tracking-widest text-white/30">
                The queue is empty. No analysis jobs are recorded.
              </p>
            ) : (
              <dl className="grid grid-cols-2 gap-6 md:grid-cols-3 lg:grid-cols-6">
                {JOB_STATES.map((state) => {
                  const value = Number(counts[state] ?? 0);
                  return (
                    <div
                      key={state}
                      className={cn(
                        "space-y-3 rounded-2xl border bg-white/[0.02] p-6",
                        value > 0 && isActiveState(state)
                          ? "border-accent/20"
                          : "border-white/5"
                      )}
                    >
                      <dt className="text-[9px] font-bold uppercase tracking-widest text-white/25">
                        {JOB_STATE_LABELS[state]}
                      </dt>
                      <dd
                        className={cn(
                          "text-3xl font-bold tracking-tighter",
                          value === 0
                            ? "text-white/20"
                            : toneForState(state) === "critical"
                              ? "text-critical"
                              : toneForState(state) === "success"
                                ? "text-success"
                                : "text-white"
                        )}
                      >
                        {value}
                      </dd>
                    </div>
                  );
                })}
              </dl>
            )}
          </section>

          {possiblyNoWorker && (
            <div
              role="alert"
              className="rounded-2xl border border-accent/30 bg-accent/5 p-6 text-[11px] leading-relaxed text-accent"
            >
              {queued} job(s) are queued with nothing in flight. That is the
              signature of no worker consuming the queue — the usual cause is
              that the API runs with JOB_EXECUTION_MODE=worker and no worker
              process was started. This is inferred from the queue, not
              observed on the worker.
            </div>
          )}

          {/* The endpoint's own caveat, carried through rather than dropped. */}
          <UnavailablePanel
            title="What this screen cannot tell you"
            explanation={
              (health.note ??
                "Inferred from the job queue; the worker process is not contacted.") +
              " There is no per-worker telemetry, no latency or throughput measurement, and no agent registry in this build. Nothing here has been observed against live AWS infrastructure."
            }
          />
        </>
      )}
    </motion.div>
  );
}
