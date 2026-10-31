import asyncio
import os
from pathlib import Path
from typing import Optional
import click
import typer
from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.styles import Style
from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from typer.core import TyperGroup

from opal_sync.auth.credentials import delete_credentials, get_credentials, store_credentials
from opal_sync.auth.session import SessionManager
from opal_sync.auth.shibboleth import login_shibboleth, ShibbolethAuthError
from opal_sync.config import DEFAULT_CONFIG_PATH, AppConfig, load_config, save_config
from opal_sync.models.course import Course
from opal_sync.scraper.courses import fetch_my_courses
from opal_sync.storage.db import Database
from opal_sync.sync.differ import SyncAction
from opal_sync.sync.engine import SyncEngine
from opal_sync.utils.logging import get_logger, setup_logger


class SetupHintTyperGroup(TyperGroup):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        try:
            return super().parse_args(ctx, args)
        except click.UsageError as error:
            if not any(argument in self.commands for argument in args):
                console.print(f"[bold red]Error:[/bold red] {error.format_message()}")
                _show_no_command_guidance()
                console.print(ctx.get_help())
                raise click.exceptions.Exit(error.exit_code)
            raise


app = typer.Typer(
    name="opal_sync",
    help=(
        "Automated synchronization client for OPAL Bildungsportal Sachsen course "
        "materials. After installation, run opal_sync from any directory."
    ),
    add_completion=False,
    cls=SetupHintTyperGroup,
)
console = Console()
logger = get_logger()


def _show_setup_hint() -> None:
    console.print(
        Align.center(
            Panel(
                "[bold]No config detected.[/bold]\n\n"
                "To get started, run [bold cyan]opal_sync setup[/bold cyan].",
                title="[bold cyan]Getting started[/bold cyan]",
                border_style="cyan",
                padding=(1, 4),
                width=64,
            )
        )
    )


def _show_no_command_guidance() -> None:
    config_path = (Path.cwd() / DEFAULT_CONFIG_PATH).resolve()
    if config_path.is_file():
        console.print(
            "To synchronize your courses, run "
            "[bold cyan]opal_sync sync[/bold cyan]."
        )
    else:
        _show_setup_hint()


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    if ctx.invoked_subcommand is None and not ctx.args:
        console.print("[bold red]Error:[/bold red] No command was provided.")
        _show_no_command_guidance()
        console.print(ctx.get_help())
        raise typer.Exit(code=2)


def get_configured_engine(config_path: Optional[Path] = None) -> SyncEngine:
    config = load_config(config_path)
    setup_logger(config.general.log_level, str(config.general.log_file) if config.general.log_file else None)
    return SyncEngine(config=config)


def _prompt_university_username(value: Optional[str] = None) -> str:
    username = value
    while not username or not username.strip():
        username = typer.prompt("University username")
        if not username.strip():
            console.print("[yellow]University username cannot be empty. Please try again.[/yellow]")
    return username.strip()


def _prompt_university_password() -> str:
    console.print("[dim]Password characters are hidden while you type.[/dim]")
    return typer.prompt("University Password", hide_input=True)


def _username_was_not_found(error: ShibbolethAuthError) -> bool:
    message = str(error).casefold()
    return any(
        phrase in message
        for phrase in (
            "benutzername wurde nicht gefunden",
            "username was not found",
            "username not found",
        )
    )


