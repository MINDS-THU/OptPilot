import json
import traceback
from typing import Optional, Literal
from pathlib import Path

from smolagents import Tool
# Import Tools
from .spec_generator import ModelSpecFormulator
from .spec_checker import SpecValidator

from .classify_single import ModelClassifier
from .classify_arbitrator import ModelArbitrator

from .coupled_plan_checker import CoupledPlanValidator
from .coupled_plan_refiner import CoupledPlanRefiner
from .coupled_plan_generator import CoupledSplitter, SubModelPlan, CoupledDecomposition

from ...base_types import StandardContextModel, StandardContext, PlanResult, coupled_plan_to_plan_result
from ...utils import get_content_strict

class PlanGeneratorChecked:
    def __init__(self, model_id: dict[str, str]):
        super().__init__()
        self.model_id = model_id
        
        # --- Tools ---
        self.classifier = ModelClassifier(model_id=model_id['strong'])
        self.formulator = ModelSpecFormulator(model_id=model_id['strong'])
        self.spec_validator = SpecValidator(model_id=model_id['strong'])
        self.arbitrator = ModelArbitrator(default_judge_id=model_id['strong'])
        
        # --- Splitting & Refining Tools ---
        self.plan_splitter = CoupledSplitter(model_id=model_id['strong'])
        self.plan_validator = CoupledPlanValidator(model_id=model_id['strong'])
        self.plan_refiner = CoupledPlanRefiner(model_id=model_id['strong']) 

    def forward(self, model_info: StandardContextModel, context: StandardContext, retry: int = 3) -> PlanResult:
        """
        Orchestrates: Spec Loop -> Voting -> (If Coupled) Split Loop -> Merge.
        """
        print(f"\n[PlanGenerator] === Processing '{model_info.file_path}' ===")

        print(f"  > model spec: {model_info.specification}")

        # STEP 1: Classification Voting (Atomic vs Coupled)
        final_type = self._classify_model(model_info, context)

        # STEP 2: Branching Logic
        if final_type == "atomic":
            print("[Result] Atomic Model Finalized.")
            return PlanResult(
                type="atomic",
                model_info=model_info,
                children_plan=[],
                coupling_specification=None,
            )

        # If Coupled, Decompose with Refinement Loop
        return self._plan_split(model_info, context, retry)

    def generate_spec(self, model_info: StandardContextModel, requirements: str, context: StandardContext, retry: int):
        """Generates and validates the model specification."""
        print(f"Formulating Specification...")
        print(f"  > Raw requirements detected, formulating ...")
        spec_feedback = ""
        success = False
        
        for attempt in range(retry):
            # A. Generate
            raw_spec = self.formulator.forward(
                model_name=model_info.class_name,
                requirements=requirements,
                feedback_context=spec_feedback,
                context=context,
            )
            
            # B. Validate
            val_res = self.spec_validator.forward(
                model_name=model_info.class_name,
                model_spec=raw_spec,
                requirements=requirements,
                context=context
            )
            
            if val_res.is_valid:
                model_info.specification = raw_spec
                success = True
                print(f"  > Spec Approved (Attempt {attempt+1})")
                break
            else:
                if val_res.feedback_summary:
                    spec_feedback = f"The previous spec with rejected.\nThe previous spec is: {raw_spec}\nFeedback: {val_res.feedback_summary}\nPlease generate a new one. "
                print(f"  > Spec Rejected: {val_res.feedback_summary}. Origin spec: {raw_spec}")
                
        if not success:
            raise Exception("Failed to generate a valid spec after all retries.")

    def _classify_model(self, model_info: StandardContextModel, context: StandardContext) -> Literal["atomic", "coupled"]:
        """Runs the voting and arbitration logic."""
        if len(context.ancestors) == 0:
            print(f"Classification Voting Skipped (Root Model must be Coupled)")
            return "coupled"
        
        print(f"[Step 1] Classification Voting with Arbitration...")
        llm_pool = ["gpt-4.1", "gpt-5.1"] 
        vote_results = [] 

        for i, llm_id in enumerate(llm_pool):
            try:
                res = self.classifier.forward(model_info, context, llm_id)
                vote_results.append({
                    "model": llm_id, "vote": res.model_type, 
                    "reasoning": res.reasoning, "submodels": res.submodels
                })
            except Exception as e:
                print(f"  > Vote No. {i} Error: {e}")
                vote_results.append({"model": llm_id, "vote": "atomic", "reasoning": "Error", "submodels": []})

        votes = [r['vote'] for r in vote_results]
        
        if all(votes[0] == v for v in votes):
            print(f"  > Consensus: {votes[0]}")
            return votes[0]
        
        # Disagreement
        print(f"  > Disagreement detected! Invoking Judge...")
        arb_res = self.arbitrator.forward(
            model_info=model_info, context=context, 
            votes_summary=json.dumps(vote_results)
        )
        print(f"  > Judge Verdict: {arb_res.final_verdict}")
        return arb_res.final_verdict

    def _plan_split(self, model_info: StandardContextModel, context: StandardContext, retry: int) -> PlanResult:
        """
        Decomposes a Coupled Model.
        Implements the "Generator -> Validator -> Refiner" loop.
        """
        print(f"[Step 2] Decomposing Coupled Model (Gen -> Check -> Refine)...")
        
        # Strategic Guidance (Global feedback that persists across refinements)
        cumulative_feedback = "" 
        
        # 1. Initial Creation (The "Genesis")
        current_decomposition = self.plan_splitter.forward(
            model_info=model_info, context=context, feedback_context=""
        )
        
        attempt = 0
        refine_count = 0
        MAX_REFINEMENTS = 3  # How many times to try fixing before giving up and regenerating
        
        while attempt < retry:
            # --- Convert to PlanResult for Validation ---
            curr_plan_result = coupled_plan_to_plan_result(current_decomposition, model_info)

            # 2. Validation
            print(f"  > Validating Plan (Iter {attempt+1})...")
            val_res = self.plan_validator.forward(model_plan=curr_plan_result, context=context)

            if val_res.is_valid:
                print(f"  > Plan Approved!")
                return curr_plan_result

            print(f"  > Issues Found: {len(val_res.issues)}")
            print(f"  > Feedback Summary: {val_res.feedback_summary}")
            for issue in val_res.issues:
                 if issue.severity == "CRITICAL":
                     print(f"    - [CRITICAL] {issue.description}")

            # 3. Decision: Refine or Regenerate?
            
            if refine_count < MAX_REFINEMENTS:
                print(f"  > Attempting Surgical Refinement ({refine_count+1}/{MAX_REFINEMENTS})...")
                try:
                    # REFINER STEP
                    current_decomposition = self.plan_refiner.forward(
                        old_plan=curr_plan_result,
                        context=context,
                        feedback_instruction=(val_res.feedback_summary or "")+"\n"+(val_res.finegrained or ""),
                        guiding_feedback=cumulative_feedback # Pass persistence strategy
                    )
                    refine_count += 1
                    attempt += 1 
                    continue
                    
                except Exception as e:
                    print(f"  > Refiner Crashed: {e}. Falling back to Regeneration.")
                    # If Refiner crashes, fall through to Regeneration logic
            
            # 4. Regeneration (The "Hard Reset")
            print(f"  > Triggering Full Regeneration (Refine limit reached or crashed)...")
            cumulative_feedback += f"\nPrevious attempt failed due to: {val_res.feedback_summary}"
            
            current_decomposition = self.plan_splitter.forward(
                model_info=model_info, 
                context=context, 
                feedback_context=cumulative_feedback
            )
            
            # Reset refine counter because we have a fresh new body
            refine_count = 0
            attempt += 1

        raise Exception(f"Failed to decompose model after {retry} iterations.")