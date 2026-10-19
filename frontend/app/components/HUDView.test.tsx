import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import HUDView from "./HUDView";

/**
 * Regression tests for Phase 2 defect C: Launch Audit never started an audit.
 *
 * Audit reference: docs/PROJECT_AUDIT.md section 9.7.
 *
 * The button uploaded the file via POST /upload-reel and then stopped. It never
 * called POST /analyze/{video_id}, so the pipeline never ran, yet the UI began
 * polling for violations and listed the submission as "READY". A user pressed
 * "Launch Audit" and nothing was ever audited.
 *
 * `fetch` is stubbed per test; nothing contacts a real backend.
 */

const UPLOAD_OK = {
  video_id: "vid_abc12345",
  video_url: "http://backend.test/uploads/vid_abc12345.mp4",
  filename: "vid_abc12345.mp4",
  status: "UPLOADED",
};

/** Phase 4: a job record, the authoritative progress source. */
function job(overrides: Record<string, unknown> = {}) {
  return {
    video_id: "vid_abc12345",
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

const ANALYZE_OK = {
  video_id: "vid_abc12345",
  status: "queued",
  job: job(),
  message: "Analysis queued.",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * Route stubbed fetch calls by URL so a test states the backend's behaviour
 * per endpoint rather than by call order.
 */
function stubBackend(handlers: {
  upload?: () => Response | Promise<Response>;
  analyze?: () => Response | Promise<Response>;
  violations?: () => Response | Promise<Response>;
  jobs?: () => Response | Promise<Response>;
}) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (url: string) => {
    calls.push(url);
    if (url.includes("/upload-reel")) {
      return handlers.upload ? handlers.upload() : jsonResponse(UPLOAD_OK);
    }
    if (url.includes("/jobs/")) {
      return handlers.jobs ? handlers.jobs() : jsonResponse(job());
    }
    if (url.includes("/analyze/")) {
      return handlers.analyze ? handlers.analyze() : jsonResponse(ANALYZE_OK);
    }
    if (url.includes("/violations/")) {
      return handlers.violations
        ? handlers.violations()
        : jsonResponse({
            video_id: UPLOAD_OK.video_id,
            detections: [],
            frame_count: 0,
            audited_frames: 0,
            failed_frames: 0,
            next_cursor: null,
            has_more: false,
            job: job(),
          });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}

function selectVideoFile(container: HTMLElement) {
  const input = container.querySelector(
    'input[type="file"]'
  ) as HTMLInputElement;
  expect(input).toBeTruthy();
  const file = new File(["video-bytes"], "campaign-reel.mp4", {
    type: "video/mp4",
  });
  fireEvent.change(input, { target: { files: [file] } });
  return file;
}

function launchButton() {
  return screen.getByTestId("launch-audit") as HTMLButtonElement;
}

beforeEach(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://backend.test";
});

// --------------------------------------------------------------------------
// the defect itself
// --------------------------------------------------------------------------

describe("Launch Audit triggers the real analysis pipeline", () => {
  it("calls BOTH /upload-reel and /analyze/{video_id}", async () => {
    const { calls } = stubBackend({});
    const { container } = render(<HUDView />);

    selectVideoFile(container);
    fireEvent.click(launchButton());

    await waitFor(() => {
      expect(calls.some((u) => u.includes("/upload-reel"))).toBe(true);
    });

    // This is the regression assertion: before the fix, no /analyze call was
    // ever made.
    await waitFor(() => {
      expect(
        calls.some((u) => u === "http://backend.test/analyze/vid_abc12345")
      ).toBe(true);
    });
  });

  it("uploads before triggering analysis, using the returned video_id", async () => {
    const { calls } = stubBackend({});
    const { container } = render(<HUDView />);

    selectVideoFile(container);
    fireEvent.click(launchButton());

    await waitFor(() => {
      const uploadIdx = calls.findIndex((u) => u.includes("/upload-reel"));
      const analyzeIdx = calls.findIndex((u) => u.includes("/analyze/"));
      expect(uploadIdx).toBeGreaterThanOrEqual(0);
      expect(analyzeIdx).toBeGreaterThan(uploadIdx);
    });
  });

  it("does not call /analyze when the upload fails", async () => {
    const { calls } = stubBackend({
      upload: () => jsonResponse({ detail: "File too large" }, 413),
    });
    const { container } = render(<HUDView />);

    selectVideoFile(container);
    fireEvent.click(launchButton());

    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toMatch(/audit failed/i);
    });
    expect(calls.some((u) => u.includes("/analyze/"))).toBe(false);
  });
});

// --------------------------------------------------------------------------
// loading / success / failure / retry states
// --------------------------------------------------------------------------

describe("audit workflow states", () => {
  it("shows no status strip before anything is submitted", () => {
    stubBackend({});
    render(<HUDView />);
    expect(screen.queryByRole("status")).toBeNull();
    expect(launchButton().textContent).toMatch(/launch audit/i);
  });

  it("disables the button until a file is selected", () => {
    stubBackend({});
    render(<HUDView />);
    expect(launchButton().disabled).toBe(true);
  });

  it("reports an in-flight upload, then the analyzing state", async () => {
    let releaseUpload: (r: Response) => void = () => {};
    const uploadPromise = new Promise<Response>((resolve) => {
      releaseUpload = resolve;
    });

    stubBackend({ upload: () => uploadPromise });
    const { container } = render(<HUDView />);

    selectVideoFile(container);
    fireEvent.click(launchButton());

    // While the upload is in flight the UI says so, and the button is locked.
    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toMatch(/uploading/i);
    });
    expect(launchButton().disabled).toBe(true);

    releaseUpload(jsonResponse(UPLOAD_OK));

    // Phase 4: the stage comes from the backend job record.
    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toMatch(
        /queued for analysis|extracting|analyzing frames/i
      );
    });
  });

  it("surfaces the backend's error detail when the trigger fails", async () => {
    stubBackend({
      analyze: () => jsonResponse({ detail: "Video payload not found." }, 404),
    });
    const { container } = render(<HUDView />);

    selectVideoFile(container);
    fireEvent.click(launchButton());

    await waitFor(() => {
      const status = screen.getByRole("status").textContent ?? "";
      expect(status).toMatch(/audit failed/i);
      expect(status).toMatch(/video payload not found/i);
    });
  });

  it("offers a retry after a failure, and re-enables the button", async () => {
    stubBackend({
      analyze: () => jsonResponse({ detail: "Video payload not found." }, 404),
    });
    const { container } = render(<HUDView />);

    selectVideoFile(container);
    fireEvent.click(launchButton());

    await waitFor(() => {
      expect(launchButton().textContent).toMatch(/retry audit/i);
    });
    expect(launchButton().disabled).toBe(false);
  });

  it("retrying after a failed trigger does not re-upload the file", async () => {
    let analyzeAttempts = 0;
    const { calls } = stubBackend({
      analyze: () => {
        analyzeAttempts += 1;
        return analyzeAttempts === 1
          ? jsonResponse({ detail: "transient" }, 503)
          : jsonResponse(ANALYZE_OK);
      },
    });
    const { container } = render(<HUDView />);

    selectVideoFile(container);
    fireEvent.click(launchButton());

    await waitFor(() => {
      expect(launchButton().textContent).toMatch(/retry audit/i);
    });

    fireEvent.click(launchButton());

    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toMatch(
        /queued for analysis|extracting|analyzing frames/i
      );
    });

    // The upload succeeded the first time; retrying must reuse that video_id
    // rather than storing a second copy of the same file.
    const uploadCalls = calls.filter((u) => u.includes("/upload-reel"));
    expect(uploadCalls).toHaveLength(1);
    expect(analyzeAttempts).toBe(2);
  });

  it("prevents duplicate submissions while a run is in flight", async () => {
    let releaseUpload: (r: Response) => void = () => {};
    const uploadPromise = new Promise<Response>((resolve) => {
      releaseUpload = resolve;
    });
    const { calls } = stubBackend({ upload: () => uploadPromise });
    const { container } = render(<HUDView />);

    selectVideoFile(container);
    const btn = launchButton();

    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.click(btn);

    releaseUpload(jsonResponse(UPLOAD_OK));

    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toMatch(
        /queued for analysis|extracting|analyzing frames/i
      );
    });

    expect(calls.filter((u) => u.includes("/upload-reel"))).toHaveLength(1);
    expect(calls.filter((u) => u.includes("/analyze/"))).toHaveLength(1);
  });
});

