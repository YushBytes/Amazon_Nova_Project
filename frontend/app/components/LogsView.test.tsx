import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import LogsView from "./LogsView";

/**
 * Phase 7: this screen was entirely fabricated.
 *
 * It listed three hardcoded "incidents" with stock photographs, a "LIVE AI"
 * badge and a "Live Anomaly Detection" subtitle. They were physical-security
 * events, not brand-compliance findings, and no endpoint produced them.
 *
 * It now lists the caller's own uploads from GET /submissions. These tests pin
 * that the invented content cannot come back, that the real states are shown
 * using the backend's own vocabulary, and that an empty list reads as empty
 * rather than being padded with samples.
 */

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const SUBMISSIONS = [
  {
    id: "vid_aaaaaaa1",
    title: "spring-campaign.mp4",
    original_filename: "spring-campaign.mp4",
    filename: "vid_aaaaaaa1.mp4",
    status: "completed",
    created_at: "2026-10-19T10:00:00+00:00",
    size_bytes: 5 * 1024 * 1024,
    url: "http://backend.test/uploads/vid_aaaaaaa1.mp4?access_token=tok1",
  },
  {
    id: "vid_aaaaaaa2",
    title: "teaser.mov",
    status: "failed",
    created_at: "2026-10-19T11:00:00+00:00",
    size_bytes: 1024,
    url: "http://backend.test/uploads/vid_aaaaaaa2.mov?access_token=tok2",
  },
  {
    id: "vid_aaaaaaa3",
    title: "draft.mp4",
    status: "analyzing",
    created_at: "2026-10-19T12:00:00+00:00",
    url: "http://backend.test/uploads/vid_aaaaaaa3.mp4?access_token=tok3",
  },
  {
    id: "vid_aaaaaaa4",
    title: "untouched.mp4",
    // Legacy upper-case value, as pre-Phase-4 records carry.
    status: "UPLOADED",
    created_at: null,
    url: "http://backend.test/uploads/vid_aaaaaaa4.mp4?access_token=tok4",
  },
];

function stubSubmissions(factory: () => Response | Promise<Response>) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (url: string) => {
    calls.push(url);
    if (url.includes("/submissions")) return factory();
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

beforeEach(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://backend.test";
});

describe("no fabricated compliance log", () => {
  it("does not render the invented incidents it used to", async () => {
    stubSubmissions(() => jsonResponse({ submissions: SUBMISSIONS }));
    render(<LogsView />);

    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());

    expect(screen.queryByText(/unauthorized access attempt/i)).toBeNull();
    expect(screen.queryByText(/tailgating detected/i)).toBeNull();
    expect(screen.queryByText(/after-hours activity/i)).toBeNull();
    expect(screen.queryByText(/live ai/i)).toBeNull();
    expect(screen.queryByText(/live anomaly detection/i)).toBeNull();
    expect(screen.queryByText(/server room a/i)).toBeNull();
  });

  it("renders no stock photography", async () => {
    stubSubmissions(() => jsonResponse({ submissions: SUBMISSIONS }));
    const { container } = render(<LogsView />);
    await waitFor(() => expect(screen.getByText("teaser.mov")).toBeTruthy());
    expect(container.querySelectorAll("img")).toHaveLength(0);
  });

  it("says plainly that it is not a security event feed", async () => {
    stubSubmissions(() => jsonResponse({ submissions: [] }));
    render(<LogsView />);
    await waitFor(() =>
      expect(screen.getByText(/not a security event feed/i)).toBeTruthy()
    );
  });

  it("reads only the submissions endpoint, not one request per row", async () => {
    const stub = stubSubmissions(() => jsonResponse({ submissions: SUBMISSIONS }));
    render(<LogsView />);
    await waitFor(() => expect(screen.getByText("draft.mp4")).toBeTruthy());

    expect(stub.calls.filter((u) => u.includes("/submissions"))).toHaveLength(1);
    expect(stub.calls.some((u) => u.includes("/jobs/"))).toBe(false);
  });
});

