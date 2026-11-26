"""
Knowledge Base Initialization Tools
This module contains utilities for creating structured knowledge base directories
with predefined hierarchical organization for specific domains.
"""

import os
import json
from pathlib import Path
from typing import List, Dict, Any


def create_or_knowledge_base(base_directory: str, iise_taxonomy_file: str = None) -> str:
    """
    Initialize a comprehensive Operations Research knowledge base directory structure
    using the IISE Body of Knowledge taxonomy.
    
    Creates a hierarchical directory structure based on the IISE Operations Research
    taxonomy to ensure consistent, academic-standard organization of knowledge.
    
    Args:
        base_directory (str): Path where the knowledge base directory should be created
        iise_taxonomy_file (str, optional): Path to IISE_BoK.json file. If None, searches for it.
        
    Returns:
        str: Success message with the path of the created knowledge base
        
    Raises:
        OSError: If directory creation fails
        PermissionError: If insufficient permissions to create directories
        FileNotFoundError: If IISE_BoK.json file is not found
    """
    
    try:
        # Find IISE_BoK.json file if not provided
        if iise_taxonomy_file is None:
            # Search for the file in common locations
            possible_paths = [
                Path(base_directory).parent / "IISE_BoK.json",
                Path(__file__).parent.parent.parent / "apps" / "operations_research" / "IISE_BoK.json",
                Path.cwd() / "apps" / "operations_research" / "IISE_BoK.json",
                Path.cwd() / "IISE_BoK.json"
            ]
            
            iise_taxonomy_file = None
            for path in possible_paths:
                if path.exists():
                    iise_taxonomy_file = str(path)
                    break
                    
            if iise_taxonomy_file is None:
                raise FileNotFoundError(
                    "IISE_BoK.json file not found. Please provide the path to the IISE taxonomy file."
                )
        
        # Load IISE taxonomy structure
        with open(iise_taxonomy_file, 'r', encoding='utf-8') as f:
            iise_taxonomy = json.load(f)
        
        # Create base directory
        base_path = Path(base_directory)
        base_path.mkdir(parents=True, exist_ok=True)
        
        # Extract all directory paths from IISE taxonomy
        directory_paths = _extract_taxonomy_paths(iise_taxonomy["or_knowledge_base"])
        
        # Create all directories
        created_dirs = []
        for dir_path in directory_paths:
            full_path = base_path / dir_path
            full_path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(str(full_path.relative_to(base_path)))
            
        return (
            f"Successfully created Operations Research knowledge base at '{base_directory}' "
            f"using IISE Body of Knowledge taxonomy with {len(created_dirs)} directories "
            f"covering all major OR topics including: Linear Programming, Integer Programming, "
            f"Network Flows, Dynamic Programming, Nonlinear Programming, Metaheuristics, "
            f"Decision Analysis, Stochastic Modeling, Queuing Theory, Simulation, and Systems Dynamics."
        )
        
    except FileNotFoundError as e:
        raise FileNotFoundError(f"IISE taxonomy file not found: {e}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in IISE taxonomy file: {e}")
    except PermissionError as e:
        raise PermissionError(f"Insufficient permissions to create knowledge base at '{base_directory}': {e}")
    except OSError as e:
        raise OSError(f"Failed to create knowledge base directory structure: {e}")


def _extract_taxonomy_paths(taxonomy_node: Dict[str, Any], current_path: str = "") -> List[str]:
    """
    Recursively extract all directory paths from the IISE taxonomy structure.
    
    Args:
        taxonomy_node: Dictionary representing the current node in the taxonomy
        current_path: Current path being built
        
    Returns:
        List of all directory paths in the taxonomy
    """
    paths = []
    
    for key, value in taxonomy_node.items():
        new_path = f"{current_path}/{key}" if current_path else key
        
        if value is None:
            # Leaf node - add the complete path
            paths.append(new_path)
        elif isinstance(value, dict):
            # Branch node - recursively process children
            paths.extend(_extract_taxonomy_paths(value, new_path))
            
    return paths


def _parse_hierarchical_structure(structure_list: List[str]) -> List[str]:
    """
    Parse hierarchical structure and convert to directory paths.
    
    DEPRECATED: This function is kept for backwards compatibility.
    Use IISE taxonomy structure instead.
    
    Args:
        structure_list: List of numbered hierarchical items (e.g., "2.1. Title")
        
    Returns:
        List of directory paths representing the hierarchy
    """
    directory_paths = []
    section_map = {}  # Map section numbers to their titles
    
    for item in structure_list:
        # Extract section number and title
        parts = item.split('. ', 1)
        if len(parts) != 2:
            continue
            
        section_num = parts[0]
        title = parts[1]
        
        # Store the mapping
        section_map[section_num] = title
        
        # Build the directory path based on the hierarchy
        levels = section_num.split('.')
        path_components = []
        
        # Build path by looking up each level in the hierarchy
        for i in range(len(levels)):
            current_section = '.'.join(levels[:i+1])
            if current_section in section_map:
                path_components.append(section_map[current_section])
        
        # Create directory path from path components
        if path_components:
            dir_path = '/'.join(path_components)
            directory_paths.append(dir_path)
    
    return directory_paths


# Function removed - sample content creation is unnecessary
# The knowledge base will be populated with actual content by the agents


if __name__ == "__main__":
    # Example usage
    kb_path = "or_knowledge_base_example"
    
    try:
        # Create the knowledge base structure
        result = create_or_knowledge_base(kb_path)
        print(result)
        
        print(f"\nKnowledge base created successfully at: {os.path.abspath(kb_path)}")
        
    except Exception as e:
        print(f"Error: {e}") 