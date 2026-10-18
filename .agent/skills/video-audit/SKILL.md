# Skill: handle-video-coordinates

Teaches the agent how to parse and transform $x, y$ coordinates and timestamps from Amazon Nova 2 Pro outputs.

## Context
Amazon Nova 2 Pro reports coordinates on a normalized $1000 \times 1000$ grid.
Internally, it may resize videos to $672 \times 672$ for processing.
The UI and remediation logic must map these normalized coordinates back to the original video resolution.

## Coordinate Transformation Logic
Given:
- `orig_w`, `orig_h`: Original video dimensions.
- `nova_x`, `nova_y`: Normalized coordinates from Nova (0-1000).

Calculation:
- `real_x = (nova_x / 1000) * orig_w`
- `real_y = (nova_y / 1000) * orig_h`

## JSON Structure Handling
```json
{
  "violations": [
    {
      "type": "logo",
      "timestamp_ms": 12500,
      "bounding_box": {
        "x": 450,
        "y": 200,
        "width": 100,
        "height": 50
      },
      "confidence": 0.98
    }
  ]
}
```

## Agent Instructions
1. Always parse `bounding_box` values as floats divided by 1000.
2. Use the `Coordinate Transformer` pattern to scale to actual resolution.
3. Timestamps are typically in milliseconds; align with video player `currentTime`.
