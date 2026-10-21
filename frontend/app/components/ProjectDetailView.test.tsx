import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ProjectDetailView from "./ProjectDetailView";
import type { Submission } from "../lib/api";

/**
 * Phase 7 regression tests for the worst fabrication in the frontend.
 *
 * This view rendered an unconditional success card - "AUDIT PASSED / The
 * content was verified by NovaFlow Neural Agent #xyz. Compliance & quality
 * standards met. Frame consistency scan validated." - for every video,
 * whether analysis had been requested, was still running, or had failed.
 *
 * The assertions below are the ones that must never regress:
 *
 *   * no screen state may say passed/verified/compliant unless the backend
 *     reports state === "completed" with zero recorded violations;
 *   * a failed job says so and offers no verdict;
 *   * a job still running says so and offers no verdict;
 *   * detections are fetched ONLY for a completed job, so a partial read
 *     cannot be presented as a final result.
 */

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const SUBMISSION: Submission = {
  id: "vid_abc12345",
  title: "campaign-reel.mp4",
  original_filename: "campaign-reel.mp4",
  filename: "vid_abc12345.mp4",
  status: "completed",
  created_at: "2026-10-19T10:00:00+00:00",
  size_bytes: 2 * 1024 * 1024,
  url: "http://backend.test/uploads/vid_abc12345.mp4?access_token=media-token",
};

function job(overrides: Record<string, unknown> = {}) {
  return {
    video_id: "vid_abc12345",
    state: "completed",
    stage: "Analysis complete",
    is_terminal: true,
    is_active: false,
    attempt: 1,
    max_attempts: 3,
    recovered_count: 0,
    frames_extracted: 10,
    frames_audited: 10,
    frames_failed: 0,
    detections_recorded: 0,
    error_summary: null,
    model_id: "amazon.nova-pro-v1:0",
    pipeline_version: "4.0",
    original_filename: "campaign-reel.mp4",
    size_bytes: 2 * 1024 * 1024,
    created_at: "2026-10-19T10:00:00+00:00",
    updated_at: "2026-10-19T10:05:00+00:00",
    completed_at: "2026-10-19T10:05:00+00:00",
    ...overrides,
  };
}

function violations(detections: unknown[] = [], jobRecord: unknown = job()) {
  return {
    video_id: "vid_abc12345",
    detections,
    frame_count: 10,
    audited_frames: 10,
    failed_frames: 0,
    next_cursor: null,
    has_more: false,
    job: jobRecord,
  };
}

