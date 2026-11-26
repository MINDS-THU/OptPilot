from typing import Any
import json
import re

def extract_json(content: str) -> dict:
    content = content.strip()
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    m = re.search(r'```(?:json)?\s*({.*?})\s*```', content, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    start = content.find('{')
    if start != -1:
        depth = 0
        for i in range(start, len(content)):
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(content[start:i + 1])
                        if isinstance(data, dict):
                            return data
                    except json.JSONDecodeError:
                        break
    raise ValueError(f"Could not extract JSON. Content: {content[:200]}")

def get_content_strict(response: Any) -> str:
    """
    Safely extract content from a completion response.
    Raises ValueError if the response format is invalid.
    """
    # 检查 choices
    if not hasattr(response, 'choices') or not response.choices:
        raise ValueError("Invalid API Response: 'choices' missing or empty")
    
    # 检查 message
    first_choice = response.choices[0]
    if not hasattr(first_choice, 'message'):
        raise ValueError("Invalid API Response: 'message' missing")
        
    # 检查 content
    content = first_choice.message.content
    if content is None:
        # Fallback: some reasoning models (e.g. glm-4.7) put the response in
        # reasoning_content when using complex nested Pydantic response_format
        rc = getattr(first_choice.message, 'reasoning_content', None)
        if rc:
            return rc
        raise ValueError("Invalid API Response: 'content' is None")
        
    return content