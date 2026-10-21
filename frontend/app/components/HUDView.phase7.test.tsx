import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import HUDView from "./HUDView";

/**
 * Phase 7 additions to the dashboard.
 *
 * What this file pins, beyond the Phase 2/4 behaviour already covered by
 * HUDView.test.tsx and HUDView.job.test.tsx:
 *
 *  * the placeholder video is gone. The player previously fell back to a
 *    sample clip hosted on an MDN domain, which looked like the user's own
 *    footage, was not theirs, and meant the dashboard made a request to a
 *    third party on every render;
 *  * the fabricated "Network 82 / Nodes 54" gauges are gone;
 *  * a chosen file is validated against the real configured limits before a
 *    byte is uploaded;
 *  * findings are rendered, and a completed-with-nothing result is clearly
 *    distinct from a failed or unfinished one;
 *  * the "Neural Command" box is not offered, because the route behind it
 *    returns placeholder text from an unimplemented orchestrator.
 */

const VIDEO_ID = "vid_abc12345";

const UPLOAD_OK = {
  video_id: VIDEO_ID,
  video_url: `http://backend.test/uploads/${VIDEO_ID}.mp4?access_token=media-token`,
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
    recovered_count: 0,
    frames_extracted: null,
    frames_audited: null,
    frames_failed: null,
    detections_recorded: null,
    error_summary: null,
    model_id: null,
    pipeline_version: "4.0",
    original_filename: "reel.mp4",
    size_bytes: 1024,
    created_at: null,
    updated_at: null,
    completed_at: null,
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubBackend(handlers: {
  jobs?: () => Response | Promise<Response>;
  violations?: () => Response | Promise<Response>;
  upload?: () => Response | Promise<Response>;
} = {}) {
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
      return jsonResponse({ video_id: VIDEO_ID, status: "queued", job: job() });
    }
    if (url.includes("/violations/")) {
      return handlers.violations
        ? handlers.violations()
        : jsonResponse({
            video_id: VIDEO_ID,
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
  return { calls, fetchMock };
}

function fileInput(container: HTMLElement): HTMLInputElement {
  return container.querySelector('input[type="file"]') as HTMLInputElement;
}

function chooseFile(container: HTMLElement, name: string, size = 1024) {
  const file = new File(["bytes"], name, { type: "video/mp4" });
  Object.defineProperty(file, "size", { value: size });
  fireEvent.change(fileInput(container), { target: { files: [file] } });
  return file;
}

function launch() {
  fireEvent.click(screen.getByTestId("launch-audit"));
}

beforeEach(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://backend.test";
  delete process.env.NEXT_PUBLIC_MAX_UPLOAD_BYTES;
});

// ---------------------------------------------------------------------------
// removed fabrications
// ---------------------------------------------------------------------------

describe("no fabricated dashboard content", () => {
  it("renders no placeholder video and contacts no third party", async () => {
    const stub = stubBackend();
    const { container } = render(<HUDView />);

    const video = container.querySelector("video");
    expect(video).toBeNull();
    expect(screen.getByTestId("no-video")).toBeTruthy();
    expect(screen.getByText(/no video loaded/i)).toBeTruthy();

    await new Promise((r) => setTimeout(r, 30));
    expect(stub.calls).toHaveLength(0);
  });

  it("renders no invented network or node gauges", () => {
    stubBackend();
    const { container } = render(<HUDView />);
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/\bnodes\b/i);
    expect(text).not.toMatch(/4096 clusters/i);
    expect(text).not.toMatch(/ready for neural sync/i);
  });

  it("does not offer a control for the unimplemented orchestrator", () => {
    stubBackend();
    const { container } = render(<HUDView />);

    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: /pulse protocol/i })).toBeNull();
    // And it says why, rather than silently dropping the panel.
    expect(container.textContent).toMatch(/not implemented in this build/i);
  });

  it("states the limits of what this build verifies", () => {
    stubBackend();
    render(<HUDView />);
    expect(screen.getByText(/detection accuracy has not been measured/i)).toBeTruthy();
    expect(screen.getByText(/not as a compliance sign-off/i)).toBeTruthy();
  });

  it("shows the session upload list as empty rather than inventing rows", () => {
    stubBackend();
    render(<HUDView />);
    expect(screen.getByText(/no uploads yet in this session/i)).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// file validation
// ---------------------------------------------------------------------------

describe("file validation before upload", () => {
  it("states the real configured limits", () => {
    stubBackend();
    render(<HUDView />);
    // backend/config.py defaults: 100 MiB, mp4/mov/avi/webm/mkv.
    expect(screen.getByText(/up to 100\.0 MB/i)).toBeTruthy();
    expect(screen.getByText(/mp4, mov, avi, webm, mkv/i)).toBeTruthy();
  });

  it("rejects a disallowed extension inline and uploads nothing", async () => {
    const stub = stubBackend();
    const { container } = render(<HUDView />);

    chooseFile(container, "payload.exe");

    expect(screen.getByTestId("file-error").textContent).toMatch(/unsupported file type/i);
    expect(screen.getByTestId("launch-audit").hasAttribute("disabled")).toBe(true);

    launch();
    await new Promise((r) => setTimeout(r, 30));
    expect(stub.calls).toHaveLength(0);
  });

  it("rejects a file over the configured cap before transferring it", async () => {
    process.env.NEXT_PUBLIC_MAX_UPLOAD_BYTES = "1048576";
    const stub = stubBackend();
    const { container } = render(<HUDView />);

    chooseFile(container, "huge.mp4", 5 * 1024 * 1024);

    expect(screen.getByTestId("file-error").textContent).toMatch(/the limit is 1\.0 MB/i);
    expect(stub.calls).toHaveLength(0);
  });

  it("announces the validation failure to assistive technology", () => {
    stubBackend();
    const { container } = render(<HUDView />);
    chooseFile(container, "payload.exe");
    expect(screen.getByRole("alert").textContent).toMatch(/unsupported file type/i);
  });

  it("clears the error once an acceptable file is chosen", async () => {
    stubBackend();
    const { container } = render(<HUDView />);

    chooseFile(container, "payload.exe");
    expect(screen.getByTestId("file-error")).toBeTruthy();

    chooseFile(container, "reel.mp4");
    expect(screen.queryByTestId("file-error")).toBeNull();
    expect(screen.getByTestId("launch-audit").hasAttribute("disabled")).toBe(false);
  });

  it("accepts an allowed file and proceeds to upload", async () => {
    const stub = stubBackend();
    const { container } = render(<HUDView />);

    chooseFile(container, "reel.mov");
    launch();

    await waitFor(() => expect(stub.calls.some((u) => u.includes("/upload-reel"))).toBe(true));
  });

  it("gives the file input an accessible name", () => {
    stubBackend();
    render(<HUDView />);
    expect(screen.getByLabelText(/video file to audit/i)).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// findings
// ---------------------------------------------------------------------------

describe("findings", () => {
  it("shows no findings panel content before an audit exists", () => {
    stubBackend();
    render(<HUDView />);
    expect(screen.getByText(/no audit yet/i)).toBeTruthy();
  });

  it("renders the persisted detections for a completed job", async () => {
    stubBackend({
      jobs: () =>
        jsonResponse(
          job({
            state: "completed",
            stage: "Analysis complete",
            is_terminal: true,
            is_active: false,
            frames_extracted: 10,
            frames_audited: 10,
            frames_failed: 0,
            detections_recorded: 1,
          })
        ),
      violations: () =>
        jsonResponse({
          video_id: VIDEO_ID,
          detections: [
            {
              category: "competitor_logo",
              description: "A rival mark appears on the bottle.",
              confidence: 0.84,
              risk_level: "High",
              bounding_box: [100, 200, 300, 400],
              timestamp: 4,
              frame_number: 4,
            },
          ],
          frame_count: 10,
          audited_frames: 10,
          failed_frames: 0,
          next_cursor: null,
          has_more: false,
          job: job({ state: "completed", is_terminal: true }),
        }),
    });
    const { container } = render(<HUDView />);

    chooseFile(container, "reel.mp4");
    launch();

    await waitFor(() => expect(screen.getByTestId("violation-list")).toBeTruthy());
    expect(screen.getByText("competitor_logo")).toBeTruthy();
    expect(screen.getByText(/a rival mark appears/i)).toBeTruthy();
    expect(screen.getByText("High risk")).toBeTruthy();
    expect(screen.getByText("84%")).toBeTruthy();
  });

  it("reports a completed audit with nothing found as exactly that", async () => {
    stubBackend({
      jobs: () =>
        jsonResponse(
          job({
            state: "completed",
            is_terminal: true,
            is_active: false,
            frames_extracted: 8,
            frames_audited: 8,
            detections_recorded: 0,
          })
        ),
    });
    const { container } = render(<HUDView />);

    chooseFile(container, "reel.mp4");
    launch();

    // Appears twice by design: the section badge and the empty state.
    await waitFor(() =>
      expect(screen.getAllByText(/no violations found/i).length).toBeGreaterThan(0)
    );
    expect(screen.getByText(/8 audited frame\(s\)/i)).toBeTruthy();
    // The sampling limitation is stated rather than implied away.
    expect(screen.getAllByText(/1 fps/i).length).toBeGreaterThan(0);
    expect(screen.queryByTestId("violation-list")).toBeNull();
  });

  it("does not describe a failed audit as having found nothing", async () => {
    stubBackend({
      jobs: () =>
        jsonResponse(
          job({
            state: "failed",
            stage: "Analysis failed",
            is_terminal: true,
            is_active: false,
            error_summary: "The uploaded video could not be read.",
          })
        ),
    });
    const { container } = render(<HUDView />);

    chooseFile(container, "reel.mp4");
    launch();

    await waitFor(() =>
      expect(screen.getByText(/analysis failed — no verdict/i)).toBeTruthy()
    );
    // The backend's safe summary, shown in both the status strip and the
    // findings panel.
    expect(
      screen.getAllByText(/the uploaded video could not be read/i).length
    ).toBeGreaterThan(0);
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/no violations found/i);
    expect(text).not.toMatch(/compliant/i);
  });

  it("says the analysis is in progress rather than showing a partial result", async () => {
    stubBackend({
      jobs: () =>
        jsonResponse(job({ state: "analyzing", stage: "Analyzing frames", frames_extracted: 4 })),
    });
    const { container } = render(<HUDView />);

    chooseFile(container, "reel.mp4");
    launch();

    await waitFor(() => expect(screen.getByText(/analysis in progress/i)).toBeTruthy());
    expect(container.textContent).not.toMatch(/no violations found/i);
  });

  it("reports frames that could not be analyzed", async () => {
    stubBackend({
      jobs: () =>
        jsonResponse(
          job({
            state: "completed",
            is_terminal: true,
            is_active: false,
            frames_extracted: 10,
            frames_audited: 8,
            frames_failed: 2,
            detections_recorded: 0,
          })
        ),
    });
    const { container } = render(<HUDView />);

    chooseFile(container, "reel.mp4");
    launch();

    await waitFor(() =>
      expect(screen.getByTestId("audit-status").textContent).toMatch(
        /2 frame\(s\) could not be analyzed/i
      )
    );
  });

  it("reports an automatically restarted run", async () => {
    stubBackend({
      jobs: () => jsonResponse(job({ recovered_count: 2 })),
    });
    const { container } = render(<HUDView />);

    chooseFile(container, "reel.mp4");
    launch();

    await waitFor(() =>
      expect(screen.getByTestId("audit-status").textContent).toMatch(
        /automatically restarted 2 time\(s\)/i
      )
    );
  });

  it("reports results that could not be loaded instead of showing none", async () => {
    stubBackend({
      jobs: () =>
        jsonResponse(
          job({
            state: "completed",
            is_terminal: true,
            is_active: false,
            frames_audited: 5,
            detections_recorded: 3,
          })
        ),
      violations: () => jsonResponse({ detail: "Analysis store unavailable" }, 502),
    });
    const { container } = render(<HUDView />);

    chooseFile(container, "reel.mp4");
    launch();

    await waitFor(() =>
      expect(screen.getByTestId("poll-notice").textContent).toMatch(
        /analysis store unavailable/i
      )
    );
  });
});

// ---------------------------------------------------------------------------
// playback
// ---------------------------------------------------------------------------

describe("playback", () => {
  it("plays the uploaded video once the upload returns its URL", async () => {
    const { container } = render(<HUDView />);
    stubBackend();

    chooseFile(container, "reel.mp4");
    launch();

    await waitFor(() => expect(container.querySelector("video")).toBeTruthy());
    const video = container.querySelector("video") as HTMLVideoElement;
    expect(video.getAttribute("src")).toBe(UPLOAD_OK.video_url);
    expect(screen.queryByTestId("no-video")).toBeNull();
  });
});
