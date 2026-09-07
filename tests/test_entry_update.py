import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.audit_ios_a_side import main
from scripts.update_skill import UpdateError


class EntryUpdateTests(unittest.TestCase):
    def test_update_failure_stops_before_scanning_and_report_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'project'
            root.mkdir()
            output = Path(directory) / 'report'
            with patch('scripts.audit_ios_a_side.ensure_latest', side_effect=UpdateError('offline')), \
                 patch('scripts.audit_ios_a_side.Auditor') as auditor, redirect_stderr(io.StringIO()):
                result = main([str(root), '--output-dir', str(output)])
            self.assertEqual(result, 2)
            auditor.assert_not_called()
            self.assertFalse(output.exists())

    def test_updated_release_runs_in_fresh_process_with_original_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            args = [directory, '--format', 'pdf', '--output-dir', str(Path(directory).parent / 'report with spaces')]
            with patch('scripts.audit_ios_a_side.ensure_latest', return_value=True), \
                 patch('scripts.audit_ios_a_side.subprocess.call', return_value=7) as run, \
                 patch('scripts.audit_ios_a_side.Auditor') as auditor, redirect_stdout(io.StringIO()):
                result = main(args)
            self.assertEqual(result, 7)
            auditor.assert_not_called()
            self.assertEqual(run.call_args.args[0][0], sys.executable)
            self.assertEqual(run.call_args.args[0][2:], args)

    def test_current_release_can_run_scanner(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('scripts.audit_ios_a_side.ensure_latest', return_value=False) as check, \
                 patch('scripts.audit_ios_a_side.Auditor', side_effect=ValueError('scanner reached')) as auditor, \
                 redirect_stderr(io.StringIO()):
                self.assertEqual(main([directory]), 2)
            check.assert_called_once()
            auditor.assert_called_once()


if __name__ == '__main__':
    unittest.main()
