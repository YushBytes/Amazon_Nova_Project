import { expect, test } from "@playwright/test";

import {
  AGENT,
  CLIENT,
  installApiFallback,
  seedSession,
  stubAuthMe,
  stubJob,
  stubRemediationCapabilities,
  stubRemediationList,
  stubSubmissions,
  stubSubmissionsError,
  stubViolations,
} from "./fixtures/api";

/**
 * Journey 4: the compliance workflow, from the video list to the findings.
 *
 * The single most important assertion in this file is the negative one: an
 * analysis that has NOT completed must never be described as clean. That is
 * the rule an earlier phase violated with an unconditional "AUDIT PASSED"
 * card rendered for every video regardless of job state, and it is a
 * correctness problem with real consequences - a brand would be told its
 * footage is compliant on the strength of an analysis that never ran.
 *
 * `verdictFor` is the single place that decides this. These tests exercise it
 * through the real UI rather than calling it directly, so a view that
 * re-derived a verdict locally would be caught.
 *
 * Every response here is a fixture. Nothing proves the backend produces these
 * shapes, and no video is analysed - see the fixture module's header.
 */

const VIDEO_ID = "vid_e2e0001a";

function submission(overrides: Record<string, unknown> = {}) {
  return {
    id: VIDEO_ID,
    filename: "vid_e2e0001a.mp4",
    original_filename: "campaign-cut.mp4",
    status: "completed",
    created_at: "2026-10-20T10:00:00Z",
    size_bytes: 2_400_000,
    ...overrides,
  };
}

function detection(overrides: Record<string, unknown> = {}) {
  return {
    frame_number: 3,
    detection_index: 0,
    timestamp: 3,
    category: "competitor_logo",
    description: "A rival mark is visible on the cup.",
    risk_level: "high",
    confidence: 0.91,
    bounding_box: [100, 200, 300, 400],
    ...overrides,
  };
}

async function signedInAgent(page: import("@playwright/test").Page) {
  const api = await installApiFallback(page);
  await seedSession(page, AGENT);
  await stubAuthMe(page, AGENT);
  await stubRemediationCapabilities(page);
  return api;
}

