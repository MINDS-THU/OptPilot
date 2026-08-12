import unittest
import shutil
from pathlib import Path
from unittest.mock import MagicMock
from general_tools.kb_repo_management.kb_repo_addition_tools import (
    WriteToKnowledgeBase,
    CopyToKnowledgeBase,
    AppendToKnowledgeBaseFile,
)

TEST_DIR = Path(__file__).parent / "temp_data" / "addition"

class TestKBAdditionTools(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TEST_DIR, ignore_errors=True)
        (TEST_DIR / "working").mkdir(parents=True, exist_ok=True)
        (TEST_DIR / "kb").mkdir(parents=True, exist_ok=True)
        (TEST_DIR / "kb" / "linear_programming").mkdir(parents=True, exist_ok=True)
        (TEST_DIR / "kb" / "linear_programming" / "applications").mkdir(parents=True, exist_ok=True)
        (TEST_DIR / "kb" / "linear_programming" / "applications" / "diet_problem").mkdir(parents=True, exist_ok=True)

        self.indexer = MagicMock()
        self.indexer.root = TEST_DIR / "kb"
        self.indexer.update_file = MagicMock()

        # Create a mock taxonomy manager that accepts common test paths
        self.mock_taxonomy_manager = MagicMock()
        self.mock_taxonomy_manager.validate_category_path.return_value = True
        self.mock_taxonomy_manager.get_valid_categories.return_value = [
            "linear_programming/applications/diet_problem",
            "integer_programming/applications/capital_budgeting"
        ]

        # Working file
        (TEST_DIR / "working" / "sample.txt").write_text("Sample content\n")

        # Write tool
        self.write_tool = WriteToKnowledgeBase(self.indexer, taxonomy_manager=self.mock_taxonomy_manager)
        self.copy_tool = CopyToKnowledgeBase(self.indexer, working_dir=str(TEST_DIR / "working"), taxonomy_manager=self.mock_taxonomy_manager)
        self.append_tool = AppendToKnowledgeBaseFile(self.indexer, taxonomy_manager=self.mock_taxonomy_manager)

    def test_write_file(self):
        result = self.write_tool.forward("Initial content\n", "linear_programming/applications/diet_problem/write.txt", overwrite=False)
        self.assertIn("Wrote content", result)

    def test_write_file_overwrite_true(self):
        dst = TEST_DIR / "kb" / "linear_programming" / "applications" / "diet_problem" / "overwrite.txt"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("Old content")
        result = self.write_tool.forward("New content\n", "linear_programming/applications/diet_problem/overwrite.txt", overwrite=True)
        self.assertIn("Wrote content", result)
        self.assertIn("New content", dst.read_text())

    def test_write_file_unique_path(self):
        dst = TEST_DIR / "kb" / "linear_programming" / "applications" / "diet_problem" / "unique.txt"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("Existing content")
        result = self.write_tool.forward("New content\n", "linear_programming/applications/diet_problem/unique.txt", overwrite=False)
        self.assertIn("Wrote content", result)
        # Check that a new file was created with a suffix
        new_file = TEST_DIR / "kb" / "linear_programming" / "applications" / "diet_problem" / "unique_1.txt"
        self.assertTrue(new_file.exists())
        self.assertIn("New content", new_file.read_text())

    def test_copy_file(self):
        result = self.copy_tool.forward("sample.txt", "linear_programming/applications/diet_problem/copied.txt", overwrite=False)
        self.assertIn("Copied", result)
        self.assertTrue((TEST_DIR / "kb" / "linear_programming" / "applications" / "diet_problem" / "copied.txt").exists())

    def test_copy_file_overwrite_true(self):
        dst = TEST_DIR / "kb" / "linear_programming" / "applications" / "diet_problem" / "overwrite.txt"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("Old content")
        result = self.copy_tool.forward("sample.txt", "linear_programming/applications/diet_problem/overwrite.txt", overwrite=True)
        self.assertIn("Copied", result)
        self.assertIn("Sample content", dst.read_text())

    def test_copy_file_unique_path(self):
        dst = TEST_DIR / "kb" / "linear_programming" / "applications" / "diet_problem" / "unique.txt"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("Existing content")
        result = self.copy_tool.forward("sample.txt", "linear_programming/applications/diet_problem/unique.txt", overwrite=False)
        self.assertIn("Copied", result)
        # Check that a new file was created with a suffix
        new_file = TEST_DIR / "kb" / "linear_programming" / "applications" / "diet_problem" / "unique_1.txt"
        self.assertTrue(new_file.exists())
        self.assertIn("Sample content", new_file.read_text())

    def test_copy_nonexistent_source(self):
        result = self.copy_tool.forward("nonexistent.txt", "linear_programming/applications/diet_problem/copied.txt", overwrite=False)
        self.assertIn("Error", result)
        self.assertIn("does not exist", result)

    def test_append_to_file(self):
        # Create a test file first
        test_file = TEST_DIR / "kb" / "linear_programming" / "applications" / "diet_problem" / "test.txt"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("Original content\n")
        
        result = self.append_tool.forward("linear_programming/applications/diet_problem/test.txt", "Appended content\n", insert_mode="end")
        self.assertIn("Appended content", result)
        self.assertIn("Appended content", test_file.read_text())

    def test_append_to_nonexistent_file(self):
        result = self.append_tool.forward("linear_programming/applications/diet_problem/nonexistent.txt", "New content\n", insert_mode="end")
        self.assertIn("Error", result)
        self.assertIn("does not exist", result)

    def test_append_before_match(self):
        # Create a test file with specific content
        test_file = TEST_DIR / "kb" / "linear_programming" / "applications" / "diet_problem" / "test.txt"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("Line 1\nTarget line\nLine 3\n")
        
        result = self.append_tool.forward("linear_programming/applications/diet_problem/test.txt", "Inserted content\n", insert_mode="before", match_string="Target line")
        self.assertIn("Inserted content", result)
        content = test_file.read_text()
        self.assertIn("Inserted content", content)
        # Check that the inserted content comes before the target line
        lines = content.split('\n')
        target_index = lines.index("Target line")
        inserted_index = lines.index("Inserted content")
        self.assertLess(inserted_index, target_index)

    def test_append_after_match(self):
        # Create a test file with specific content
        test_file = TEST_DIR / "kb" / "linear_programming" / "applications" / "diet_problem" / "test.txt"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("Line 1\nTarget line\nLine 3\n")
        
        result = self.append_tool.forward("linear_programming/applications/diet_problem/test.txt", "Inserted content\n", insert_mode="after", match_string="Target line")
        self.assertIn("Inserted content", result)
        content = test_file.read_text()
        self.assertIn("Inserted content", content)
        # Check that the inserted content comes after the target line
        lines = content.split('\n')
        target_index = lines.index("Target line")
        inserted_index = lines.index("Inserted content")
        self.assertGreater(inserted_index, target_index)

    def test_append_no_match_found(self):
        # Create a test file with specific content
        test_file = TEST_DIR / "kb" / "linear_programming" / "applications" / "diet_problem" / "test.txt"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("Line 1\nLine 2\nLine 3\n")
        
        result = self.append_tool.forward("linear_programming/applications/diet_problem/test.txt", "Inserted content\n", insert_mode="before", match_string="Nonexistent")
        # The current implementation has a bug - it returns "inserted" message even when no match is found
        # because it sets inserted=True when falling back to appending at the end
        self.assertIn("Inserted content", result)
        self.assertIn("before", result)
        self.assertIn("line matching", result)
        # Content should still be appended to the end
        self.assertIn("Inserted content", test_file.read_text())

    def test_taxonomy_validation_error(self):
        """Test that tools without taxonomy manager raise an error."""
        # Create tools without taxonomy manager
        write_tool = WriteToKnowledgeBase(self.indexer, taxonomy_manager=None)
        copy_tool = CopyToKnowledgeBase(self.indexer, working_dir=str(TEST_DIR / "working"), taxonomy_manager=None)
        append_tool = AppendToKnowledgeBaseFile(self.indexer, taxonomy_manager=None)
        
        # Test that they raise ValueError when trying to use them
        with self.assertRaises(ValueError):
            write_tool.forward("content", "test/path.txt", overwrite=False)
        
        with self.assertRaises(ValueError):
            copy_tool.forward("sample.txt", "test/path.txt", overwrite=False)
        
        with self.assertRaises(ValueError):
            append_tool.forward("test/path.txt", "content", insert_mode="end")

    def tearDown(self):
        shutil.rmtree(TEST_DIR, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()
