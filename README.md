# AMPA - Automated Project Management Agent

AMPA (Automated Project Management Agent) is an intelligent project management agent designed to work with OpenCode. It automates routine project management tasks, monitors project health, and assists with workflow orchestration.

## Overview

AMPA provides automated project management capabilities including:

- **Automated Scheduling**: Intelligent task scheduling and delegation
- **Discord Integration**: Bot for notifications and team communication
- **Audit & Monitoring**: Continuous project health monitoring
- **PR Monitoring**: Automated pull request tracking and notifications
- **Session Management**: Work session blocking and management

## Installation

AMPA is installed as a Worklog plugin for OpenCode:

```bash
# Use the Install AMPA Skill
opencode /install-ampa
```

For detailed installation instructions, see the [Installation Skill](../skill/install-ampa/SKILL.md).

## Architecture

AMPA consists of several key components:

- **Daemon**: Background service that runs scheduled tasks
- **Discord Bot**: Handles notifications and team interactions
- **Scheduler**: Manages task execution and timing
- **Audit System**: Monitors project health and generates reports
- **Engine**: Core decision-making and dispatch logic

## Configuration

AMPA is configured through environment variables and `.env` files:

```bash
# Required environment variables
DISCORD_TOKEN=your_discord_bot_token
GITHUB_TOKEN=your_github_token
OPENAI_API_KEY=your_openai_api_key
```

Configuration files are stored in the project's `.worklog/ampa/` directory.

## Development

### Prerequisites

- Python 3.11+
- Poetry (dependency management)
- Discord bot token (for Discord integration)

### Setup

```bash
# Clone the repository
git clone https://github.com/SorraTheOrc/ampa.git
cd ampa

# Install dependencies
poetry install

# Run tests
poetry run pytest
```

### Project Structure

```
ampa/
├── __init__.py           # Package initialization
├── daemon.py             # Main daemon process
├── discord_bot.py        # Discord bot implementation
├── scheduler.py          # Task scheduler
├── scheduler_executor.py # Task execution logic
├── scheduler_store.py    # Task persistence
├── audit/                # Audit system
│   ├── __init__.py
│   ├── handlers.py
│   └── result.py
├── engine/               # Core engine
│   ├── __init__.py
│   ├── adapters.py
│   ├── candidates.py
│   ├── core.py
│   ├── descriptor.py
│   └── dispatch.py
└── ...
```

## Usage

### Running the Daemon

```bash
# Start the AMPA daemon
python -m ampa.daemon

# Or using the scheduler CLI
python -m ampa.scheduler_cli
```

### Discord Commands

When Discord integration is enabled, AMPA responds to various commands:

- `!ampa status` - Show current project status
- `!ampa health` - Show project health metrics
- `!ampa tasks` - List scheduled tasks

### Audit attachments

Audit notifications include the full audit markdown as an attachment payload (in-memory). There is no persistence to a project-local filesystem path by default.

## Contributing

Contributions are welcome! Please see our [Contributing Guide](CONTRIBUTING.md) for details.

## License

AMPA is licensed under the MIT License. See [LICENSE](LICENSE) for details.

## Related Projects

- [OpenCode](https://github.com/opencode/opencode) - The core OpenCode project
- [Worklog](https://github.com/opencode/worklog) - Project management tooling

AMPA was originally developed as part of OpenCode but is now maintained as an independent project.

## Support

For support, please:
1. Check the [Documentation](docs/)
2. Open an issue on GitHub
3. Join our Discord community

## Worklog Status: input_needed

AMPA and the intake automation use a status value `input_needed` to indicate work items which require additional information from the requester. This status is orthogonal to stages (idea, intake_complete, plan_complete, etc.) and can be used by automation and operators to track items awaiting requester input.

See `docs/operator-guide-input-needed.md` and `docs/developer-guide-status-integration.md` for details on how to handle and integrate the new status.

---

**AMPA** - Automating project management so you can focus on what matters.
