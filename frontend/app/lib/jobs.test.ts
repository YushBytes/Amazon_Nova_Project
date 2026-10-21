import { describe, it, expect } from "vitest";
import type { Detection, Job } from "./api";
import {
  JOB_STATE_LABELS,
  formatConfidence,
  formatDateTime,
  formatTimestamp,
  isActiveState,
  isTerminalState,
  jobStateOf,
  severityLabel,
  sortBySeverity,
  toneForSeverity,
  toneForState,
  verdictFor,
} from "./jobs";

/**
 * The rules in ./jobs are the ones that keep the UI from overstating what the
 * backend said. They are unit-tested directly because four views depend on
 * them, and a regression here would be a dishonest screen in all four.
 */

function job(overrides: Partial<Job> = {}): Job {
  return {
    video_id: "vid_abc12345",
    state: "completed",
    stage: "Analysis complete",
    is_terminal: true,
    is_active: false,
    attempt: 1,
    max_attempts: 3,
    frames_extracted: 10,
    frames_audited: 10,
    frames_failed: 0,
    detections_recorded: 0,
    error_summary: null,
    model_id: "amazon.nova-pro-v1:0",
    pipeline_version: "4.0",
    original_filename: "reel.mp4",
    size_bytes: 1024,
    created_at: null,
    updated_at: null,
    completed_at: null,
    ...overrides,
  };
}

describe("jobStateOf", () => {
  it("accepts every state the backend's machine reports", () => {
    for (const state of [
      "uploaded",
      "queued",
      "extracting",
      "analyzing",
      "completed",
      "failed",
    ]) {
      expect(jobStateOf(state)).toBe(state);
    }
  });

  it("tolerates the upper-case values pre-Phase-4 records carry", () => {
    // backend/job_state.py coerce() does the same, because refusing to read
    // an old record would make it permanently inaccessible.
    expect(jobStateOf("UPLOADED")).toBe("uploaded");
    expect(jobStateOf("ANALYZING")).toBe("analyzing");
  });

  it("reads anything unrecognised as the state that claims the least", () => {
    expect(jobStateOf("VERIFIED")).toBe("uploaded");
    expect(jobStateOf(undefined)).toBe("uploaded");
    expect(jobStateOf(null)).toBe("uploaded");
    expect(jobStateOf(42)).toBe("uploaded");
  });

  it("never labels an unknown state as analyzed or compliant", () => {
    const label = JOB_STATE_LABELS[jobStateOf("whatever")];
    expect(label).toBe("Not analyzed");
    expect(label).not.toMatch(/verified|compliant|passed/i);
  });
});

describe("terminal and active states match the backend", () => {
  it("treats only completed and failed as terminal", () => {
    expect(isTerminalState("completed")).toBe(true);
    expect(isTerminalState("failed")).toBe(true);
    for (const state of ["uploaded", "queued", "extracting", "analyzing"] as const) {
      expect(isTerminalState(state)).toBe(false);
    }
  });

  it("treats queued, extracting and analyzing as in-flight", () => {
    expect(isActiveState("queued")).toBe(true);
    expect(isActiveState("extracting")).toBe(true);
    expect(isActiveState("analyzing")).toBe(true);
    expect(isActiveState("uploaded")).toBe(false);
    expect(isActiveState("completed")).toBe(false);
  });

  it("renders an unfinished job as neither good nor bad news", () => {
    expect(toneForState("uploaded")).toBe("neutral");
    expect(toneForState("queued")).toBe("active");
    expect(toneForState("completed")).toBe("success");
    expect(toneForState("failed")).toBe("critical");
  });
});

// ---------------------------------------------------------------------------
// the verdict rule - the single most important assertion in this file
// ---------------------------------------------------------------------------

