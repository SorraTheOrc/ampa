# Contributing to AMPA

Thank you for your interest in contributing to AMPA! We welcome contributions from the community.

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/yourusername/ampa.git`
3. Install dependencies: `poetry install`
4. Create a branch: `git checkout -b feature/your-feature-name`

## Development Workflow

### Code Style

We use the following tools for code quality:

- **Black**: Code formatting
- **isort**: Import sorting
- **flake8**: Linting
- **mypy**: Type checking

Run all checks:
```bash
poetry run black .
poetry run isort .
poetry run flake8 ampa tests
poetry run mypy ampa
```

### Testing

Run tests with pytest:
```bash
poetry run pytest
```

Run tests with coverage:
```bash
poetry run pytest --cov=ampa --cov-report=html
```

### Pre-commit Hooks

We recommend setting up pre-commit hooks:
```bash
poetry run pre-commit install
```

## Submitting Changes

1. Ensure all tests pass
2. Update documentation if needed
3. Add a clear commit message
4. Push to your fork
5. Create a Pull Request

## Code of Conduct

Please be respectful and constructive in all interactions.

## Questions?

Open an issue or join our Discord community.