def _authenticate_user(
    config: AppConfig,
    username: Optional[str] = None,
    password: Optional[str] = None,
    university: Optional[str] = None,
    show_header: bool = True,
) -> list[dict[str, object]]:
    """Prompt for login and return authenticated session cookies."""
    setup_logger(config.general.log_level)
    if show_header:
        console.print(
            Panel.fit(
                "[bold blue]OPAL Bildungsportal[/bold blue]\n"
                "[dim]Sign in to continue[/dim]",
                title="OPAL Sync",
                border_style="cyan",
            )
        )

    if not university:
        university = typer.prompt(
            "Select university",
            default=config.auth.university or "htwk-leipzig",
        )
    config.auth.university = university.strip().lower()
    try:
        wayf_value = config.auth.wayf_value
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    config.auth.username = _prompt_university_username(username)
    if not password:
        password = _prompt_university_password()

    async def _do_login() -> tuple[bool, list[dict[str, object]]]:
        import httpx

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            success = await login_shibboleth(
                client=client,
                username=config.auth.username,
                password=password,
                wayf_value=wayf_value,
            )
            cookies = [
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "secure": cookie.secure,
                }
                for cookie in client.cookies.jar
            ]
            return success, cookies

    auth_logger = get_logger()
    previous_log_level = auth_logger.level
    auth_logger.setLevel("WARNING")
    try:
        while True:
            try:
                with console.status("[bold green]Contacting Shibboleth IdP and verifying credentials...[/bold green]"):
                    success, cookies = asyncio.run(_do_login())
                break
            except ShibbolethAuthError as e:
                console.print(f"\n[bold red]Authentication failed:[/bold red] {e}")
                if _username_was_not_found(e):
                    config.auth.username = _prompt_university_username()
                    password = _prompt_university_password()
                else:
                    password = _prompt_university_password()
            except Exception as e:
                console.print(f"\n[bold red]Network or connection error:[/bold red] {e}")
                raise typer.Exit(code=1)
    finally:
        auth_logger.setLevel(previous_log_level)

    if not success:
        console.print("\n[bold red]Authentication failed.[/bold red]")
        raise typer.Exit(code=1)

    store_credentials(config.auth.username, password)
    console.print("\n[bold green]✓ Login successful![/bold green]")
    console.print(
        f"Credentials saved securely in the OS credential store for user: "
        f"[cyan]{config.auth.username}[/cyan]"
    )
    return cookies


def _course_filters_from_positions(
    courses: list[Course], positions: list[int]
) -> tuple[list[str], list[str]]:
    included_names = [
        course.name for course, position in zip(courses, positions) if position == 1
    ]
    excluded_names = [
        course.name for course, position in zip(courses, positions) if position == -1
    ]
    return included_names, excluded_names


def _course_sync_states(positions: list[int]) -> list[bool]:
    has_include_filter = 1 in positions
    return [
        position == 1 or (position == 0 and not has_include_filter)
        for position in positions
    ]


def _select_course_filters(
    courses: list[Course],
    current_include: list[str],
    current_exclude: list[str],
) -> tuple[list[str], list[str]]:
    """Interactively place courses in excluded, unset, or included positions."""
    include_patterns = [pattern.casefold() for pattern in current_include]
    exclude_patterns = [pattern.casefold() for pattern in current_exclude]
    positions = []
    for course in courses:
        name = course.name.casefold()
        if any(pattern in name for pattern in exclude_patterns):
            positions.append(-1)
        elif any(pattern in name for pattern in include_patterns):
            positions.append(1)
        else:
            positions.append(0)
    selected_index = 0
    key_bindings = KeyBindings()

    def render_courses():
        fragments = [
            (
                "class:hint",
                "↑/↓ select   ← Exclude   Include →   Enter continue\n\n",
            )
        ]
        sync_states = _course_sync_states(positions)
        for index, course in enumerate(courses):
            position_value = positions[index]
            cursor = "›" if index == selected_index else " "
            indentation = (position_value + 1) * 4
            status = "✓ " if sync_states[index] else "× "
            color = "class:included" if sync_states[index] else "class:excluded"
            selected = " class:selected" if index == selected_index else ""
            fragments.append(
                (
                    color + selected,
                    f"{cursor} {' ' * indentation}{status}{course.name}\n",
                )
            )
        return fragments

    @key_bindings.add("up")
    def move_up(event):
        nonlocal selected_index
        selected_index = (selected_index - 1) % len(courses)
        event.app.invalidate()

    @key_bindings.add("down")
    def move_down(event):
        nonlocal selected_index
        selected_index = (selected_index + 1) % len(courses)
        event.app.invalidate()

    @key_bindings.add("left")
    def move_left(event):
        positions[selected_index] = max(-1, positions[selected_index] - 1)
        event.app.invalidate()

    @key_bindings.add("right")
    def move_right(event):
        positions[selected_index] = min(1, positions[selected_index] + 1)
        event.app.invalidate()

    @key_bindings.add("enter")
    def finish_selection(event):
        event.app.exit()

    @key_bindings.add("c-c")
    def abort_selection(event):
        event.app.exit(exception=KeyboardInterrupt())

    application = Application(
        layout=Layout(Window(FormattedTextControl(render_courses), wrap_lines=True)),
        key_bindings=key_bindings,
        style=Style.from_dict(
            {
                "hint": "ansigray",
                "included": "ansigreen",
                "excluded": "ansired",
                "selected": "bold",
            }
        ),
        full_screen=False,
        erase_when_done=False,
    )
    application.run()
    return _course_filters_from_positions(courses, positions)


