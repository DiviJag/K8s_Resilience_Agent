#requires -Version 5.1
# Thin wrapper so PowerShell users do not have to remember `uv run`.
# Example: ./scripts/eval.ps1 run --split dev --repeats 1
uv run chaosloop-eval @args
exit $LASTEXITCODE
