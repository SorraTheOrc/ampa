# AMPA Repository Setup Instructions

## Repository Created Successfully

The AMPA repository structure has been created at:
`/home/rgardler/.config/opencode/ampa-repo/`

## Repository Structure

```
ampa-repo/
├── .github/
│   └── workflows/
│       └── ci.yml          # GitHub Actions CI/CD pipeline
├── ampa/
│   └── __init__.py         # Package initialization
├── tests/
│   └── test_init.py        # Basic tests
├── .gitignore              # Python gitignore
├── CONTRIBUTING.md         # Contribution guidelines
├── LICENSE                 # Apache 2.0 License
├── pyproject.toml          # Poetry project configuration
└── README.md               # Project documentation
```

## GitHub Repository Setup

Since I don't have permission to create repositories in the opencode organization, please have an organization admin run the following:

### 1. Create the Repository

```bash
# Option 1: Using GitHub CLI (with admin permissions)
gh repo create opencode/ampa --public --description "AMPA - Automated Project Management Agent for OpenCode"

# Option 2: Manually via GitHub UI
# Go to: https://github.com/organizations/opencode/repositories/new
# Name: ampa
# Description: AMPA - Automated Project Management Agent for OpenCode
# Visibility: Public
# Initialize with: None (we'll push our own files)
```

### 2. Push the Code

```bash
cd /home/rgardler/.config/opencode/ampa-repo

# Add the remote (replace with actual URL after repo creation)
git remote add origin https://github.com/opencode/ampa.git

# Push to main
git branch -M main
git push -u origin main
```

### 3. Configure Branch Protection Rules

Via GitHub UI (Settings > Branches > Add rule):

**Branch name pattern:** `main`

**Protect matching branches:**
- [x] Require a pull request before merging
  - [x] Require approvals (1)
  - [x] Dismiss stale PR approvals when new commits are pushed
  - [x] Require review from CODEOWNERS (if CODEOWNERS file added)
- [x] Require status checks to pass before merging
  - Status checks: `lint`, `test`, `build`
- [x] Require conversation resolution before merging
- [x] Require signed commits (optional but recommended)
- [x] Include administrators

## Acceptance Criteria Status

| Criteria | Status | Notes |
|----------|--------|-------|
| Repository exists and accessible | Pending | Requires admin to create |
| README.md with project info | Complete | Created with overview, installation, usage |
| CI/CD with GitHub Actions | Complete | Full pipeline with lint, test, build, release |
| Python project structure | Complete | pyproject.toml, Poetry, package structure |
| LICENSE file (Apache 2.0) | Complete | Same as OpenCode |
| Branch protection rules | Pending | Requires admin to configure |

## Next Steps

1. **Create GitHub repository** (requires opencode org admin)
2. **Push code** to the new repository
3. **Configure branch protection** as documented above
4. **Add CODEOWNERS file** (optional but recommended)
5. **Enable required status checks** in branch protection
6. **Test CI/CD pipeline** by creating a test PR

## CI/CD Pipeline Features

The GitHub Actions workflow includes:

- **Lint & Format Check**: black, isort, flake8, mypy
- **Test**: pytest with coverage for Python 3.11 and 3.12
- **Build**: Package building with artifact upload
- **Release**: Automatic release creation on version tag

## Poetry Configuration

The project uses Poetry for dependency management:

```bash
# Install Poetry
curl -sSL https://install.python-poetry.org | python3 -

# Install dependencies
poetry install

# Run commands
poetry run python -m ampa.daemon
poetry run pytest
```

## Notes

- Dependencies are currently minimal (core dependencies will be added during code migration)
- The repository is ready to receive the actual AMPA source code
- All tests currently pass (placeholder tests)
- CI/CD pipeline will need secrets configured for any deployment steps
