"""
Alex-Nano-Bot Configuration Module
"""
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""
    
    # Bot Configuration
    BOT_TOKEN: str = Field(..., description="Telegram Bot Token")
    ADMIN_IDS: List[int] = Field(default=[], description="List of admin Telegram IDs")

    @field_validator("ADMIN_IDS", mode="before")
    def parse_admin_ids(cls, v):
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            if v.strip() == "":
                return []
            return [int(x.strip()) for x in v.split(",")]
        return v

    # Knowledge Base channels
    KB_CHANNEL_IDS: List[int] = Field(
        default=[],
        description="Telegram channel IDs to monitor for articles (e.g. -1001234567890)"
    )

    @field_validator("KB_CHANNEL_IDS", mode="before")
    def parse_kb_channel_ids(cls, v):
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            if v.strip() == "":
                return []
            return [int(x.strip()) for x in v.split(",")]
        return v

    KB_STALE_DAYS: int = Field(
        default=30,
        description="Days after which an article is considered stale and eligible for refresh"
    )
    
    # Database
    DATABASE_URL: str = Field(
        default="sqlite+aiosqlite:///data/bot.db",
        description="Database connection string"
    )
    
    # Vector Store
    VECTOR_STORE_PATH: str = Field(
        default="data/vector_store",
        description="Path to vector store directory"
    )
    EMBEDDING_MODEL: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        description="Model for text embeddings"
    )
    
    # LLM Configuration (OpenRouter) - используется как fallback
    OPENROUTER_API_KEY: str = Field(..., description="OpenRouter API Key")
    OPENROUTER_BASE_URL: str = Field(
        default="https://openrouter.ai/api/v1",
        description="OpenRouter API base URL"
    )
    
    # Groq API (primary provider)
    GROQ_API_KEY: str = Field(
        default="",
        description="Groq API Key (если пусто, используется OPENROUTER_API_KEY)"
    )
    GROQ_BASE_URL: str = Field(
        default="https://api.groq.com/openai/v1",
        description="Groq API base URL"
    )
    
    # Anthropic API (optional)
    ANTHROPIC_API_KEY: str = Field(
        default="",
        description="Anthropic API Key (optional)"
    )
    
    # OpenAI API (optional)
    OPENAI_API_KEY: str = Field(
        default="",
        description="OpenAI API Key (optional)"
    )
    
    # Web Search APIs (optional)
    ENABLE_WEB_SEARCH: bool = Field(
        default=True,
        description="Enable web search functionality"
    )
    SERPER_API_KEY: str = Field(
        default="",
        description="Serper.dev API key for Google search (recommended)"
    )
    SERPAPI_KEY: str = Field(
        default="",
        description="SerpAPI key for Google search (optional)"
    )
    BING_API_KEY: str = Field(
        default="",
        description="Bing Search API key (optional)"
    )
    WEB_SEARCH_RESULTS: int = Field(
        default=3,
        description="Default number of search results"
    )
    
    # Multi-Provider Settings
    ENABLE_MULTI_PROVIDER: bool = Field(
        default=True,
        description="Enable automatic fallback between providers"
    )
    MAX_PROVIDER_RETRIES: int = Field(
        default=3,
        description="Maximum retries per provider"
    )
    PROVIDER_HEALTH_CHECK_INTERVAL: int = Field(
        default=60,
        description="Health check interval in seconds"
    )
    
    # Agent Models
    FASTBOT_MODEL: str = Field(
        default="llama-3.1-8b-instant",
        description="Model for FastBot (conversations/RAG)"
    )
    VISION_MODEL: str = Field(
        default="meta-llama/llama-4-scout-17b-16e-instruct",
        description="Model for vision tasks"
    )
    SKILLBOT_MODEL: str = Field(
        default="llama-3.1-8b-instant",
        description="Model for SkillBot (code/skills)"
    )
    PLANBOT_MODEL: str = Field(
        default="mixtral-8x7b-32768",
        description="Model for PlanBot (DAG planning/verification)"
    )

    # Agent System Prompts
    FASTBOT_SYSTEM_PROMPT: str = Field(
        default="You are a fast assistant...",
        description="System prompt for FastBot"
    )
    PLANBOT_SYSTEM_PROMPT: str = Field(
        default="You are a planning agent...",
        description="System prompt for PlanBot"
    )
    SKILLBOT_SYSTEM_PROMPT: str = Field(
        default="You manage skills...",
        description="System prompt for SkillBot"
    )
    
    # Model Parameters
    MAX_TOKENS: int = Field(default=2048, description="Maximum tokens per response")
    TEMPERATURE: float = Field(default=0.7, description="Sampling temperature")
    
    # Skills
    SKILLS_DIR: str = Field(default="skills", description="Skills directory")
    MAX_SKILL_FILE_SIZE: int = Field(default=1048576, description="Max skill file size in bytes")
    
    # Logging
    LOG_LEVEL: str = Field(default="INFO", description="Logging level")
    LOG_FILE: str = Field(default="logs/bot.log", description="Log file path")

    # Timezone
    BOT_TIMEZONE: str = Field(
        default="Europe/Moscow",
        description=(
            "Timezone for scheduler, reminders, and display. "
            "Use IANA names: Europe/Moscow, Asia/Irkutsk, UTC, etc. "
            "This is the SINGLE source of truth — replaces hardcoded 'Europe/Moscow' "
            "scattered across scheduler.py and reminders.py."
        )
    )
    
    # Encryption (for provider key storage)
    ENCRYPTION_KEY: str = Field(
        default="",
        description="Fernet key for encrypting provider API keys in DB"
    )

    # Application
    APP_NAME: str = Field(default="Alex-Nano-Bot", description="Application name")
    APP_VERSION: str = Field(default="1.5.0", description="Application version")
    
    # Voice Processing
    TEMP_DIR: str = Field(default="data/temp", description="Temporary files directory")
    MAX_VOICE_DURATION: int = Field(default=300, description="Maximum voice message duration in seconds")
    WHISPER_MODEL: str = Field(default="whisper-large-v3", description="Whisper model for transcription")
    ENABLE_VOICE_IN_GROUPS: bool = Field(default=True, description="Enable voice processing in groups")
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Global settings instance
settings = Settings()
