"""Logging configuration for Tether."""
from __future__ import annotations

import logging
import sys

from .config import CONFIG_DIR

# Create logs directory
LOG_DIR = CONFIG_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Log file path
LOG_FILE = LOG_DIR / "tether.log"

# Logging format
DETAILED_FORMAT = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

SIMPLE_FORMAT = logging.Formatter(
    fmt="%(levelname)s: %(message)s"
)

def setup_logging(level: int = logging.INFO) -> None:
    """Set up logging for the Tether application.
    
    Args:
        level: Logging level (default: INFO)
    """
    # Get the root logger
    logger = logging.getLogger("tether")
    logger.setLevel(level)
    
    # Prevent duplicate handlers if setup_logging is called multiple times
    if logger.handlers:
        return
    
    # File handler - detailed logging
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(DETAILED_FORMAT)
    logger.addHandler(file_handler)
    
    # Console handler — only surface warnings/errors to the terminal so the
    # startup screen and live output stay clean. Full INFO/DEBUG still lands in
    # the log file via the file handler above.
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(SIMPLE_FORMAT)
    logger.addHandler(console_handler)

    # Log the startup (file only)
    logger.info(f"Logging initialized. Log file: {LOG_FILE}")

def get_logger(name: str) -> logging.Logger:
    """Get a logger for a specific module.
    
    Args:
        name: Usually __name__ from the calling module
        
    Returns:
        Configured logger instance
    """
    return logging.getLogger(f"tether.{name}")