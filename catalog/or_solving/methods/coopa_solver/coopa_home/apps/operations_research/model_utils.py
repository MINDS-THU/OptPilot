"""
Model utilities for Operations Research experiments.

This module provides utilities for building different types of models
based on the model name, supporting both LiteLLMModel and InferenceClientModel.
"""

from smolagents import LiteLLMModel, InferenceClientModel


def build_model(model_name):
    """
    Build a model instance based on the model name.
    
    Args:
        model_name (str): The model identifier/name
        
    Returns:
        Model instance (LiteLLMModel or InferenceClientModel)
        
    Raises:
        ValueError: If the model name is not supported
        
    Examples:
        >>> model = build_model("gpt-4.1")  # Returns LiteLLMModel
        >>> model = build_model("Qwen/Qwen3-32B")  # Returns InferenceClientModel with nebius provider
        >>> model = build_model("google/gemma-3-27b-it")  # Returns InferenceClientModel with nebius provider
    """
    if any(x in model_name for x in ["gemini"]):
        return LiteLLMModel(model_id=model_name, extra_body={"reasoning": {"effort": "high"}})
    elif any(x in model_name for x in ["o3", "o4", "gpt-5"]):
        return LiteLLMModel(model_id=model_name, reasoning_effort="high")
    elif any(x in model_name for x in ["gpt", "thinking"]):
        return LiteLLMModel(model_id=model_name)
    else:
        raise ValueError(f"Unsupported model name: {model_name}")


def get_supported_models():
    """
    Get a list of supported model patterns.
    
    Returns:
        dict: Dictionary with model categories and their patterns
    """
    return {
        "litellm_models": ["gpt", "o3", "claude", "gemini"],
        "nebius_models": ["Qwen", "gemma", "Mistral"]
    }


def is_supported_model(model_name):
    """
    Check if a model name is supported.
    
    Args:
        model_name (str): The model identifier/name
        
    Returns:
        bool: True if supported, False otherwise
    """
    supported = get_supported_models()
    all_patterns = supported["litellm_models"] + supported["nebius_models"]
    return any(pattern in model_name for pattern in all_patterns) 