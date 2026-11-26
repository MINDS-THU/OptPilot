import unittest
import shutil
from pathlib import Path
from unittest.mock import MagicMock
from general_tools.kb_repo_management.kb_repo_maintanence_tools import (
    ListKnowledgeBaseDirectory,
    SeeKnowledgeBaseFile,
    MoveOrRenameInKnowledgeBase,
    DeleteFromKnowledgeBase,
)

TEST_DIR = Path(__file__).parent / "temp_data" / "maintenance"

class TestKBMaintenanceTools(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TEST_DIR, ignore_errors=True)
        (TEST_DIR / "docs").mkdir(parents=True, exist_ok=True)
        (TEST_DIR / "tmp").mkdir(parents=True, exist_ok=True)
        (TEST_DIR / "linear_programming").mkdir(parents=True, exist_ok=True)
        (TEST_DIR / "linear_programming" / "applications").mkdir(parents=True, exist_ok=True)
        (TEST_DIR / "linear_programming" / "applications" / "diet_problem").mkdir(parents=True, exist_ok=True)
        (TEST_DIR / "docs" / "readme.md").write_text("Hello\nWorld")
        (TEST_DIR / "tmp" / "to_delete.txt").write_text("Delete me")
        (TEST_DIR / "linear_programming" / "applications" / "diet_problem" / "test.md").write_text("Test content")

        self.indexer = MagicMock()
        self.indexer.root = TEST_DIR

        # Create a mock taxonomy manager that accepts common test paths
        self.mock_taxonomy_manager = MagicMock()
        self.mock_taxonomy_manager.validate_category_path.return_value = True
        self.mock_taxonomy_manager.get_valid_categories.return_value = [
            "linear_programming/applications/diet_problem",
            "integer_programming/applications/capital_budgeting"
        ]

    def test_list_directory(self):
        tool = ListKnowledgeBaseDirectory(self.indexer)
        result = tool.forward("docs")
        self.assertIn("readme.md", result)

    def test_see_file(self):
        tool = SeeKnowledgeBaseFile(self.indexer)
        result = tool.forward("docs/readme.md")
        self.assertIn("1: Hello", result)

    def test_see_file_nonexistent(self):
        tool = SeeKnowledgeBaseFile(self.indexer)
        result = tool.forward("nonexistent.md")
        self.assertIn("does not exist", result)

    def test_move_or_rename(self):
        tool = MoveOrRenameInKnowledgeBase(self.indexer, taxonomy_manager=self.mock_taxonomy_manager)
        tool.forward("docs/readme.md", "linear_programming/applications/diet_problem/renamed.md", overwrite=True)
        self.assertTrue((TEST_DIR / "linear_programming/applications/diet_problem/renamed.md").exists())

    def test_delete(self):
        tool = DeleteFromKnowledgeBase(self.indexer, taxonomy_manager=self.mock_taxonomy_manager)
        result = tool.forward("linear_programming/applications/diet_problem/test.md")
        self.assertFalse((TEST_DIR / "linear_programming/applications/diet_problem/test.md").exists())

    def test_delete_nonexistent(self):
        tool = DeleteFromKnowledgeBase(self.indexer, taxonomy_manager=self.mock_taxonomy_manager)
        result = tool.forward("no/such/file.txt")
        self.assertIn("does not exist", result)

    def test_taxonomy_validation_error(self):
        """Test that tools without taxonomy manager raise an error."""
        # Create tools without taxonomy manager
        move_tool = MoveOrRenameInKnowledgeBase(self.indexer, taxonomy_manager=None)
        delete_tool = DeleteFromKnowledgeBase(self.indexer, taxonomy_manager=None)
        
        # Test that they raise ValueError when trying to use them
        with self.assertRaises(ValueError):
            move_tool.forward("docs/readme.md", "test/path.txt", overwrite=False)
        
        with self.assertRaises(ValueError):
            delete_tool.forward("test/path.txt")

    def tearDown(self):
        shutil.rmtree(TEST_DIR, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()