describe("verdictFor never calls an unfinished analysis clean", () => {
  it("is clean only when the job completed with zero recorded violations", () => {
    expect(verdictFor(job({ detections_recorded: 0 }))).toBe("clean");
  });

  it("is flagged when a completed job recorded violations", () => {
    expect(verdictFor(job({ detections_recorded: 3 }))).toBe("flagged");
  });

  for (const state of ["uploaded", "queued", "extracting", "analyzing"] as const) {
    it(`is pending, not clean, for a ${state} job with no detections`, () => {
      const verdict = verdictFor(
        job({ state, is_terminal: false, detections_recorded: null }),
        0
      );
      expect(verdict).toBe("pending");
      expect(verdict).not.toBe("clean");
    });
  }

  it("is failed, not clean, when analysis failed with no detections", () => {
    // The distinction the backend's state machine exists to make: an empty
    // result set from a failed run says nothing about the video.
    const verdict = verdictFor(
      job({ state: "failed", is_terminal: true, detections_recorded: 0 }),
      0
    );
    expect(verdict).toBe("failed");
  });

  it("is unknown when there is no job record at all", () => {
    expect(verdictFor(null)).toBe("unknown");
    expect(verdictFor(undefined)).toBe("unknown");
  });

  it("prefers the server's recorded total over whatever this client paged in", () => {
    // A client that failed to page in the detections must not report clean.
    expect(verdictFor(job({ detections_recorded: 5 }), 0)).toBe("flagged");
  });

  it("falls back to the client count only when the server recorded none", () => {
    expect(verdictFor(job({ detections_recorded: null }), 2)).toBe("flagged");
    expect(verdictFor(job({ detections_recorded: null }), 0)).toBe("clean");
    expect(verdictFor(job({ detections_recorded: null }), null)).toBe("unknown");
  });
});

// ---------------------------------------------------------------------------
// detection fields
// ---------------------------------------------------------------------------

describe("formatConfidence", () => {
  it("renders a 0-1 confidence as a percentage", () => {
    expect(formatConfidence({ confidence: 0.91 })).toBe("91%");
  });

  it("accepts a 0-100 confidence from a model that ignored the scale", () => {
    expect(formatConfidence({ confidence: 91 })).toBe("91%");
  });

  it("returns null when the model reported no confidence", () => {
    // A fabricated "0%" would assert certainty the model never expressed.
    expect(formatConfidence({})).toBeNull();
    expect(formatConfidence({ confidence: undefined })).toBeNull();
    expect(formatConfidence({ confidence: Number.NaN })).toBeNull();
  });

  it("distinguishes a reported zero from an absent value", () => {
    expect(formatConfidence({ confidence: 0 })).toBe("0%");
  });
});

describe("formatTimestamp", () => {
  it("renders seconds as m:ss", () => {
    expect(formatTimestamp(0)).toBe("0:00");
    expect(formatTimestamp(9)).toBe("0:09");
    expect(formatTimestamp(75)).toBe("1:15");
  });

  it("returns null for an absent or nonsensical value", () => {
    expect(formatTimestamp(undefined)).toBeNull();
    expect(formatTimestamp(null)).toBeNull();
    expect(formatTimestamp(-1)).toBeNull();
    expect(formatTimestamp(Number.NaN)).toBeNull();
  });
});

describe("severity", () => {
  it("labels the documented levels", () => {
    expect(severityLabel({ risk_level: "High" })).toBe("High risk");
    expect(severityLabel({ risk_level: "medium" })).toBe("Medium risk");
  });

  it("says so when the model reported no severity", () => {
    expect(severityLabel({})).toBe("Severity not reported");
    expect(severityLabel({ risk_level: "   " })).toBe("Severity not reported");
  });

  it("shows an unrecognised severity verbatim rather than guessing", () => {
    expect(severityLabel({ risk_level: "Catastrophic" })).toBe("Catastrophic");
    expect(toneForSeverity({ risk_level: "Catastrophic" })).toBe("neutral");
  });

  it("sorts most severe first and keeps unrated findings last", () => {
    const detections: Detection[] = [
      { category: "a", risk_level: "Low" },
      { category: "b" },
      { category: "c", risk_level: "High" },
      { category: "d", risk_level: "Medium" },
    ];
    expect(sortBySeverity(detections).map((d) => d.category)).toEqual([
      "c",
      "d",
      "a",
      "b",
    ]);
  });

  it("does not mutate the input array", () => {
    const detections: Detection[] = [
      { category: "a", risk_level: "Low" },
      { category: "c", risk_level: "High" },
    ];
    sortBySeverity(detections);
    expect(detections[0].category).toBe("a");
  });
});

describe("formatDateTime", () => {
  it("renders a dash for an absent or invalid timestamp", () => {
    expect(formatDateTime(null)).toBe("--");
    expect(formatDateTime(undefined)).toBe("--");
    expect(formatDateTime("not a date")).toBe("--");
  });

  it("renders a real ISO timestamp", () => {
    expect(formatDateTime("2026-10-19T00:00:00+00:00")).not.toBe("--");
  });
});
