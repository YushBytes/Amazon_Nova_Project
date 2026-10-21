import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ProjectDetailView from "./ProjectDetailView";
import type { Submission } from "../lib/api";

/**
 * Phase 8: the remediation journey inside a completed audit.
 *
 * Open a completed analysis, inspect a finding, propose a remediation, review
 * it, approve or reject it, and see honest status. Kept in its own file so the
 * Phase 7 verdict tests stay focused on the audit itself.
 *
 * The capability read is what decides whether a generation control exists, so
 * every stub here answers it - a view that guessed at what the server
 * supports would be the defect.
 *
 * `fetch` is stubbed; nothing contacts a backend, and nothing here proves
 * anything about real provider behaviour.
 */

const VIDEO_ID = "vid_abc12345";

const SUBMISSION: Submission = {
  id: VIDEO_ID,
  title: "campaign-reel.mp4",
  original_filename: "campaign-reel.mp4",
  filename: `${VIDEO_ID}.mp4`,
  status: "completed",
  created_at: "2026-10-19T10:00:00+00:00",
  size_bytes: 2 * 1024 * 1024,
  url: `http://backend.test/uploads/${VIDEO_ID}.mp4?access_token=media-token`,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function job(overrides: Record<string, unknown> = {}) {
  return {
    video_id: VIDEO_ID,
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
    detections_recorded: 1,
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

const FLAGGED = {
  video_id: VIDEO_ID,
  detections: [
    {
      category: "competitor_logo",
      description: "A rival mark is visible on the cup.",
      confidence: 0.91,
      risk_level: "High",
      bounding_box: [100, 200, 300, 400],
      timestamp: 3,
      frame_number: 3,
      detection_index: 0,
    },
  ],
  frame_count: 10,
  audited_frames: 10,
  failed_frames: 0,
  next_cursor: null,
  has_more: false,
  job: job(),
};

const CAPABILITIES = {
  remediation_enabled: true,
  proposals_supported: true,
  generation_available: false,
  generation_provider: "none",
  generation_unavailable_reason:
    "Automated image generation is not configured on this deployment.",
  min_confidence: 0.8,
  applies_assets_to_video: false,
  verifies_compliance: false,
  notes: "Proposals only; this deployment changes no video.",
};

function remediationRecord(
  state = "proposed",
  overrides: Record<string, unknown> = {}
) {
  return {
    remediation_id: "rem_abc123abc123",
    video_id: VIDEO_ID,
    frame_number: 3,
    detection_index: 0,
    state,
    stage: "Proposed; awaiting your review",
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
      limitations:
        "Approving this proposal records your decision; it does not by itself change any video.",
      requires_approval: true,
      confidence_gate: "pass",
      gate_explanation: "At or above the configured threshold of 0.8.",
    },
    approved_by: null,
    approved_at: null,
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
    created_at: "2026-10-21T10:00:00+00:00",
    updated_at: "2026-10-21T10:00:00+00:00",
    ...overrides,
  };
}

/** Routes the remediation endpoints alongside the audit ones. */
function stubBackend(
  options: {
    existing?: unknown[];
    caps?: unknown;
    capsStatus?: number;
    jobRecord?: unknown;
    create?: () => Response;
    approve?: () => Response;
    reject?: () => Response;
  } = {}
) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push(`${init?.method ?? "GET"} ${url}`);
    if (url.includes("/remediations/capabilities")) {
      return jsonResponse(options.caps ?? CAPABILITIES, options.capsStatus ?? 200);
    }
    if (url.includes("/approve")) {
      return options.approve
        ? options.approve()
        : jsonResponse(
            remediationRecord("approved", {
              stage: "Approved; no asset generated yet",
              approved_by: "alice",
              approved_at: "2026-10-21T10:05:00+00:00",
            })
          );
    }
    if (url.includes("/reject")) {
      return options.reject
        ? options.reject()
        : jsonResponse(remediationRecord("rejected", { is_terminal: true }));
    }
    if (url.includes("/remediations?video_id=")) {
      return jsonResponse({
        video_id: VIDEO_ID,
        remediations: options.existing ?? [],
      });
    }
    if (url.includes("/remediations")) {
      return options.create
        ? options.create()
        : jsonResponse(remediationRecord());
    }
    if (url.includes("/jobs/")) {
      return jsonResponse(options.jobRecord ?? job());
    }
    if (url.includes("/violations/")) {
      return jsonResponse(FLAGGED);
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

describe("remediation on a completed audit", () => {
  it("offers a proposal control for each real finding", async () => {
    stubBackend();
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() => expect(screen.getByTestId("propose-3-0")).toBeTruthy());
  });

  it("reads capabilities and existing records once the audit loads", async () => {
    const stub = stubBackend();
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() =>
      expect(
        stub.calls.some((c) => c.includes("/remediations/capabilities"))
      ).toBe(true)
    );
    expect(
      stub.calls.some((c) => c.includes(`/remediations?video_id=${VIDEO_ID}`))
    ).toBe(true);
  });

  it("creates a proposal and then shows it for review", async () => {
    stubBackend();
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() => expect(screen.getByTestId("propose-3-0")).toBeTruthy());
    fireEvent.click(screen.getByTestId("propose-3-0"));

    await waitFor(() =>
      expect(screen.getByTestId("remediation-rem_abc123abc123")).toBeTruthy()
    );
    expect(
      screen.getByText(/obscure the reported competitor_logo/i)
    ).toBeTruthy();
    expect(screen.getByText("Awaiting your review")).toBeTruthy();
  });

  it("renders an existing record without re-proposing", async () => {
    const stub = stubBackend({ existing: [remediationRecord("approved")] });
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() =>
      expect(screen.getByText(/awaiting a replacement image/i)).toBeTruthy()
    );
    expect(screen.queryByTestId("propose-3-0")).toBeNull();
    expect(stub.calls.filter((c) => c.startsWith("POST"))).toHaveLength(0);
  });

  it("approves a proposal and reports it as approved, not resolved", async () => {
    stubBackend({ existing: [remediationRecord("proposed")] });
    const { container } = render(
      <ProjectDetailView submission={SUBMISSION} onBack={() => {}} />
    );

    await waitFor(() =>
      expect(screen.getByTestId("approve-rem_abc123abc123")).toBeTruthy()
    );
    fireEvent.click(screen.getByTestId("approve-rem_abc123abc123"));

    await waitFor(() =>
      expect(screen.getByText(/awaiting a replacement image/i)).toBeTruthy()
    );
    const text = (container.textContent ?? "").toLowerCase();
    expect(text).not.toContain("corrected");
    expect(text).not.toContain("compliant");
    // The audit verdict is unchanged: approval altered nothing about the video.
    expect(verdict()).toMatch(/violations found/i);
  });

  it("rejects a proposal", async () => {
    stubBackend({ existing: [remediationRecord("proposed")] });
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() =>
      expect(screen.getByTestId("reject-rem_abc123abc123")).toBeTruthy()
    );
    fireEvent.click(screen.getByTestId("reject-rem_abc123abc123"));

    await waitFor(() => expect(screen.getByText("Rejected")).toBeTruthy());
  });

  it("shows that generation is unavailable on an approved proposal", async () => {
    stubBackend({ existing: [remediationRecord("approved")] });
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() =>
      expect(screen.getByTestId("generation-unavailable")).toBeTruthy()
    );
    expect(screen.getByTestId("generation-unavailable").textContent).toMatch(
      /not configured/i
    );
    expect(screen.queryByTestId("execute-rem_abc123abc123")).toBeNull();
  });

  it("offers no remediation control when the capability read fails", async () => {
    // A deployment with remediation disabled answers 404 here. That is a
    // legitimate answer, so the audit still renders and no control appears.
    stubBackend({ capsStatus: 404, caps: { detail: "Not found" } });
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() =>
      expect(screen.getByTestId("violation-list")).toBeTruthy()
    );
    expect(screen.queryByTestId("propose-3-0")).toBeNull();
    // The audit itself is unaffected.
    expect(verdict()).toMatch(/violations found/i);
  });

  it("does not read remediations for an incomplete analysis", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        calls.push(url);
        if (url.includes("/jobs/")) {
          return jsonResponse(
            job({ state: "analyzing", is_terminal: false, is_active: true })
          );
        }
        throw new Error(`unexpected request: ${url}`);
      })
    );
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() => expect(verdict()).toMatch(/analysis incomplete/i));
    expect(calls.some((c) => c.includes("/remediations"))).toBe(false);
  });

  it("surfaces a superseded proposal rather than acting on it", async () => {
    stubBackend({
      existing: [remediationRecord("proposed")],
      approve: () =>
        jsonResponse(
          {
            detail:
              "The video was re-analysed and this finding is no longer the same.",
          },
          409
        ),
    });
    render(<ProjectDetailView submission={SUBMISSION} onBack={() => {}} />);

    await waitFor(() =>
      expect(screen.getByTestId("approve-rem_abc123abc123")).toBeTruthy()
    );
    fireEvent.click(screen.getByTestId("approve-rem_abc123abc123"));

    await waitFor(() =>
      expect(
        screen
          .getAllByRole("alert")
          .some((el) => /re-analysed/i.test(el.textContent ?? ""))
      ).toBe(true)
    );
  });

  it("issues no remediation request after unmount", async () => {
    const stub = stubBackend();
    const { unmount } = render(
      <ProjectDetailView submission={SUBMISSION} onBack={() => {}} />
    );

    await waitFor(() =>
      expect(
        stub.calls.some((c) => c.includes("/remediations/capabilities"))
      ).toBe(true)
    );
    const before = stub.calls.length;
    unmount();
    await new Promise((r) => setTimeout(r, 40));
    expect(stub.calls.length).toBe(before);
  });

  it("never describes the video as corrected after any decision", async () => {
    stubBackend({ existing: [remediationRecord("proposed")] });
    const { container } = render(
      <ProjectDetailView submission={SUBMISSION} onBack={() => {}} />
    );

    await waitFor(() =>
      expect(screen.getByTestId("approve-rem_abc123abc123")).toBeTruthy()
    );
    fireEvent.click(screen.getByTestId("approve-rem_abc123abc123"));
    await waitFor(() =>
      expect(screen.getByText(/awaiting a replacement image/i)).toBeTruthy()
    );

    const text = (container.textContent ?? "").toLowerCase();
    for (const forbidden of [
      "audit passed",
      "compliant",
      "video updated",
      "no violations found",
    ]) {
      expect(text).not.toContain(forbidden);
    }
    // And the honest statement is present.
    expect(screen.getByText("Original unchanged")).toBeTruthy();
  });
});