test.describe("compliance workflow", () => {
  test("an empty video list says so rather than rendering nothing", async ({
    page,
  }) => {
    const api = await signedInAgent(page);
    await stubSubmissions(page, []);

    await page.goto("/");
    await page.getByRole("button", { name: /Your Videos/i }).click();

    await expect(page.getByText("No videos yet")).toBeVisible();
    expect(api.unmatched).toEqual([]);
  });

  test("a failed video list shows an error with a retry, not an empty list", async ({
    page,
  }) => {
    await signedInAgent(page);
    await stubSubmissionsError(page, 500);

    await page.goto("/");
    await page.getByRole("button", { name: /Your Videos/i }).click();

    // "Nothing yet" and "something went wrong" are different states on
    // purpose; showing the empty state here would be a lie.
    await expect(page.getByText("No videos yet")).toHaveCount(0);
    await expect(page.getByRole("button", { name: /Retry|Try again/i })).toBeVisible();
  });

  test("the list renders the caller's own videos", async ({ page }) => {
    const api = await signedInAgent(page);
    await stubSubmissions(page, [submission()]);

    await page.goto("/");
    await page.getByRole("button", { name: /Your Videos/i }).click();

    await expect(page.getByText("campaign-cut.mp4")).toBeVisible();
    await expect(page.getByRole("button", { name: /Review audit/i })).toBeVisible();
    expect(api.unmatched).toEqual([]);
  });

  test("filtering to no matches is distinguished from having no videos", async ({
    page,
  }) => {
    await signedInAgent(page);
    await stubSubmissions(page, [submission()]);

    await page.goto("/");
    await page.getByRole("button", { name: /Your Videos/i }).click();
    await expect(page.getByText("campaign-cut.mp4")).toBeVisible();

    await page.getByPlaceholder("Filter by name...").fill("no-such-video");

    await expect(page.getByText("No matches")).toBeVisible();
    await expect(page.getByText("No videos yet")).toHaveCount(0);
  });

  test("a completed audit with findings reports violations and lists them", async ({
    page,
  }) => {
    const api = await signedInAgent(page);
    await stubSubmissions(page, [submission()]);
    await stubJob(page, VIDEO_ID, {
      video_id: VIDEO_ID,
      state: "completed",
      stage: "done",
      frames_total: 10,
      frames_audited: 10,
      detections_recorded: 1,
    });
    await stubViolations(page, VIDEO_ID, [detection()]);
    await stubRemediationList(page, []);

    await page.goto("/");
    await page.getByRole("button", { name: /Your Videos/i }).click();
    await page.getByRole("button", { name: /Review audit/i }).click();

    await expect(page.getByTestId("verdict-title")).toHaveText("Violations found");
    // The finding itself is rendered as text, not only as an overlay box.
    await expect(
      page.getByText("A rival mark is visible on the cup.")
    ).toBeVisible();
    expect(api.unmatched).toEqual([]);
  });

  test("a completed audit with no findings is the only thing called clean", async ({
    page,
  }) => {
    await signedInAgent(page);
    await stubSubmissions(page, [submission()]);
    await stubJob(page, VIDEO_ID, {
      video_id: VIDEO_ID,
      state: "completed",
      stage: "done",
      frames_total: 10,
      frames_audited: 10,
      detections_recorded: 0,
    });
    await stubViolations(page, VIDEO_ID, []);
    await stubRemediationList(page, []);

    await page.goto("/");
    await page.getByRole("button", { name: /Your Videos/i }).click();
    await page.getByRole("button", { name: /Review audit/i }).click();

    await expect(page.getByTestId("verdict-title")).toHaveText("No violations found");
  });

  test("a running analysis is never described as clean", async ({ page }) => {
    // THE assertion this file exists for. An empty detections list means
    // nothing unless the job completed.
    await signedInAgent(page);
    await stubSubmissions(page, [submission({ status: "analyzing" })]);
    await stubJob(page, VIDEO_ID, {
      video_id: VIDEO_ID,
      state: "analyzing",
      stage: "analyzing_frames",
      frames_total: 10,
      frames_audited: 4,
      detections_recorded: 0,
    });
    await stubViolations(page, VIDEO_ID, []);
    await stubRemediationList(page, []);

    await page.goto("/");
    await page.getByRole("button", { name: /Your Videos/i }).click();
    await page.getByRole("button", { name: /Review audit/i }).click();

    await expect(page.getByTestId("verdict-title")).toHaveText("Analysis incomplete");
    await expect(page.getByTestId("verdict-title")).not.toHaveText(
      "No violations found"
    );
    const body = await page.locator("body").innerText();
    expect(body).not.toMatch(/audit passed|compliant|verified by/i);
  });

  test("a failed analysis is reported as failed, not as clean", async ({ page }) => {
    await signedInAgent(page);
    await stubSubmissions(page, [submission({ status: "failed" })]);
    await stubJob(page, VIDEO_ID, {
      video_id: VIDEO_ID,
      state: "failed",
      stage: "analyzing_frames",
      frames_total: 10,
      frames_audited: 2,
      detections_recorded: 0,
      error: "The video could not be read.",
    });
    await stubViolations(page, VIDEO_ID, []);
    await stubRemediationList(page, []);

    await page.goto("/");
    await page.getByRole("button", { name: /Your Videos/i }).click();
    await page.getByRole("button", { name: /Review audit/i }).click();

    await expect(page.getByTestId("verdict-title")).toHaveText("Analysis failed");
    await expect(page.getByTestId("verdict-title")).not.toHaveText(
      "No violations found"
    );
  });

  test("a partial findings fetch cannot read as clean", async ({ page }) => {
    // The server recorded 2 findings; this client paged in none of them (an
    // aborted or paginated fetch). `verdictFor` prefers the server's recorded
    // total precisely so this case reports violations rather than "clean" -
    // the worst possible wrong answer, since it would tell a brand its
    // footage passed when the server knows it did not.
    await signedInAgent(page);
    await stubSubmissions(page, [submission()]);
    await stubJob(page, VIDEO_ID, {
      video_id: VIDEO_ID,
      state: "completed",
      stage: "done",
      frames_total: 10,
      frames_audited: 10,
      detections_recorded: 2,
    });
    await stubViolations(page, VIDEO_ID, []);
    await stubRemediationList(page, []);

    await page.goto("/");
    await page.getByRole("button", { name: /Your Videos/i }).click();
    await page.getByRole("button", { name: /Review audit/i }).click();

    await expect(page.getByTestId("verdict-title")).toHaveText("Violations found");
  });

  test("an unrecognised job state does not render raw or read as clean", async ({
    page,
  }) => {
    // `jobStateOf` maps anything unknown to "uploaded" - the state that
    // claims the least - rather than printing it.
    await signedInAgent(page);
    await stubSubmissions(page, [submission({ status: "banana_state" })]);
    await stubJob(page, VIDEO_ID, {
      video_id: VIDEO_ID,
      state: "banana_state",
      stage: "banana",
      detections_recorded: 0,
    });
    await stubViolations(page, VIDEO_ID, []);
    await stubRemediationList(page, []);

    await page.goto("/");
    await page.getByRole("button", { name: /Your Videos/i }).click();
    await page.getByRole("button", { name: /Review audit/i }).click();

    const body = await page.locator("body").innerText();
    expect(body).not.toContain("banana_state");
    await expect(page.getByTestId("verdict-title")).not.toHaveText(
      "No violations found"
    );
  });

  test("a malformed violations payload does not crash the view", async ({
    page,
  }) => {
    // The frontend treats model output as untrusted. A detection missing its
    // confidence and box must be handled, not thrown on.
    await signedInAgent(page);
    await stubSubmissions(page, [submission()]);
    await stubJob(page, VIDEO_ID, {
      video_id: VIDEO_ID,
      state: "completed",
      stage: "done",
      frames_total: 1,
      frames_audited: 1,
      detections_recorded: 1,
    });
    await stubViolations(page, VIDEO_ID, [
      {
        frame_number: 1,
        detection_index: 0,
        description: "Something was detected.",
        // no confidence, no bounding_box, no risk_level
      },
    ]);
    await stubRemediationList(page, []);

    await page.goto("/");
    await page.getByRole("button", { name: /Your Videos/i }).click();
    await page.getByRole("button", { name: /Review audit/i }).click();

    await expect(page.getByText("Something was detected.")).toBeVisible();
    // A missing confidence stays missing; a rendered 0% would assert a
    // certainty the model never expressed.
    const body = await page.locator("body").innerText();
    expect(body).not.toMatch(/\b0\s?%/);
  });

  test("a client sees their videos without the agent dashboard", async ({
    page,
  }) => {
    const api = await installApiFallback(page);
    await seedSession(page, CLIENT);
    await stubAuthMe(page, CLIENT);
    await stubRemediationCapabilities(page);
    await stubSubmissions(page, [submission()]);

    await page.goto("/");

    // A client lands on their own videos; the agent dashboard is not offered.
    await expect(page.getByText("campaign-cut.mp4")).toBeVisible();
    await expect(page.getByRole("button", { name: /^Dashboard$/i })).toHaveCount(0);
    expect(api.unmatched).toEqual([]);
  });
});
