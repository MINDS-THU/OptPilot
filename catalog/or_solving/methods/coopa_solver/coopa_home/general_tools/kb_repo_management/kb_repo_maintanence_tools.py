"""
Knowledge Base Repo Maintenance Tools
This module contains tools for managing files and folders in a knowledge base stored as a structured repository.
Supports listing, viewing, moving, renaming, and deleting files within the knowledge base.
"""

from smolagents.tools import Tool
import os
import shutil
from pathlib import Path
from general_tools.kb_repo_management.taxonomy_error_logger import log_taxonomy_error


class ListKnowledgeBaseDirectory(Tool):
    name = "list_knowledge_base_directory"
    description = (
        "List all files and folders inside a directory in the knowledge base. "
        "Use this to explore the structure of the knowledge base."
    )
    inputs = {
        "directory": {"type": "string", "description": "Relative path of the directory in the knowledge base."}
    }
    output_type = "string"

    def __init__(self, repo_indexer):
        super().__init__()
        self.root = Path(repo_indexer.root)

    def forward(self, directory: str) -> str:
        try:
            chosen_dir = self._safe_kb_path(directory)
        except PermissionError as e:
            return str(e)
        if not chosen_dir.exists():
            return f"The directory '{directory}' does not exist in the knowledge base."
        if not chosen_dir.is_dir():
            return f"The path '{directory}' is not a directory."
        files = os.listdir(chosen_dir)
        if not files:
            return f"The directory '{directory}' is empty."
        return "\n".join(files)
    
    def _safe_kb_path(self, path: str) -> Path:
        abs_root = self.root.resolve()
        abs_path = (self.root / path).resolve()
        if not str(abs_path).startswith(str(abs_root)):
            raise PermissionError("Access outside the knowledge base root is not allowed.")
        return abs_path

class SeeKnowledgeBaseFile(Tool):
    name = "see_knowledge_base_file"
    description = (
        "View the content of a plain text file in the knowledge base (e.g., .txt, .md, .py). "
        "Avoid using this tool on binary files like .pdf, .docx, or images."
    )
    inputs = {
        "file_path": {"type": "string", "description": "Relative path of the file in the knowledge base."}
    }
    output_type = "string"

    def __init__(self, repo_indexer):
        super().__init__()
        self.root = Path(repo_indexer.root)

    def forward(self, file_path: str) -> str:
        try:
            filepath = self._safe_kb_path(file_path)
        except PermissionError as e:
            return str(e)
        if not filepath.exists():
            return f"The file '{file_path}' does not exist in the knowledge base."
        if not filepath.is_file():
            return f"The path '{file_path}' is not a file."
        try:
            with open(filepath, "r", encoding="utf-8") as file:
                lines = file.readlines()
        except UnicodeDecodeError:
            return f"Cannot read '{file_path}' — it may be a binary or non-text file."
        formatted_lines = [f"{i+1}: {line}" for i, line in enumerate(lines)]
        return "".join(formatted_lines)

    def _safe_kb_path(self, path: str) -> Path:
        abs_root = self.root.resolve()
        abs_path = (self.root / path).resolve()
        if not str(abs_path).startswith(str(abs_root)):
            raise PermissionError("Access outside the knowledge base root is not allowed.")
        return abs_path

