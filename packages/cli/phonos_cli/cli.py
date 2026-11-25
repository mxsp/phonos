"""Main CLI entry point for Phonos."""
import click
from rich.console import Console
from rich.table import Table

from .client import PhonosClient, PhonosAPIError
from .config import set_api_key, set_endpoint, get_api_key, get_endpoint


console = Console()


@click.group()
@click.version_option()
def main():
    """Phonos CLI - Command-line interface for the Phonos scientific computing platform."""
    pass


# ============================================================================
# CONFIG COMMANDS
# ============================================================================

@main.group()
def config():
    """Configure CLI settings."""
    pass


@config.command("set-api-key")
@click.argument("api_key")
def config_set_api_key(api_key):
    """Set your API key."""
    set_api_key(api_key)
    console.print("[green]API key configured successfully![/green]")


@config.command("set-endpoint")
@click.argument("endpoint")
def config_set_endpoint(endpoint):
    """Set the API endpoint."""
    set_endpoint(endpoint)
    console.print(f"[green]Endpoint set to: {endpoint}[/green]")


@config.command("show")
def config_show():
    """Show current configuration."""
    api_key = get_api_key()
    endpoint = get_endpoint()

    table = Table(title="Phonos CLI Configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("API Key", f"{api_key[:8]}..." if api_key else "Not configured")
    table.add_row("Endpoint", endpoint)

    console.print(table)


# ============================================================================
# PROJECT COMMANDS
# ============================================================================

@main.group()
def project():
    """Manage projects."""
    pass


@project.command("list")
def project_list():
    """List all projects."""
    try:
        client = PhonosClient()
        response = client.list_projects()

        projects = response.get("projects", [])

        if not projects:
            console.print("[yellow]No projects found.[/yellow]")
            return

        table = Table(title="Projects")
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Description")
        table.add_column("Created", style="dim")

        for p in projects:
            table.add_row(
                str(p["id"]),
                p["name"],
                p.get("description", "")[:50],
                p.get("created_at", "")[:10]
            )

        console.print(table)

    except PhonosAPIError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


@project.command("create")
@click.argument("name")
@click.option("--description", default="", help="Project description")
def project_create(name, description):
    """Create a new project."""
    try:
        client = PhonosClient()
        response = client.create_project(name, description)

        console.print(f"[green]Project created successfully![/green]")
        console.print(f"Project ID: {response['project']['id']}")

    except PhonosAPIError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


@project.command("get")
@click.argument("project_id", type=int)
def project_get(project_id):
    """Get project details."""
    try:
        client = PhonosClient()
        response = client.get_project(project_id)

        project = response.get("project", {})

        console.print(f"\n[cyan]Project: {project['name']}[/cyan]")
        console.print(f"ID: {project['id']}")
        console.print(f"Description: {project.get('description', 'N/A')}")
        console.print(f"Created: {project.get('created_at', 'N/A')}")

    except PhonosAPIError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


# ============================================================================
# JOB COMMANDS
# ============================================================================

@main.group()
def job():
    """Manage jobs."""
    pass


@job.command("list")
@click.option("--project", type=int, required=True, help="Project ID")
def job_list(project):
    """List jobs in a project."""
    try:
        client = PhonosClient()
        response = client.list_jobs(project)

        jobs = response.get("jobs", [])

        if not jobs:
            console.print("[yellow]No jobs found.[/yellow]")
            return

        table = Table(title=f"Jobs in Project {project}")
        table.add_column("Job ID", style="cyan")
        table.add_column("Model", style="green")
        table.add_column("Status")
        table.add_column("Created", style="dim")

        for j in jobs:
            status_color = {
                "completed": "green",
                "running": "yellow",
                "queued": "blue",
                "failed": "red",
            }.get(j.get("status", ""), "white")

            table.add_row(
                j["job_id"],
                j["model"],
                f"[{status_color}]{j.get('status', 'unknown')}[/{status_color}]",
                j.get("created_at", "")[:16]
            )

        console.print(table)

    except PhonosAPIError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


@job.command("submit")
@click.option("--project", type=int, required=True, help="Project ID")
@click.option("--model", required=True, help="Model ID (e.g., type-0)")
@click.option("--sequence", help="Amino acid sequence")
@click.option("--file", type=int, help="Input file ID")
@click.option("--template", help="Template ID to use")
def job_submit(project, model, sequence, file, template):
    """Submit a new job."""
    try:
        client = PhonosClient()

        if template:
            # Get template config
            template_data = client.get_template(template)
            model = template_data["template"]["model"]
            # Use template config

        if not sequence and not file:
            console.print("[red]Error: Either --sequence or --file must be provided.[/red]")
            raise click.Abort()

        response = client.submit_job(project, model, sequence, file)

        console.print(f"[green]Job submitted successfully![/green]")
        console.print(f"Job ID: {response['job']['job_id']}")
        console.print(f"Status: {response['job']['status']}")

    except PhonosAPIError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


@job.command("status")
@click.argument("job_id")
def job_status(job_id):
    """Check job status."""
    try:
        client = PhonosClient()
        response = client.get_job(job_id)

        job_data = response.get("job", {})

        status = job_data.get("status", "unknown")
        status_color = {
            "completed": "green",
            "running": "yellow",
            "queued": "blue",
            "failed": "red",
        }.get(status, "white")

        console.print(f"\n[cyan]Job: {job_data['job_id']}[/cyan]")
        console.print(f"Status: [{status_color}]{status}[/{status_color}]")
        console.print(f"Model: {job_data.get('model', 'N/A')}")
        console.print(f"Created: {job_data.get('created_at', 'N/A')}")

        if job_data.get("message"):
            console.print(f"Message: {job_data['message']}")

        if status == "completed" and job_data.get("output_file"):
            console.print(f"\n[green]Job completed! Download results with:[/green]")
            console.print(f"  phonos job download {job_id}")

    except PhonosAPIError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


@job.command("download")
@click.argument("job_id")
@click.option("--output", "-o", required=True, help="Output file path")
def job_download(job_id, output):
    """Download job results."""
    try:
        client = PhonosClient()
        data = client.download_job_result(job_id)

        with open(output, "wb") as f:
            f.write(data)

        console.print(f"[green]Results downloaded to: {output}[/green]")

    except PhonosAPIError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()
    except Exception as e:
        console.print(f"[red]Failed to save file: {e}[/red]")
        raise click.Abort()


# ============================================================================
# FILE COMMANDS
# ============================================================================

@main.group()
def file():
    """Manage files."""
    pass


@file.command("list")
@click.option("--project", type=int, required=True, help="Project ID")
def file_list(project):
    """List files in a project."""
    try:
        client = PhonosClient()
        response = client.list_files(project)

        files = response.get("files", [])

        if not files:
            console.print("[yellow]No files found.[/yellow]")
            return

        table = Table(title=f"Files in Project {project}")
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Size")
        table.add_column("Created", style="dim")

        for f in files:
            size_mb = f.get("size", 0) / (1024 * 1024)
            table.add_row(
                str(f["id"]),
                f["name"],
                f"{size_mb:.2f} MB",
                f.get("created_at", "")[:10]
            )

        console.print(table)

    except PhonosAPIError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


@file.command("upload")
@click.argument("file_path")
@click.option("--project", type=int, required=True, help="Project ID")
@click.option("--notes", default="", help="File notes")
def file_upload(file_path, project, notes):
    """Upload a file to a project."""
    try:
        import os
        if not os.path.exists(file_path):
            console.print(f"[red]File not found: {file_path}[/red]")
            raise click.Abort()

        client = PhonosClient()
        response = client.upload_file(project, file_path, notes)

        console.print(f"[green]File uploaded successfully![/green]")
        console.print(f"File ID: {response['file']['id']}")

    except PhonosAPIError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


@file.command("download")
@click.argument("file_id", type=int)
@click.option("--output", "-o", required=True, help="Output file path")
def file_download(file_id, output):
    """Download a file."""
    try:
        client = PhonosClient()
        data = client.download_file(file_id)

        with open(output, "wb") as f:
            f.write(data)

        console.print(f"[green]File downloaded to: {output}[/green]")

    except PhonosAPIError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()
    except Exception as e:
        console.print(f"[red]Failed to save file: {e}[/red]")
        raise click.Abort()


# ============================================================================
# TEMPLATE COMMANDS
# ============================================================================

@main.group()
def template():
    """Manage job templates."""
    pass


@template.command("list")
def template_list():
    """List available templates."""
    try:
        client = PhonosClient()
        response = client.list_templates()

        templates = response.get("templates", [])

        if not templates:
            console.print("[yellow]No templates found.[/yellow]")
            return

        table = Table(title="Job Templates")
        table.add_column("Template ID", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Model")
        table.add_column("Public")
        table.add_column("Uses", justify="right")

        for t in templates:
            table.add_row(
                t["template_id"],
                t["name"],
                t["model"],
                "✓" if t.get("is_public") else "✗",
                str(t.get("use_count", 0))
            )

        console.print(table)

    except PhonosAPIError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


# ============================================================================
# ANALYTICS COMMANDS
# ============================================================================

@main.group()
def analytics():
    """View usage analytics."""
    pass


@analytics.command("summary")
def analytics_summary():
    """View usage summary."""
    try:
        client = PhonosClient()
        response = client.get_usage_summary()

        summary = response.get("summary", {})

        console.print("\n[cyan]Usage Summary[/cyan]")
        console.print(f"Total Events: {summary.get('total_events', 0)}")
        console.print(f"Total Credits: {summary.get('total_credits', 0)}")
        console.print(f"Total Compute Time: {summary.get('total_compute_time', 0):.2f} seconds")
        console.print(f"Projects Used: {summary.get('projects_used', 0)}")
        console.print(f"Active Days: {summary.get('active_days', 0)}")

        by_type = summary.get("by_event_type", [])
        if by_type:
            console.print("\n[cyan]By Event Type:[/cyan]")
            for event in by_type:
                console.print(f"  {event['event_type']}: {event['count']} events, {event['credits']} credits")

    except PhonosAPIError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


@analytics.command("daily")
@click.option("--days", default=30, help="Number of days to show")
def analytics_daily(days):
    """View daily usage statistics."""
    try:
        client = PhonosClient()
        response = client.get_daily_usage(days)

        daily_usage = response.get("daily_usage", [])

        if not daily_usage:
            console.print("[yellow]No usage data available.[/yellow]")
            return

        table = Table(title=f"Daily Usage (Last {days} Days)")
        table.add_column("Date", style="cyan")
        table.add_column("Events", justify="right")
        table.add_column("Credits", justify="right")
        table.add_column("Compute Time", justify="right")

        for day in daily_usage:
            table.add_row(
                day["date"],
                str(day.get("events", 0)),
                str(day.get("credits", 0)),
                f"{day.get('compute_time', 0):.2f}s"
            )

        console.print(table)

    except PhonosAPIError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


# ============================================================================
# SEARCH COMMAND
# ============================================================================

@main.command()
@click.argument("query")
@click.option("--category", multiple=True, help="Categories to search (projects, jobs, files, etc.)")
def search(query, category):
    """Search across all resources."""
    try:
        client = PhonosClient()
        response = client.search(query, list(category) if category else None)

        results = response.get("results", {})
        total = results.get("total_results", 0)

        console.print(f"\n[cyan]Search Results for '{query}':[/cyan] {total} total")

        for category_name, items in results.items():
            if category_name in ["query", "total_results"] or not items:
                continue

            console.print(f"\n[green]{category_name.upper()}:[/green]")
            for item in items[:5]:  # Show first 5 results per category
                name = item.get("name") or item.get("username") or item.get("job_id") or item.get("title", "N/A")
                console.print(f"  • {name}")

    except PhonosAPIError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


if __name__ == "__main__":
    main()
