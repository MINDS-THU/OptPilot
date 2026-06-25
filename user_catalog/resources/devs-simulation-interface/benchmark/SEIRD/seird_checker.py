import argparse
import json
import math
from typing import List, Dict, Any

# 假设 checker_utils 已经包含了这些基类
from checker_utils import BaseValidator, RuleType, ScoringMethod

# ==========================================
# 原封不动保留的常量
# ==========================================
EXACT_TOLERANCE = 0.01          # For exact value matching
POPULATION_TOLERANCE = 0.05     # 5% for population conservation
MORTALITY_TOLERANCE = 0.02      # 2% for mortality ratio

class SEIRDValidator(BaseValidator):
    """
    SEIRD Model Validator - Adapted to Standard Framework
    Logic is strictly preserved from the original implementation.
    Includes extended scenario checks from L0-L2 definitions.
    """

    def define_rules(self):
        # 1. LOG_FORMAT_CORRECTNESS - Output format validation
        self.register_rule(
            'output_format', 
            'Output Format Correctness', 
            RuleType.LOG_FORMAT_CORRECTNESS,
            description='Validate JSON format and required fields',
            scoring_method=ScoringMethod.BINARY,
            weight=1.0
        )
        
        # 2. LOGIC_CORRECTNESS - State values validation
        self.register_rule(
            'state_values',
            'State Values Correctness',
            RuleType.SYSTEM_LEVEL,
            description='Validate S, E, I, R, D state values match expected',
            scoring_method=ScoringMethod.RATIO,
            weight=3.0
        )
        
        # 3. BEHAVIOR_CONSISTENCY - Derived metrics validation
        self.register_rule(
            'derived_metrics',
            'Derived Metrics Correctness',
            RuleType.SYSTEM_LEVEL,
            description='Validate population conservation and mortality ratio',
            scoring_method=ScoringMethod.RATIO,
            weight=1.0
        )

    def validate_logic(self):
        """
        Standard Framework Entry Point for per-file validation.
        Executes the exact logic flow from the original script.
        """

        # --- Step 1: Load and Format ---
        self._check_format_and_load()

        # --- Step 2, 3 & 4: Validate Logic ---
        if not self.stats.get('format_valid', False):
            return
            
        self._validate_state_values()
        self._validate_derived_metrics()
        self._validate_scenario_checks()

    def validate_kpis(self, batch_stats: List[Dict]):
        pass
    
    def validate_log_entry_hook(self, entry: Dict, line_num: int) -> bool:
        return True

    def _check_format_and_load(self) -> bool:
        """
        Reimplementation of original 'load_logs' method logic.
        Validates that self.logs contains at least one valid record with all required numeric fields.
        Searches from the last record backwards.
        """
        fmt_rule = self.rules['output_format']
        required_fields = ['time', 'susceptible', 'exposed', 'infective', 'recovered', 'deceased']
        
        try:
            # 1. Ensure logs is a list of dicts
            target_logs = []
            if isinstance(self.logs, list):
                target_logs = self.logs
            elif isinstance(self.logs, dict):
                target_logs = [self.logs]
            else:
                fmt_rule.add_error(f"Logs must be a list or dict, got {type(self.logs).__name__}")
                return False

            if not target_logs:
                fmt_rule.add_error("Logs list is empty")
                return False

            # 2. Iterate backwards to find the last valid record
            # We store the reason for failure of the *last* checked item to report meaningful errors if none are found.
            last_failure_reason = "No records checked"
            
            for record in reversed(target_logs):
                # Basic Type Check
                if not isinstance(record, dict):
                    last_failure_reason = f"Log item is not a dictionary, got {type(record).__name__}"
                    continue

                # Missing Fields Check
                missing_fields = [f for f in required_fields if f not in record]
                if missing_fields:
                    last_failure_reason = f"Missing required fields: {missing_fields}"
                    continue
                
                # Numeric Value Check
                is_numeric_valid = True
                for field in required_fields:
                    value = record[field]
                    if not isinstance(value, (int, float)):
                        last_failure_reason = f"Field '{field}' should be numeric, got {type(value).__name__}"
                        is_numeric_valid = False
                        break
                
                if not is_numeric_valid:
                    continue

                # === FOUND VALID RECORD ===
                self.output_data = record
                
                # Success
                fmt_rule.add_case(True)
                self.stats['format_valid'] = True
                return True

            # 3. If loop finishes, no valid record was found
            fmt_rule.add_error(f"No valid record found in logs. Last error encountered: {last_failure_reason}")
            self.stats['format_valid'] = False
            return False
            
        except json.JSONDecodeError as e:
            fmt_rule.add_error(f"JSON parse error: {str(e)}")
            self.stats['format_valid'] = False
            return False
            
        except Exception as e:
            fmt_rule.add_error(f"Error loading output: {str(e)}")
            self.stats['format_valid'] = False
            return False

    def _validate_state_values(self):
        """Validate S, E, I, R, D state values (Exact Match logic)"""
        rule = self.rules['state_values']
        expected = {
            'susceptible': self.checker_config['expected_s'],
            'exposed': self.checker_config['expected_e'],
            'infective': self.checker_config['expected_i'],
            'recovered': self.checker_config['expected_r'],
            'deceased': self.checker_config['expected_d'],
        }
        state_fields = ['susceptible', 'exposed', 'infective', 'recovered', 'deceased']
        
        for field in state_fields:
            if field not in expected or expected[field] is None:
                rule.add_warning(f"No expected value for '{field}' in config")
                continue
            
            actual = self.output_data.get(field, 0.0)
            expected_val = expected[field]
            
            is_correct = abs(actual - expected_val) / max(expected_val, 1.0) <= EXACT_TOLERANCE
            rule.add_case(is_correct, case_id=field)
            
            if not is_correct:
                rule.add_error(
                    f"{field}: expected {expected_val:.2f}, got {actual:.2f}, diff={abs(actual - expected_val):.4f}",
                    case_id=field
                )
            
            self.stats[f'actual_{field}'] = actual
            self.stats[f'expected_{field}'] = expected_val

    def _validate_derived_metrics(self):
        """Validate derived metrics (Population Sum & Mortality Ratio)"""
        rule = self.rules['derived_metrics']
        
        S = self.output_data.get('susceptible', 0.0)
        E = self.output_data.get('exposed', 0.0)
        I = self.output_data.get('infective', 0.0)
        R = self.output_data.get('recovered', 0.0)
        D = self.output_data.get('deceased', 0.0)
        
        checks_performed = 0
        
        # 1. Population conservation check
        total_population = self.checker_config.get('total_population', 0)
        if total_population > 0:
            actual_sum = S + E + I + R + D
            relative_error = abs(actual_sum - total_population) / total_population
            
            is_conserved = relative_error <= POPULATION_TOLERANCE
            rule.add_case(is_conserved, case_id='population_conservation')
            checks_performed += 1
            
            if not is_conserved:
                rule.add_error(
                    f"Population not conserved: Sum={actual_sum:.2f}, expected={total_population}, error={relative_error*100:.2f}%",
                    case_id='population_conservation'
                )
            self.stats['population_sum'] = actual_sum
        
        # 2. Mortality ratio check
        mortality = self.checker_config.get('mortality', 0)
        if R + D > 0 and mortality is not None:
            expected_ratio = mortality / 100.0
            actual_ratio = D / (R + D)
            
            is_correct = abs(actual_ratio - expected_ratio) <= MORTALITY_TOLERANCE
            rule.add_case(is_correct, case_id='mortality_ratio')
            checks_performed += 1
            
            if not is_correct:
                rule.add_error(
                    f"Mortality ratio incorrect: D/(R+D)={actual_ratio:.4f}, expected={expected_ratio:.4f}",
                    case_id='mortality_ratio'
                )
            self.stats['actual_mortality_ratio'] = actual_ratio

        if checks_performed == 0:
            rule.add_case(True, case_id='no_checks_needed')
            self.stats['derived_metrics_skipped'] = True

    def _validate_scenario_checks(self):
        """
        Validate specific scenario logic based on input parameters.
        Corresponds to 'no_new_infections', 'ird_conservation', 'near_zero', 'epidemic_spread'.
        """
        rule = self.rules['derived_metrics']
        
        transmission_rate = self.checker_config.get('transmission_rate', None)
        initial_infective = self.checker_config.get('initial_infective', 0)
        total_population = self.checker_config.get('total_population', 0)
        
        S = self.output_data.get('susceptible', 0.0)
        E = self.output_data.get('exposed', 0.0)
        I = self.output_data.get('infective', 0.0)
        R = self.output_data.get('recovered', 0.0)
        D = self.output_data.get('deceased', 0.0)

        checks_run = 0

        # Check 1: No New Infections (no_new_infections)
        # Condition: When transmission_rate is 0, Exposed should remain 0
        if transmission_rate is not None and transmission_rate == 0:
            is_zero_exposure = (E <= EXACT_TOLERANCE)
            rule.add_case(is_zero_exposure, case_id='no_new_infections')
            if not is_zero_exposure:
                rule.add_error(f"Logic Error: Transmission is 0 but Exposed={E}", case_id='no_new_infections')
            checks_run += 1

        # Check 2: IRD Conservation (ird_conservation)
        # Condition: When transmission_rate is 0, I+R+D should approx equal initial_infective
        if transmission_rate is not None and transmission_rate == 0 and initial_infective > 0:
            current_ird = I + R + D
            # Tolerance: 1.0 absolute + 1% relative
            is_ird_conserved = abs(current_ird - initial_infective) <= (initial_infective * 0.01 + 1.0) 
            rule.add_case(is_ird_conserved, case_id='ird_conservation')
            if not is_ird_conserved:
                rule.add_error(f"IRD Conservation Failed: I+R+D={current_ird:.2f}, Initial I={initial_infective}", case_id='ird_conservation')
            checks_run += 1

        # Check 3: Epidemic Spread (epidemic_spread)
        # Condition: If transmission is non-zero, S should decrease
        if transmission_rate is not None and transmission_rate > 0 and total_population > 0:
            initial_s = total_population - initial_infective
            
            is_spreading = S < initial_s
            
            # 如果 S 几乎没变 (考虑到浮点误差，稍微给一点点空间，比如必须减少 0.01 以上才算 spread)
            # 但设计要求 simply "decrease"，所以直接比大小即可
            rule.add_case(is_spreading, case_id='epidemic_spread_trend')
            
            if not is_spreading:
                rule.add_error(
                    f"Epidemic Spread Failed: S ({S:.2f}) did not decrease from Initial ({initial_s:.2f})", 
                    case_id='epidemic_spread_trend'
                )
            checks_run += 1
            
        # Check 4: Near Zero / Exhaustion (near_zero)
        # Condition: High transmission usually exhausts S to near 0
        expected_s = self.checker_config.get('susceptible')
        if expected_s is not None and expected_s < 1.0 and total_population > 100:
            is_exhausted = S <= 1.0 # Tolerance threshold
            rule.add_case(is_exhausted, case_id='near_zero_susceptible')
            if not is_exhausted:
                rule.add_error(f"Susceptible not exhausted: {S} > 1.0", case_id='near_zero_susceptible')
            checks_run += 1

        if checks_run == 0:
            rule.add_case(True, case_id='no_scenario_checks_needed')

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SEIRD Model Validator")
    parser.add_argument("log_files", nargs='+', help="Path to model output JSON files")
    # Sim Args
    parser.add_argument("--test_name", type=str, default="Unknown")

    args = parser.parse_args()

    global_config = vars(args)

    validator = SEIRDValidator(args.log_files, global_config)
    result = validator.run()
    
    print(json.dumps(result, indent=2, ensure_ascii=False))