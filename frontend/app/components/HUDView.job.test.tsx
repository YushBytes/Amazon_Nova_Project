import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import HUDView from "./HUDView";

/**
 * Phase 4: the UI tracks the backend's job state.
 *
 * Phase 2 could only show interim counts, because the backend had no
 * completion signal. There is now a validated job record, and these tests pin
 * the properties that make what the user sees trustworthy:
 *
 *  * polling stops the moment the job is terminal, and on unmount;
 *  * requests never overlap;
 *  * an empty detections list is NEVER shown as "compliant" unless the
 *    backend says the job completed;
 *  * a failed job shows the backend's safe summary, not an invented one;
 *  * 401/403/404/429 each stop or slow polling appropriately;
 *  * a network interruption does not fabricate completion.
 *
 * `fetch` is stubbed; nothing contacts a backend.
 */

const VIDEO_ID = "vid_abc12345";

const UPLOAD_OK = {
  video_id: VIDEO_ID,
  video_url: `http://backend.test/uploads/${VIDEO_ID}.mp4`,
  filename: `${VIDEO_ID}.mp4`,
  status: "UPLOADED",
};

function job(overrides: Record<string, unknown> = {}) {
  return {
    video_id: VIDEO_ID,
    state: "queued",
    stage: "Queued for analysis",
    is_terminal: false,
    is_active: true,
    attempt: 1,
    max_attempts: 3,
    frames_extracted: null,
    frames_audited: null,
    frames_failed: null,
    detections_recorded: null,
    error_summary: null,
    model_id: null,
    pipeline_version: "4.0",
    original_filename: "campaign-reel.mp4",
    size_bytes: 1024,
    created_at: "2026-10-19T00:00:00+00:00",
    updated_at: "2026-10-19T00:00:00+00:00",
    completed_at: null,
    ...overrides,
  };
}

const COMPLETED = job({
  state: "completed",
  stage: "Analysis complete",
  is_terminal: true,
  is_active: false,
  frames_extracted: 4,
  frames_audited: 4,
  frames_failed: 0,
  detections_recorded: 0,
  model_id: "amazon.nova-pro-v1:0",
  completed_at: "2026-10-19T00:05:00+00:00",
});

const FAILED = job({
  state: "failed",
  stage: "Analysis failed",
  is_terminal: true,
  is_active: false,
  frames_extracted: 4,
  frames_audited: 0,
  frames_failed: 4,
  detections_recorded: 0,
  error_summary: "No frames could be analysed.",
  completed_at: "2026-10-19T00:05:00+00:00",
});

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function violations(detections: unknown[] = [], jobRecord = COMPLETED) {
  return {
    video_id: VIDEO_ID,
    detections,
    frame_count: detections.length,
    audited_frames: 4,
    failed_frames: 0,
    next_cursor: null,
    has_more: false,
    job: jobRecord,
  };
}

/** A stub whose /jobs answers can change between polls. */
function stubBackend(jobSequence: unknown[], options: {
  violations?: () => Response;
  jobStatus?: number;
} = {}) {
  const calls: string[] = [];
  let jobIndex = 0;
  let inFlight = 0;
  let maxConcurrent = 0;

  const fetchMock = vi.fn(async (url: string) => {
    calls.push(url);
    inFlight += 1;
    maxConcurrent = Math.max(maxConcurrent, inFlight);
    try {
      if (url.includes("/upload-reel")) return jsonResponse(UPLOAD_OK);
      if (url.includes("/analyze/") && url.includes("/retry")) {
        return jsonResponse({ video_id: VIDEO_ID, status: "queued", job: job({ attempt: 2 }) });
      }
      if (url.includes("/analyze/")) {
        return jsonResponse({ video_id: VIDEO_ID, status: "queued", job: job() });
      }
      if (url.includes("/jobs/")) {
        if (options.jobStatus) {
          return jsonResponse({ detail: "nope" }, options.jobStatus, { "Retry-After": "5" });
        }
        const next = jobSequence[Math.min(jobIndex, jobSequence.length - 1)];
        jobIndex += 1;
        return jsonResponse(next);
      }
      if (url.includes("/violations/")) {
        return options.violations ? options.violations() : jsonResponse(violations());
      }
      throw new Error(`unexpected request: ${url}`);
    } finally {
      inFlight -= 1;
    }
  });

  vi.stubGlobal("fetch", fetchMock);
  return {
    calls,
    fetchMock,
    jobPolls: () => calls.filter((u) => u.includes("/jobs/")).length,
    maxConcurrent: () => maxConcurrent,
  };
}

