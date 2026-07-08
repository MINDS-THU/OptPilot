"""
Wrapped completion function that logs all LLM calls with timing and token usage.
"""
import time
import litellm
from litellm import completion as original_completion
from typing import Optional
from .llm_call_logger import log_llm_call, get_llm_logger


def completion_with_logging(
    model: str,
    messages: list,
    phase: str = "unknown",
    target: str = "unknown",
    attempt: int = 0,
    **kwargs
):
    # Build input text from messages
    input_text = "\n\n".join(
        f"[{m.get('role', 'user')}]\n{m.get('content', '')}" 
        for m in messages
    )
    
    start_time = time.time()
    try:
        response = original_completion(
            model=model,
            messages=messages,
            **kwargs
        )
        duration = time.time() - start_time
        
        # Extract output
        output_text = ""
        token_usage = None
        try:
            if hasattr(response, 'choices') and response.choices:
                output_text = response.choices[0].message.content or ""
            if hasattr(response, 'usage') and response.usage:
                token_usage = {
                    "prompt_tokens": getattr(response.usage, 'prompt_tokens', 0),
                    "completion_tokens": getattr(response.usage, 'completion_tokens', 0),
                    "total_tokens": getattr(response.usage, 'total_tokens', 0),
                }
        except Exception:
            pass
        
        # Log the call
        try:
            log_llm_call(
                phase=phase,
                model_name=model,
                target=target,
                input_text=input_text,
                output_text=output_text,
                duration=duration,
                token_usage=token_usage,
                attempt=attempt,
                status="success",
            )
        except Exception as log_err:
            print(f"[LLM Logger] Failed to log call: {log_err}")
            
        return response
        
    except Exception as e:
        duration = time.time() - start_time
        log_llm_call(
            phase=phase,
            model_name=model,
            target=target,
            input_text=input_text,
            output_text="",
            duration=duration,
            attempt=attempt,
            status="error",
            error=str(e),
        )
        raise