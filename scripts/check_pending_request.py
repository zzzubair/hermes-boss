#!/usr/bin/env python3
"""Exercise current, legacy and missing T3 question capabilities without live credentials."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


if __name__ == "__main__":
    cases = {
        "tests.test_boss.CLITest": [
            "test_pending_input_and_answer",
            "test_new_pending_tool_lists_reads_and_answers_without_legacy_names",
            "test_new_pending_tool_is_preferred_when_both_are_advertised",
            "test_pending_tool_errors_are_not_treated_as_missing_capabilities",
            "test_missing_questions_cannot_allow_landing_or_settlement_with_pending_inputs",
        ],
        "tests.test_wake.WakeTest": [
            "test_new_pending_tool_completion_queues_finished_inbox_and_status",
            "test_missing_optional_tools_preserve_done_and_decision_and_log_once",
            "test_missing_question_read_still_queues_decision",
            "test_missing_question_list_still_surfaces_pending_input",
        ],
    }
    suite = unittest.defaultTestLoader.loadTestsFromNames(
        [f"{group}.{test}" for group, tests in cases.items() for test in tests])
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