def _normalize_path_input(value: str) -> Path:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    if os.name != "nt":
        value = value.replace("\\", "/")
    return Path(value).expanduser().resolve()


def _prompt_sync_directory(default_path: Path) -> Path:
    while True:
        path_input = typer.prompt(
            "Where should OPAL Sync save course folders and files?",
            default=str(default_path),
        )
        try:
            sync_directory = _normalize_path_input(path_input)
        except (OSError, RuntimeError, ValueError) as e:
            console.print(f"[yellow]That path could not be used: {e} Please try again.[/yellow]")
            continue

        if sync_directory.exists():
            if sync_directory.is_dir():
                return sync_directory
            console.print(
                f"[yellow]That path exists but is not a directory: "
                f"{sync_directory}. Please choose another path.[/yellow]"
            )
            continue

        if not typer.confirm(
            f"The directory does not exist: {sync_directory}. Create it?",
            default=True,
        ):
            continue

        try:
            sync_directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            console.print(
                f"[bold red]Could not create sync directory:[/bold red] "
                f"{sync_directory}\n{e}"
            )
            raise typer.Exit(code=1)
        return sync_directory


def _default_sync_directory() -> Path:
    current_directory = Path.cwd().resolve()
    home_directory = Path.home().resolve()
    if current_directory == home_directory:
        return home_directory / "opal_sync"
    return current_directory


