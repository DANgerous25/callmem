# Prompt: Test/Fix Mode

Use this prompt when tests are failing and you need a coding agent to diagnose and fix them.

## Test failure diagnosis

```
Tests are failing in the llm-mem project. Diagnose and fix.

## Failing tests
```
{paste test output}
```

## Context
- Work order being implemented: {WO number and title}
- Files recently changed: {list files}

## Process
1. Read the failing test to understand what it expects
2. Read the implementation being tested
3. Identify the root cause (is the test wrong, or the implementation?)
4. Fix the issue
5. Re-run the full test suite: `pytest tests/`
6. Ensure no new failures were introduced
7. Report: what was wrong, what you changed, and why

## Rules
- If the test expectation matches the work order spec, fix the implementation
- If the test expectation contradicts the work order spec, fix the test AND note the discrepancy
- Never delete or skip a failing test without explaining why
- If a fix requires changing an interface, check downstream consumers first
```

## Post-implementation test sweep

```
Run the full test suite for llm-mem and fix any failures.

```bash
cd /path/to/llm-mem
pytest tests/ -v --tb=short
```

For each failure:
1. Categorize: unit test, integration test, or flaky test
2. Identify root cause
3. Fix (prefer fixing implementation over fixing test, unless the test is wrong)
4. Re-run until all pass

Report a summary:
- Total tests: X
- Passed: X
- Fixed: X (list each with one-line explanation)
- Skipped: X (list each with reason)
```

## Regression check

```
A change was made to {file_path} in the llm-mem project.

The change:
{describe change or paste diff}

Run the full test suite and check for regressions:
```bash
pytest tests/ -v --tb=short
```

If any tests that were previously passing now fail:
1. Determine if the test failure is expected (the test needs updating for the new behavior)
2. Or if it's a regression (the change broke something unintentionally)
3. Fix appropriately
4. Report findings
```

## Writing missing tests

```
The following module in llm-mem lacks test coverage:

File: {file_path}
Module purpose: {description}

Write comprehensive tests covering:
1. Happy path for each public method
2. Edge cases: empty inputs, None values, boundary conditions
3. Error cases: invalid inputs, database errors, missing dependencies
4. Integration: does this module work correctly with its dependencies?

Use the existing test fixtures from tests/conftest.py. Follow the patterns in existing test files.

Place tests in: {test_file_path}
Run them: `pytest {test_file_path} -v`
```
