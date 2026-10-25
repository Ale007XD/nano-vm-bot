"""
Logging configuration
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from pythonjsonlogger import jsonlogger

from app.core.config import settings


def setup_logging():
    """Setup application logging"""
    
    # Create logs directory
    os.makedirs(os.path.dirname(settings.LOG_FILE), exist_ok=True)
    
    # Configure root logger
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, settings.LOG_LEVEL.upper()))
    
    # Clear existing handlers
    logger.handlers = []
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    # File handler with rotation
    file_handler = RotatingFileHandler(
        settings.LOG_FILE,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    
    # JSON formatter for structured logging
    json_formatter = jsonlogger.JsonFormatter(
        '%(asctime)s %(name)s %(levelname)s %(message)s',
        rename_fields={'asctime': 'timestamp', 'levelname': 'level'}
    )
    file_handler.setFormatter(json_formatter)
    logger.addHandler(file_handler)
    
    # Reduce noise from third-party libraries
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('chromadb').setLevel(logging.WARNING)
    # ChromaDB PostHog telemetry bug: capture() signature mismatch
    # anonymized_telemetry=False в PersistentClient не успевает до инициализации
    # модуля posthog — глушим на уровне логгера
    logging.getLogger('chromadb.telemetry').setLevel(logging.CRITICAL)
    logging.getLogger('chromadb.telemetry.product.posthog').setLevel(logging.CRITICAL)
    
    return logger
