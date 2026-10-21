"use client";

/**
 * The caller's own uploads, from GET /submissions.
 *
 * Phase 7 changes, all of them removals of claims the backend never made:
 *
 *  - The "VERIFIED" shield badge compared `sub.status` to the string
 *    "VERIFIED". `/submissions` returns the lower-case job state
 *    ("uploaded", "completed", ...), so that badge could never appear - and
 *    had it appeared, it would have asserted a verdict from a field that only
 *    describes whether analysis ran. Rows now show the real job state.
 *  - The filter tabs read "LLAVA-CORE", "ACT-AGENT" and "VERIFIED". There is
 *    no LLaVA in this system (the README claim was removed in Phase 5) and no
 *    agent routing; the tabs also filtered nothing, since `activeTab` was
 *    never read. Replaced with a search that actually filters.
 *  - The "Post" button, which claimed social publishing. No such endpoint
 *    exists.
 *  - "Verified Neural Output Feed" as a subtitle for a list of unverified
 *    uploads.
 *
 * The search box filters the already-fetched list in the browser. There is no
 * search endpoint, and pretending otherwise would mean one request per
 * keystroke against a route that ignores the query.
 */

import React from "react";
import { LayoutGrid, Play, Search } from "lucide-react";
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
  JOB_STATE_LABELS,
  formatDateTime,
  jobStateOf,
  toneForState,
} from "../lib/jobs";
import { EmptyState, ErrorBanner, LoadingPanel, StatusBadge } from "./ui/Feedback";

export default function ClientPortal({
  onSelect,
}: {
  onSelect: (sub: Submission) => void;
}) {
  const [submissions, setSubmissions] = React.useState<Submission[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [query, setQuery] = React.useState("");
  const [reloadToken, setReloadToken] = React.useState(0);

  React.useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    const load = async () => {
      try {
        // /submissions requires authentication and returns only the
        // signed-in user's own uploads.
        const data = await fetchSubmissions({ signal: controller.signal });
        if (cancelled) return;
        setSubmissions(data);
        setLoadError(null);
      } catch (err) {
        if (cancelled || isAbortError(err)) return;
        setLoadError(describeError(err, "Could not load your assets."));
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

  const visible = React.useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return submissions;
    return submissions.filter((sub) => {
      const haystack = [sub.title, sub.original_filename, sub.id]
        .filter((part): part is string => typeof part === "string")
        .join(" ")
        .toLowerCase();
      return haystack.includes(needle);
    });
  }, [submissions, query]);

  const reload = () => {
    setLoading(true);
    setReloadToken((n) => n + 1);
  };

  return (
    <div className="space-y-12">
      <header className="flex flex-col justify-between gap-10 border-b border-white/5 pb-10 md:flex-row md:items-end">
        <div className="space-y-2">
          <h1 className="flex items-center space-x-4 text-3xl font-bold tracking-tight text-white">
            <LayoutGrid className="h-8 w-8 text-accent" aria-hidden="true" />
            <span>Your Videos</span>
          </h1>
          <p className="text-[11px] font-semibold uppercase tracking-[0.3em] text-white/30">
            {submissions.length} upload(s) · owned by you
          </p>
        </div>

        <div className="flex items-center space-x-6">
          <div className="flex items-center space-x-4 rounded-2xl border border-white/5 bg-white/[0.02] px-5 py-3 transition-all focus-within:border-accent/40">
            <Search className="h-4 w-4 text-white/20" aria-hidden="true" />
            <label htmlFor="asset-search" className="sr-only">
              Filter your videos by name
            </label>
            <input
              id="asset-search"
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter by name..."
              className="w-64 border-none bg-transparent text-xs font-medium text-white outline-none placeholder:text-white/20"
            />
          </div>
        </div>
      </header>

      {loading && submissions.length === 0 && (
        <LoadingPanel label="Loading your videos" />
      )}

      {loadError && <ErrorBanner message={loadError} onRetry={reload} />}

      {!loading && !loadError && submissions.length === 0 && (
        <EmptyState
          title="No videos yet"
          hint="Uploads you make from the dashboard appear here, with the state of each one's analysis."
        />
      )}

      {!loadError && submissions.length > 0 && visible.length === 0 && (
        <EmptyState
          title="No matches"
          hint={`None of your ${submissions.length} upload(s) match "${query.trim()}".`}
        />
      )}

      {visible.length > 0 && (
        <ul className="grid grid-cols-1 gap-10 pb-20 md:grid-cols-2 lg:grid-cols-3">
          {visible.map((sub) => {
            const state = jobStateOf(sub.status);
            const title = sub.title || sub.original_filename || sub.id;

            return (
              <motion.li
                key={sub.id}
                whileHover={{ y: -8 }}
                className="flex flex-col overflow-hidden rounded-[2rem] border border-white/5 bg-white/[0.02] transition-all hover:border-accent/20"
              >
                <div className="relative aspect-video overflow-hidden bg-black/40">
                  {/* The playback URL carries a short-lived media token; it is
                      never logged and never persisted. `preload="metadata"`
                      keeps the grid from downloading every video in full. */}
                  <video
                    src={sub.url}
                    preload="metadata"
                    muted
                    aria-label={`Preview of ${title}`}
                    className="h-full w-full object-cover opacity-60 transition-opacity hover:opacity-100"
                  />
                  <div className="absolute left-5 top-5">
                    <StatusBadge
                      label={JOB_STATE_LABELS[state]}
                      tone={toneForState(state)}
                      className="backdrop-blur-xl"
                    />
                  </div>
                </div>

                <div className="flex flex-1 flex-col justify-between space-y-6 p-8">
                  <div>
                    <h3 className="truncate text-base font-bold tracking-wide text-white">
                      {title}
                    </h3>
                    <p className="mt-2 text-[10px] uppercase tracking-tighter text-white/25">
                      {formatDateTime(sub.created_at)} · {formatBytes(sub.size_bytes)}
                    </p>
                  </div>

                  <button
                    type="button"
                    onClick={() => onSelect(sub)}
                    className={cn(
                      "flex items-center justify-center space-x-3 rounded-2xl border border-white/5",
                      "bg-white/5 py-4 text-xs font-bold uppercase tracking-widest text-white/60",
                      "transition-all hover:bg-accent hover:text-white",
                      "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                    )}
                  >
                    <Play className="h-4 w-4" aria-hidden="true" />
                    <span>Review audit</span>
                  </button>
                </div>
              </motion.li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
