# NovaFlow Command Center

NovaFlow is a next-generation video compliance and automation platform designed for high-fidelity digital asset management. It combines multi-modal AI analysis (LLaVA) with robust browser automation (Nova Act) to provide a unified interface for content verification and orchestration.

## Features

- **Unified Command Center**: A Futuristic HUD for real-time video ingestion and goal definition.
- **Role-Based Access Control**: 
  - **Agents**: Full access to diagnostics, neural telemetry, and compliance logs.
  - **Clients**: Streamlined "Digital Assets" view for reviewing and sharing verified output.
- **Neural Telemetry**: Real-time bounding box visualization for detected compliance violations.
- **Security Intelligence**: Comprehensive logging of AI-detected anomalies.

## Getting Started

### Local Access
Once the services are running, access the dashboard at:
[http://localhost:3000](http://localhost:3000)

### Testing Credentials
| Role | Username | Password |
| :--- | :--- | :--- |
| **Agent** | `agent_test` | `password123` |
| **Client** | `client_test` | `password123` |

## Architecture

- **Frontend**: Next.js 14, TailwindCSS, Framer Motion, Lucide.
- **Backend**: FastAPI, MongoDB (GridFS), Python, Ollama (LLaVA), AWS Bedrock.