function selectAndLaunch(container: HTMLElement) {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, {
    target: { files: [new File(["bytes"], "reel.mp4", { type: "video/mp4" })] },
  });
  fireEvent.click(screen.getByTestId("launch-audit"));
}

function status() {
  return screen.getByTestId("audit-status").textContent ?? "";
}

beforeEach(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://backend.test";
  vi.useRealTimers();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// polling lifecycle
// ---------------------------------------------------------------------------

describe("job polling", () => {
  it("polls the job endpoint once analysis is queued", async () => {
    const stub = stubBackend([job()]);
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(stub.jobPolls()).toBeGreaterThan(0));
    expect(stub.calls.some((u) => u === `http://backend.test/jobs/${VIDEO_ID}`)).toBe(true);
  });

  it("does not poll before a job exists", async () => {
    const stub = stubBackend([job()]);
    render(<HUDView />);

    await new Promise((r) => setTimeout(r, 60));
    expect(stub.jobPolls()).toBe(0);
  });

  it("stops polling once the job is terminal", async () => {
    const stub = stubBackend([COMPLETED]);
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(status()).toMatch(/audit complete/i));

    const pollsAtCompletion = stub.jobPolls();
    await new Promise((r) => setTimeout(r, 250));
    expect(stub.jobPolls()).toBe(pollsAtCompletion);
  });

  it("stops polling on a failed job", async () => {
    const stub = stubBackend([FAILED]);
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(status()).toMatch(/audit failed/i));

    const polls = stub.jobPolls();
    await new Promise((r) => setTimeout(r, 250));
    expect(stub.jobPolls()).toBe(polls);
  });

  it("stops polling when the component unmounts", async () => {
    const stub = stubBackend([job()]);
    const { container, unmount } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(stub.jobPolls()).toBeGreaterThan(0));
    unmount();

    const polls = stub.jobPolls();
    await new Promise((r) => setTimeout(r, 250));
    expect(stub.jobPolls()).toBe(polls);
  });

  it("never issues overlapping job requests", async () => {
    const stub = stubBackend([job()]);
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(stub.jobPolls()).toBeGreaterThan(0));
    await new Promise((r) => setTimeout(r, 120));
    // The next poll is scheduled only after the previous one settles.
    expect(stub.maxConcurrent()).toBeLessThanOrEqual(1);
  });

  it("shows the backend stage while work is in flight", async () => {
    stubBackend([
      job({ state: "extracting", stage: "Extracting frames" }),
    ]);
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(status()).toMatch(/extracting frames/i));
  });

  it("shows real extracted counts, never a percentage", async () => {
    stubBackend([
      job({ state: "analyzing", stage: "Analyzing frames", frames_extracted: 7 }),
    ]);
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(status()).toMatch(/7 frame\(s\) extracted/i));
    expect(status()).not.toMatch(/%/);
  });
});

// ---------------------------------------------------------------------------
// the honesty properties
// ---------------------------------------------------------------------------

describe("completion is only ever the backend's verdict", () => {
  it("reports a completed audit with zero violations as complete", async () => {
    stubBackend([COMPLETED], { violations: () => jsonResponse(violations([])) });
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(status()).toMatch(/audit complete/i));
    expect(status()).toMatch(/4 frame\(s\) analyzed/i);
    expect(status()).toMatch(/0 violation\(s\) found/i);
  });

  it("does NOT claim completion when analysis failed with zero detections", async () => {
    // Same empty detections list as a clean audit. The difference is the job
    // state, and it must be what the UI reports.
    stubBackend([FAILED], { violations: () => jsonResponse(violations([], FAILED)) });
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(status()).toMatch(/audit failed/i));
    expect(status()).toMatch(/no frames could be analysed/i);
    expect(status()).not.toMatch(/audit complete/i);
    expect(status()).not.toMatch(/compliant/i);
  });

  it("does not load results while the job is non-terminal", async () => {
    const stub = stubBackend([
      job({ state: "analyzing", stage: "Analyzing frames" }),
    ]);
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(status()).toMatch(/analyzing frames/i));
    // Results are only read once the backend says the job finished, so a
    // partial read can never be shown as a final verdict.
    expect(stub.calls.some((u) => u.includes("/violations/"))).toBe(false);
  });

  it("loads results after the backend reports completion", async () => {
    const stub = stubBackend([COMPLETED]);
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() =>
      expect(stub.calls.some((u) => u.includes("/violations/"))).toBe(true)
    );
  });

  it("shows the backend's error summary, not an invented one", async () => {
    const custom = job({
      state: "failed",
      is_terminal: true,
      error_summary: "The video is 95.0s long; the limit for analysis is 60s.",
    });
    stubBackend([custom]);
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(status()).toMatch(/95\.0s long/i));
  });

  it("reports the attempt number when a job has been retried", async () => {
    stubBackend([job({ attempt: 2, state: "analyzing", stage: "Analyzing frames" })]);
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(status()).toMatch(/attempt 2 of 3/i));
  });
});

