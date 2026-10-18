import os
import boto3
from dotenv import load_dotenv

load_dotenv()

class NovaFlowSafety:
    def __init__(self):
        self.bedrock_runtime = boto3.client(
            service_name='bedrock-runtime',
            region_name=os.getenv("AWS_REGION", "us-east-1")
        )
        self.guardrail_id = os.getenv("BEDROCK_GUARDRAIL_ID")
        self.guardrail_version = os.getenv("BEDROCK_GUARDRAIL_VERSION", "1")

    def intercept_action(self, intent: str, action_type: str):
        """
        Pauses the agent for high-risk actions.
        """
        critical_actions = ["Submit", "Pay", "Delete", "Buy"]
        if any(act in action_type for act in critical_actions):
            return True # Needs HITL
        return False

    def validate_with_automated_reasoning(self, intent: str):
        """
        Uses Bedrock Guardrails' Automated Reasoning to check against policies.
        Note: Implementation depends on specific Bedrock SDK versions (2026 feature set).
        """
        try:
            response = self.bedrock_runtime.apply_guardrail(
                guardrailIdentifier=self.guardrail_id,
                guardrailVersion=self.guardrail_version,
                source='INPUT',
                content=[{'text': f"Action Intent: {intent}"}]
            )
            
            if response['action'] == 'GUARDRAIL_INTERVENED':
                print(f"Safety Alert: Action blocked by Automated Reasoning. Reason: {response['outputs'][0]['text']}")
                return False, response['outputs'][0]['text']
            
            return True, "Safe"
        except Exception as e:
            print(f"Safety Verification Error: {e}")
            return False, str(e)

if __name__ == "__main__":
    pass
