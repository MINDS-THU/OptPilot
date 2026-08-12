"""Shared utilities for formulation text wrapping."""


def wrap_formulation_text(formulation_text: str) -> str:
    """
    Wrap raw formulation text (## PARAMETERS through ## CONSTRAINTS)
    with the delegation preamble and CRITICAL INSTRUCTIONS.

    This produces the same prompt as format_formulation_prompt() in
    run_exp_with_kb_full_multiprocess.py but from raw text instead
    of an OptimizationFormulation object.

    Args:
        formulation_text: Raw formulation text extracted from logs

    Returns:
        Full prompt string ready for the manager agent
    """
    parts = []

    parts.append("Delegate the following operations research problem to the correct optimizer agent:\n")
    parts.append(formulation_text.strip())

    parts.append("\n\n## CRITICAL INSTRUCTIONS:")
    parts.append("- You are the MANAGER. You MUST NOT solve this problem yourself. Do NOT write solver code, do NOT perform calculations, and do NOT reason about the solution.")
    parts.append("- Your ONLY job is to delegate the COMPLETE problem above to the appropriate optimizer agent (mathematical_optimizer_agent, combinatorial_optimizer_agent, metaheuristic_optimizer_agent, or general_optimizer_agent) in your FIRST Code block.")
    parts.append("- The optimizer agent will handle everything: saving parameters to JSON via create_file_with_content(), building the solver, executing it, and returning the result.")
    parts.append("- Do NOT call final_answer() in the same response where you call an optimizer agent. You MUST wait for the system to return the optimizer's REAL result first, then call final_answer() in a SEPARATE response.")
    parts.append("- Your code block MUST start with EXACTLY ```py (three backticks followed by py). Do NOT omit the backticks. If you write just 'py' without backticks, the code will NOT execute and the delegation will FAIL.")
    parts.append("- AFTER writing ```<end_code>, STOP IMMEDIATELY. Do NOT output any more text. Do NOT write 'Successfully executed', do NOT guess results, do NOT write the next Thought/Code block. Any text after ```<end_code> means you are hallucinating and your answer will be WRONG.")

    return "\n".join(parts)
