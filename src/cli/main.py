from __future__ import annotations

import asyncio
import sys
from typing import Optional

import typer

from src.orchestration import TaskRunRequest, TaskRunner, get_blueprint, list_blueprints

try:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    _print = console.print
except ImportError:
    _print = typer.echo  # type: ignore[assignment]
    console = None  # type: ignore[assignment]

__version__ = "0.1.0"
YOLO_MAX_ITERATIONS = 200

app = typer.Typer(
    name="minion",
    help="Minions CLI — autonomous coding agent",
    no_args_is_help=True,
)
blueprints_app = typer.Typer(help="Manage blueprints")
app.add_typer(blueprints_app, name="blueprints")


# -- Version callback --------------------------------------------------------

def _version_callback(value: bool) -> None:
    if value:
        _print(f"minion {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Minions CLI — autonomous coding agents."""


# -- run ---------------------------------------------------------------------


def _resolve_max_iterations_override(
    max_iterations: int | None,
    yolo: bool,
) -> int | None:
    if yolo:
        return max(max_iterations or 0, YOLO_MAX_ITERATIONS)
    return max_iterations

@app.command()
def run(
    description: str = typer.Argument(..., help="Task description or Jira ticket ID (e.g. PROJ-123)"),
    repo: str = typer.Option(".", "--repo", "-r", help="Repository path"),
    blueprint: str = typer.Option("default", "--blueprint", "-b", help="Blueprint name"),
    model: str = typer.Option(
        "claude-sonnet-4-20250514", "--model", "-m", help="LLM model to use"
    ),
    max_iterations: Optional[int] = typer.Option(
        None,
        "--max-iterations",
        help="Override max agent iterations for each agentic step",
    ),
    yolo: bool = typer.Option(
        False,
        "--yolo",
        help="Raise the agent iteration budget for unattended runs",
    ),
    jira: Optional[str] = typer.Option(None, "--jira", "-j", help="Jira ticket ID (e.g. PROJ-123)"),
) -> None:
    """Run a task locally using the agent runtime."""
    effective_max_iterations = _resolve_max_iterations_override(max_iterations, yolo)

    _print(f"Running task: {description}")
    _print(f"  repo={repo}  blueprint={blueprint}  model={model}")
    if effective_max_iterations is not None:
        _print(f"  max_iterations={effective_max_iterations}")
    if yolo:
        _print("  mode=yolo")
    if jira:
        _print(f"  jira={jira}")

    async def _execute() -> None:
        if get_blueprint(blueprint) is None:
            _print(f"[red]Blueprint '{blueprint}' not found.[/red]" if console else f"Blueprint '{blueprint}' not found.")
            sys.exit(1)

        request = TaskRunRequest(
            description=description,
            repo=repo,
            blueprint=blueprint,
            model=model,
            max_iterations=effective_max_iterations,
            jira=jira,
            source="cli",
        )
        if yolo:
            request.rules_override.append("Operate in yolo mode: favor autonomous completion within existing safety limits.")
        bp_result = await TaskRunner(status_callback=_print).run(request)

        if bp_result.status.value in ("completed", "partial"):
            _print("\n[green]Task completed successfully[/green]" if console else "\nTask completed successfully")
        else:
            _print(f"\n[red]Task failed:[/red] {bp_result.error}" if console else f"\nTask failed: {bp_result.error}")
            sys.exit(1)

        _print("\nSteps:")
        for step in bp_result.steps:
            status_color = "green" if step.status == "completed" else "red"
            if console:
                _print(f"  [{status_color}]{step.status}[/{status_color}]  {step.name}  ({step.duration_seconds:.1f}s)")
            else:
                _print(f"  [{step.status}]  {step.name}  ({step.duration_seconds:.1f}s)")

        _print(f"\nTotal tokens: {bp_result.total_tokens}")
        _print(f"Duration: {bp_result.duration_seconds:.1f}s")

    asyncio.run(_execute())


# -- serve -------------------------------------------------------------------

@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", "-H", help="Bind host"),
    port: int = typer.Option(8000, "--port", "-p", help="Bind port"),
) -> None:
    """Start the Minions API server."""
    _print(f"Starting API server on {host}:{port}")

    from src.api.server import start

    start(host=host, port=port, role="all")


@app.command("serve-api")
def serve_api(
    host: str = typer.Option("0.0.0.0", "--host", "-H", help="Bind host"),
    port: int = typer.Option(8000, "--port", "-p", help="Bind port"),
) -> None:
    """Start the Minions API server without running the task dispatcher."""
    _print(f"Starting API-only server on {host}:{port}")

    from src.api.server import start

    start(host=host, port=port, role="api")


@app.command()
def worker(
    host: str = typer.Option("127.0.0.1", "--host", "-H", help="Bind host"),
    port: int = typer.Option(8000, "--port", "-p", help="Bind port"),
) -> None:
    """Start a worker-capable process for dispatching queued tasks."""
    _print(f"Starting worker process on {host}:{port}")

    from src.api.server import start

    start(host=host, port=port, role="worker")


# -- blueprints list / show --------------------------------------------------

@blueprints_app.command("list")
def blueprints_list() -> None:
    """List available blueprints."""
    blueprints = list_blueprints()
    if console is not None:
        table = Table(title="Available Blueprints")
        table.add_column("Name", style="cyan")
        table.add_column("Description")
        table.add_column("Steps")
        for bp in blueprints:
            table.add_row(bp.name, bp.description, " -> ".join(step.name for step in bp.steps))
        console.print(table)
    else:
        for bp in blueprints:
            typer.echo(f"  {bp.name}: {bp.description}")


@blueprints_app.command("show")
def blueprints_show(name: str = typer.Argument(..., help="Blueprint name")) -> None:
    """Show details for a specific blueprint."""
    bp = get_blueprint(name)
    if not bp:
        _print(f"Blueprint '{name}' not found.")
        raise typer.Exit(1)

    _print(f"Name:        {bp.name}")
    _print(f"Description: {bp.description}")
    _print(f"Steps:       {' -> '.join(step.name for step in bp.steps)}")


# -- status ------------------------------------------------------------------

@app.command()
def status(
    task_id: str = typer.Argument(..., help="Task ID"),
    api_url: str = typer.Option("http://localhost:8000", "--api-url", help="API base URL"),
) -> None:
    """Check task status via the API server."""
    import httpx

    try:
        resp = httpx.get(f"{api_url}/api/tasks/{task_id}/status", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        _print(f"Task:   {data['task_id']}")
        _print(f"Status: {data['status']}")
        _print(f"Step:   {data['current_step']} ({data['steps_completed']}/{data['total_steps']})")
        if data.get("error"):
            _print(f"Error:  {data['error']}")
    except httpx.HTTPStatusError as exc:
        _print(f"API error: {exc.response.status_code} - {exc.response.text}")
        raise typer.Exit(1)
    except httpx.ConnectError:
        _print(f"Cannot connect to {api_url}")
        raise typer.Exit(1)

if __name__ == "__main__":
    app()
