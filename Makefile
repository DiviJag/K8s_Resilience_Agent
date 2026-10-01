# PowerShell scripts under scripts/ are the real entrypoints on Windows; these are thin wrappers
# so the HLD's `make lab-up` naming works from git-bash without installing GNU Make elsewhere.

.PHONY: install test lint eval validate lab-up lab-down lab-status

install: ; uv sync

test: ; uv run pytest

lint: ; uv run ruff check . && uv run ruff format --check .

validate: ; uv run chaosloop-eval validate

eval: ; uv run chaosloop-eval run --split test

lab-up: ; powershell -ExecutionPolicy Bypass -File scripts/lab-up.ps1

lab-down: ; powershell -ExecutionPolicy Bypass -File scripts/lab-down.ps1

lab-status: ; powershell -ExecutionPolicy Bypass -File scripts/lab-status.ps1
