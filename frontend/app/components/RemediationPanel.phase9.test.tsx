import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import RemediationPanel from "./RemediationPanel";
import type {
  Detection,
  Remediation,
  RemediationCapabilities,
  RemediationState,
} from "../lib/api";

/**
 * Phase 9: supplying an image, building a new video, and reporting the check.
 *
 * The assertions are organised around what the panel must never say, because
 * that is where the damage is:
 *
 *  * "corrected" or "compliant" for a video nothing has checked;
 *  * a verdict derived from the state string rather than the server's evidence;
 *  * a download action before a real authorized output exists;
 *  * "compliant" when the target is gone but other findings remain;
 *  * a fabricated percentage.
 *
 * `fetch` is stubbed at the network boundary. Nothing here proves anything
 * about real FFmpeg, real AWS, or real detection behaviour.
 */

const VIDEO_ID = "vid_abc12345";
const REM_ID = "rem_abc123abc123";
const OUTPUT_ID = "vid_0000beef";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function capabilities(
  overrides: Partial<RemediationCapabilities> = {}
): RemediationCapabilities {
  return {
    remediation_enabled: true,
    proposals_supported: true,
    generation_available: false,
    generation_provider: "none",
    generation_unavailable_reason:
      "Automated image generation is not configured on this deployment.",
    min_confidence: 0.8,
    applies_assets_to_video: true,
    verifies_compliance: true,
    notes: "Supply your own replacement image.",
    asset_upload_supported: true,
    max_asset_bytes: 8 * 1024 * 1024,
    accepted_asset_types: ["image/png", "image/jpeg"],
    reconstruction_available: true,
    reconstruction_unavailable_reason: null,
    reconstruction_method: "scaled_overlay",
    verification_policy: "target_only",
    ...overrides,
  };
}

function detection(overrides: Partial<Detection> = {}): Detection {
  return {
    category: "competitor_logo",
    description: "A rival mark is visible on the cup.",
    confidence: 0.91,
    risk_level: "High",
    bounding_box: [100, 200, 300, 400],
    timestamp: 3,
    frame_number: 3,
    detection_index: 0,
    ...overrides,
  };
}

function remediation(
  state: RemediationState = "approved",
  overrides: Partial<Remediation> = {}
): Remediation {
  return {
    remediation_id: REM_ID,
    video_id: VIDEO_ID,
    frame_number: 3,
    detection_index: 0,
    state,
    stage: "Approved; no asset generated yet",
    is_terminal: false,
    is_active: false,
    is_compliance_claim: false,
    source_video_modified: false,
    finding: {
      frame_number: 3,
      detection_index: 0,
      category: "competitor_logo",
      description: "A rival mark is visible on the cup.",
      risk_level: "high",
      confidence: 0.91,
      bounding_box: [100, 200, 300, 400],
      timestamp_seconds: 3,
    },
    proposal: {
      action: "remove_region",
      summary: "Obscure the reported competitor_logo in frame 3.",
      rationale: "The frame analysis flagged it; not a confirmed violation.",
      model_claim: "A rival mark is visible on the cup.",
      target_description: "A region covering roughly 20%-40% across the frame.",
      limitations: "Approving this records your decision; it changes no video.",
      requires_approval: true,
      confidence_gate: "pass",
      gate_explanation: "At or above the configured threshold of 0.8.",
    },
    approved_by: "alice",
    approved_at: "2026-10-21T10:05:00+00:00",
    decided_by: null,
    decided_at: null,
    decision_note: null,
    provider: null,
    provider_model_id: null,
    has_asset: false,
    asset_bytes: null,
    asset_content_type: null,
    output_video_id: null,
    verification_video_id: null,
    verified_at: null,
    error_code: null,
    error_summary: null,
    attempt: 0,
    max_attempts: 3,
    created_at: "2026-10-21T10:00:00+00:00",
    updated_at: "2026-10-21T10:00:00+00:00",
    ...overrides,
  };
}

