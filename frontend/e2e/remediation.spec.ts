import { expect, test } from "@playwright/test";

import {
  AGENT,
  installApiFallback,
  seedSession,
  stubAuthMe,
  stubJob,
  stubRemediationCapabilities,
  stubRemediationList,
  stubSubmissions,
  stubViolations,
} from "./fixtures/api";

/**
 * Journey 5: the reviewed remediation workflow, through the real UI.
 *
 * WHAT IS AND IS NOT BEING SHOWN HERE. Every remediation record below is a
 * fixture this file invented. No image is generated, no video is built, no
 * FFmpeg runs, and the "output video" in the one test that has one is a URL
 * that is never fetched. **Nothing here is AI output, and a passing run is
 * not evidence that any correction works.** The real pipeline has no
 * automated image generation at all.
 *
 * What these tests do protect is the thing the UI is uniquely responsible
 * for: that the review states are never collapsed into "fixed". The backend's
 * state machine has twelve states and only ONE of them - `verified`, and only
 * when the server's own `is_compliance_claim` says so - may be described as
 * compliant. A UI that called `applied` "corrected" would be asserting that a
 * video is compliant when nothing has checked it.
 */

const VIDEO_ID = "vid_e2e0002b";
const REM_ID = "rem_e2e000000001";

function remediation(overrides: Record<string, unknown> = {}) {
  return {
    remediation_id: REM_ID,
    video_id: VIDEO_ID,
    frame_number: 3,
    detection_index: 0,
    state: "proposed",
    category: "competitor_logo",
    description: "A rival mark is visible on the cup.",
    confidence: 0.91,
    has_asset: false,
    is_compliance_claim: false,
    source_video_modified: false,
    is_terminal: false,
    is_active: true,
    stage: "Proposed; awaiting your review",
    created_at: "2026-10-21T09:00:00Z",
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
    // Required in practice: the panel renders the proposal's own wording, and
    // `app/lib/api.ts` casts the list response without validating it. A record
    // without this used to crash the whole view - see the Phase 10.1 report
    // and the guard regression tests in RemediationPanel.test.tsx.
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
    ...overrides,
  };
}

async function openAuditDetail(
  page: import("@playwright/test").Page,
  records: unknown[],
  capabilities?: unknown
) {
  const api = await installApiFallback(page);
  await seedSession(page, AGENT);
  await stubAuthMe(page, AGENT);
  await stubRemediationCapabilities(page, capabilities);
  await stubSubmissions(page, [
    {
      id: VIDEO_ID,
      filename: `${VIDEO_ID}.mp4`,
      original_filename: "campaign-cut.mp4",
      status: "completed",
      created_at: "2026-10-20T10:00:00Z",
    },
  ]);
  await stubJob(page, VIDEO_ID, {
    video_id: VIDEO_ID,
    state: "completed",
    stage: "done",
    frames_total: 10,
    frames_audited: 10,
    detections_recorded: 1,
  });
  await stubViolations(page, VIDEO_ID, [
    {
      frame_number: 3,
      detection_index: 0,
      timestamp: 3,
      category: "competitor_logo",
      description: "A rival mark is visible on the cup.",
      risk_level: "high",
      confidence: 0.91,
      bounding_box: [100, 200, 300, 400],
    },
  ]);
  await stubRemediationList(page, records);

  await page.goto("/");
  await page.getByRole("button", { name: /Your Videos/i }).click();
  await page.getByRole("button", { name: /Review audit/i }).click();
  return api;
}

