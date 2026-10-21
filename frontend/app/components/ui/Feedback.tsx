"use client";

/**
 * Shared feedback primitives: loading, empty, error, unavailable, badges.
 *
 * Phase 7. Each view previously rolled its own, which is why some states were
 * missing entirely - an empty submission list rendered nothing at all, and a
 * failed load in one view was a styled banner while in another it was a
 * browser `alert()`. These replace all of that.
 *
 * Accessibility rules encoded here, so a view cannot forget them:
 *
 *  - An error is `role="alert"`, so assistive technology announces it without
 *    the user having to go looking.
 *  - A loading placeholder is labelled, not a bare pulsing rectangle, and is
 *    `aria-busy` so a screen reader is told work is in progress.
 *  - A status badge always carries a word. Colour is never the only signal,
 *    which matters both for colour-blind users and for the severity scale,
 *    where red vs amber is the entire message.
 */

import React from "react";
import { AlertTriangle, Inbox, Loader2, Lock } from "lucide-react";
import { cn } from "../../lib/utils";
import type { StateTone } from "../../lib/jobs";

/** Tailwind classes per tone. One place, so tones stay consistent. */
const TONE_CLASSES: Record<StateTone, string> = {
  success: "border-success/40 bg-success/10 text-success",
  critical: "border-critical/40 bg-critical/10 text-critical",
  active: "border-accent/40 bg-accent/10 text-accent",
  neutral: "border-white/10 bg-white/5 text-white/60",
};

/**
 * A status pill.
 *
 * `label` is mandatory: there is no icon-only or colour-only variant, by
 * design.
 */
export function StatusBadge({
  label,
  tone = "neutral",
  className,
  title,
}: {
  label: string;
  tone?: StateTone;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-2 rounded-lg border px-3 py-1",
        "text-[10px] font-bold uppercase tracking-widest whitespace-nowrap",
        TONE_CLASSES[tone],
        className
      )}
    >
      {label}
    </span>
  );
}

/**
 * An error the user can act on.
 *
 * `message` is the backend's own `detail` where there is one. It is never an
 * exception string: the API returns a flat generic message for a 500, and the
 * client's own fallbacks are written for a reader.
 */
export function ErrorBanner({
  message,
  onRetry,
  retryLabel = "Try again",
  className,
}: {
  message: string;
  onRetry?: () => void;
  retryLabel?: string;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col gap-4 rounded-2xl border border-critical/30 bg-critical/5 p-6",
        "sm:flex-row sm:items-center sm:justify-between",
        className
      )}
    >
      <div className="flex items-start gap-4">
        <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-critical" aria-hidden="true" />
        <p className="text-xs font-semibold leading-relaxed text-critical">{message}</p>
      </div>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className={cn(
            "shrink-0 rounded-xl border border-critical/40 bg-critical/10 px-6 py-3",
            "text-[10px] font-bold uppercase tracking-widest text-critical transition-colors",
            "hover:bg-critical/20 focus-visible:outline-2 focus-visible:outline-offset-2",
            "focus-visible:outline-critical"
          )}
        >
          {retryLabel}
        </button>
      )}
    </div>
  );
}

/** A labelled loading placeholder. */
export function LoadingPanel({
  label,
  className,
}: {
  label: string;
  className?: string;
}) {
  return (
    <div
      aria-busy="true"
      aria-live="polite"
      className={cn(
        "flex items-center justify-center gap-4 rounded-2xl border border-white/5",
        "bg-white/[0.01] p-10 text-[10px] font-bold uppercase tracking-widest text-white/40",
        className
      )}
    >
      <Loader2 className="h-4 w-4 animate-spin text-primary" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

/**
 * Nothing here yet - which is different from "something went wrong" and
 * different again from "this is not built". Each has its own component so a
 * view cannot blur them.
 */
export function EmptyState({
  title,
  hint,
  className,
  children,
}: {
  title: string;
  hint?: string;
  className?: string;
  children?: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center gap-3 rounded-2xl border border-dashed border-white/10",
        "bg-white/[0.01] px-8 py-12 text-center",
        className
      )}
    >
      <Inbox className="h-6 w-6 text-white/20" aria-hidden="true" />
      <p className="text-xs font-bold uppercase tracking-widest text-white/50">{title}</p>
      {hint && <p className="max-w-md text-[11px] leading-relaxed text-white/30">{hint}</p>}
      {children}
    </div>
  );
}

/**
 * A capability this build does not have.
 *
 * Used where a screen previously showed invented data for a feature the
 * backend never implemented. Saying so is the honest state; the alternative
 * is a panel that looks like it works.
 */
export function UnavailablePanel({
  title,
  explanation,
  className,
}: {
  title: string;
  explanation: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col gap-4 rounded-2xl border border-white/10 bg-white/[0.01] p-8",
        className
      )}
    >
      <div className="flex items-center gap-3">
        <Lock className="h-4 w-4 shrink-0 text-white/30" aria-hidden="true" />
        <h3 className="text-[11px] font-bold uppercase tracking-[0.3em] text-white/50">
          {title}
        </h3>
      </div>
      <p className="text-[11px] leading-relaxed text-white/40">{explanation}</p>
    </div>
  );
}
