# AgentFlow Development Guide

## Project Purpose

AgentFlow is a distributed multi-agent framework based on
publish-subscribe messaging.

Its primary goals are:

1. Decouple agents through asynchronous messaging.
2. Support distributed deployment.
3. Provide fault isolation.
4. Support one-to-many and many-to-one communication.
5. Maintain traceable message flows.
6. Allow agents to be added, removed, restarted, and scaled independently.

## Review Priorities

Review the code in this order:

1. Functional correctness
2. Message reliability
3. Concurrency safety
4. Fault isolation
5. Retry and timeout behavior
6. Resource management
7. Scalability
8. Observability
9. Security
10. Maintainability

## Critical Invariants

1. A failure in one agent must not stop unrelated agents.
2. Every task and message should be traceable.
3. Duplicate messages must not cause duplicate side effects.
4. Retry logic must not create infinite message loops.
5. Queues must not grow without control.
6. Shutdown must not leave uncontrolled background tasks.
7. Message schema changes must be versioned.
8. Transport-specific implementation must remain separated from core agent logic.

## Working Rules

- Analyze before modifying.
- Do not change public APIs without explicit justification.
- Do not silently change message schemas.
- Add tests before fixing high-risk behavior.
- Make one logical change per commit.
- Do not combine unrelated refactoring with bug fixes.
- Every finding must reference actual files and code locations.
- Do not infer behavior only from class or method names.
- Run relevant tests after every modification.

## Commands

CI (`.github/workflows/ci.yml`) runs these on ubuntu and windows with
Python 3.11 and 3.13. Install the tools with
`python -m pip install -r requirements-dev.txt`.

- Build: `python -m build` (sdist and wheel in `dist/`)
- Unit tests: `python -m pytest` (`pyproject.toml` sets `testpaths` and `pythonpath`)
- Coverage: `python -m pytest --cov=agentflow` (fails below `fail_under` in `pyproject.toml`)
- Integration tests: none yet (the `integration` marker is reserved for live-broker tests)
- Lint: `ruff check src tests` and `mypy`
- Run: no CLI; applications start agents with `Agent.start()` / `start_thread()`
