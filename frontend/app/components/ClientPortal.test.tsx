import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import ClientPortal from "./ClientPortal";

/**
 * Phase 7: the asset grid asserted things /submissions never said.
 *
 * It compared `sub.status` to the string "VERIFIED" to show a shield badge.
 * The endpoint returns the lower-case job state, so the badge could never
 * appear - and had it appeared it would have claimed a verdict from a field
 * that only describes whether analysis ran. The filter tabs named a model
 * ("LLAVA-CORE") and an agent ("ACT-AGENT") that do not exist in this system
 * and filtered nothing, and a "Post" button claimed social publishing that
 * has no endpoint.
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
    status: "completed",
    created_at: "2026-10-19T10:00:00+00:00",
    size_bytes: 5 * 1024 * 1024,
    url: "http://backend.test/uploads/vid_aaaaaaa1.mp4?access_token=tok1",
  },
  {
    id: "vid_aaaaaaa2",
    title: "winter-teaser.mov",
    status: "uploaded",
    created_at: "2026-10-19T11:00:00+00:00",
    url: "http://backend.test/uploads/vid_aaaaaaa2.mov?access_token=tok2",
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

describe("no fabricated verdicts or capabilities", () => {
  it("does not render the removed tabs, badge or publish action", async () => {
    stubSubmissions(() => jsonResponse({ submissions: SUBMISSIONS }));
    const { container } = render(<ClientPortal onSelect={() => {}} />);

    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());

    const text = container.textContent ?? "";
    expect(text).not.toMatch(/llava/i);
    expect(text).not.toMatch(/act-agent/i);
    expect(text).not.toMatch(/all nodes/i);
    expect(text).not.toMatch(/verified neural output/i);
    expect(text).not.toMatch(/verified/i);
    expect(screen.queryByRole("button", { name: /^post$/i })).toBeNull();
  });

  it("shows the real job state for an upload the server called completed", async () => {
    stubSubmissions(() => jsonResponse({ submissions: SUBMISSIONS }));
    render(<ClientPortal onSelect={() => {}} />);

    await waitFor(() => expect(screen.getByText("Analyzed")).toBeTruthy());
    expect(screen.getByText("Not analyzed")).toBeTruthy();
  });

  it("does not describe an uploaded-but-unanalysed video as audited", async () => {
    stubSubmissions(() =>
      jsonResponse({ submissions: [{ id: "vid_bbbbbbb1", status: "uploaded" }] })
    );
    render(<ClientPortal onSelect={() => {}} />);

    await waitFor(() => expect(screen.getByText("vid_bbbbbbb1")).toBeTruthy());
    // The "Analyzed" badge must be absent; only "Not analyzed" is true here.
    expect(screen.queryByText("Analyzed")).toBeNull();
    expect(screen.getByText("Not analyzed")).toBeTruthy();
  });
});

describe("real data, loading, empty and error states", () => {
  it("announces loading before the list arrives", () => {
    stubSubmissions(() => new Promise<Response>(() => {}));
    render(<ClientPortal onSelect={() => {}} />);
    expect(screen.getByText(/loading your videos/i)).toBeTruthy();
  });

  it("shows an honest empty state rather than nothing at all", async () => {
    stubSubmissions(() => jsonResponse({ submissions: [] }));
    render(<ClientPortal onSelect={() => {}} />);

    await waitFor(() => expect(screen.getByText(/no videos yet/i)).toBeTruthy());
  });

  it("reports an expired session and offers a retry", async () => {
    stubSubmissions(() => jsonResponse({ detail: "Could not validate credentials" }, 401));
    render(<ClientPortal onSelect={() => {}} />);

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/session has expired/i)
    );
    expect(screen.getByRole("button", { name: /try again/i })).toBeTruthy();
  });

  it("retries the real endpoint on demand", async () => {
    let attempt = 0;
    const stub = stubSubmissions(() => {
      attempt += 1;
      if (attempt === 1) throw new TypeError("Failed to fetch");
      return jsonResponse({ submissions: SUBMISSIONS });
    });
    render(<ClientPortal onSelect={() => {}} />);

    await waitFor(() => expect(screen.getByRole("button", { name: /try again/i })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));

    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());
    expect(stub.calls).toHaveLength(2);
  });
});

describe("search filters locally without extra requests", () => {
  it("filters the already-fetched list", async () => {
    const stub = stubSubmissions(() => jsonResponse({ submissions: SUBMISSIONS }));
    render(<ClientPortal onSelect={() => {}} />);
    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());

    fireEvent.change(screen.getByLabelText(/filter your videos by name/i), {
      target: { value: "winter" },
    });

    expect(screen.getByText("winter-teaser.mov")).toBeTruthy();
    expect(screen.queryByText("spring-campaign.mp4")).toBeNull();
    // No request-per-keystroke against a route that ignores the query.
    expect(stub.calls).toHaveLength(1);
  });

  it("reports no matches rather than an empty grid", async () => {
    stubSubmissions(() => jsonResponse({ submissions: SUBMISSIONS }));
    render(<ClientPortal onSelect={() => {}} />);
    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());

    fireEvent.change(screen.getByLabelText(/filter your videos by name/i), {
      target: { value: "zzzz" },
    });

    expect(screen.getByText(/no matches/i)).toBeTruthy();
  });

  it("has an accessible label on the search field", async () => {
    stubSubmissions(() => jsonResponse({ submissions: SUBMISSIONS }));
    render(<ClientPortal onSelect={() => {}} />);
    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());
    expect(screen.getByLabelText(/filter your videos by name/i)).toBeTruthy();
  });
});

describe("selection and media", () => {
  it("passes the real submission record up when a card is reviewed", async () => {
    const onSelect = vi.fn();
    stubSubmissions(() => jsonResponse({ submissions: [SUBMISSIONS[0]] }));
    render(<ClientPortal onSelect={onSelect} />);

    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /review audit/i }));

    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ id: "vid_aaaaaaa1" })
    );
  });

  it("does not eagerly download every video in the grid", async () => {
    stubSubmissions(() => jsonResponse({ submissions: SUBMISSIONS }));
    const { container } = render(<ClientPortal onSelect={() => {}} />);
    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());

    for (const video of Array.from(container.querySelectorAll("video"))) {
      expect(video.getAttribute("preload")).toBe("metadata");
    }
  });

  it("labels each preview for assistive technology", async () => {
    stubSubmissions(() => jsonResponse({ submissions: [SUBMISSIONS[0]] }));
    render(<ClientPortal onSelect={() => {}} />);
    await waitFor(() => expect(screen.getByText("spring-campaign.mp4")).toBeTruthy());

    const item = within(screen.getByRole("listitem"));
    expect(item.getByLabelText(/preview of spring-campaign\.mp4/i)).toBeTruthy();
  });
});