const ASSET_READY = () =>
  remediation("asset_ready", {
    has_asset: true,
    asset_bytes: 2048,
    asset_content_type: "image/png",
    asset_source: "upload",
    asset_supplied_by: "alice",
    asset_supplied_at: "2026-10-21T10:06:00+00:00",
    stage: "Replacement asset ready; video unchanged",
  });

const WITH_OUTPUT = (state: RemediationState, overrides = {}) =>
  remediation(state, {
    has_asset: true,
    asset_content_type: "image/png",
    has_output: true,
    output_video_id: OUTPUT_ID,
    output_bytes: 4096,
    output_duration_seconds: 10,
    output_has_audio: true,
    analysis_job_video_id: OUTPUT_ID,
    reconstruction: {
      method: "scaled_overlay",
      region_pixels: { x: 384, y: 108, width: 384, height: 216 },
      window_seconds: [2, 4],
      source_width: 1920,
      source_height: 1080,
      source_had_audio: true,
      output_has_audio: true,
      audio_preserved: true,
      warnings: [],
    },
    ...overrides,
  });

function renderPanel(
  props: {
    record?: Remediation | null;
    caps?: RemediationCapabilities | null;
    detection?: Detection;
    onChanged?: (record: Remediation) => void;
  } = {}
) {
  const onChanged = props.onChanged ?? vi.fn();
  const result = render(
    <RemediationPanel
      videoId={VIDEO_ID}
      detection={props.detection ?? detection()}
      capabilities={props.caps === undefined ? capabilities() : props.caps}
      record={props.record ?? null}
      onChanged={onChanged}
    />
  );
  return { ...result, onChanged };
}

function stubBackend(handlers: Record<string, () => Response> = {}) {
  const calls: { url: string; method: string }[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, method: init?.method ?? "GET" });
    for (const [fragment, handler] of Object.entries(handlers)) {
      if (url.includes(fragment)) return handler();
    }
    // Default: no output URL exists.
    if (url.includes("/output")) {
      return jsonResponse({ detail: "No corrected video exists yet." }, 404);
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

function pngFile(name = "logo.png", size = 2048): File {
  const file = new File([new Uint8Array(8)], name, { type: "image/png" });
  Object.defineProperty(file, "size", { value: size });
  return file;
}

beforeEach(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://backend.test";
});

afterEach(() => {
  vi.useRealTimers();
});

// ===========================================================================
// supplying the replacement image
// ===========================================================================