// ---------------------------------------------------------------------------
// authorization and throttling
// ---------------------------------------------------------------------------

describe("HTTP status handling while polling", () => {
  it("stops and reports an expired session on 401", async () => {
    const stub = stubBackend([job()], { jobStatus: 401 });
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() =>
      expect(screen.getByTestId("poll-notice").textContent).toMatch(
        /session expired/i
      )
    );

    const polls = stub.jobPolls();
    await new Promise((r) => setTimeout(r, 250));
    expect(stub.jobPolls()).toBe(polls);
  });

  it("stops on 404 without claiming a result", async () => {
    stubBackend([job()], { jobStatus: 404 });
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() =>
      expect(screen.getByTestId("poll-notice").textContent).toMatch(
        /no longer available/i
      )
    );
    expect(status()).not.toMatch(/audit complete/i);
  });

  it("stops on 403", async () => {
    stubBackend([job()], { jobStatus: 403 });
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() =>
      expect(screen.getByTestId("poll-notice").textContent).toMatch(
        /no longer available/i
      )
    );
  });

  it("slows down rather than giving up on 429", async () => {
    stubBackend([job()], { jobStatus: 429 });
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() =>
      expect(screen.getByTestId("poll-notice").textContent).toMatch(/busy/i)
    );
    expect(status()).not.toMatch(/audit failed/i);
    expect(status()).not.toMatch(/audit complete/i);
  });

  it("a network failure does not fabricate completion", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("/upload-reel")) return jsonResponse(UPLOAD_OK);
      if (url.includes("/analyze/"))
        return jsonResponse({ video_id: VIDEO_ID, status: "queued", job: job() });
      throw new TypeError("Failed to fetch");
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(screen.getByTestId("poll-notice")).toBeTruthy());
    expect(status()).not.toMatch(/audit complete/i);
    expect(status()).not.toMatch(/compliant/i);
  });
});

// ---------------------------------------------------------------------------
// retry
// ---------------------------------------------------------------------------

describe("retry", () => {
  it("offers retry only for a failed job with attempts left", async () => {
    stubBackend([FAILED]);
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(screen.getByTestId("retry-audit")).toBeTruthy());
  });

  it("does not offer retry for a completed job", async () => {
    stubBackend([COMPLETED]);
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(status()).toMatch(/audit complete/i));
    expect(screen.queryByTestId("retry-audit")).toBeNull();
  });

  it("does not offer retry once attempts are exhausted", async () => {
    stubBackend([job({ ...FAILED, attempt: 3, max_attempts: 3 })]);
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(status()).toMatch(/audit failed/i));
    expect(screen.queryByTestId("retry-audit")).toBeNull();
  });

  it("re-queues and resumes tracking when retry is pressed", async () => {
    const stub = stubBackend([FAILED]);
    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(screen.getByTestId("retry-audit")).toBeTruthy());
    fireEvent.click(screen.getByTestId("retry-audit"));

    await waitFor(() =>
      expect(
        stub.calls.some((u) => u.includes(`/analyze/${VIDEO_ID}/retry`))
      ).toBe(true)
    );
  });

  it("surfaces a refused retry safely", async () => {
    const calls: string[] = [];
    const fetchMock = vi.fn(async (url: string) => {
      calls.push(url);
      if (url.includes("/upload-reel")) return jsonResponse(UPLOAD_OK);
      if (url.includes("/retry"))
        return jsonResponse({ detail: "maximum of 3" }, 429);
      if (url.includes("/analyze/"))
        return jsonResponse({ video_id: VIDEO_ID, status: "queued", job: job() });
      if (url.includes("/jobs/")) return jsonResponse(FAILED);
      return jsonResponse(violations([], FAILED));
    });
    vi.stubGlobal("fetch", fetchMock);

    const { container } = render(<HUDView />);
    selectAndLaunch(container);

    await waitFor(() => expect(screen.getByTestId("retry-audit")).toBeTruthy());
    fireEvent.click(screen.getByTestId("retry-audit"));

    await waitFor(() =>
      expect(screen.getByTestId("poll-notice").textContent).toMatch(/maximum of 3/i)
    );
  });
});
