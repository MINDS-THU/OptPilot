from smolagents import Tool
import os
from pathlib import Path
from default_tools.kb_repo_management.repo_indexer import RepoIndexer

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL")

DEVS_construct_direct_template = """## **[Task]**
Construct a DEVS model called {name} using package xDEVS.py. The model should meet the requirements in [Requirements]. The code should be executable directly without any parameters and free of syntax errors. You can refer to the [Example] for basic usage.

Note: 
- For 'Atomic' models, you should at least implement the `deltint`, `deltxt`, `lambdaf`, `initialize`, `exit`. And you can implement the other methods as needed. 
- For 'Coupled' models, no components are required, you can implement the methods as needed.
- You are only allowed to import any necessary modules from packages 'numpy', 'xdevs', 'logging', 'math', 'random', 'time'. You are not allowed to import any other modules.

## **[Requirements]**
{requirements}

## **[Class Definitions]**
{definitions}

## **[Output Format]**
You should output the code of the DEVS model using markdown format. The code should be enclosed within triple backticks (```) and specify the language as 'python' right after the opening backticks.

## **[Example]**
{example}
"""
# Use absolute paths based on the file location
TOOL_DIR = Path(__file__).parent
DEVS_construct_example_file = str(TOOL_DIR / "materials/coupled_example.py")
DEVS_construct_definitions_file = str(TOOL_DIR / "materials/definitions.md")

DEVS_summarize_file_template = """## **[Task]**
Summarize the DEVS model presented in the xdevs based [code]. Briefly describe the model's structure, behavior, and function.

And then you should extract the docstring of all the models and list them in the provided json format: 
- class_name: The name of the class of the model. 
- function: A brief description of the function of the model.
- inputs: A list of tuple (input port name, type, description).
- outputs: A list of tuple (output port name, type, description).

## **[Code]**
```python
{code}
```

## **[Output Format]**
Output the summary using JSON format:
```json
{{
    "summary": "...", 
    "docstrings": [
        {{
            "class_name": "...",
            "function": "...",
            "inputs": [
                {{"input_port_name": "...", "type": "...", "description": "..." }},
                ...
            ],
            "outputs": [
                {{"output_port_name": "...", "type": "...", "description": "..." }},
                ...
            ]
        }}
    ]
}}
```
"""

class DEVSConstructSimple(Tool):
    name = "devs_construct_simple"
    description = "Construct a DEVS model. Auto write the generated code to file `target_path` (overwrite if exists). Then it will summarize the model to tell you what it really does. "
    inputs = {
        "model_name": {"type": "string", "description": "Name of the DEVS model."},
        "model_description": {"type": "string", "description": "Description of the DEVS model, including detailed function and behavior."},
        "target_path": {"type": "string", "description": "Path to save the generated DEVS model."},
    }
    output_type = "string"

    def __init__(self, repo_indexer: RepoIndexer, model_id: str = "gpt-4.1", working_directory: str = "./working_dir"):
        super().__init__()
        self.repo_indexer = repo_indexer
        self.model_id = model_id
        self.working_directory = working_directory
        self.root = Path(repo_indexer.root)

    def _get_generate_prompt(self, model_name: str, model_description: str) -> str:
        with open(DEVS_construct_example_file, "r") as f:
            example_code = f.read()
        with open(DEVS_construct_definitions_file, "r") as f:
            definitions = f.read()
        example = f"```python\n{example_code}\n```"
        return DEVS_construct_direct_template.format(
            name=model_name,
            requirements=model_description,
            definitions=definitions,
            example=example
        )

    def _get_summarize_prompt(self, code: str) -> str:
        return DEVS_summarize_file_template.format(
            code=code
        )

    def _safe_kb_path(self, path: str) -> Path:
        """Ensure path is within the knowledge base root directory"""
        # Ensure root is absolute for proper comparison
        abs_root = self.root.resolve()

        # Handle both absolute and relative paths
        if Path(path).is_absolute():
            abs_path = Path(path).resolve()
        else:
            abs_path = (self.root / path).resolve()

        # Convert both to strings with consistent separators for comparison
        abs_root_str = str(abs_root).replace('\\', '/')
        abs_path_str = str(abs_path).replace('\\', '/')

        if not abs_path_str.startswith(abs_root_str):
            raise PermissionError("Access outside the knowledge base root is not allowed.")
        return abs_path

    def forward(self, model_name: str, model_description: str, target_path: str) -> str:
        if not isinstance(model_description, str):
            try:
                model_description = str(model_description)
            except:
                return "model_description must be a string."
            
        if not isinstance(target_path, str):
            try:
                target_path = str(target_path)
            except:
                return "target_path must be a string."
            
        if not target_path.endswith(".py"):
            return "target_path must end with '.py'."
        
        # Construct the DEVS model
        generate_prompt = self._get_generate_prompt(model_name, model_description)

        # Use LiteLLM to call the model, following HAMLET conventions
        from litellm import completion

        # Prepare messages for the LLM
        messages = [
            {"role": "system", "content": "You are an expert in DEVS modeling using xDEVS.py. Generate clean, executable code that meets the specified requirements."},
            {"role": "user", "content": generate_prompt}
        ]

        # Call the model using LiteLLM
        response = completion(
            model=self.model_id,
            messages=messages,
            temperature=0.2,
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_BASE_URL
        )

        # Extract the generated code
        generated_code = response.choices[0].message.content

        # Remove markdown code block markers if present
        if generated_code.startswith("```python"):
            # Find the end of the first line (after ```python)
            first_line_end = generated_code.find("\n") + 1
            # Find the start of the closing code block
            last_code_start = generated_code.rfind("```")
            if last_code_start > first_line_end:
                # Extract only the code between the markers
                generated_code = generated_code[first_line_end:last_code_start]
        
        # # Create destination path in knowledge base
        # kb_dest_path = f"devs_models/{model_name}.py"
        # try:
        #     dst = self._safe_kb_path(kb_dest_path)
        # except PermissionError as e:
        #     return str(e)

        # # Ensure parent directory exists
        # dst.parent.mkdir(parents=True, exist_ok=True)

        # print(f"Saving DEVS model '{model_name}' to '{dst}'")

        # # Write the generated code to file in knowledge base
        # with open(dst, "w", encoding="utf-8") as f:
        #     f.write(generated_code)

        # # Update the index for the new file
        # self.repo_indexer.update_file(dst)

        # Also save to working directory for backward compatibility
        working_dir_path = Path(self.working_directory)
        sub_path = Path(target_path)
        wordking_dir_dst = working_dir_path / sub_path
        os.makedirs(wordking_dir_dst.parent, exist_ok=True)
        with open(wordking_dir_dst, "w", encoding="utf-8") as f:
            f.write(generated_code)

        # Generate a summary of the model
        summarize_prompt = self._get_summarize_prompt(generated_code)
        summarize_messages = [
            {"role": "system", "content": "You are an expert in DEVS modeling. Summarize the provided DEVS model in JSON format."},
            {"role": "user", "content": summarize_prompt}
        ]

        summarize_response = completion(
            model=self.model_id,
            messages=summarize_messages,
            temperature=0.1, 
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_BASE_URL
        )

        summarization = summarize_response.choices[0].message.content

        # Safe relative path calculation
        try:
            rel_path = wordking_dir_dst.relative_to(working_dir_path)
        except ValueError:
            rel_path = wordking_dir_dst.name

        return f"DEVS model '{model_name}' constructed and saved to '{rel_path}'. The file has been indexed for semantic search. Summary: {summarization}"
