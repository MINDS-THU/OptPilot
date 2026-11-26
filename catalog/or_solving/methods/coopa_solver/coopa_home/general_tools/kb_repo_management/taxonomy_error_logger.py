#!/usr/bin/env python3
"""
Comprehensive logger for taxonomy validation errors
Captures both LLM errors and tool validation errors with dataset and problem context
"""

import json
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any


class TaxonomyErrorLogger:
    """Comprehensive logger for taxonomy validation errors"""
    
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        
    def log_llm_invalid_path(
        self, 
        suggested_path: str, 
        content: str, 
        content_type: str,
        dataset_name: str = "unknown",
        problem_index: Optional[int] = None,
        model_id: str = "unknown",
        error: Optional[str] = None,
    ):
        """Log invalid paths suggested by the LLM"""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "error_type": "llm_suggestion",
            "dataset_name": dataset_name,
            "problem_index": problem_index,
            "model_id": model_id,
            "suggested_path": suggested_path,
            "content_type": content_type,
            "content_preview": content[:500] + "..." if len(content) > 500 else content,
            "error_message": error, 
        }
        
        self._write_log_entry(log_entry)
        
    def log_tool_validation_error(
        self,
        dataset_name: str = "unknown",
        problem_index: Optional[int] = None,
        model_id: str = "unknown",
        **tool_args
    ):
        """Log validation errors from tools (WriteToKnowledgeBase, CopyToKnowledgeBase, etc.)"""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "error_type": "tool_validation",
            "dataset_name": dataset_name,
            "problem_index": problem_index,
            "model_id": model_id,            
            **tool_args
        }
        
        self._write_log_entry(log_entry)
        
    def _write_log_entry(self, log_entry: Dict[str, Any]):
        """Write log entry to file"""
        # Extract context information for filename
        dataset_name = log_entry.get("dataset_name", "unknown")
        problem_index = log_entry.get("problem_index", "unknown")
        model_id = log_entry.get("model_id", "unknown")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Create filename with context
        filename = f"taxonomy_errors_{dataset_name}_p{problem_index}_{model_id}_{timestamp}.log"
        # Clean filename (replace invalid characters)
        filename = "".join(c for c in filename if c.isalnum() or c in "._-")
        
        log_file = self.log_dir / filename
        
        # Write to log file
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, indent=2) + "\n")
            f.write("-" * 80 + "\n")
        
        print(f"Taxonomy validation error logged to: {log_file}")


# Global logger instance
_taxonomy_logger = None
_current_context = {}

def get_taxonomy_logger() -> TaxonomyErrorLogger:
    """Get the global taxonomy logger instance"""
    global _taxonomy_logger
    if _taxonomy_logger is None:
        _taxonomy_logger = TaxonomyErrorLogger()
    return _taxonomy_logger

def set_logging_context(dataset_name: str = "unknown", problem_index: Optional[int] = None, model_id: str = "unknown"):
    """Set the current logging context for taxonomy errors"""
    global _current_context
    _current_context = {
        "dataset_name": dataset_name,
        "problem_index": problem_index,
        "model_id": model_id
    }

def get_logging_context() -> Dict[str, Any]:
    """Get the current logging context"""
    global _current_context
    return _current_context.copy()


def log_taxonomy_error(
    error_type: str,
    dataset_name: str = "unknown",
    problem_index: Optional[int] = None,
    model_id: str = "unknown",
    **kwargs
):
    """Convenience function to log taxonomy errors"""
    logger = get_taxonomy_logger()
    
    # Get current context if not explicitly provided
    context = get_logging_context()
    if dataset_name == "unknown" and context.get("dataset_name"):
        dataset_name = context["dataset_name"]
    if problem_index is None and context.get("problem_index"):
        problem_index = context["problem_index"]
    if model_id == "unknown" and context.get("model_id"):
        model_id = context["model_id"]
    
    if error_type == "llm_suggestion":
        logger.log_llm_invalid_path(
            suggested_path=kwargs.get("suggested_path", ""),
            content=kwargs.get("content", ""),
            content_type=kwargs.get("content_type", "unknown"),
            dataset_name=dataset_name,
            problem_index=problem_index,
            model_id=model_id,
            error=kwargs.get("error")
        )
    elif error_type == "tool_validation":
        logger.log_tool_validation_error(
            dataset_name=dataset_name,
            problem_index=problem_index,
            model_id=model_id,
            **kwargs
        ) 