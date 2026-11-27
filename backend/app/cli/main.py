"""KiteHut master CLI entry point.

^^^ Shebang not needed since we use `python -m kitehut` or `kitehut` entry point.
"""

import sys
import click

from app import __version__

from app.cli.commands.ask_cmd import ask_command
from app.cli.commands.doctor_cmd import doctor_command
from app.cli.commands.findings_cmd import findings_command
from app.cli.commands.fix_cmd import fix_command
from app.cli.commands.git_cmd import git_group
from app.cli.commands.github_cmd import github_group
from app.cli.commands.init_cmd import init_command
from app.cli.commands.knowledge_cmds import (
    analyze_command,
    context_command,
    evidence_command,
    graph_command,
    knowledge_command,
    mcp_command,
    refresh_command,
    visualize_command,
)
from app.cli.commands.models_cmd import models_command
from app.cli.commands.impact_cmd import impact_command
from app.cli.commands.report_cmd import report_command
from app.cli.commands.review_cmd import review_command
from app.cli.commands.start_cmd import start_command
from app.cli.commands.status_cmd import status_command
from app.cli.commands.traverse_cmd import traverse_command

from app.cli.commands.github_cmd import github_group


@click.group(
    name="kitehut",
    help="KiteHut -- local-first repository intelligence & evidence engine.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(version=__version__, prog_name="kitehut")
def cli():
    """KiteHut: graph to understand, context to reason, evidence to trust."""
    pass


# Register all subcommands
# KiteHut knowledge commands
for _cmd in (
    analyze_command,
    graph_command,
    knowledge_command,
    visualize_command,
    refresh_command,
    context_command,
    evidence_command,
    mcp_command,
):
    cli.add_command(_cmd)

# Impact analysis command
cli.add_command(impact_command)

# Legacy agent / guardian commands (kept for compatibility)
cli.add_command(init_command)
cli.add_command(start_command)
cli.add_command(ask_command)
cli.add_command(review_command)
cli.add_command(fix_command)
cli.add_command(status_command)
cli.add_command(findings_command)
cli.add_command(report_command)
cli.add_command(git_group)
cli.add_command(github_group)
cli.add_command(models_command)
cli.add_command(doctor_command)
cli.add_command(traverse_command)


def main():
    """Main entrypoint function for setup.py or direct execution."""
    cli()


if __name__ == "__main__":
    main()