function stubBackend(handlers: {
  jobs?: () => Response | Promise<Response>;
  violations?: () => Response | Promise<Response>;
  analyze?: () => Response | Promise<Response>;
} = {}) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (url: string) => {
    calls.push(url);
    if (url.includes("/jobs/")) {
      return handlers.jobs ? handlers.jobs() : jsonResponse(job());
    }
    if (url.includes("/violations/")) {
      return handlers.violations ? handlers.violations() : jsonResponse(violations());
    }
    if (url.includes("/analyze/")) {
      return handlers.analyze
        ? handlers.analyze()
        : jsonResponse({ video_id: "vid_abc12345", status: "queued", job: job({ state: "queued", is_terminal: false, is_active: true }) });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

function verdict() {
  return screen.getByTestId("verdict-title").textContent ?? "";
}

beforeEach(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://backend.test";
});

// ---------------------------------------------------------------------------
// the fabrication itself
// ---------------------------------------------------------------------------

describe("the fabricated success card is gone", () => {
  it("never renders the invented agent attribution or scan claims", async () => {
    stubBackend();
    const { container } = render(
      <ProjectDetailView submission={SUBMISSION} onBack={() => {}} />
    );

    await waitFor(() => expect(screen.getByTestId("verdict-title")).toBeTruthy());

    const text = container.textContent ?? "";
    expect(text).not.toMatch(/audit passed/i);
    expect(text).not.toMatch(/neural agent/i);
    expect(text).not.toMatch(/frame consistency scan/i);
    expect(text).not.toMatch(/compliance & quality standards met/i);
    // Fabricated telemetry and capabilities that went with it.
    expect(text).not.toMatch(/h\.265/i);
    expect(text).not.toMatch(/sync integrity/i);
    expect(text).not.toMatch(/turbo boost/i);
    expect(text).not.toMatch(/post to social/i);
  });
});

describe("the verdict is only ever the backend's", () => {
  it("reports no violations found for a completed job that recorded none", async () => {
    stubBackend();
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() => expect(verdict()).toMatch(/no violations found/i));
    // And it still says what that does and does not mean.
    expect(screen.getByText(/not a compliance sign-off/i)).toBeTruthy();
    expect(screen.getByText(/1 fps/i)).toBeTruthy();
  });

  it("reports violations found, with the counts, for a completed job", async () => {
    stubBackend({
      jobs: () => jsonResponse(job({ detections_recorded: 2 })),
      violations: () =>
        jsonResponse(
          violations(
            [
              {
                category: "competitor_logo",
                description: "A competitor mark is visible on the cup.",
                confidence: 0.91,
                risk_level: "High",
                bounding_box: [100, 200, 300, 400],
                timestamp: 3,
                frame_number: 3,
              },
              {
                category: "offensive_text",
                confidence: 0.6,
                risk_level: "Low",
                timestamp: 7,
                frame_number: 7,
              },
            ],
            job({ detections_recorded: 2 })
          )
        ),
    });
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() => expect(verdict()).toMatch(/violations found/i));
    expect(screen.getByText(/competitor_logo/i)).toBeTruthy();
    expect(screen.getByText(/a competitor mark is visible/i)).toBeTruthy();
    expect(screen.getByText("High risk")).toBeTruthy();
    expect(screen.getByText("91%")).toBeTruthy();
  });

  it("says the analysis failed, and offers no verdict, for a failed job", async () => {
    stubBackend({
      jobs: () =>
        jsonResponse(
          job({
            state: "failed",
            stage: "Analysis failed",
            is_terminal: true,
            detections_recorded: 0,
            error_summary: "The video is 95.0s long; the limit for analysis is 60s.",
          })
        ),
    });
    const { container } = render(
      <ProjectDetailView submission={SUBMISSION} onBack={() => {}} />
    );

    await waitFor(() => expect(verdict()).toMatch(/analysis failed/i));
    expect(screen.getByText(/the limit for analysis is 60s/i)).toBeTruthy();
    // Zero detections from a failed run is NOT compliance.
    expect(container.textContent).not.toMatch(/no violations found/i);
    expect(container.textContent).not.toMatch(/compliant/i);
  });

  it("says the analysis is incomplete while a job is still running", async () => {
    stubBackend({
      jobs: () =>
        jsonResponse(
          job({
            state: "analyzing",
            stage: "Analyzing frames",
            is_terminal: false,
            is_active: true,
            frames_audited: null,
            detections_recorded: null,
          })
        ),
    });
    const { container } = render(
      <ProjectDetailView submission={SUBMISSION} onBack={() => {}} />
    );

    await waitFor(() => expect(verdict()).toMatch(/analysis incomplete/i));
    expect(screen.getByText(/no verdict exists until/i)).toBeTruthy();
    expect(container.textContent).not.toMatch(/no violations found/i);
  });

  it("says status unavailable when the job cannot be read", async () => {
    stubBackend({ jobs: () => jsonResponse({ detail: "Video not found" }, 404) });
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/video not found/i)
    );
    expect(screen.queryByTestId("verdict-title")).toBeNull();
  });

  it("does not claim compliance when a completed job's findings fail to load", async () => {
    stubBackend({
      jobs: () => jsonResponse(job({ detections_recorded: 3 })),
      violations: () => jsonResponse({ detail: "Analysis store unavailable" }, 502),
    });
    const { container } = render(
      <ProjectDetailView submission={SUBMISSION} onBack={() => {}} />
    );

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/analysis store unavailable/i)
    );
    expect(container.textContent).not.toMatch(/no violations found/i);
  });
});

// ---------------------------------------------------------------------------
// what gets fetched, and when
// ---------------------------------------------------------------------------

describe("results are read only for a finished job", () => {
  it("does not request violations while the job is non-terminal", async () => {
    const stub = stubBackend({
      jobs: () =>
        jsonResponse(job({ state: "queued", is_terminal: false, is_active: true })),
    });
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() => expect(verdict()).toMatch(/analysis incomplete/i));
    expect(stub.calls.some((u) => u.includes("/violations/"))).toBe(false);
  });

  it("does not request violations for a failed job", async () => {
    const stub = stubBackend({
      jobs: () => jsonResponse(job({ state: "failed", is_terminal: true })),
    });
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() => expect(verdict()).toMatch(/analysis failed/i));
    expect(stub.calls.some((u) => u.includes("/violations/"))).toBe(false);
  });

  it("requests violations once the job is completed", async () => {
    const stub = stubBackend();
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() =>
      expect(stub.calls.some((u) => u.includes("/violations/"))).toBe(true)
    );
  });

  it("does not poll: this view loads once", async () => {
    const stub = stubBackend();
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() => expect(verdict()).toMatch(/no violations found/i));
    const before = stub.calls.length;
    await new Promise((r) => setTimeout(r, 80));
    expect(stub.calls.length).toBe(before);
  });

  it("issues no request after unmount", async () => {
    const stub = stubBackend();
    const { unmount } = render(
      <ProjectDetailView submission={SUBMISSION} onBack={() => {}} />
    );
    await waitFor(() => expect(stub.calls.length).toBeGreaterThan(0));
    const before = stub.calls.length;
    unmount();
    await new Promise((r) => setTimeout(r, 40));
    expect(stub.calls.length).toBe(before);
  });
});