describe("supplying a replacement image", () => {
  it("offers an upload for an approved proposal with no asset", () => {
    stubBackend();
    renderPanel({ record: remediation("approved") });
    expect(screen.getByTestId(`upload-asset-${REM_ID}`)).toBeTruthy();
  });

  it("explains that automatic generation is unavailable", () => {
    stubBackend();
    renderPanel({ record: remediation("approved") });
    const text = screen.getByTestId("generation-unavailable").textContent ?? "";
    assertHonest(text);
    expect(text).toMatch(/not configured/i);
    expect(text).toMatch(/supply the replacement image yourself/i);
  });

  it("labels the file input for assistive technology", () => {
    stubBackend();
    renderPanel({ record: remediation("approved") });
    expect(
      screen.getByLabelText(/replacement image for this finding/i)
    ).toBeTruthy();
  });

  it("uploads the chosen file to the real endpoint", async () => {
    const stub = stubBackend({ "/asset": () => jsonResponse(ASSET_READY()) });
    const { onChanged } = renderPanel({ record: remediation("approved") });

    const input = screen.getByLabelText(
      /replacement image for this finding/i
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { files: [pngFile()] } });

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    const call = stub.calls.find((c) => c.url.includes("/asset"));
    expect(call?.url).toBe(`http://backend.test/remediations/${REM_ID}/asset`);
    expect(call?.method).toBe("POST");
  });

  it("rejects an oversized image inline and uploads nothing", async () => {
    const stub = stubBackend();
    renderPanel({
      record: remediation("approved"),
      caps: capabilities({ max_asset_bytes: 1024 }),
    });

    const input = screen.getByLabelText(
      /replacement image for this finding/i
    ) as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [pngFile("big.png", 5 * 1024 * 1024)] },
    });

    expect(screen.getByTestId("asset-file-error").textContent).toMatch(
      /the limit is/i
    );
    await new Promise((r) => setTimeout(r, 20));
    expect(stub.calls.filter((c) => c.url.includes("/asset"))).toHaveLength(0);
  });

  it("rejects an empty file inline", () => {
    stubBackend();
    renderPanel({ record: remediation("approved") });

    const input = screen.getByLabelText(
      /replacement image for this finding/i
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { files: [pngFile("empty.png", 0)] } });

    expect(screen.getByTestId("asset-file-error").textContent).toMatch(/empty/i);
  });

  it("announces a validation failure to assistive technology", () => {
    stubBackend();
    renderPanel({ record: remediation("approved") });

    const input = screen.getByLabelText(
      /replacement image for this finding/i
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { files: [pngFile("x.png", 0)] } });

    expect(screen.getByRole("alert").textContent).toMatch(/empty/i);
  });

  it("surfaces the backend's rejection of the bytes", async () => {
    stubBackend({
      "/asset": () =>
        jsonResponse(
          { detail: "The image service returned an unsupported image format." },
          422
        ),
    });
    renderPanel({ record: remediation("approved") });

    const input = screen.getByLabelText(
      /replacement image for this finding/i
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { files: [pngFile()] } });

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(
        /unsupported image format/i
      )
    );
  });

  it("reports an attached asset without calling it a correction", () => {
    stubBackend();
    const { container } = renderPanel({ record: ASSET_READY() });

    expect(screen.getByText(/replacement image attached/i)).toBeTruthy();
    expect(screen.getByText("Image ready, video unchanged")).toBeTruthy();
    assertHonest(container.textContent ?? "");
  });

  it("offers no upload when reconstruction is unavailable", () => {
    stubBackend();
    renderPanel({
      record: remediation("approved"),
      caps: capabilities({
        reconstruction_available: false,
        reconstruction_unavailable_reason:
          "Building corrected videos is not enabled on this deployment.",
      }),
    });
    expect(screen.queryByTestId(`upload-asset-${REM_ID}`)).toBeNull();
    expect(
      screen.getByTestId("reconstruction-unavailable").textContent
    ).toMatch(/not enabled/i);
  });
});

// ===========================================================================
// building the new video
// ===========================================================================

describe("building the new video", () => {
  it("offers the build control once an asset is ready", () => {
    stubBackend();
    renderPanel({ record: ASSET_READY() });
    expect(screen.getByTestId(`execute-${REM_ID}`)).toBeTruthy();
  });

  it("says plainly that the original is left alone", () => {
    stubBackend();
    renderPanel({ record: ASSET_READY() });
    expect(
      screen.getByText(/your original is left exactly as uploaded/i)
    ).toBeTruthy();
    expect(screen.getByText("Original unchanged")).toBeTruthy();
  });

  it("does not promise the outcome in the button label", () => {
    // "Build the corrected video" would assert the result before anything has
    // checked it.
    stubBackend();
    renderPanel({ record: ASSET_READY() });
    const label = screen.getByTestId(`execute-${REM_ID}`).textContent ?? "";
    expect(label.toLowerCase()).not.toContain("corrected");
  });

  it("posts to the real execute endpoint", async () => {
    const stub = stubBackend({
      "/execute": () => jsonResponse(remediation("reconstructing")),
    });
    const { onChanged } = renderPanel({ record: ASSET_READY() });

    fireEvent.click(screen.getByTestId(`execute-${REM_ID}`));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    const call = stub.calls.find((c) => c.url.includes("/execute"));
    expect(call?.method).toBe("POST");
  });

  it("disables the control while the request is in flight", async () => {
    let release: (r: Response) => void = () => {};
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    stubBackend({ "/execute": () => pending as unknown as Response });
    renderPanel({ record: ASSET_READY() });

    fireEvent.click(screen.getByTestId(`execute-${REM_ID}`));
    await waitFor(() =>
      expect(
        (screen.getByTestId(`execute-${REM_ID}`) as HTMLButtonElement).disabled
      ).toBe(true)
    );

    // Released inside `act` so the resulting state update lands in the test's
    // act scope rather than after it.
    await act(async () => {
      release(jsonResponse(remediation("reconstructing")));
    });
  });

  it("surfaces a refusal without claiming a failure of the proposal", async () => {
    stubBackend({
      "/execute": () =>
        jsonResponse(
          { detail: "Building corrected videos is not enabled." },
          503
        ),
    });
    renderPanel({ record: ASSET_READY() });

    fireEvent.click(screen.getByTestId(`execute-${REM_ID}`));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/not enabled/i)
    );
    // The record is unchanged.
    expect(screen.getByText("Image ready, video unchanged")).toBeTruthy();
  });

  it("explains a 409 as a state change rather than a fault", async () => {
    stubBackend({
      "/execute": () =>
        jsonResponse(
          { detail: "The video was re-analysed and this finding changed." },
          409
        ),
    });
    renderPanel({ record: ASSET_READY() });

    fireEvent.click(screen.getByTestId(`execute-${REM_ID}`));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/re-analysed/i)
    );
  });
});

