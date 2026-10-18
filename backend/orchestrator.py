import os
import asyncio
try:
    from strands.agent import Agent
    from strands.models.bedrock import BedrockModel
except ImportError:
    try:
        from strands_agents.agents import Agent
        from strands_agents.models.bedrock import BedrockModel
    except ImportError:
        # Mock classes for local development if strands-agents is not found
        class BedrockModel:
            def __init__(self, **kwargs): pass
        class Agent:
            def __init__(self, **kwargs): pass
            async def run(self, *args, **kwargs):
                class MockResponse: content = "Mock Response (strands_agents missing)"
                return MockResponse()
from dotenv import load_dotenv

load_dotenv()

class NovaFlowOrchestrator:
    def __init__(self):
        # Using Nova 2 Pro for high-level reasoning and decomposition
        self.model = BedrockModel(
            model_id="amazon.nova-pro-v1:0",
            region_name=os.getenv("AWS_REGION", "us-east-1")
        )
        
        self.agent = Agent(
            name="NovaFlowOrchestrator",
            system_prompt="""
            You are NovaFlow, a Universal Autonomous Agent.
            Your goal is to decompose complex user requests into discrete, actionable browser steps.
            For each step, specify the intent and the target element/action.
            """,
            model=self.model
        )

    async def decompose_goal(self, goal: str):
        print(f"Decomposing goal: {goal}")
        response = await self.agent.run(f"Break down this goal into browser-based steps: {goal}")
        return response.content if hasattr(response, 'content') else response

if __name__ == "__main__":
    orchestrator = NovaFlowOrchestrator()
    # Simple test run
    # asyncio.run(orchestrator.decompose_goal("Go to amazon.com and find a mechanical keyboard under $50"))