class MoveOrRenameInKnowledgeBase(Tool):
    name = "move_or_rename_in_knowledge_base"
    description = (
        "Move or rename a file or folder within the knowledge base. "
        "Use this to reorganize files or to change file/folder names. "
        "If overwrite=True, replaces the destination. If overwrite=False, adds a numeric suffix to avoid conflict."
    )
    inputs = {
        "source_path": {"type": "string", "description": "Current path of the file or folder in the knowledge base."},
        "destination_path": {"type": "string", "description": "New desired path or name in the knowledge base."},
        "overwrite": {"type": "boolean", "description": "Whether to overwrite the destination if it exists."}
    }
    output_type = "string"

    def __init__(self, repo_indexer, taxonomy_manager=None):
        super().__init__()
        self.root = Path(repo_indexer.root)
        self.taxonomy_manager = taxonomy_manager

    def _get_unique_path(self, base_path: Path) -> Path:
        counter = 1
        new_path = base_path
        while new_path.exists():
            new_path = base_path.with_name(f"{base_path.stem}_{counter}{base_path.suffix}")
            counter += 1
        return new_path

    def _validate_taxonomy_path(self, destination_path: str) -> bool:
        """Validate that the destination path is under a valid IISE taxonomy leaf node."""
        if self.taxonomy_manager is None:
            return False  # No taxonomy manager means validation fails
        
        # Extract the directory part of the path (remove filename)
        path_parts = Path(destination_path).parts
        if len(path_parts) < 2:
            return False  # Need at least a category and filename
        
        # Find the parent directory that should be a valid taxonomy leaf node
        # Start from the root and find the longest valid taxonomy path
        valid_taxonomy_path = None
        for i in range(1, len(path_parts)):
            potential_path = "/".join(path_parts[:i])
            if self.taxonomy_manager.validate_category_path(potential_path):
                valid_taxonomy_path = potential_path
        
        # If we found a valid taxonomy path, the destination is valid
        return valid_taxonomy_path is not None

    def forward(self, source_path: str, destination_path: str, overwrite: bool) -> str:
        # Validate taxonomy path first
        if not self._validate_taxonomy_path(destination_path):
            # Log the validation error
            log_taxonomy_error(
                error_type="tool_validation",
                tool_name="MoveOrRenameInKnowledgeBase",
                source_path=source_path,
                destination_path=destination_path,
                overwrite=overwrite
            )
            
            raise ValueError(f"Invalid destination path '{destination_path}'. The path must be under a valid IISE taxonomy leaf node.")
        
        try:
            src = self._safe_kb_path(source_path)
            dst = self._safe_kb_path(destination_path)
        except PermissionError as e:
            return str(e)

        if not src.exists():
            return f"Source '{source_path}' does not exist in the knowledge base."

        if dst.exists() and not overwrite:
            dst = self._get_unique_path(dst)

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"Moved or renamed '{source_path}' to '{dst.relative_to(self.root)}' in the knowledge base."

    def _safe_kb_path(self, path: str) -> Path:
        abs_root = self.root.resolve()
        abs_path = (self.root / path).resolve()
        if not str(abs_path).startswith(str(abs_root)):
            raise PermissionError("Access outside the knowledge base root is not allowed.")
        return abs_path

class DeleteFromKnowledgeBase(Tool):
    name = "delete_from_knowledge_base"
    description = (
        "Delete a file or folder from the knowledge base. "
        "Use this to remove outdated or invalid content."
    )
    inputs = {
        "target_path": {"type": "string", "description": "Path to the file or folder to delete."}
    }
    output_type = "string"

    def __init__(self, repo_indexer, taxonomy_manager=None):
        super().__init__()
        self.root = Path(repo_indexer.root)
        self.taxonomy_manager = taxonomy_manager

    def _validate_taxonomy_path(self, target_path: str) -> bool:
        """Validate that the target path is under a valid IISE taxonomy leaf node."""
        if self.taxonomy_manager is None:
            return False  # No taxonomy manager means validation fails
        
        # Extract the directory part of the path (remove filename)
        path_parts = Path(target_path).parts
        if len(path_parts) < 2:
            return False  # Need at least a category and filename
        
        # Find the parent directory that should be a valid taxonomy leaf node
        # Start from the root and find the longest valid taxonomy path
        valid_taxonomy_path = None
        for i in range(1, len(path_parts)):
            potential_path = "/".join(path_parts[:i])
            if self.taxonomy_manager.validate_category_path(potential_path):
                valid_taxonomy_path = potential_path
        
        # If we found a valid taxonomy path, the destination is valid
        return valid_taxonomy_path is not None

    def forward(self, target_path: str) -> str:
        # Validate taxonomy path first
        if not self._validate_taxonomy_path(target_path):
            # Log the validation error
            log_taxonomy_error(
                error_type="tool_validation",
                tool_name="DeleteFromKnowledgeBase",
                target_path=target_path
            )
            
            raise ValueError(f"Invalid target path '{target_path}'. The path must be under a valid IISE taxonomy leaf node.")
        
        try:
            target = self._safe_kb_path(target_path)
        except PermissionError as e:
            return str(e)

        if not target.exists():
            return f"The path '{target_path}' does not exist in the knowledge base."

        if target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)
        return f"Deleted '{target_path}' from the knowledge base."

    def _safe_kb_path(self, path: str) -> Path:
        abs_root = self.root.resolve()
        abs_path = (self.root / path).resolve()
        if not str(abs_path).startswith(str(abs_root)):
            raise PermissionError("Access outside the knowledge base root is not allowed.")
        return abs_path