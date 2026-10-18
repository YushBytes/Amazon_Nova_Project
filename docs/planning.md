# Brand Guardian: Planner-Critic Architecture

## Overview
The Brand Guardian uses a Planner-Critic architecture to ensure high-fidelity video remediation.

### 1. The Planner (Amazon Nova 2 Pro)
- **Input**: Raw video file + Brand Safety Policy.
- **Role**: Analyze the video for violations.
- **Output**: JSON containing:
    - `violation_type`: (e.g., Logo, Safety, Tobacco)
    - `timestamp`: Start and end frames.
    - `coordinates`: $[x, y, w, h]$ bounding boxes (grid $1000 \times 1000$).
    - `remediation_strategy`: Instructions for Nova Canvas.

### 2. The Critic (Amazon Nova 2 Pro)
- **Input**: Remedied video + Original violation report.
- **Role**: Verify if the violation was successfully removed without introducing artifacts.
- **Output**: Pass/Fail + Feedback logic.

### 3. The Remediator (Amazon Nova Canvas)
- **Input**: Frames + Bounding boxes.
- **Role**: Multi-modal inpainting to "wash out" or replace violating content.

## State Machine Logic (AWS Step Functions)
1. **Source**: S3 Video Upload.
2. **Audit**: Invoke Nova 2 Pro (Planner).
3. **Loop**: For each violation:
    - Extract Frames.
    - Inpaint (Nova Canvas).
    - Validate (Nova 2 Pro - Critic).
    - **Human-in-the-Loop**: If Critic fails or confidence < 0.8.
4. **Merge**: Reconstruct video and store in S3 Output.
