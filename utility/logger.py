import os
import logging
import sys
from datetime import datetime
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from colorama import init, Fore, Back, Style

init(autoreset=True)

console = Console()

class CustomFormatter(logging.Formatter):
    """Custom formatter with colors for different log levels"""
    
    COLORS = {
        'DEBUG': Fore.CYAN,
        'INFO': Fore.GREEN,
        'WARNING': Fore.YELLOW,
        'ERROR': Fore.RED,
        'CRITICAL': Fore.RED + Back.WHITE
    }
    
    def format(self, record):
        log_color = self.COLORS.get(record.levelname, '')
        record.levelname = f"{log_color}{record.levelname}{Style.RESET_ALL}"
        record.msg = f"{log_color}{record.msg}{Style.RESET_ALL}"
        return super().format(record)

class MigrationLogger:
    def __init__(self, log_file='migration.log', console_level=logging.INFO):
        self.logger = logging.getLogger('migration')
        self.logger.setLevel(logging.DEBUG)
        
        if self.logger.handlers:
            self.logger.handlers.clear()
        
        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        log_path = os.path.join(script_dir, log_file)
        
        file_handler = logging.FileHandler(log_path, mode='w')
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        
        console_handler = RichHandler(
            console=console,
            show_path=False,
            markup=True,
            rich_tracebacks=True
        )
        console_handler.setLevel(console_level)
        
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
        
        self.progress = None
        self.current_task = None
    
    def debug(self, message):
        self.logger.debug(message)
    
    def info(self, message):
        self.logger.info(message)
    
    def warning(self, message):
        self.logger.warning(message)
    
    def error(self, message):
        self.logger.error(message)
    
    def critical(self, message):
        self.logger.critical(message)
    
    def success(self, message):
        """Print success message with green checkmark"""
        console.print(f"[green]✓[/green] {message}")
        self.logger.info(f"SUCCESS: {message}")
    
    def failure(self, message):
        """Print failure message with red X"""
        console.print(f"[red]✗[/red] {message}")
        self.logger.error(f"FAILURE: {message}")
    
    def section(self, title):
        """Print a section header"""
        console.print()
        console.print(Panel(f"[bold cyan]{title}[/bold cyan]", expand=False))
        self.logger.info(f"=== {title} ===")
    
    def subsection(self, title):
        """Print a subsection header"""
        console.print(f"\n[bold yellow]→ {title}[/bold yellow]")
        self.logger.info(f"--- {title} ---")
    
    def print_stats(self, title, stats_dict):
        """Print statistics in a nice table format"""
        table = Table(title=title, show_header=True, header_style="bold cyan")
        table.add_column("Metric", style="cyan", no_wrap=True)
        table.add_column("Value", style="green")
        
        for key, value in stats_dict.items():
            table.add_row(key, str(value))
        
        console.print(table)
        self.logger.info(f"Stats - {title}: {stats_dict}")
    
    def print_summary(self, items, title="Summary"):
        """Print a summary list"""
        console.print(f"\n[bold]{title}:[/bold]")
        for item in items:
            console.print(f"  • {item}")
        self.logger.info(f"{title}: {items}")
    
    def start_progress(self, total, description="Processing"):
        """Start a progress bar"""
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console
        )
        self.progress.start()
        self.current_task = self.progress.add_task(description, total=total)
        return self.progress, self.current_task
    
    def update_progress(self, advance=1, description=None):
        """Update the progress bar"""
        if self.progress and self.current_task is not None:
            if description:
                self.progress.update(self.current_task, description=description)
            self.progress.update(self.current_task, advance=advance)
    
    def stop_progress(self):
        """Stop the progress bar"""
        if self.progress:
            self.progress.stop()
            self.progress = None
            self.current_task = None
    
    def print_json(self, data, title="JSON Data"):
        """Pretty print JSON data"""
        from rich.json import JSON
        console.print(Panel(JSON.from_data(data), title=title, expand=False))
        self.logger.debug(f"{title}: {data}")
    
    def print_exception(self, exc, context=""):
        """Print exception with full traceback"""
        import traceback
        console.print(f"[red]Exception in {context}:[/red]")
        console.print_exception()
        self.logger.exception(f"Exception in {context}: {exc}")

_logger_instance = None

def get_logger():
    """Get or create singleton logger instance"""
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = MigrationLogger()
    return _logger_instance

def log(message, level='info'):
    """Backward compatible log function"""
    logger = get_logger()
    getattr(logger, level.lower())(message)

def log_success(message):
    """Log success message"""
    get_logger().success(message)

def log_error(message):
    """Log error message"""
    get_logger().error(message)

def log_warning(message):
    """Log warning message"""
    get_logger().warning(message)

def log_section(title):
    """Log section header"""
    get_logger().section(title)

def log_subsection(title):
    """Log subsection header"""
    get_logger().subsection(title)

def log_stats(title, stats):
    """Log statistics"""
    get_logger().print_stats(title, stats)