// ===========================================================================
// progress
// ===========================================================================

describe("progress while work is in flight", () => {
  it("shows the backend's named stage, never a percentage", async () => {
    stubBackend({
      "/status": () =>
        jsonResponse({
          ...remediation("verifying", {
            stage: "Re-analysing the output video",
            analysis_job_video_id: OUTPUT_ID,
            has_output: true,
            output_video_id: OUTPUT_ID,
          }),
          output_analysis: {
            video_id: OUTPUT_ID,
            state: "analyzing",
            stage: "Analyzing frames",
            is_terminal: false,
            is_active: true,
            attempt: 1,
            max_attempts: 3,
            frames_extracted: 10,
            frames_audited: null,
            frames_failed: null,
            detections_recorded: null,
            error_summary: null,
            model_id: null,
            pipeline_version: "4.0",
            original_filename: null,
            size_bytes: null,
            created_at: null,
            updated_at: null,
            completed_at: null,
          },
        }),
    });

    renderPanel({
      record: remediation("verifying", {
        stage: "Re-analysing the output video",
        has_output: true,
        output_video_id: OUTPUT_ID,
      }),
    });

    await waitFor(() =>
      expect(screen.getByTestId(`progress-${REM_ID}`).textContent).toMatch(
        /analyzing frames/i
      )
    );
    const text = screen.getByTestId(`progress-${REM_ID}`).textContent ?? "";
    expect(text).not.toContain("%");
  });

  it("announces progress politely", () => {
    stubBackend({ "/status": () => jsonResponse({ ...remediation("reconstructing"), output_analysis: null }) });
    renderPanel({ record: remediation("reconstructing") });

    const region = screen.getByTestId(`progress-${REM_ID}`);
    expect(region.getAttribute("role")).toBe("status");
    expect(region.getAttribute("aria-live")).toBe("polite");
  });

  it("polls while work is in flight and stops on a terminal state", async () => {
    let calls = 0;
    const stub = stubBackend({
      "/status": () => {
        calls += 1;
        return jsonResponse({
          ...remediation("verification_failed", {
            is_terminal: true,
            verification: {
              outcome: "verification_failed",
              target_resolved: false,
              policy_passed: false,
              reason: "The targeted competitor_logo is still detected.",
              other_findings: [],
            },
          }),
          output_analysis: null,
        });
      },
    });

    renderPanel({ record: remediation("reconstructing") });

    await waitFor(() => expect(calls).toBe(1));
    // Terminal: no further poll is scheduled.
    await new Promise((r) => setTimeout(r, 60));
    expect(stub.calls.filter((c) => c.url.includes("/status"))).toHaveLength(1);
  });

  it("does not poll a record that is not executing", async () => {
    const stub = stubBackend();
    renderPanel({ record: ASSET_READY() });

    await new Promise((r) => setTimeout(r, 40));
    expect(stub.calls.filter((c) => c.url.includes("/status"))).toHaveLength(0);
  });

  it("stops polling when unmounted", async () => {
    const stub = stubBackend({
      "/status": () =>
        jsonResponse({ ...remediation("reconstructing"), output_analysis: null }),
    });
    const { unmount } = renderPanel({ record: remediation("reconstructing") });

    await waitFor(() =>
      expect(stub.calls.filter((c) => c.url.includes("/status")).length).toBe(1)
    );
    unmount();
    await new Promise((r) => setTimeout(r, 80));
    expect(stub.calls.filter((c) => c.url.includes("/status"))).toHaveLength(1);
  });

  it("reports an automatically restarted run rather than hiding it", async () => {
    stubBackend({
      "/status": () =>
        jsonResponse({
          ...remediation("reconstructing", { recovered_count: 2 }),
          output_analysis: null,
        }),
    });
    renderPanel({ record: remediation("reconstructing", { recovered_count: 2 }) });

    expect(screen.getByTestId(`progress-${REM_ID}`).textContent).toMatch(
      /automatically restarted 2 time/i
    );
  });
});

