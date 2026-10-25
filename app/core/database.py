"""
Database models and connection management
"""
from datetime import datetime, timezone
from typing import AsyncGenerator, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

from app.core.config import settings

Base = declarative_base()


class User(Base):
    """Telegram users table"""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False, index=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    language_code = Column(String(10), nullable=True)
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    is_banned = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
    # Relationships
    messages = relationship("Message", back_populates="user", cascade="all, delete-orphan")
    memories = relationship("Memory", back_populates="user", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<User(id={self.id}, telegram_id={self.telegram_id}, username={self.username})>"


class Message(Base):
    """Chat messages table"""
    __tablename__ = "messages"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    role = Column(String(20), nullable=False)  # user, assistant, system
    content = Column(Text, nullable=False)
    agent_mode = Column(String(50), nullable=True)  # fastbot, planbot, skillbot
    extra_data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=func.now())

    # Relationships
    user = relationship("User", back_populates="messages")

    def __repr__(self):
        return f"<Message(id={self.id}, user_id={self.user_id}, role={self.role})>"


class Skill(Base):
    """Skills/abilities table"""
    __tablename__ = "skills"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    category = Column(String(100), nullable=True)
    source = Column(String(50), nullable=False)  # system, custom, external
    file_path = Column(String(500), nullable=True)
    code = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    extra_data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    def __repr__(self):
        return f"<Skill(id={self.id}, name={self.name}, source={self.source})>"


class Memory(Base):
    """User memories/notes table"""
    __tablename__ = "memories"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    memory_type = Column(String(50), nullable=False)  # note, trip, budget, plan, dialog
    extra_data = Column(JSON, nullable=True)
    vector_id = Column(String(255), nullable=True)  # Reference to vector store
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Relationships
    user = relationship("User", back_populates="memories")

    def __repr__(self):
        return f"<Memory(id={self.id}, user_id={self.user_id}, type={self.memory_type})>"


class UserState(Base):
    """User session states table"""
    __tablename__ = "user_states"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    current_agent = Column(String(50), default="fastbot")
    current_skill = Column(String(255), nullable=True)
    context = Column(JSON, nullable=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class UsageDaily(Base):
    """Расход токенов LLM по пользователям за UTC-сутки"""
    __tablename__ = "usage_daily"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    date = Column(String(10), primary_key=True)  # YYYY-MM-DD (UTC)
    prompt_tokens = Column(Integer, default=0, nullable=False)
    completion_tokens = Column(Integer, default=0, nullable=False)
    total_tokens = Column(Integer, default=0, nullable=False)
    requests = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class BotSetting(Base):
    """Глобальные настройки бота, изменяемые на лету (key-value)"""
    __tablename__ = "bot_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_by = Column(Integer, nullable=True)  # telegram_id админа
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class ScheduledTask(Base):
    """Scheduled tasks/reminders table"""
    __tablename__ = "scheduled_tasks"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(255), nullable=True)
    description = Column(Text, nullable=False)
    task_type = Column(String(50), nullable=False, default="reminder")  # reminder, recurring, one_time
    
    # Schedule settings
    cron_expression = Column(String(100), nullable=True)  # For recurring tasks
    run_date = Column(DateTime, nullable=True)  # For one-time tasks
    timezone = Column(String(50), default="UTC")
    
    # Task settings
    message_text = Column(Text, nullable=True)  # Message to send
    agent_mode = Column(String(50), default="fastbot")  # Which agent to use
    
    # Status
    is_active = Column(Boolean, default=True)
    is_completed = Column(Boolean, default=False)
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)
    run_count = Column(Integer, default=0)
    max_runs = Column(Integer, nullable=True)  # For limiting recurring tasks
    
    # Error tracking
    error_count = Column(Integer, default=0)
    last_error = Column(Text, nullable=True)
    
    # Metadata
    extra_data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
    def __repr__(self):
        return f"<ScheduledTask(id={self.id}, user_id={self.user_id}, type={self.task_type}, active={self.is_active})>"


# Database engine and session
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)


