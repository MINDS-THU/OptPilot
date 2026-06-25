"""
LLM Call Logger - Records timing, token usage, input/output for all LLM calls.
Saves organized log files per phase per model for analysis.
"""
import json
import os
import time
import threading
from pathlib import Path
from typing import Optional, Any, Dict
from datetime import datetime


class LLMCallLogger:
    """Thread-safe logger for LLM API calls."""
    
    _instance = None
    _lock = threading.Lock()
    
    def __init__(self, log_dir: Optional[str] = None):
        self.log_dir = Path(log_dir) if log_dir else Path("./llm_call_logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._call_counter = 0
        self._summary_path = self.log_dir / "llm_calls_summary.jsonl"
        # Clear previous summary
        with open(self._summary_path, "w") as f:
            pass
    
    @classmethod
    def get_instance(cls, log_dir: Optional[str] = None) -> 'LLMCallLogger':
        """Get or create the singleton instance."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(log_dir)
            return cls._instance
    
    @classmethod
    def reset_instance(cls, log_dir: Optional[str] = None) -> 'LLMCallLogger':
        """Reset the singleton instance."""
        with cls._lock:
            cls._instance = cls(log_dir)
            return cls._instance
    
    def _next_id(self) -> int:
        with self._lock:
            self._call_counter += 1
            return self._call_counter
    
    def log_call(
        self,
        phase: str,
        model_name: str,
        target: str,
        input_text: str,
        output_text: str,
        duration: float,
        token_usage: Optional[dict] = None,
        attempt: int = 0,
        status: str = "success",
        error: str = "",
        extra: Optional[dict] = None,
    ):
        """Log a single LLM call with all metadata."""
        call_id = self._next_id()
        timestamp = datetime.now().isoformat()
        
        # Calculate input size
        input_chars = len(input_text)
        output_chars = len(output_text)
        
        # Estimate tokens (rough: ~4 chars per token for English)
        input_tokens_est = input_chars // 4
        output_tokens_est = output_chars // 4
        
        # Build record
        record = {
            "call_id": call_id,
            "timestamp": timestamp,
            "phase": phase,
            "model": model_name,
            "target": target,
            "attempt": attempt,
            "status": status,
            "duration_sec": round(duration, 3),
            "input_chars": input_chars,
            "output_chars": output_chars,
            "input_tokens_est": input_tokens_est,
            "output_tokens_est": output_tokens_est,
            "token_usage": token_usage or {},
            "error": error,
        }
        if extra:
            record["extra"] = extra
        
        # Save individual call files
        phase_dir = self.log_dir / phase
        phase_dir.mkdir(parents=True, exist_ok=True)
        
        safe_target = "".join(c if c.isalnum() or c in "-_" else "_" for c in target)
        file_prefix = f"{call_id:04d}_{safe_target}"
        
        # Save input
        input_file = phase_dir / f"{file_prefix}_input.txt"
        with open(input_file, "w", encoding="utf-8") as f:
            f.write(f"=== LLM Call #{call_id} ===\n")
            f.write(f"Phase: {phase}\n")
            f.write(f"Model: {model_name}\n")
            f.write(f"Target: {target}\n")
            f.write(f"Attempt: {attempt}\n")
            f.write(f"Timestamp: {timestamp}\n")
            f.write(f"Duration: {duration:.3f}s\n")
            f.write(f"Input chars: {input_chars} (~{input_tokens_est} tokens)\n")
            f.write(f"=" * 80 + "\n\n")
            f.write(input_text)
        
        # Save output
        ext = "py" if "code" in target.lower() else ("json" if "json" in target.lower() else "txt")
        output_file = phase_dir / f"{file_prefix}_output.{ext}"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(f"=== LLM Call #{call_id} Output ===\n")
            f.write(f"Status: {status}\n")
            f.write(f"Output chars: {output_chars} (~{output_tokens_est} tokens)\n")
            if error:
                f.write(f"Error: {error}\n")
            f.write(f"=" * 80 + "\n\n")
            f.write(output_text)
        
        # Save metadata as JSON
        meta_file = phase_dir / f"{file_prefix}_meta.json"
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False, default=str)
        
        # Append to summary
        with self._lock:
            with open(self._summary_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        
        return call_id
    
    def get_summary(self) -> dict:
        """Get aggregated summary of all LLM calls."""
        calls = []
        with open(self._summary_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    calls.append(json.loads(line))
        
        if not calls:
            return {"total_calls": 0}
        
        total_duration = sum(c.get("duration_sec", 0) for c in calls)
        total_input_chars = sum(c.get("input_chars", 0) for c in calls)
        total_output_chars = sum(c.get("output_chars", 0) for c in calls)
        
        # Per-phase breakdown
        phases = {}
        for c in calls:
            phase = c.get("phase", "unknown")
            if phase not in phases:
                phases[phase] = {"count": 0, "duration": 0, "input_chars": 0, "output_chars": 0}
            phases[phase]["count"] += 1
            phases[phase]["duration"] += c.get("duration_sec", 0)
            phases[phase]["input_chars"] += c.get("input_chars", 0)
            phases[phase]["output_chars"] += c.get("output_chars", 0)
        
        # Per-model breakdown
        models = {}
        for c in calls:
            model = c.get("model", "unknown")
            if model not in models:
                models[model] = {"count": 0, "duration": 0, "input_chars": 0, "output_chars": 0}
            models[model]["count"] += 1
            models[model]["duration"] += c.get("duration_sec", 0)
            models[model]["input_chars"] += c.get("input_chars", 0)
            models[model]["output_chars"] += c.get("output_chars", 0)
        
        return {
            "total_calls": len(calls),
            "total_duration_sec": round(total_duration, 3),
            "total_input_chars": total_input_chars,
            "total_output_chars": total_output_chars,
            "total_input_tokens_est": total_input_chars // 4,
            "total_output_tokens_est": total_output_chars // 4,
            "phases": phases,
            "models": models,
            "calls": calls,
        }


# Global instance
_llm_logger: Optional[LLMCallLogger] = None


def get_llm_logger(log_dir: Optional[str] = None) -> LLMCallLogger:
    global _llm_logger
    if _llm_logger is None:
        _llm_logger = LLMCallLogger.get_instance(log_dir)
    return _llm_logger

def reset_llm_logger(log_dir: Optional[str] = None) -> LLMCallLogger:
    global _llm_logger
    _llm_logger = LLMCallLogger.reset_instance(log_dir)
    return _llm_logger


def log_llm_call(
    phase: str,
    model_name: str,
    target: str,
    input_text: str,
    output_text: str,
    duration: float,
    token_usage: Optional[dict] = None,
    attempt: int = 0,
    status: str = "success",
    error: str = "",
    extra: Optional[dict] = None,
):
    """Convenience function to log an LLM call."""
    logger = get_llm_logger()
    return logger.log_call(
        phase=phase,
        model_name=model_name,
        target=target,
        input_text=input_text,
        output_text=output_text,
        duration=duration,
        token_usage=token_usage,
        attempt=attempt,
        status=status,
        error=error,
        extra=extra,
    )
