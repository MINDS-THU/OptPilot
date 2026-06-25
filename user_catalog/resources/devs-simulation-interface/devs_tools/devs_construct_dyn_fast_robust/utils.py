from typing import Any

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
        raise ValueError("Invalid API Response: 'content' is None")
        
    return content