describe("real audit history", () => {
  it("lists the caller's uploads with the backend's own state labels", async () => {
    stubSubmissions(() => jsonResponse({ submissions: SUBMISSIONS }));
    render(<LogsView />);

    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());

    // Scoped to the list, because the filter buttons carry the same words.
    const rows = within(screen.getByRole("list"));
    expect(rows.getByText("Analyzed")).toBeTruthy();
    expect(rows.getByText("Failed")).toBeTruthy();
    expect(rows.getByText("Analyzing")).toBeTruthy();
    expect(rows.getByText("Not analyzed")).toBeTruthy();
    // The long-form stage labels come from backend/job_state.py.
    expect(rows.getByText("Analysis complete")).toBeTruthy();
    expect(rows.getByText("Uploaded; analysis not started")).toBeTruthy();
  });

  it("never labels a row verified or compliant", async () => {
    stubSubmissions(() => jsonResponse({ submissions: SUBMISSIONS }));
    const { container } = render(<LogsView />);
    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());

    // "Analyzed" describes whether analysis ran. A verdict needs the recorded
    // violation count, which /submissions does not carry.
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/verified/i);
    expect(text).not.toMatch(/compliant/i);
    expect(text).not.toMatch(/audit passed/i);
  });

  it("filters by real job state", async () => {
    stubSubmissions(() => jsonResponse({ submissions: SUBMISSIONS }));
    render(<LogsView />);
    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /^failed$/i }));

    expect(screen.getByText("teaser.mov")).toBeTruthy();
    expect(screen.queryByText("spring-campaign.mp4")).toBeNull();
    expect(screen.queryByText("draft.mp4")).toBeNull();
  });

  it("reports an empty filter result rather than showing nothing", async () => {
    stubSubmissions(() =>
      jsonResponse({ submissions: [SUBMISSIONS[0]] })
    );
    render(<LogsView />);
    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /^failed$/i }));
    expect(screen.getByText(/nothing matches this filter/i)).toBeTruthy();
  });

  it("marks the active filter for assistive technology", async () => {
    stubSubmissions(() => jsonResponse({ submissions: SUBMISSIONS }));
    render(<LogsView />);
    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());

    expect(screen.getByRole("button", { name: /all audits/i }).getAttribute("aria-pressed")).toBe(
      "true"
    );
    fireEvent.click(screen.getByRole("button", { name: /^analyzed$/i }));
    expect(screen.getByRole("button", { name: /^analyzed$/i }).getAttribute("aria-pressed")).toBe(
      "true"
    );
  });

  it("opens one audit when a row is selected", async () => {
    const onSelect = vi.fn();
    stubSubmissions(() => jsonResponse({ submissions: [SUBMISSIONS[0]] }));
    render(<LogsView onSelect={onSelect} />);

    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /open audit/i }));

    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ id: "vid_aaaaaaa1" })
    );
  });
});

describe("loading, empty and error states", () => {
  it("announces loading before data arrives", () => {
    stubSubmissions(() => new Promise<Response>(() => {}));
    render(<LogsView />);
    expect(screen.getByText(/loading your audit history/i)).toBeTruthy();
  });

  it("shows an honest empty state when the user has no uploads", async () => {
    stubSubmissions(() => jsonResponse({ submissions: [] }));
    render(<LogsView />);

    await waitFor(() => expect(screen.getByText(/no audits yet/i)).toBeTruthy());
    // Not padded with examples.
    expect(screen.queryByText(/critical/i)).toBeNull();
  });

  it("reports an expired session as an instruction to sign in", async () => {
    stubSubmissions(() =>
      jsonResponse({ detail: "Could not validate credentials" }, 401)
    );
    render(<LogsView />);

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/session has expired/i)
    );
  });

  it("offers a retry when the backend is unreachable", async () => {
    let attempt = 0;
    const stub = stubSubmissions(() => {
      attempt += 1;
      if (attempt === 1) throw new TypeError("Failed to fetch");
      return jsonResponse({ submissions: SUBMISSIONS });
    });
    render(<LogsView />);

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/could not reach the server/i)
    );
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));

    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());
    expect(stub.calls.length).toBe(2);
  });

  it("tolerates a submission with no title, filename or timestamp", async () => {
    stubSubmissions(() => jsonResponse({ submissions: [{ id: "vid_bbbbbbb1" }] }));
    render(<LogsView />);

    await waitFor(() => expect(screen.getByText("vid_bbbbbbb1")).toBeTruthy());
    // A missing status reads as the state that claims the least.
    expect(within(screen.getByRole("list")).getByText("Not analyzed")).toBeTruthy();
  });

  it("makes no further request after unmount", async () => {
    const stub = stubSubmissions(() => jsonResponse({ submissions: SUBMISSIONS }));
    const { unmount } = render(<LogsView />);
    await waitFor(() => expect(stub.calls.length).toBe(1));
    unmount();
    await new Promise((r) => setTimeout(r, 40));
    expect(stub.calls.length).toBe(1);
  });
});