async def init_db():
    """Initialize database tables"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all не добавляет новые колонки к существующим таблицам —
        # для SQLite дополняем вручную
        if engine.dialect.name == "sqlite":
            from sqlalchemy import text

            def _ensure_columns(sync_conn):
                cols = {
                    row[1]
                    for row in sync_conn.execute(text("PRAGMA table_info(users)"))
                }
                if "is_banned" not in cols:
                    sync_conn.execute(
                        text("ALTER TABLE users ADD COLUMN is_banned BOOLEAN NOT NULL DEFAULT 0")
                    )

            await conn.run_sync(_ensure_columns)


async def is_user_banned(telegram_id: int) -> bool:
    async with async_session_maker() as session:
        result = await session.execute(
            select(User.is_banned).where(User.telegram_id == telegram_id)
        )
        row = result.scalar_one_or_none()
        return bool(row)


async def set_user_banned(telegram_id: int, banned: bool) -> None:
    async with async_session_maker() as session:
        user = (
            await session.execute(
                select(User).where(User.telegram_id == telegram_id)
            )
        ).scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id, is_banned=banned)
            session.add(user)
        else:
            user.is_banned = banned
        await session.commit()


async def count_today_messages(telegram_id: int) -> int:
    """Сообщения пользователя за текущие UTC-сутки (created_at в SQLite — UTC)."""
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    async with async_session_maker() as session:
        result = await session.execute(
            select(func.count(Message.id))
            .join(User, Message.user_id == User.id)
            .where(
                User.telegram_id == telegram_id,
                Message.role == "user",
                Message.created_at >= start,
            )
        )
        return result.scalar() or 0


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def add_usage_daily(telegram_id: int, usage: dict) -> None:
    """Прибавить расход токенов пользователя за текущие UTC-сутки (UPSERT)."""
    date_str = _today_str()
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    requests = int(usage.get("requests") or 0)
    if not (prompt or completion or total or requests):
        return

    async with async_session_maker() as session:
        user = (
            await session.execute(select(User).where(User.telegram_id == telegram_id))
        ).scalar_one_or_none()
        if user is None:
            return

        result = await session.execute(
            update(UsageDaily)
            .where(UsageDaily.user_id == user.id, UsageDaily.date == date_str)
            .values(
                prompt_tokens=UsageDaily.prompt_tokens + prompt,
                completion_tokens=UsageDaily.completion_tokens + completion,
                total_tokens=UsageDaily.total_tokens + total,
                requests=UsageDaily.requests + requests,
            )
        )
        if result.rowcount == 0:
            session.add(
                UsageDaily(
                    user_id=user.id,
                    date=date_str,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    total_tokens=total,
                    requests=requests,
                )
            )
        await session.commit()


async def get_today_usage(telegram_id: int) -> Optional[UsageDaily]:
    """Расход токенов пользователя за текущие UTC-сутки."""
    async with async_session_maker() as session:
        result = await session.execute(
            select(UsageDaily)
            .join(User, UsageDaily.user_id == User.id)
            .where(User.telegram_id == telegram_id, UsageDaily.date == _today_str())
        )
        return result.scalar_one_or_none()


async def get_today_totals() -> dict:
    """Суммарный расход токенов всех пользователей за текущие UTC-сутки."""
    async with async_session_maker() as session:
        result = await session.execute(
            select(
                func.sum(UsageDaily.prompt_tokens),
                func.sum(UsageDaily.completion_tokens),
                func.sum(UsageDaily.total_tokens),
                func.sum(UsageDaily.requests),
                func.count(UsageDaily.user_id),
            ).where(UsageDaily.date == _today_str())
        )
        row = result.one()
        return {
            "prompt_tokens": row[0] or 0,
            "completion_tokens": row[1] or 0,
            "total_tokens": row[2] or 0,
            "requests": row[3] or 0,
            "users": row[4] or 0,
        }


async def get_bot_setting(key: str) -> Optional[str]:
    """Значение динамической настройки бота (None — не задано)."""
    async with async_session_maker() as session:
        result = await session.execute(
            select(BotSetting.value).where(BotSetting.key == key)
        )
        return result.scalar_one_or_none()


async def set_bot_setting(key: str, value: str, updated_by: Optional[int] = None) -> None:
    async with async_session_maker() as session:
        row = (
            await session.execute(select(BotSetting).where(BotSetting.key == key))
        ).scalar_one_or_none()
        if row is None:
            row = BotSetting(key=key, value=value, updated_by=updated_by)
            session.add(row)
        else:
            row.value = value
            row.updated_by = updated_by
        await session.commit()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting database session"""
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    language_code: Optional[str] = None
) -> User:
    """Get existing user or create new one"""
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        is_admin = telegram_id in settings.ADMIN_IDS
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language_code=language_code,
            is_admin=is_admin
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    
    return user


async def save_message(
    session: AsyncSession,
    user_id: int,
    role: str,
    content: str,
    agent_mode: Optional[str] = None,
    extra_data: Optional[dict] = None
) -> Message:
    """Save message to database"""
    message = Message(
        user_id=user_id,
        role=role,
        content=content,
        agent_mode=agent_mode,
        extra_data=extra_data
    )
    session.add(message)
    await session.commit()
    await session.refresh(message)
    return message


async def get_user_messages(
    session: AsyncSession,
    user_id: int,
    limit: int = 50
) -> List[Message]:
    """Get recent messages for user"""
    result = await session.execute(
        select(Message)
        .where(Message.user_id == user_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


class ProviderConfig(Base):
    """
    Hot-swappable LLM provider configuration stored in DB.
    Overrides .env values when present.
    Keys are stored encrypted (see app/core/crypto.py).
    """
    __tablename__ = "provider_configs"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), unique=True, nullable=False, index=True)  # groq, openrouter, etc.
    encrypted_key = Column(Text, nullable=True)   # Fernet-encrypted API key
    priority = Column(Integer, default=99)         # Lower = higher priority
    is_enabled = Column(Boolean, default=True)
    role_models = Column(JSON, nullable=True)      # {"default": "model", "coder": "model", "planner": "model"}
    updated_by = Column(Integer, nullable=True)    # telegram_id of admin who changed it
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    created_at = Column(DateTime, default=func.now())

    def __repr__(self):
        return f"<ProviderConfig(name={self.name}, priority={self.priority}, enabled={self.is_enabled})>"


async def get_provider_config(session: AsyncSession, name: str) -> Optional["ProviderConfig"]:
    """Get provider config by name"""
    result = await session.execute(
        select(ProviderConfig).where(ProviderConfig.name == name)
    )
    return result.scalar_one_or_none()


async def upsert_provider_config(
    session: AsyncSession,
    name: str,
    encrypted_key: Optional[str] = None,
    priority: Optional[int] = None,
    is_enabled: Optional[bool] = None,
    role_models: Optional[dict] = None,
    updated_by: Optional[int] = None,
) -> "ProviderConfig":
    """Create or update provider config"""
    config = await get_provider_config(session, name)

    if not config:
        config = ProviderConfig(name=name)
        session.add(config)

    if encrypted_key is not None:
        config.encrypted_key = encrypted_key
    if priority is not None:
        config.priority = priority
    if is_enabled is not None:
        config.is_enabled = is_enabled
    if role_models is not None:
        config.role_models = dict(role_models)  # copy — SQLAlchemy JSON change tracking
    if updated_by is not None:
        config.updated_by = updated_by

    await session.commit()
    await session.refresh(config)
    return config