@app.command()
def setup():
    """Log in, choose a sync directory and course filters, then optionally sync."""
    console.print(
        Panel(
            "[bold white]OPAL Sync[/bold white]\n"
            "[cyan]FIRST-TIME SETUP[/cyan]\n\n"
            "Sign in, choose where course files belong, and select your courses.\n\n" +
            "[dim]Values in brackets are \\[defaults]. Enter to accept.[/dim]",
            title="[bold cyan]OPAL Sync · Command Line[/bold cyan]",
            border_style="cyan",
            padding=(1, 3),
        )
    )
    config = load_config()
    console.rule("[bold cyan]1 · Sign in[/bold cyan]")
    cookies = _authenticate_user(config, show_header=False)

    console.rule("[bold cyan]2 · Choose a destination[/bold cyan]")
    sync_directory = _prompt_sync_directory(_default_sync_directory())

    config_path = sync_directory / "config.toml"
    if config_path.exists() and not typer.confirm(
        f"{config_path} already exists. Overwrite it?", default=False
    ):
        console.print("[yellow]Setup stopped. Your login credentials were saved.[/yellow]")
        raise typer.Exit()

    config.config_file_path = config_path
    config.general.sync_directory = sync_directory
    config.general.log_file = sync_directory / "opal-sync.log"
    config.general.db_file = sync_directory / "state.db"
    config.auth.session.cookie_file = sync_directory / "session.json"
    save_config(config, config_path)
    SessionManager(
        config.auth.session.cookie_file,
        config.auth.session.session_ttl_hours,
    ).save_session_cookies(cookies)

    engine = get_configured_engine(config_path)

    async def _fetch_courses() -> list[Course]:
        import httpx

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            await engine.ensure_authenticated(client)
            enrolled_courses = await fetch_my_courses(client)
            for course_item in enrolled_courses:
                engine.db.upsert_course(course_item)
            return enrolled_courses

    with console.status("[bold green]Loading your OPAL course list...[/bold green]"):
        enrolled_courses = asyncio.run(_fetch_courses())

    console.rule("[bold cyan]3 · Choose courses[/bold cyan]")
    if enrolled_courses:
        console.print(
            "[dim]Green courses will sync; red courses will not. "
            "When any course is included, any other courses are not synced.[/dim]"
        )
        config.courses.include, config.courses.exclude = _select_course_filters(
            enrolled_courses,
            config.courses.include,
            config.courses.exclude,
        )
    else:
        config.courses.include = []
        config.courses.exclude = []
        console.print("[yellow]No enrolled courses were found; no course filters were set.[/yellow]")

    save_config(config, config_path)
    console.rule("[bold cyan]4 · Review and sync[/bold cyan]")
    console.print(f"Course files and local data: [cyan]{sync_directory}[/cyan]")
    console.print(f"Config and additional settings: [cyan]{config_path}[/cyan]")

    initial_sync_error = False
    if typer.confirm("Run an initial sync now?", default=True):
        with console.status("[bold green]Running initial synchronization...[/bold green]"):
            summary = asyncio.run(engine.run_sync())
        console.print(
            f"Initial sync {summary.status}: {summary.courses_processed} courses, "
            f"{summary.files_new} new files, {summary.files_updated} updated files, "
            f"{summary.files_failed} failures."
        )
        if summary.status != "success":
            initial_sync_error = True
            console.print(
                f"[bold red]{summary.error_message or 'Initial sync did not complete successfully.'}[/bold red]"
            )

    console.rule("[bold cyan]All set[/bold cyan]")
    console.print(
        Panel(
            "[bold]Run a sync whenever you need[/bold]\n\n"
            f"[cyan]From the sync directory[/cyan]  [dim]({sync_directory})[/dim]\n"
            "  [bold]opal_sync sync[/bold]\n\n"
            "[cyan]From anywhere[/cyan]\n"
            f'  [bold]opal_sync sync --config "{config_path}"[/bold]\n\n'
            f"More settings and course filters: [dim]{config_path}[/dim]",
            title="[bold cyan]Your sync commands[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    if initial_sync_error:
        raise typer.Exit(code=1)


@app.command()
def sync(
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Simulate sync without downloading files"),
    course: Optional[str] = typer.Option(None, "--course", help="Sync only a specific course (name or ID)"),
    force: bool = typer.Option(False, "--force", "-f", help="Re-download files even if unchanged"),
    list_skipped_files: bool = typer.Option(
        False,
        "--list-skipped-files",
        help="List files skipped due to extension filters or the size limit",
    ),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Custom config file path"
    ),
):
    """Synchronize course files from OPAL to local disk."""
    expected_config_path = Path(
        config_path or (Path.cwd() / DEFAULT_CONFIG_PATH)
    ).expanduser().resolve()
    if not expected_config_path.is_file():
        console.print(
            f"[bold red]Error:[/bold red] No config file found at "
            f"[cyan]{expected_config_path}[/cyan]."
        )
        _show_setup_hint()
        raise typer.Exit(code=1)

    engine = get_configured_engine(config_path)

    title = "[bold yellow]Starting OPAL Dry-Run Sync...[/bold yellow]" if dry_run else "[bold green]Starting OPAL Synchronization...[/bold green]"
    console.print(Panel.fit(title))
    console.print(f"Target directory: [cyan]{engine.config.general.sync_directory}[/cyan]\n")

    summary = asyncio.run(
        engine.run_sync(
            dry_run=dry_run,
            course_filter=course,
            force_download=force,
        )
    )

    if list_skipped_files:
        skipped_items = [
            item
            for item in summary.items
            if item.action in (SyncAction.SKIP_EXCLUDED, SyncAction.SKIP_TOO_LARGE)
        ]
        console.print("\n[bold]Skipped files:[/bold]")
        if skipped_items:
            for item in skipped_items:
                reason = (
                    "extension"
                    if item.action == SyncAction.SKIP_EXCLUDED
                    else "size"
                )
                line = Text(f"- {item.course_name}/{item.node_title}/")
                line.append(item.filename, style="cyan")
                line.append(f" ({reason}: {item.details})")
                console.print(line)
        else:
            console.print("None")

    # Print summary table
    table = Table(title="Synchronization Summary", header_style="bold blue")
    table.add_column("Metric", style="bold")
    table.add_column("Value", style="cyan")

    table.add_row("Status", f"[green]{summary.status.upper()}[/green]" if summary.status == "success" else f"[red]{summary.status.upper()}[/red]")
    table.add_row("Courses Processed", str(summary.courses_processed))
    table.add_row("New Files", str(summary.files_new))
    table.add_row("Updated Files", str(summary.files_updated))
    table.add_row("Excluded Files", str(summary.files_excluded))
    table.add_row("Too Large Files", str(summary.files_too_large))
    if summary.files_excluded:
        extensions = ", ".join(
            f"{extension} ({count})"
            for extension, count in sorted(summary.excluded_extensions.items())
        ) or "Unknown"
        table.add_row("Excluded Extensions", extensions)
    if summary.files_too_large:
        excluded_mb = summary.excluded_bytes / (1024 * 1024)
        table.add_row("Excluded Data", f"{excluded_mb:.2f} MB")
    table.add_row("Failed Files", str(summary.files_failed))
    table.add_row(
        "Removed Files",
        str(sum(change.prefix == "-" for change in summary.changes)),
    )
    mb = summary.bytes_downloaded / (1024 * 1024)
    table.add_row("Data Downloaded", f"{mb:.2f} MB")
    table.add_row("Total Sync Time", f"{summary.duration_seconds:.2f} seconds")

    console.print("\n", table)
    change_styles = {"+": "green", "~": "blue", "-": "red"}
    if summary.changes:
        console.print("\n[bold]File changes:[/bold]")
    for change in summary.changes:
        console.print(
            Text(
                f"{change.prefix} {change.path}",
                style=change_styles[change.prefix],
            )
        )
    if summary.error_message:
        console.print(
            Panel(
                summary.error_message,
                title="[bold red]Sync failed[/bold red]",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)


@app.command()
def courses(
    refresh: bool = typer.Option(False, "--refresh", "-r", help="Fetch live course list from OPAL"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Custom config file path"
    ),
):
    """List enrolled courses and their synchronization status."""
    engine = get_configured_engine(config_path)

    async def _fetch_and_store() -> list:
        import httpx
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            await engine.ensure_authenticated(client)
            courses_list = await fetch_my_courses(client)
            for c in courses_list:
                engine.db.upsert_course(c)
            return engine.db.get_courses()

    course_items = []
    if refresh:
        with console.status("[bold green]Connecting to OPAL and refreshing course catalog...[/bold green]"):
            course_items = asyncio.run(_fetch_and_store())
    else:
        course_items = engine.db.get_courses()
        if not course_items:
            console.print("[yellow]No courses cached in local database. Fetching live from OPAL...[/yellow]")
            with console.status("[bold green]Fetching courses...[/bold green]"):
                course_items = asyncio.run(_fetch_and_store())

    if not course_items:
        console.print("[yellow]No enrolled courses found.[/yellow]")
        return

    table = Table(title=f"OPAL Enrolled Courses ({len(course_items)})", header_style="bold cyan")
    table.add_column("OPAL ID", style="dim", width=12)
    table.add_column("Course Name", style="bold")
    table.add_column("Semester", style="magenta")
    table.add_column("Last Synced", style="green")
    table.add_column("Status", style="yellow")

    for c in course_items:
        synced_str = c.last_synced_at.strftime("%Y-%m-%d %H:%M") if c.last_synced_at else "Never"
        status = "[red]Excluded[/red]" if c.is_excluded else "[green]Active[/green]"
        table.add_row(c.opal_id, c.name, c.semester or "—", synced_str, status)

    console.print(table)


@app.command()
def status(
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Custom config file path"
    ),
):
    """Display overall synchronization statistics and system status."""
    engine = get_configured_engine(config_path)
    stats = engine.db.get_stats()

    table = Table(title="OPAL Sync System Status", header_style="bold magenta")
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("Configured University", engine.config.auth.university)
    table.add_row("Username", engine.config.auth.username or "[italic red]Not set[/italic red]")

    if engine.config.auth.password:
        pass_status = "[green]Saved (Config)[/green]"
    else:
        has_keyring = bool(get_credentials(engine.config.auth.username)) if engine.config.auth.username else False
        pass_status = "[green]Saved (Keyring)[/green]" if has_keyring else "[red]Missing[/red]"
    table.add_row("Password Status", pass_status)

    sess_file = engine.config.auth.session.cookie_file
    table.add_row("Session Cache", f"[green]Present ({sess_file.name})[/green]" if sess_file.is_file() else "[dim]None[/dim]")

    table.add_row("Sync Directory", str(engine.config.general.sync_directory))
    table.add_row("Total Courses Tracked", str(stats["total_courses"]))
    table.add_row("Total Files Tracked", str(stats["total_files"]))
    total_mb = stats["total_bytes"] / (1024 * 1024)
    table.add_row("Total Downloaded Size", f"{total_mb:.2f} MB")

    last_run = stats.get("last_run")
    if last_run:
        table.add_row("Last Sync Run", f"{last_run['started_at']} ({last_run['status']})")
    else:
        table.add_row("Last Sync Run", "Never")

    console.print(table)


@app.command()
def login(
    username: Optional[str] = typer.Option(None, "--username", "-u", help="University username"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="Password for university login"),
    university: Optional[str] = typer.Option(None, "--university", help="University key (e.g. htwk-leipzig)"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Custom config file path"
    ),
):
    """Authenticate with OPAL via Shibboleth and store credentials securely."""
    config = load_config(config_path)
    setup_logger(config.general.log_level)

    console.print(Panel.fit("[bold blue]OPAL Bildungsportal — Shibboleth Authentication[/bold blue]"))

    # Determine university
    if not university:
        default_uni = config.auth.university or "htwk-leipzig"
        university = typer.prompt("Select university", default=default_uni)
    config.auth.university = university.strip().lower()

    try:
        wayf_val = config.auth.wayf_value
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(code=1)

    config.auth.username = _prompt_university_username(username)

    # Determine password
    if not password:
        password = _prompt_university_password()

    async def _do_login() -> bool:
        import httpx
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            return await login_shibboleth(
                client=client,
                username=config.auth.username,
                password=password,
                wayf_value=wayf_val,
            )

    while True:
        try:
            with console.status("[bold green]Contacting Shibboleth IdP and verifying credentials...[/bold green]"):
                success = asyncio.run(_do_login())
            break
        except ShibbolethAuthError as e:
            console.print(f"\n[bold red]Authentication failed:[/bold red] {e}")
            if _username_was_not_found(e):
                config.auth.username = _prompt_university_username()
                password = _prompt_university_password()
            else:
                password = _prompt_university_password()
        except Exception as e:
            console.print(f"\n[bold red]Network or connection error:[/bold red] {e}")
            raise typer.Exit(code=1)

    if success:
        # Save password to OS keyring
        store_credentials(config.auth.username, password)
        # Save config
        saved_path = save_config(config, config_path)

        console.print("\n[bold green]✓ Login successful![/bold green]")
        console.print(f"Credentials saved securely in OS Credential Manager for user: [cyan]{config.auth.username}[/cyan]")
        console.print(f"Configuration saved to: [italic]{saved_path}[/italic]\n")


@app.command()
def logout(
    username: Optional[str] = typer.Option(None, "--username", "-u", help="Username to clear credentials for"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Custom config file path"
    ),
):
    """Remove stored credentials and clear cached session cookies."""
    config = load_config(config_path)
    user = username or config.auth.username

    if user:
        delete_credentials(user)
        console.print(f"[green]✓ Removed keyring credentials for user '{user}'.[/green]")

    session_mgr = SessionManager(config.auth.session.cookie_file)
    session_mgr.clear_session()
    console.print("[green]✓ Cleared local session cookies.[/green]")


@app.command()
def log(
    limit: int = typer.Option(30, "--limit", "-l", help="Number of log records to show"),
    errors: bool = typer.Option(False, "--errors", "-e", help="Show only errors and warnings"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Custom config file path"
    ),
):
    """Display recent sync log records and error events."""
    engine = get_configured_engine(config_path)
    logs = engine.db.get_recent_logs(limit=limit, only_errors=errors)

    if not logs:
        console.print("[dim]No sync logs found in database.[/dim]")
        return

    table = Table(title=f"Recent Sync Activity (Last {len(logs)})", header_style="bold cyan")
    table.add_column("Timestamp", style="dim", width=19)
    table.add_column("Level", width=8)
    table.add_column("Action", style="bold", width=16)
    table.add_column("Course", style="cyan")
    table.add_column("Details")

    for entry in logs:
        lvl = entry["level"].upper()
        lvl_colored = f"[red]{lvl}[/red]" if lvl == "ERROR" else f"[yellow]{lvl}[/yellow]" if lvl == "WARNING" else f"[green]{lvl}[/green]"
        table.add_row(
            entry["timestamp"],
            lvl_colored,
            entry["action"],
            entry.get("course_name") or "—",
            entry.get("details") or entry.get("file_path") or "—",
        )

    console.print(table)


@app.command("config")
def show_config(
    show: bool = typer.Option(True, "--show", help="Display active configuration"),
    path: bool = typer.Option(False, "--path", help="Print configuration file path"),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Custom config file path"
    ),
):
    """View or inspect configuration settings."""
    cfg = load_config(config_path)
    if path:
        console.print(str(cfg.config_file_path))
        return

    from rich.syntax import Syntax
    if cfg.config_file_path and cfg.config_file_path.is_file():
        with open(cfg.config_file_path, "r", encoding="utf-8") as f:
            syntax = Syntax(f.read(), "toml", theme="monokai", line_numbers=True)
            console.print(syntax)
    else:
        console.print(f"[yellow]No config file found at {cfg.config_file_path}. Showing in-memory defaults:[/yellow]")
        import tomli_w
        raw_toml = tomli_w.dumps({
            "general": {"sync_directory": str(cfg.general.sync_directory), "log_level": cfg.general.log_level},
            "auth": {"university": cfg.auth.university, "username": cfg.auth.username},
            "sync": {
                "include_extensions": cfg.sync.include_extensions,
                "exclude_extensions": cfg.sync.exclude_extensions,
                "max_file_size_mb": cfg.sync.max_file_size_mb,
                "parallel_downloads": cfg.sync.parallel_downloads,
                "request_delay_seconds": cfg.sync.request_delay_seconds,
                "delete_removed": cfg.sync.delete_removed,
            },
        })
        console.print(Syntax(raw_toml, "toml", theme="monokai"))