// ---------------------------------------------------------------------------
// analysis actions
// ---------------------------------------------------------------------------

describe("analysis actions follow the backend's contract", () => {
  it("offers Start analysis only for a video that was never analysed", async () => {
    stubBackend({
      jobs: () => jsonResponse(job({ state: "uploaded", is_terminal: false })),
    });
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() => expect(screen.getByTestId("start-analysis")).toBeTruthy());
    expect(screen.queryByTestId("retry-analysis")).toBeNull();
  });

  it("offers Re-run for a terminal job with attempts left", async () => {
    stubBackend({ jobs: () => jsonResponse(job({ attempt: 1 })) });
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() => expect(screen.getByTestId("retry-analysis")).toBeTruthy());
    expect(screen.queryByTestId("start-analysis")).toBeNull();
  });

  it("offers neither once attempts are exhausted", async () => {
    stubBackend({
      jobs: () => jsonResponse(job({ state: "failed", is_terminal: true, attempt: 3 })),
    });
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() => expect(verdict()).toMatch(/analysis failed/i));
    expect(screen.queryByTestId("retry-analysis")).toBeNull();
    expect(screen.queryByTestId("start-analysis")).toBeNull();
  });

  it("offers neither while a job is in flight", async () => {
    stubBackend({
      jobs: () =>
        jsonResponse(job({ state: "analyzing", is_terminal: false, is_active: true })),
    });
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() => expect(verdict()).toMatch(/analysis incomplete/i));
    expect(screen.queryByTestId("retry-analysis")).toBeNull();
    expect(screen.queryByTestId("start-analysis")).toBeNull();
  });

  it("queues a run and reports it as queued, not as finished", async () => {
    const stub = stubBackend({
      jobs: () => jsonResponse(job({ state: "uploaded", is_terminal: false })),
    });
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() => expect(screen.getByTestId("start-analysis")).toBeTruthy());
    fireEvent.click(screen.getByTestId("start-analysis"));

    await waitFor(() =>
      expect(screen.getByTestId("action-notice").textContent).toMatch(/queued/i)
    );
    expect(stub.calls.some((u) => u === "http://backend.test/analyze/vid_abc12345")).toBe(
      true
    );
    expect(screen.getByTestId("action-notice").textContent).not.toMatch(/complete/i);
  });

  it("surfaces a refused run safely", async () => {
    stubBackend({
      jobs: () => jsonResponse(job({ attempt: 1 })),
      analyze: () =>
        jsonResponse({ detail: "This video has already been retried the maximum of 3 times." }, 429),
    });
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() => expect(screen.getByTestId("retry-analysis")).toBeTruthy());
    fireEvent.click(screen.getByTestId("retry-analysis"));

    await waitFor(() =>
      expect(screen.getByTestId("action-notice").textContent).toMatch(/maximum of 3/i)
    );
  });
});

// ---------------------------------------------------------------------------
// metadata and media
// ---------------------------------------------------------------------------

describe("metadata and media handling", () => {
  it("renders the real record fields, with dashes for absent ones", async () => {
    stubBackend({
      jobs: () => jsonResponse(job({ model_id: null, completed_at: null })),
    });
    render(
      <ProjectDetailView
        submission={{ id: "vid_abc12345", url: SUBMISSION.url }}
        onBack={() => {}}
      />
    );

    await waitFor(() => expect(screen.getByText("vid_abc12345")).toBeTruthy());
    expect(screen.getByText("Not recorded")).toBeTruthy();
    // No fabricated size or timestamp for a record that carries neither.
    expect(screen.getAllByText("--").length).toBeGreaterThan(0);
  });

  it("does not autoplay the video", async () => {
    stubBackend();
    const { container } = render(
      <ProjectDetailView submission={SUBMISSION} onBack={() => {}} />
    );
    await waitFor(() => expect(screen.getByTestId("verdict-title")).toBeTruthy());

    const video = container.querySelector("video") as HTMLVideoElement;
    expect(video).toBeTruthy();
    expect(video.autoplay).toBe(false);
  });

  it("goes back without issuing a request", async () => {
    const onBack = vi.fn();
    stubBackend();
    render(<ProjectDetailView submission={SUBMISSION} onBack={onBack} />);

    await waitFor(() => expect(screen.getByTestId("verdict-title")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /back to your videos/i }));
    expect(onBack).toHaveBeenCalledOnce();
  });
});
