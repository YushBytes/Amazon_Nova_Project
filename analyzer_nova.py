import boto3
import json
import base64
from prompts import SYSTEM_PROMPT


def analyze_frame(image_path):

    client = boto3.client("bedrock-runtime", region_name="us-east-1")

    # Convert image to base64
    with open(image_path, "rb") as img:
        encoded = base64.b64encode(img.read()).decode("utf-8")

    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"text": SYSTEM_PROMPT},
                    {
                        "image": {
                            "format": "jpeg",
                            "source": {"bytes": encoded}
                        }
                    }
                ]
            }
        ]
    }

    response = client.invoke_model(
        modelId="amazon.nova-pro-v1:0",
        body=json.dumps(payload),
        contentType="application/json",
        accept="application/json"
    )

    result = json.loads(response["body"].read())

    return result


if __name__ == "__main__":

    image = "test_frame.jpg"

    print("\nAnalyzing frame using Nova Pro...\n")

    result = analyze_frame(image)

    # Extract readable AI text
    analysis_text = result["output"]["message"]["content"][0]["text"]

    print("FRAME ANALYSIS REPORT")
    print("=" * 50)
    print(analysis_text)
    print("=" * 50)