test.describe("remediation review", () => {
  test("a proposal awaits review and offers approve and reject", async ({
    page,
  }) => {
    const api = await openAuditDetail(page, [remediation()]);

    const card = page.getByTestId(`remediation-${REM_ID}`);
    await expect(card).toBeVisible();
    await expect(card).toContainText("Awaiting your review");
    await expect(page.getByTestId(`approve-${REM_ID}`)).toBeVisible();
    await expect(page.getByTestId(`reject-${REM_ID}`)).toBeVisible();
    expect(api.unmatched).toEqual([]);
  });

  test("a proposal is never described as fixed or compliant", async ({ page }) => {
    await openAuditDetail(page, [remediation()]);

    const card = page.getByTestId(`remediation-${REM_ID}`);
    const text = (await card.innerText()) ?? "";
    expect(text).not.toMatch(/compliant|corrected|fixed|resolved|seamless/i);
  });

  test("an unreviewed proposal offers no way to build a video", async ({
    page,
  }) => {
    // The review gate. From `proposed` the backend refuses execution, so
    // offering the control would be a lie - and a user who found one would be
    // bypassing the only human checkpoint in the workflow.
    await openAuditDetail(page, [remediation({ state: "proposed" })]);

    await expect(page.getByTestId(`upload-asset-${REM_ID}`)).toHaveCount(0);
    await expect(page.getByTestId(`execute-${REM_ID}`)).toHaveCount(0);
  });

  test("an approved proposal asks for the replacement image, not a build", async ({
    page,
  }) => {
    // `approved` without an asset cannot be executed: no image provider
    // exists, so the owner must supply the image.
    await openAuditDetail(
      page,
      [remediation({ state: "approved", has_asset: false })]
    );

    const card = page.getByTestId(`remediation-${REM_ID}`);
    await expect(card).toContainText("Approved, awaiting a replacement image");
    await expect(page.getByTestId(`upload-asset-${REM_ID}`)).toBeVisible();
    // The decision buttons are gone: it is already decided.
    await expect(page.getByTestId(`approve-${REM_ID}`)).toHaveCount(0);
  });

  test("automatic generation is stated as unavailable rather than offered", async ({
    page,
  }) => {
    // There is no image generation in this build. The panel says so instead
    // of showing a "Generate" button that could not work.
    await openAuditDetail(
      page,
      [remediation({ state: "approved", has_asset: false })],
      {
        remediation_enabled: true,
        proposals_supported: true,
        generation_available: false,
        generation_provider: "none",
        generation_unavailable_reason:
          "No image provider is configured on this deployment.",
        min_confidence: 0.8,
        applies_assets_to_video: true,
        verifies_compliance: true,
        notes: "Owner-supplied replacement images only.",
        asset_upload_supported: true,
        reconstruction_available: true,
        reconstruction_unavailable_reason: null,
      }
    );

    const notice = page.getByTestId("generation-unavailable");
    await expect(notice).toBeVisible();
    await expect(notice).toContainText(/Automatic generation unavailable/i);
    await expect(page.getByRole("button", { name: /^Generate$/i })).toHaveCount(0);
  });

  test("an attached image is not described as a changed video", async ({
    page,
  }) => {
    await openAuditDetail(
      page,
      [remediation({ state: "asset_ready", has_asset: true })]
    );

    const card = page.getByTestId(`remediation-${REM_ID}`);
    await expect(card).toContainText("Image ready, video unchanged");
    const text = (await card.innerText()) ?? "";
    expect(text).not.toMatch(/compliant|corrected|fixed/i);
  });

  test("a built but unchecked output is not called corrected", async ({ page }) => {
    // The most load-bearing wording in the whole UI. `applied` means a new
    // video exists and NOTHING has analysed it.
    await openAuditDetail(
      page,
      [
        remediation({
          state: "applied",
          has_asset: true,
          is_compliance_claim: false,
          output_video_id: "vid_e2eout001",
        }),
      ]
    );

    const card = page.getByTestId(`remediation-${REM_ID}`);
    await expect(card).toContainText("New video built, not yet checked");
    const text = (await card.innerText()) ?? "";
    expect(text).not.toMatch(/compliant|corrected\b|verified\b|fixed/i);
  });

  test("a failed verification says the finding is still there", async ({
    page,
  }) => {
    // A `verification_failed` record is NOT clean, and must not read as a
    // neutral error either - the finding survived the correction.
    await openAuditDetail(
      page,
      [
        remediation({
          state: "verification_failed",
          has_asset: true,
          is_compliance_claim: false,
          output_video_id: "vid_e2eout001",
        }),
      ]
    );

    const card = page.getByTestId(`remediation-${REM_ID}`);
    await expect(card).toContainText("Checked: the finding is still there");
    const text = (await card.innerText()) ?? "";
    expect(text).not.toMatch(/compliant|no violations/i);
  });

  test("only a server-confirmed compliance claim is reported as checked-clean", async ({
    page,
  }) => {
    // `verified` AND `is_compliance_claim: true`, which is the server's own
    // answer. The UI never re-derives this from the state string.
    await openAuditDetail(
      page,
      [
        remediation({
          state: "verified",
          has_asset: true,
          is_compliance_claim: true,
          output_video_id: "vid_e2eout001",
          verification: {
            target_resolved: true,
            policy_passed: true,
            other_findings: 0,
          },
        }),
      ]
    );

    const card = page.getByTestId(`remediation-${REM_ID}`);
    await expect(card).toContainText("Checked: the finding is gone");
  });

  test("a verified state without the server's claim flag is not called clean", async ({
    page,
  }) => {
    // `is_compliance_claim` absent means "no". A UI that inferred the claim
    // from `state === "verified"` would pass this test wrongly, so it asserts
    // the compliance WORDING is withheld rather than the state label.
    await openAuditDetail(
      page,
      [
        remediation({
          state: "verified",
          has_asset: true,
          // is_compliance_claim deliberately omitted
          output_video_id: "vid_e2eout001",
        }),
      ]
    );

    const card = page.getByTestId(`remediation-${REM_ID}`);
    const text = (await card.innerText()) ?? "";
    expect(text).not.toMatch(/\bcompliant\b/i);
  });

  test("a stale proposal is shown as superseded and cannot be acted on", async ({
    page,
  }) => {
    // A re-analysis overwrites the frame row, so a proposal that addressed an
    // older finding must become `superseded` rather than remain approvable.
    await openAuditDetail(page, [remediation({ state: "superseded" })]);

    const card = page.getByTestId(`remediation-${REM_ID}`);
    await expect(card).toContainText("Superseded");
    await expect(page.getByTestId(`approve-${REM_ID}`)).toHaveCount(0);
    await expect(page.getByTestId(`upload-asset-${REM_ID}`)).toHaveCount(0);
  });

  test("a rejected proposal offers no path forward", async ({ page }) => {
    await openAuditDetail(page, [remediation({ state: "rejected" })]);

    const card = page.getByTestId(`remediation-${REM_ID}`);
    await expect(card).toContainText("Rejected");
    await expect(page.getByTestId(`approve-${REM_ID}`)).toHaveCount(0);
    await expect(page.getByTestId(`upload-asset-${REM_ID}`)).toHaveCount(0);
  });

  test("the word seamless never appears anywhere in the panel", async ({
    page,
  }) => {
    // The overlay is a concealment with hard edges: no inpainting, no grain
    // matching, no motion tracking. Calling it seamless would overstate what
    // the pipeline does.
    await openAuditDetail(
      page,
      [
        remediation({
          state: "verified",
          has_asset: true,
          is_compliance_claim: true,
          reconstruction_method: "scaled_overlay",
          output_video_id: "vid_e2eout001",
        }),
      ]
    );

    const body = await page.locator("body").innerText();
    expect(body).not.toMatch(/seamless|inpaint/i);
  });

  test("a findings list with no proposals still renders the finding", async ({
    page,
  }) => {
    const api = await openAuditDetail(page, []);

    await expect(
      page.getByText("A rival mark is visible on the cup.")
    ).toBeVisible();
    // And a propose control for it, since the job completed with a finding.
    await expect(page.getByTestId("propose-3-0")).toBeVisible();
    expect(api.unmatched).toEqual([]);
  });
});