// ===========================================================================
// the output video
// ===========================================================================

describe("the output video", () => {
  it("offers no player and no link before an output exists", () => {
    stubBackend();
    const { container } = renderPanel({ record: ASSET_READY() });

    expect(container.querySelector("video")).toBeNull();
    expect(screen.queryByTestId(`output-video-${REM_ID}`)).toBeNull();
  });

  it("plays the output through an authorized, scoped URL", async () => {
    stubBackend({
      "/output": () =>
        jsonResponse({
          remediation_id: REM_ID,
          output_video_id: OUTPUT_ID,
          playback_url: `http://backend.test/uploads/${OUTPUT_ID}.mp4?access_token=tok`,
          duration_seconds: 10,
          has_audio: true,
          size_bytes: 4096,
        }),
      "/status": () =>
        jsonResponse({ ...WITH_OUTPUT("verifying"), output_analysis: null }),
    });
    renderPanel({ record: WITH_OUTPUT("applied") });

    await waitFor(() =>
      expect(screen.getByTestId(`output-video-${REM_ID}`)).toBeTruthy()
    );
    const video = screen.getByTestId(
      `output-video-${REM_ID}`
    ) as HTMLVideoElement;
    expect(video.getAttribute("src")).toContain("access_token=");
    // The storage key is never a link.
    expect(video.getAttribute("src")).not.toContain("remediated/");
  });

  it("offers no player when the URL cannot be had", async () => {
    stubBackend({
      "/output": () => jsonResponse({ detail: "Not found" }, 404),
      "/status": () =>
        jsonResponse({ ...WITH_OUTPUT("verifying"), output_analysis: null }),
    });
    renderPanel({ record: WITH_OUTPUT("applied") });

    await waitFor(() =>
      expect(screen.getByText(/preparing playback/i)).toBeTruthy()
    );
    expect(screen.queryByTestId(`output-video-${REM_ID}`)).toBeNull();
  });

  it("describes the method plainly, never as seamless", async () => {
    stubBackend({
      "/output": () => jsonResponse({ detail: "Not found" }, 404),
      "/status": () =>
        jsonResponse({ ...WITH_OUTPUT("verifying"), output_analysis: null }),
    });
    const { container } = renderPanel({ record: WITH_OUTPUT("applied") });

    expect(
      screen.getByText(/scaled overlay over the approved region/i)
    ).toBeTruthy();
    const text = (container.textContent ?? "").toLowerCase();
    expect(text).not.toContain("seamless");
    expect(text).not.toContain("inpaint");
  });

  it("reports whether the audio survived", async () => {
    stubBackend({
      "/output": () => jsonResponse({ detail: "Not found" }, 404),
      "/status": () =>
        jsonResponse({ ...WITH_OUTPUT("verifying"), output_analysis: null }),
    });
    renderPanel({ record: WITH_OUTPUT("applied") });
    expect(screen.getByText("Preserved")).toBeTruthy();
  });
});

