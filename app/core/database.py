"""
Database models and connection management
"""
from typing import AsyncGenerator, List, Optional
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, 
    ForeignKey, Boolean, JSON, select
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
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
