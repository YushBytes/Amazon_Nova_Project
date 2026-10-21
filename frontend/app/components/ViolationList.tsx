"use client";

/**
 * The actual findings an audit produced.
 *
 * Phase 7. Until now the frontend had nowhere to show a violation's
 * description, severity or confidence: detections were drawn as bounding
 * boxes over the video for the half-second their timestamp matched playback,
 * and were otherwise invisible. Everything rendered here comes from
 * GET /violations/{video_id}.
 *
 * Fields are rendered only when the model actually supplied them:
 *
 *  - `description` can be an empty string (the backend normalises a missing
 *    one to ""), so it is shown only when non-empty.
 *  - `confidence` is absent when the model omitted it. It stays absent -
 *    printing "0%" would assert certainty the model never expressed.
 *  - `risk_level` is model-supplied free text. A recognised High/Medium/Low
 *    gets a tone; anything else is shown verbatim rather than mapped to a
 *    severity it may not mean.
 *  - `bounding_box` may be missing or malformed, so the "on-frame" marker
 *    appears only for a box that passes validation.
 */

import React from "react";
import { Crosshair, ShieldAlert } from "lucide-react";
import { cn } from "../lib/utils";
import { isRenderableDetection, type Detection } from "../lib/api";
import {
  formatConfidence,
  formatTimestamp,
  severityLabel,
  sortBySeverity,
  toneForSeverity,
} from "../lib/jobs";
import { StatusBadge } from "./ui/Feedback";

export default function ViolationList({
  detections,
  onSeek,
  className,
  renderActions,
}: {
  detections: Detection[];
  /** Jump playback to a finding's timestamp. Omitted where there is no player. */
  onSeek?: (seconds: number) => void;
  className?: string;
  /**
   * Extra controls for one finding, rendered inside its row.
   *
   * Phase 8 uses this for the remediation panel. A render prop rather than a
   * direct import so this component stays a presentation of detections and
   * does not acquire a dependency on the remediation API - the dashboard
   * passes nothing and shows no remediation controls.
   */
  renderActions?: (detection: Detection) => React.ReactNode;
}) {
  const ordered = React.useMemo(() => sortBySeverity(detections), [detections]);

  return (
    <ul className={cn("space-y-4", className)} data-testid="violation-list">
      {ordered.map((detection, index) => {
        const confidence = formatConfidence(detection);
        const at = formatTimestamp(detection.timestamp);
        const description =
          typeof detection.description === "string" && detection.description.trim()
            ? detection.description.trim()
            : null;
        const canSeek = onSeek && typeof detection.timestamp === "number" && at !== null;

        return (
          <li
            key={`${detection.frame_number ?? "f"}-${index}`}
            className="rounded-2xl border border-white/5 bg-white/[0.02] p-6 space-y-4"
          >
            <div className="flex flex-wrap items-center gap-3">
              <ShieldAlert
                className="h-4 w-4 shrink-0 text-critical"
                aria-hidden="true"
              />
              <h4 className="text-xs font-bold tracking-wide text-white">
                {detection.category?.trim() || "Violation"}
              </h4>
              <StatusBadge
                label={severityLabel(detection)}
                tone={toneForSeverity(detection)}
              />
              {isRenderableDetection(detection) && (
                <StatusBadge
                  label="Located on frame"
                  tone="neutral"
                  title="The model returned a bounding box for this finding."
                />
              )}
            </div>

            {description && (
              <p className="text-[11px] leading-relaxed text-white/50">{description}</p>
            )}

            <dl className="flex flex-wrap items-center gap-x-8 gap-y-2 text-[10px] uppercase tracking-widest">
              <div className="flex items-center gap-2">
                <dt className="font-bold text-white/25">Confidence</dt>
                {/* Absent, not zero, when the model did not report one. */}
                <dd className="font-bold text-white/70">
                  {confidence ?? "Not reported"}
                </dd>
              </div>
              <div className="flex items-center gap-2">
                <dt className="font-bold text-white/25">Timestamp</dt>
                <dd className="font-bold text-white/70">{at ?? "Not reported"}</dd>
              </div>
              {typeof detection.frame_number === "number" && (
                <div className="flex items-center gap-2">
                  <dt className="font-bold text-white/25">Frame</dt>
                  <dd className="font-bold text-white/70">{detection.frame_number}</dd>
                </div>
              )}
            </dl>

            {renderActions && renderActions(detection)}

            {canSeek && (
              <button
                type="button"
                onClick={() => onSeek!(detection.timestamp as number)}
                className={cn(
                  "inline-flex items-center gap-3 rounded-xl border border-white/10 bg-white/5",
                  "px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-white/60",
                  "transition-colors hover:bg-white/10 hover:text-white",
                  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
                )}
              >
                <Crosshair className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Jump to {at}</span>
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}