// ===========================================================================
// the verdict
// ===========================================================================

describe("the verdict", () => {
  it("reports a resolved target without claiming compliance", () => {
    stubBackend();
    const { container } = renderPanel({
      record: WITH_OUTPUT("verified", {
        is_terminal: true,
        is_compliance_claim: true,
        verified_at: "2026-10-21T10:30:00+00:00",
        verification: {
          outcome: "verified",
          target_resolved: true,
          policy_passed: true,
          frames_audited: 10,
          reason:
            "The targeted competitor_logo is no longer detected at its place in the corrected video.",
          other_findings: [],
        },
      }),
    });

    expect(screen.getByText(/the targeted finding is gone/i)).toBeTruthy();
    expect(screen.getByText(/based on 10 audited frame/i)).toBeTruthy();
    // "the targeted finding is gone" is not "the video is compliant".
    const text = (container.textContent ?? "").toLowerCase();
    expect(text).not.toContain("compliant video");
    expect(text).not.toContain("fully compliant");
  });

  it("reports a remaining target as still there", () => {
    stubBackend();
    renderPanel({
      record: WITH_OUTPUT("verification_failed", {
        is_terminal: true,
        verification: {
          outcome: "verification_failed",
          target_resolved: false,
          policy_passed: false,
          frames_audited: 10,
          reason: "The targeted competitor_logo is still detected.",
          other_findings: [],
        },
      }),
    });

    expect(
      screen.getByText(/the targeted finding is still there/i)
    ).toBeTruthy();
    expect(screen.getByText(/still detected/i)).toBeTruthy();
  });

  it("shows other findings separately from the verdict", () => {
    stubBackend();
    renderPanel({
      record: WITH_OUTPUT("verified", {
        is_terminal: true,
        is_compliance_claim: true,
        verification: {
          outcome: "verified",
          target_resolved: true,
          policy_passed: true,
          frames_audited: 10,
          reason: "The targeted competitor_logo is no longer detected.",
          other_finding_count: 1,
          other_findings: [
            {
              category: "unsafe_item",
              description: "Something else was flagged.",
              confidence: 0.7,
              risk_level: "Medium",
              timestamp: 7,
              frame_number: 7,
            },
          ],
        },
      }),
    });

    // Both facts are shown, and the second is not implied by the first.
    expect(screen.getByText(/the targeted finding is gone/i)).toBeTruthy();
    expect(
      screen.getByTestId(`other-findings-${REM_ID}`).textContent
    ).toMatch(/1 other finding/i);
    expect(screen.getByText(/they are not resolved/i)).toBeTruthy();
    expect(screen.getByText("unsafe_item")).toBeTruthy();
  });

  it("reports an inconclusive check as no conclusion", () => {
    stubBackend();
    renderPanel({
      record: WITH_OUTPUT("failed", {
        is_terminal: true,
        error_code: "verification_inconclusive",
        verification: {
          outcome: "inconclusive",
          target_resolved: null,
          policy_passed: null,
          reason:
            "The corrected video's analysis recorded no audited frames, so its results cannot support a conclusion.",
          other_findings: [],
        },
      }),
    });

    expect(screen.getByText(/no conclusion/i)).toBeTruthy();
    expect(screen.getByText(/cannot support a conclusion/i)).toBeTruthy();
  });

  it("shows no verdict at all before the check has run", () => {
    stubBackend({
      "/output": () => jsonResponse({ detail: "Not found" }, 404),
      "/status": () =>
        jsonResponse({ ...WITH_OUTPUT("applied"), output_analysis: null }),
    });
    renderPanel({ record: WITH_OUTPUT("applied") });
    expect(screen.queryByTestId(`verdict-${REM_ID}`)).toBeNull();
  });

  it("defers to the server's evidence, not the state string", () => {
    // A record in `verified` whose evidence says otherwise must not be shown
    // as a pass.
    stubBackend();
    renderPanel({
      record: WITH_OUTPUT("verified", {
        is_terminal: true,
        is_compliance_claim: false,
        verification: {
          outcome: "inconclusive",
          target_resolved: null,
          policy_passed: null,
          reason: "No usable evidence.",
          other_findings: [],
        },
      }),
    });
    expect(screen.getByText(/no conclusion/i)).toBeTruthy();
    expect(screen.queryByText(/the targeted finding is gone/i)).toBeNull();
  });
});