// --------------------------------------------------------------------------
// honesty: queued is not complete, and results must be real
// --------------------------------------------------------------------------

describe("the UI does not overstate backend progress", () => {
  it("never claims the audit is complete while the job is non-terminal", async () => {
    // The job reports "analyzing": work is in flight, so no completion
    // wording may appear no matter what the results endpoint says.
    stubBackend({
      jobs: () =>
        jsonResponse(
          job({
            state: "analyzing",
            stage: "Analyzing frames",
            frames_extracted: 5,
            is_terminal: false,
          })
        ),
    });
    const { container } = render(<HUDView />);

    selectVideoFile(container);
    fireEvent.click(launchButton());

    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toMatch(/analyzing frames/i);
    });

    const status = screen.getByRole("status").textContent ?? "";
    expect(status).not.toMatch(/audit complete/i);
    expect(status).not.toMatch(/verified/i);
    expect(status).not.toMatch(/compliant/i);
  });

  it("reports the real frame and violation counts from the completed job", async () => {
    stubBackend({
      jobs: () =>
        jsonResponse(
          job({
            state: "completed",
            stage: "Analysis complete",
            is_terminal: true,
            is_active: false,
            frames_extracted: 5,
            frames_audited: 5,
            frames_failed: 0,
            detections_recorded: 2,
            completed_at: "2026-10-19T00:05:00+00:00",
          })
        ),
      violations: () =>
        jsonResponse({
          video_id: UPLOAD_OK.video_id,
          detections: [
            {
              category: "competitor_logo",
              confidence: 0.91,
              bounding_box: [100, 200, 300, 400],
              timestamp: 1,
            },
            {
              category: "unsafe_item",
              confidence: 0.77,
              bounding_box: [10, 20, 30, 40],
              timestamp: 2,
            },
          ],
          frame_count: 5,
          audited_frames: 5,
          failed_frames: 0,
          next_cursor: null,
          has_more: false,
          job: job({ state: "completed", is_terminal: true }),
        }),
    });
    const { container } = render(<HUDView />);

    selectVideoFile(container);
    fireEvent.click(launchButton());

    await waitFor(() => {
      const status = screen.getByRole("status").textContent ?? "";
      expect(status).toMatch(/audit complete/i);
      expect(status).toMatch(/5 frame\(s\) analyzed/i);
      expect(status).toMatch(/2 violation\(s\) found/i);
    });
  });

  it("shows a queued job as queued, not as progress it cannot know", async () => {
    // Nothing has been extracted yet, so the UI shows the stage and no
    // counts rather than inventing a percentage or a frame total.
    stubBackend({});
    const { container } = render(<HUDView />);

    selectVideoFile(container);
    fireEvent.click(launchButton());

    await waitFor(() => {
      const status = screen.getByRole("status").textContent ?? "";
      expect(status).toMatch(/queued for analysis/i);
    });
    const status = screen.getByRole("status").textContent ?? "";
    expect(status).not.toMatch(/%/);
    expect(status).not.toMatch(/frame\(s\) extracted/i);
  });

  it("does not poll for violations before analysis has been triggered", async () => {
    const { calls } = stubBackend({});
    render(<HUDView />);

    // Nothing submitted: polling must not start, or the dashboard would be
    // requesting results for a video that was never uploaded.
    await new Promise((r) => setTimeout(r, 50));
    expect(calls.some((u) => u.includes("/violations/"))).toBe(false);
  });

  it("replaces the fabricated telemetry list with real pipeline state", async () => {
    stubBackend({});
    render(<HUDView />);

    // The old hardcoded telemetry claimed success before anything had run.
    expect(screen.queryByText(/4096 clusters synced/i)).toBeNull();
    expect(screen.getByText(/awaiting a selected video/i)).toBeTruthy();
    expect(screen.getByText(/not yet requested/i)).toBeTruthy();
  });
});
