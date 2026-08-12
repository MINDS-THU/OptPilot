import json
from pathlib import Path
from smolagents.tools import Tool
from datetime import datetime
import re
from general_tools.kb_repo_management.taxonomy_error_logger import log_taxonomy_error

class IISETaxonomyManager:
    def __init__(self, taxonomy_file_path: str):
        self.taxonomy_file = Path(taxonomy_file_path)
        self.taxonomy = self._load_taxonomy()
        self.valid_paths = self._extract_all_paths()
        
    def _load_taxonomy(self):
        with open(self.taxonomy_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def _extract_all_paths(self):
        paths = []
        def traverse(node, current_path=""):
            for key, value in node.items():
                new_path = f"{current_path}/{key}" if current_path else key
                if value is None:  # Leaf node
                    paths.append(new_path)
                elif isinstance(value, dict):  # Branch node
                    traverse(value, new_path)
        traverse(self.taxonomy["or_knowledge_base"])
        return paths
    
    def get_valid_categories(self):
        return self.valid_paths.copy()
    
    def validate_category_path(self, path: str):
        return path in self.valid_paths
    
    def get_partial_taxonomy_for_path(self, invalid_path: str):
        """Extract the partial taxonomy tree that corresponds to the invalid path"""
        path_parts = invalid_path.strip('/').split('/')
        current_node = self.taxonomy.get("or_knowledge_base", {})
        partial_tree = {"or_knowledge_base": {}}
        current_partial = partial_tree["or_knowledge_base"]
        
        # Navigate through the taxonomy tree following the path as far as possible
        valid_depth = 0
        for i, part in enumerate(path_parts):
            if isinstance(current_node, dict) and part in current_node:
                # This part exists in the taxonomy
                current_partial[part] = current_node[part]
                current_node = current_node[part]
                current_partial = current_partial[part] if isinstance(current_partial[part], dict) else {}
                valid_depth = i + 1
            else:
                # This part doesn't exist - stop here but include the parent level options
                break
        
        # If we couldn't navigate at all, return the top level
        if valid_depth == 0:
            return self.taxonomy
        
        # If we navigated some way but not completely, show what's available at the deepest valid level
        if valid_depth < len(path_parts):
            # Navigate back to the last valid node to show available options
            current_node = self.taxonomy.get("or_knowledge_base", {})
            for i in range(valid_depth):
                current_node = current_node[path_parts[i]]
            
            # Create a tree showing the valid path and available options at the invalid level
            result_tree = {"or_knowledge_base": {}}
            temp_node = result_tree["or_knowledge_base"]
            
            # Build the valid path
            for i in range(valid_depth):
                temp_node[path_parts[i]] = {}
                temp_node = temp_node[path_parts[i]]
            
            # Add all available options at the current level
            if isinstance(current_node, dict):
                temp_node.update(current_node)
            
            return result_tree
        
        return partial_tree

class CreateTaxonomyFolder(Tool):
    name = "create_taxonomy_folder"
    description = (
        "Create an appropriate IISE taxonomy folder for saving new OR knowledge. "
        "This analyzes the content and creates a folder path like 'linear_programming/applications/current_problem_name/' "
        "where the first part follows IISE taxonomy and the last part is specific to the current problem."
    )
    inputs = {
        "content": {"type": "string", "description": "The actual content to be classified"},
        "problem_title": {"type": "string", "description": "Title or name of the current problem being solved"},
        "content_type": {"type": "string", "description": "Type: 'code', 'documentation', 'example', 'theory'"}
    }
    output_type = "string"

    def __init__(self, taxonomy_manager, repo_indexer, model):
        super().__init__()
        self.taxonomy_manager = taxonomy_manager
        self.repo_indexer = repo_indexer
        self.model = model

    def forward(self, content: str, problem_title: str, content_type: str):
        # Step 1: Classify into IISE taxonomy leaf category
        iise_category = self._classify_to_iise_leaf_category(content, content_type)
        
        # Step 2: Create problem-specific subfolder name
        problem_folder = self._create_problem_folder_name(problem_title)
        
        # Step 3: Create full path
        full_path = f"{iise_category}/{problem_folder}"
        
        # Step 4: Create the directory structure (only if it doesn't exist)
        kb_root = Path(self.repo_indexer.root)
        target_dir = kb_root / full_path
        
        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
            return f"Folder '{full_path}/' has been created to save the new knowledge."
        else:
            return f"Using existing folder '{full_path}/' to save the new knowledge."
    
    def _classify_to_iise_leaf_category(self, content: str, content_type: str):
        """Use LLM with complete IISE taxonomy to classify content into leaf category"""
        # Get the complete taxonomy structure from JSON
        taxonomy_json = json.dumps(self.taxonomy_manager.taxonomy, indent=2)
        
        classification_prompt = f"""
        Based on the complete IISE Operations Research taxonomy structure provided below, classify the following content into the most appropriate LEAF category.

        Complete IISE Taxonomy Structure:
        {taxonomy_json}

        Content to classify:
        Type: {content_type}
        Content: {content[:1000]}{"..." if len(content) > 1000 else ""}

        Instructions:
        1. Analyze the content and determine what area of Operations Research it belongs to
        2. Navigate through the taxonomy structure to find the most specific leaf category (categories with null values)
        3. Return the full path to the leaf category (e.g., "linear_programming/applications/diet_problem")
        4. Choose the MOST SPECIFIC applicable leaf category

        Respond with ONLY the category path, nothing else.
        """
        
        # LiteLLMModel expects a list of messages, not a plain string
        messages = [{"role": "user", "content": classification_prompt}]
        try:
            response = self.model.generate(messages).content
        except:
            print("Generation Error.")
        
        # Extract real answer for Qwen models that use <think>...</think> format
        if self._is_qwen_model():
            suggested_path = self._extract_qwen_answer(response)
        else:
            suggested_path = response.strip()

        if self.taxonomy_manager.validate_category_path(suggested_path):
            return suggested_path
        else:
            # Log the invalid path and return error message
            self._log_invalid_path(suggested_path, content, content_type)
            partial_taxonomy = self.taxonomy_manager.get_partial_taxonomy_for_path(suggested_path)
            taxonomy_json = json.dumps(partial_taxonomy, indent=2)
            raise ValueError(f"LLM suggested invalid path: '{suggested_path}'. This path is not in the IISE taxonomy. The relevant part of the IISE taxonomy should be like this: \n {taxonomy_json}")
    
    def _is_qwen_model(self):   
        """Check if the current model is a Qwen model"""
        if hasattr(self.model, 'model_id'):
            return 'Qwen' in str(self.model.model_id)
        return False
    
    def _extract_qwen_answer(self, response: str):
        """Extract the real answer from Qwen response format <think>...</think>\n\n{answer}"""
        # Remove <think>...</think> tags and extract the answer after them
        import re
        # Pattern to match <think>...</think> at the beginning and extract everything after
        pattern = r'<think>.*?</think>\s*\n*\s*(.*)'
        match = re.search(pattern, response, re.DOTALL)
        if match:
            return match.group(1).strip()
        # If no <think> tags found, return the response as-is
        return response.strip()

    def _fallback_classification(self, content: str, content_type: str):
        """Improved keyword-based fallback classification with scoring"""
        content_lower = content.lower()
        
        # Score-based approach: count keyword matches for each category
        category_scores = {}
        
        # Linear Programming keywords
        lp_keywords = ["linear programming", "lp", "simplex", "linear optimization", "pulp", "linprog"]
        lp_app_keywords = {
            "diet_problem": ["diet", "nutrition", "food", "meal"],
            "work_scheduling": ["scheduling", "workforce", "shift", "roster"],
            "capital_budgeting": ["capital", "budget", "investment", "finance"],
            "blending_problems": ["blend", "mix", "recipe", "mixture"]
        }
        
        # Check LP first
        lp_score = sum(1 for kw in lp_keywords if kw in content_lower)
        if lp_score > 0:
            # Check for specific applications
            best_app = None
            best_app_score = 0
            for app, keywords in lp_app_keywords.items():
                app_score = sum(1 for kw in keywords if kw in content_lower)
                if app_score > best_app_score:
                    best_app_score = app_score
                    best_app = app
            
            if best_app and best_app_score > 0:
                category_scores[f"linear_programming/applications/{best_app}"] = lp_score + best_app_score
            else:
                category_scores["linear_programming/modeling_techniques"] = lp_score
        
        # Integer Programming keywords
        ip_keywords = ["integer programming", "integer", "binary", "mip", "mixed integer", "ip", "branch and bound", "cutting plane"]
        ip_score = sum(1 for kw in ip_keywords if kw in content_lower)
        if ip_score > 0:
            if "branch" in content_lower and "bound" in content_lower:
                category_scores["integer_programming/branch_and_bound_algorithm"] = ip_score + 1
            elif "cutting" in content_lower:
                category_scores["integer_programming/cutting_plane_algorithm"] = ip_score + 1
            elif "tsp" in content_lower or "traveling" in content_lower:
                category_scores["integer_programming/traveling_salesman_problem_and_solution_methods"] = ip_score + 1
            else:
                category_scores["integer_programming/applications_and_modeling_techniques/capital_budgeting"] = ip_score
        
        # Transportation keywords
        transport_keywords = ["transportation", "transport", "shipping", "distribution", "supply chain"]
        transport_score = sum(1 for kw in transport_keywords if kw in content_lower)
        if transport_score > 0:
            if "simplex" in content_lower:
                category_scores["transportation_problem/transportation_simplex_method"] = transport_score + 1
            else:
                category_scores["transportation_problem/transportation_model_and_variants"] = transport_score
        
        # Assignment keywords
        assignment_keywords = ["assignment", "matching", "hungarian", "bipartite"]
        assignment_score = sum(1 for kw in assignment_keywords if kw in content_lower)
        if assignment_score > 0:
            if "hungarian" in content_lower:
                category_scores["linear_assignment_problem/hungarian_algorithm"] = assignment_score + 1
            else:
                category_scores["linear_assignment_problem/assignment_model"] = assignment_score
        
        # Network keywords
        network_keywords = ["network", "graph", "node", "edge", "shortest path", "flow", "spanning tree"]
        network_score = sum(1 for kw in network_keywords if kw in content_lower)
        if network_score > 0:
            if "shortest" in content_lower or "path" in content_lower:
                category_scores["network_flows_and_optimization/shortest_path_problem"] = network_score + 1
            elif "flow" in content_lower:
                category_scores["network_flows_and_optimization/maximum_flow_problem"] = network_score + 1
            else:
                category_scores["network_flows_and_optimization/shortest_path_problem"] = network_score
        
        # Nonlinear Programming keywords
        nlp_keywords = ["nonlinear", "nlp", "quadratic", "gradient", "newton", "quasi-newton"]
        nlp_score = sum(1 for kw in nlp_keywords if kw in content_lower)
        if nlp_score > 0:
            if "gradient" in content_lower:
                category_scores["nonlinear_programming/unconstrained_algorithms/gradient_methods"] = nlp_score + 1
            elif "quadratic" in content_lower:
                category_scores["nonlinear_programming/constrained_algorithms/quadratic_programming"] = nlp_score + 1
            else:
                category_scores["nonlinear_programming/unconstrained_algorithms/direct_search_methods"] = nlp_score
        
        # Metaheuristics keywords
        meta_keywords = {
            "genetic_algorithms": ["genetic", "ga", "evolution", "chromosome", "mutation"],
            "simulated_annealing": ["simulated annealing", "annealing", "temperature"],
            "tabu_search": ["tabu", "tabu search"],
            "ant_colony_optimization": ["ant colony", "aco", "pheromone"],
            "particle_swarm_techniques": ["particle swarm", "pso", "swarm"],
            "steepest_ascent_and_descent_greedy_algorithms": ["greedy", "heuristic", "local search"]
        }
        
        for meta_type, keywords in meta_keywords.items():
            meta_score = sum(1 for kw in keywords if kw in content_lower)
            if meta_score > 0:
                category_scores[f"metaheuristics/{meta_type}"] = meta_score
        
        # Dynamic Programming keywords
        dp_keywords = ["dynamic programming", "dp", "bellman", "recursive"]
        dp_score = sum(1 for kw in dp_keywords if kw in content_lower)
        if dp_score > 0:
            if "knapsack" in content_lower:
                category_scores["deterministic_dynamic_programming/applications/knapsack_fly_away_cargo_loading_problems"] = dp_score + 1
            elif "inventory" in content_lower:
                category_scores["deterministic_dynamic_programming/applications/inventory"] = dp_score + 1
            else:
                category_scores["deterministic_dynamic_programming/forward_and_backward_recursions"] = dp_score
        
        # Simulation keywords
        sim_keywords = ["simulation", "monte carlo", "random", "stochastic"]
        sim_score = sum(1 for kw in sim_keywords if kw in content_lower)
        if sim_score > 0:
            if "monte carlo" in content_lower:
                category_scores["simulation/monte_carlo_simulation"] = sim_score + 1
            else:
                category_scores["simulation/continuous_and_discrete_time_models"] = sim_score
        
        # Queueing keywords
        queue_keywords = ["queue", "queueing", "waiting", "service", "arrival"]
        queue_score = sum(1 for kw in queue_keywords if kw in content_lower)
        if queue_score > 0:
            category_scores["queueing_systems/components_of_a_queueing_model"] = queue_score
        
        # Decision Analysis keywords
        decision_keywords = ["decision", "multi-criteria", "ahp", "topsis", "game theory"]
        decision_score = sum(1 for kw in decision_keywords if kw in content_lower)
        if decision_score > 0:
            if "multi" in content_lower:
                category_scores["decision_analysis_and_game_theory/multi_criteria_decision_making"] = decision_score + 1
            else:
                category_scores["decision_analysis_and_game_theory/decision_making_under_certainty"] = decision_score
        
        # Return the category with highest score
        if category_scores:
            best_category = max(category_scores.items(), key=lambda x: x[1])[0]
            return best_category
        
        # Ultimate fallback if no keywords match
        return "linear_programming/modeling_techniques"
    
    def _create_problem_folder_name(self, problem_title: str):
        """Create a clean folder name from the problem title"""
        # Remove special characters and convert to lowercase
        clean_title = re.sub(r'[^\w\s-]', '', problem_title).strip()
        clean_title = re.sub(r'[-\s]+', '_', clean_title).lower()
        
        # Truncate if too long
        if len(clean_title) > 50:
            clean_title = clean_title[:50]
        
        # Add timestamp if the folder name is too generic
        generic_terms = ['problem', 'optimization', 'solution', 'example', 'case']
        if any(term in clean_title for term in generic_terms) or len(clean_title) < 5:
            timestamp = datetime.now().strftime("%Y%m%d")
            clean_title = f"{clean_title}_{timestamp}"
        
        return clean_title or "general_problem"
    
    def _log_invalid_path(self, suggested_path: str, content: str, content_type: str, error: str = None):
        """Log invalid paths suggested by the LLM using the new taxonomy logger"""
        # Log the error with context (dataset and problem info will be set by the calling context)
        log_taxonomy_error(
            error_type="llm_suggestion",
            suggested_path=suggested_path,
            content=content,
            content_type=content_type,
            error=error
        )