// ===========================================================================
// retries
// ===========================================================================

describe("retries", () => {
  it("offers a retry for a failed build with attempts left", () => {
    stubBackend();
    renderPanel({
      record: remediation("failed", {
        is_terminal: true,
        has_asset: true,
        attempt: 1,
        max_attempts: 3,
        error_code: "reconstruction_failed",
        error_summary: "The corrected video could not be produced.",
      }),
    });
    expect(screen.getByTestId(`retry-${REM_ID}`)).toBeTruthy();
  });

  it("offers no retry once attempts are exhausted", () => {
    stubBackend();
    renderPanel({
      record: remediation("failed", {
        is_terminal: true,
        attempt: 3,
        max_attempts: 3,
      }),
    });
    expect(screen.queryByTestId(`retry-${REM_ID}`)).toBeNull();
  });

  it("offers no retry for a check that came back negative", () => {
    // The same asset would build the same video. The user needs a different
    // replacement image, which means a new proposal.
    stubBackend();
    renderPanel({
      record: WITH_OUTPUT("verification_failed", {
        is_terminal: true,
        attempt: 1,
        verification: {
          outcome: "verification_failed",
          target_resolved: false,
          policy_passed: false,
          reason: "Still detected.",
          other_findings: [],
        },
      }),
    });
    expect(screen.queryByTestId(`retry-${REM_ID}`)).toBeNull();
  });

  it("shows a failure's safe summary", () => {
    stubBackend();
    renderPanel({
      record: remediation("failed", {
        is_terminal: true,
        error_code: "unsafe_region",
        error_summary:
          "This finding has no region on the frame, so an automatic correction cannot be placed.",
      }),
    });
    expect(
      screen.getByTestId(`error-summary-${REM_ID}`).textContent
    ).toMatch(/no region on the frame/i);
  });
});

// ===========================================================================
// the sweeping honesty check
// ===========================================================================

/** Words the panel must never show for a video nothing has checked. */
function assertHonest(text: string) {
  const lowered = text.toLowerCase();
  for (const forbidden of [
    "audit passed",
    "fully compliant",
    "video corrected",
    "seamless",
    "guaranteed",
  ]) {
    expect(lowered).not.toContain(forbidden);
  }
}

describe("the panel never overstates what happened", () => {
  const states: RemediationState[] = [
    "approved",
    "asset_ready",
    "reconstructing",
    "applied",
    "verifying",
    "failed",
  ];

  it.each(states)("says nothing compliant for a %s record", (state) => {
    stubBackend({
      "/output": () => jsonResponse({ detail: "Not found" }, 404),
      "/status": () =>
        jsonResponse({ ...remediation(state), output_analysis: null }),
    });
    const { container } = renderPanel({
      record: remediation(state, { has_asset: state !== "approved" }),
    });
    assertHonest(container.textContent ?? "");
  });

  it("always says the original is unchanged", () => {
    stubBackend({
      "/output": () => jsonResponse({ detail: "Not found" }, 404),
      "/status": () =>
        jsonResponse({ ...WITH_OUTPUT("verified"), output_analysis: null }),
    });
    for (const state of states) {
      const { unmount } = renderPanel({ record: remediation(state) });
      expect(screen.getByText("Original unchanged")).toBeTruthy();
      unmount();
    }
  });

  it("marks every non-proposed state as not a compliance sign-off", () => {
    stubBackend({
      "/output": () => jsonResponse({ detail: "Not found" }, 404),
      "/status": () =>
        jsonResponse({ ...remediation("applied"), output_analysis: null }),
    });
    renderPanel({ record: WITH_OUTPUT("applied") });
    expect(screen.getByText("Not a compliance sign-off")).toBeTruthy();
  });
});
