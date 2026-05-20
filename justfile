# Default: show available recipes.
_default:
    @just --list

# Show available recipes.
list:
    @just --list

# Install / refresh dependencies from uv.lock.
sync:
    uv sync

# Run the service locally (HOST=127.0.0.1, PORT=8000 by default).
serve:
    uv run python -m passe_partout

# Run the full test suite (smoke tests deselected — see `just test-smoke`).
test:
    uv run pytest

# Run network-touching smoke tests only.
test-smoke:
    uv run pytest -m smoke

# Run a single test by path or `-k` pattern. Examples:
#   just test-one tests/test_app.py::test_name
#   just test-one "-k some_pattern"
test-one *ARGS:
    uv run pytest {{ ARGS }}

# Ruff lint check.
lint:
    uv run ruff check .

# Ruff lint + auto-fix.
lint-fix:
    uv run ruff check --fix .

# Ruff format (writes).
fmt:
    uv run ruff format .

# Ruff format CI-style verification.
fmt-check:
    uv run ruff format --check .

# Cut a release: see ./scripts/release.sh. Example: just release 0.4.3
release VERSION:
    ./scripts/release.sh {{ VERSION }}
