# Skill: handle-video-coordinates

How to read the bounding boxes and timestamps that NovaFlow's frame analysis
produces, and how to map them onto a video frame.

> **Corrected in Phase 10.3.** The previous version of this file documented a
> schema the code has never used: `bounding_box` as an object of
> `{x, y, width, height}`, a `timestamp_ms` field on each violation, and a
> `type` field. The real contract is an **array**, `[ymin, xmin, ymax, xmax]`,
> there is no `timestamp_ms` anywhere, and the field is `category`. Anything
> written against the old description would have mis-placed every overlay —
> `x` and `y` were in the opposite order to the array's first two values.
>
> Verified against `backend/prompts.py`, `backend/detections.py` and
> `frontend/app/components/HUDView.tsx`.

## The model and its coordinate space

The analyzer calls Amazon Bedrock with model id **`amazon.nova-pro-v1:0`**
(`backend/analyzer.py:71`).

⚠️ Project documentation elsewhere has referred to "Nova 2 Pro". The code has
only ever used `amazon.nova-pro-v1:0`. Which is correct is an **open question**
(CLAUDE.md §9 question 6) and is not resolved here — quote the id from the code
rather than a prose name.

Coordinates are **normalized to a 0–1000 grid**, independent of the source
video's real pixel dimensions (`backend/prompts.py`: "Coordinates must be
normalized (0-1000)").

## The real JSON contract

What the prompt asks the model to return, per frame:

```json
{
  "scene_description": "...",
  "violations": [
    {
      "category": "competitor_logo",
      "description": "A rival mark is visible on the cup.",
      "confidence": 0.91,
      "bounding_box": [100, 200, 300, 400],
      "risk_level": "High"
    }
  ],
  "recommendation": "..."
}
```

`bounding_box` is a **four-element array in y-first order**:

| Index | Meaning | Range |
| :-- | :-- | :-- |
| `[0]` | `ymin` — top edge | 0–1000 |
| `[1]` | `xmin` — left edge | 0–1000 |
| `[2]` | `ymax` — bottom edge | 0–1000 |
| `[3]` | `xmax` — right edge | 0–1000 |

It holds **edges, not an origin plus a size.** Width and height are
differences: `xmax - xmin` and `ymax - ymin`. Reading `[1]` as a width is the
single easiest way to get this wrong.

`detections.BoundingBox.parse` requires `ymin < ymax` and `xmin < xmax`, and
rejects the box otherwise rather than reordering it.

### There is no `timestamp_ms`

The model is not asked for a timestamp and does not return one. A finding's
time comes from the **frame** it was found on: `extractor.py` writes a
`timestamp` field, in **seconds**, when it stores each frame's row. Frames are
sampled at roughly 1 fps, so a timestamp identifies a frame, not a precise
moment within it.

## Mapping a box onto a frame

**To pixels**, given `orig_w` and `orig_h`:

```
left   = (box[1] / 1000) * orig_w        # xmin
top    = (box[0] / 1000) * orig_h        # ymin
width  = ((box[3] - box[1]) / 1000) * orig_w
height = ((box[2] - box[0]) / 1000) * orig_h
```

`reconstruction.resolve_region` does exactly this, then rounds each value to an
even number because the output is encoded as yuv420p, where odd dimensions
make the scaler fail or shift the region silently.

**To CSS percentages**, which is what the overlay actually uses
(`HUDView.tsx:446-449` — dividing by 10 converts 0–1000 straight to 0–100%):

```
top:    box[0] / 10 + "%"
left:   box[1] / 10 + "%"
height: (box[2] - box[0]) / 10 + "%"
width:  (box[3] - box[1]) / 10 + "%"
```

## Accepted input variants

`detections.Detection.parse` is deliberately tolerant on the way **in**,
because model output is untrusted:

- `bounding_box` or the legacy key `box_2d`;
- an array `[ymin, xmin, ymax, xmax]`, or an object with explicit
  `ymin`/`xmin`/`ymax`/`xmax` keys.

The **array form is the canonical output contract** — it is what
`backend/prompts.py` requests, what `GET /violations/{id}` returns, and what
the frontend consumes. Do not introduce the `{x, y, width, height}` shape
anywhere.

⚠️ Stored boxes come back from DynamoDB as `Decimal`, which
`BoundingBox.parse` rejects (it accepts only int/float, because its input is
model JSON). `remediation._normalised_box` converts first. Reading a stored box
without that conversion silently drops it.

## Agent instructions

1. Treat `bounding_box` as `[ymin, xmin, ymax, xmax]`, normalized 0–1000.
   Divide by 1000 for a fraction, or by 10 for a CSS percentage.
2. Compute width and height as differences between edges. Never read an
   element as a width or a height.
3. For a timestamp, read the frame's `timestamp` field in **seconds**. Do not
   look for `timestamp_ms`; it does not exist.
4. A missing or malformed box stays missing. `parse` returns `None` and the
   finding is rendered without an overlay — never substitute a default region
   or a whole-frame box, because a region nobody approved could then be
   edited.
5. A missing `confidence` stays missing too. The UI prints "Not reported"; a
   rendered `0%` would assert a certainty the model never expressed.
