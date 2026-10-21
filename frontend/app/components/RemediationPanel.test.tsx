import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import RemediationPanel from "./RemediationPanel";
import type {
  Detection,
  Remediation,
  RemediationCapabilities,
  RemediationState,
} from "../lib/api";

/**
 * Phase 8: proposing, reviewing and deciding a remediation.
 *
 * The assertions that matter most are the ones about what the panel must NOT
 * say. An approved proposal has generated nothing and changed no video, and
 * the historical defects in this codebase were both of the same shape: a
 * success flag standing in for a chain of claims that had not happened.
 *
 * `fetch` is stubbed at the network boundary; nothing contacts a backend, and
 * nothing here proves anything about real provider behaviour.
 */

const VIDEO_ID = "vid_abcd1234";

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
    applies_assets_to_video: false,
    verifies_compliance: false,
    notes: "Proposals only.",
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
  state: RemediationState = "proposed",
  overrides: Partial<Remediation> = {}
): Remediation {
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
      rationale:
        "The frame analysis flagged a competitor_logo on frame 3. That reflects what the model reported, not a confirmed violation of any specific rule.",
      model_claim: "A rival mark is visible on the cup.",
      target_description: "A region covering roughly 20%-40% across the frame.",
      limitations:
        "This proposal is derived only from one model's analysis of a single video frame. Approving this proposal records your decision; it does not by itself change any video.",
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

function renderPanel(
  props: {
    record?: Remediation | null;
    caps?: RemediationCapabilities | null | undefined;
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
  const calls: { url: string; method: string; body?: unknown }[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({
      url,
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    for (const [fragment, handler] of Object.entries(handlers)) {
      if (url.includes(fragment)) return handler();
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

beforeEach(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://backend.test";
});

// ---------------------------------------------------------------------------
// proposing
// ---------------------------------------------------------------------------

describe("proposing a remediation", () => {
  it("offers a proposal control for an addressable finding", () => {
    stubBackend();
    renderPanel();
    expect(screen.getByTestId("propose-3-0")).toBeTruthy();
  });

  it("creates the proposal against the finding's real address", async () => {
    const stub = stubBackend({
      "/remediations": () => jsonResponse(remediation()),
    });
    const { onChanged } = renderPanel();

    fireEvent.click(screen.getByTestId("propose-3-0"));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    const call = stub.calls.find((c) => c.method === "POST");
    expect(call?.body).toEqual({
      video_id: VIDEO_ID,
      frame_number: 3,
      detection_index: 0,
    });
  });

  it("offers nothing for a finding with no addressable index", () => {
    stubBackend();
    renderPanel({ detection: detection({ detection_index: undefined }) });

    expect(screen.queryByTestId("propose-3-0")).toBeNull();
    expect(screen.getByText(/cannot be addressed/i)).toBeTruthy();
  });

  it("renders nothing at all while capabilities are loading", () => {
    stubBackend();
    const { container } = renderPanel({ caps: null });
    expect(container.textContent).toBe("");
  });

  it("renders nothing when the capability read failed", () => {
    // `undefined` means unknown. Offering a control would guess at what the
    // server supports.
    stubBackend();
    const { container } = renderPanel({ caps: undefined as never });
    expect(container.textContent).not.toBe("");
  });

  it("disables the control while a request is in flight", async () => {
    let release: (r: Response) => void = () => {};
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    stubBackend({ "/remediations": () => pending as unknown as Response });
    renderPanel();

    fireEvent.click(screen.getByTestId("propose-3-0"));

    await waitFor(() =>
      expect(
        (screen.getByTestId("propose-3-0") as HTMLButtonElement).disabled
      ).toBe(true)
    );
    release(jsonResponse(remediation()));
  });

  it("surfaces a failure without claiming anything happened", async () => {
    stubBackend({
      "/remediations": () =>
        jsonResponse({ detail: "Finding not found" }, 404),
    });
    renderPanel();

    fireEvent.click(screen.getByTestId("propose-3-0"));

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/finding not found/i)
    );
  });

  it("issues no request after unmount", async () => {
    let release: (r: Response) => void = () => {};
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    const stub = stubBackend({
      "/remediations": () => pending as unknown as Response,
    });
    const { unmount, onChanged } = renderPanel();

    fireEvent.click(screen.getByTestId("propose-3-0"));
    unmount();
    release(jsonResponse(remediation()));

    await new Promise((r) => setTimeout(r, 40));
    expect(onChanged).not.toHaveBeenCalled();
    expect(stub.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// reviewing
// ---------------------------------------------------------------------------

describe("reviewing a proposal", () => {
  it("shows the proposed change, its basis and its limits", () => {
    stubBackend();
    renderPanel({ record: remediation() });

    expect(screen.getByText(/obscure the reported competitor_logo/i)).toBeTruthy();
    expect(screen.getByText(/not a confirmed violation/i)).toBeTruthy();
    expect(screen.getByText(/a region covering roughly/i)).toBeTruthy();
    expect(
      screen.getByText(/does not by itself change any video/i)
    ).toBeTruthy();
  });

  it("quotes the model's claim as the model's, not as fact", () => {
    stubBackend();
    renderPanel({ record: remediation() });
    expect(screen.getByText(/model said/i)).toBeTruthy();
  });

  it("states that the original video is unchanged", () => {
    stubBackend();
    renderPanel({ record: remediation() });
    // Phase 9 builds a NEW video, so the reassurance a reader needs is about
    // the ORIGINAL specifically.
    expect(screen.getByText("Original unchanged")).toBeTruthy();
  });

  it("reports the confidence gate", () => {
    stubBackend();
    renderPanel({ record: remediation() });
    expect(screen.getByText(/met the confidence threshold/i)).toBeTruthy();
  });

  it("reports a below-threshold gate honestly", () => {
    stubBackend();
    renderPanel({
      record: remediation("proposed", {
        proposal: {
          ...remediation().proposal,
          confidence_gate: "below_threshold",
          gate_explanation: "Below the configured threshold of 0.8.",
        },
      }),
    });
    expect(screen.getByText(/below the confidence threshold/i)).toBeTruthy();
  });

  it("offers approve and reject for a proposed record", () => {
    stubBackend();
    renderPanel({ record: remediation() });
    expect(screen.getByTestId("approve-rem_abc123abc123")).toBeTruthy();
    expect(screen.getByTestId("reject-rem_abc123abc123")).toBeTruthy();
  });

  it("labels the panel for assistive technology", () => {
    stubBackend();
    renderPanel({ record: remediation() });
    expect(
      screen.getByRole("region", { name: /remediation for frame 3/i })
    ).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// deciding
// ---------------------------------------------------------------------------

describe("deciding", () => {
  it("approves through the real endpoint and sends no identity", async () => {
    const approved = remediation("approved", {
      stage: "Approved; no asset generated yet",
      approved_by: "alice",
      approved_at: "2026-10-21T10:05:00+00:00",
    });
    const stub = stubBackend({ "/approve": () => jsonResponse(approved) });
    const { onChanged } = renderPanel({ record: remediation() });

    fireEvent.click(screen.getByTestId("approve-rem_abc123abc123"));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(approved));
    const call = stub.calls.find((c) => c.url.includes("/approve"));
    expect(call?.url).toBe(
      "http://backend.test/remediations/rem_abc123abc123/approve"
    );
    // The approver comes from the session token server-side.
    expect(call?.body).toEqual({ note: null });
  });

  it("rejects through the real endpoint", async () => {
    const rejected = remediation("rejected", { is_terminal: true });
    stubBackend({ "/reject": () => jsonResponse(rejected) });
    const { onChanged } = renderPanel({ record: remediation() });

    fireEvent.click(screen.getByTestId("reject-rem_abc123abc123"));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(rejected));
  });

  it("explains a 409 as a state change rather than a fault", async () => {
    stubBackend({
      "/approve": () =>
        jsonResponse(
          {
            detail:
              "The video was re-analysed and this finding is no longer the same.",
          },
          409
        ),
    });
    renderPanel({ record: remediation() });

    fireEvent.click(screen.getByTestId("approve-rem_abc123abc123"));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/re-analysed/i)
    );
  });

  it("reports an expired session as an instruction to sign in", async () => {
    stubBackend({
      "/approve": () =>
        jsonResponse({ detail: "Could not validate credentials" }, 401),
    });
    renderPanel({ record: remediation() });

    fireEvent.click(screen.getByTestId("approve-rem_abc123abc123"));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(
        /session has expired/i
      )
    );
  });

  it("offers no decision controls for a terminal record", () => {
    stubBackend();
    renderPanel({ record: remediation("rejected", { is_terminal: true }) });

    expect(screen.queryByTestId("approve-rem_abc123abc123")).toBeNull();
    expect(screen.queryByTestId("reject-rem_abc123abc123")).toBeNull();
    expect(screen.getByText("Rejected")).toBeTruthy();
  });

  it("lets an approved record be withdrawn", async () => {
    const cancelled = remediation("cancelled", { is_terminal: true });
    stubBackend({ "/cancel": () => jsonResponse(cancelled) });
    const { onChanged } = renderPanel({ record: remediation("approved") });

    fireEvent.click(screen.getByTestId("cancel-rem_abc123abc123"));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(cancelled));
  });

  it("shows who approved it and when", () => {
    stubBackend();
    renderPanel({
      record: remediation("approved", {
        approved_by: "alice",
        approved_at: "2026-10-21T10:05:00+00:00",
      }),
    });
    expect(screen.getByText(/approved by alice/i)).toBeTruthy();
  });

  it("shows a superseded record's reason", () => {
    stubBackend();
    renderPanel({
      record: remediation("superseded", {
        is_terminal: true,
        error_code: "finding_changed",
        error_summary:
          "The video was re-analysed and this finding is no longer the same.",
      }),
    });
    expect(screen.getByText("Superseded")).toBeTruthy();
    expect(screen.getByText(/no longer the same/i)).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// generation availability
// ---------------------------------------------------------------------------

describe("generation availability", () => {
  it("offers no generate control when no provider is available", () => {
    stubBackend();
    renderPanel({ record: remediation("approved") });

    expect(screen.queryByTestId("execute-rem_abc123abc123")).toBeNull();
    expect(screen.getByTestId("generation-unavailable").textContent).toMatch(
      /not configured/i
    );
  });

  it("offers the supplied-asset path instead, and says why", () => {
    // Phase 9's answer to an unavailable provider: a supported alternative,
    // not a dead end.
    stubBackend();
    renderPanel({ record: remediation("approved") });

    expect(screen.getByTestId("upload-asset-rem_abc123abc123")).toBeTruthy();
    expect(screen.getByTestId("generation-unavailable").textContent).toMatch(
      /supply the replacement image yourself/i
    );
  });

  it("shows the backend's own reason for a configured-but-unimplemented provider", () => {
    stubBackend();
    renderPanel({
      record: remediation("approved"),
      caps: capabilities({
        generation_provider: "nova-canvas",
        generation_unavailable_reason:
          "The nova-canvas image provider is not implemented.",
      }),
    });
    expect(screen.getByTestId("generation-unavailable").textContent).toMatch(
      /not implemented/i
    );
  });

  it("offers the build control once an asset is ready", () => {
    stubBackend();
    renderPanel({ record: remediation("asset_ready", { has_asset: true }) });
    expect(screen.getByTestId("execute-rem_abc123abc123")).toBeTruthy();
  });

  it("offers no build control for an unapproved proposal", () => {
    stubBackend();
    renderPanel({ record: remediation("proposed") });
    expect(screen.queryByTestId("execute-rem_abc123abc123")).toBeNull();
    expect(screen.queryByTestId("upload-asset-rem_abc123abc123")).toBeNull();
  });

  it("offers no build control when reconstruction is disabled", () => {
    stubBackend();
    renderPanel({
      record: remediation("asset_ready", { has_asset: true }),
      caps: capabilities({ reconstruction_available: false }),
    });
    expect(screen.queryByTestId("execute-rem_abc123abc123")).toBeNull();
  });

  it("surfaces a 503 from execute without claiming a failure of the proposal", async () => {
    stubBackend({
      "/execute": () =>
        jsonResponse(
          { detail: "Building corrected videos is not enabled." },
          503
        ),
    });
    renderPanel({ record: remediation("asset_ready", { has_asset: true }) });

    fireEvent.click(screen.getByTestId("execute-rem_abc123abc123"));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/not enabled/i)
    );
    // The record is unchanged - nothing was attempted, so nothing failed.
    expect(screen.getByText("Image ready, video unchanged")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// no fabricated compliance claim
// ---------------------------------------------------------------------------

describe("the panel never claims a video was corrected", () => {
  const states: RemediationState[] = [
    "proposed",
    "approved",
    "asset_ready",
    "reconstructing",
    "applied",
    "verifying",
    "rejected",
    "cancelled",
    "superseded",
  ];

  it.each(states)("says nothing compliant for a %s record", (state) => {
    stubBackend();
    const { container } = renderPanel({ record: remediation(state) });
    const text = (container.textContent ?? "").toLowerCase();

    expect(text).not.toContain("compliant");
    expect(text).not.toContain("corrected");
    expect(text).not.toContain("audit passed");
    expect(text).not.toContain("resolved");
    expect(text).not.toContain("fixed");
  });

  it("names what an approved proposal is still waiting for", () => {
    stubBackend();
    const { container } = renderPanel({ record: remediation("approved") });
    const text = (container.textContent ?? "").toLowerCase();

    expect(screen.getByText(/awaiting a replacement image/i)).toBeTruthy();
    expect(screen.getByText("Original unchanged")).toBeTruthy();
    expect(text).not.toContain("video updated");
  });

  it("does not treat an asset as a correction", () => {
    stubBackend();
    renderPanel({
      record: remediation("asset_ready", {
        has_asset: true,
        asset_bytes: 2048,
        asset_content_type: "image/png",
      }),
    });
    expect(screen.getByText("Image ready, video unchanged")).toBeTruthy();
    expect(screen.getByText("Original unchanged")).toBeTruthy();
  });

  it("does not treat a built video as checked or compliant", () => {
    stubBackend();
    const { container } = renderPanel({
      record: remediation("applied", {
        source_video_modified: true,
        has_output: true,
        output_video_id: "vid_0000beef",
      }),
    });
    expect(screen.getByText("New video built, not yet checked")).toBeTruthy();
    const text = (container.textContent ?? "").toLowerCase();
    expect(text).not.toContain("compliant");
    expect(text).not.toContain("corrected video is");
    // The reassurance about the original survives into this state.
    expect(screen.getByText("Original unchanged")).toBeTruthy();
  });

  it("does not report a failed check as clean", () => {
    stubBackend();
    const { container } = renderPanel({
      record: remediation("verification_failed", {
        is_terminal: true,
        source_video_modified: true,
        verification: {
          outcome: "verification_failed",
          target_resolved: false,
          policy_passed: false,
          reason: "The targeted competitor_logo is still detected.",
          other_findings: [],
        },
      }),
    });
    expect(screen.getByText("Checked: the finding is still there")).toBeTruthy();
    const text = (container.textContent ?? "").toLowerCase();
    expect(text).not.toContain("compliant");
    expect(text).not.toContain("no violations");
  });

  it("defers to the server's compliance field rather than the state string", () => {
    // A record claiming `verified` but with is_compliance_claim false must not
    // be rendered as a compliance claim: the server's field is authoritative.
    stubBackend();
    renderPanel({
      record: remediation("verified", {
        is_terminal: true,
        is_compliance_claim: false,
        verification: {
          outcome: "verified",
          target_resolved: true,
          policy_passed: true,
          reason: "x",
          other_findings: [],
        },
      }),
    });
    expect(screen.getByText("Not a compliance sign-off")).toBeTruthy();
  });
});
