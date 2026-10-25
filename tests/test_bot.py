"""
Basic tests for Alex-Nano-Bot
"""
import pytest
import asyncio


@pytest.fixture
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


class TestConfig:
    """Test configuration loading"""
    
    def test_settings_exist(self):
        """Test that settings can be imported"""
        from app.core.config import settings
        assert settings is not None
        assert settings.APP_NAME == "Alex-Nano-Bot"
    
    def test_default_models(self):
        """Test default model configurations"""
        from app.core.config import settings
        assert settings.DEFAULT_MODEL is not None
        assert settings.CODER_MODEL is not None
        assert settings.PLANNER_MODEL is not None


class TestMemory:
    """Test vector memory system"""
    
    @pytest.mark.asyncio
    async def test_vector_memory_initialization(self):
        """Test vector memory initialization"""
        from app.core.memory import VectorMemory
        
        vm = VectorMemory()
        assert vm is not None
        assert not vm._initialized
    
    @pytest.mark.asyncio
    async def test_generate_id(self):
        """Test ID generation"""
        from app.core.memory import VectorMemory
        
        vm = VectorMemory()
        id1 = vm._generate_id("test content", 123)
        id2 = vm._generate_id("test content", 123)
        id3 = vm._generate_id("different content", 123)
        
        # Same content should generate same ID
        assert id1 == id2
        # Different content should generate different ID
        assert id1 != id3


class TestSkillLoader:
    """Test skill loader"""
    
    def test_skill_loader_creation(self):
        """Test skill loader instantiation"""
        from app.core.skills_loader import SkillLoader
        
        loader = SkillLoader()
        assert loader is not None
        assert isinstance(loader.skills, dict)
        assert isinstance(loader.skill_info, dict)
    
    def test_is_valid_skill_name(self):
        """Test skill name validation"""
        from app.core.skills_loader import is_valid_skill_name
        
        assert is_valid_skill_name("valid_skill") is True
        assert is_valid_skill_name("ValidSkill123") is True
        assert is_valid_skill_name("_private") is True
        assert is_valid_skill_name("123invalid") is False
        assert is_valid_skill_name("invalid-name") is False
        assert is_valid_skill_name("invalid.name") is False


class TestLLMClient:
    """Test LLM client"""
    
    def test_message_creation(self):
        """Test message dataclass"""
        from app.core.llm_client import Message
        
        msg = Message(role="user", content="Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"
    
    def test_llm_response_creation(self):
        """Test response dataclass"""
        from app.core.llm_client import LLMResponse
        
        resp = LLMResponse(content="Hi", model="test-model")
        assert resp.content == "Hi"
        assert resp.model == "test-model"


class TestAgents:
    """Test agent implementations"""
    
    def test_fastbot_creation(self):
        """Test fastbot agent"""
        from app.agents.fastbot import FastBotAgent
        
        agent = FastBotAgent()
        assert agent.name == "fastbot"
        assert agent.display_name == "⚡ FastBot"
    
    def test_planbot_creation(self):
        """Test planbot agent"""
        from app.agents.planbot import PlanBotAgent
        
        agent = PlanBotAgent()
        assert agent.name == "planbot"
        assert agent.display_name == "🧩 PlanBot"
    
    def test_skillbot_creation(self):
        """Test skillbot agent"""
        from app.agents.skillbot import SkillBotAgent
        
        agent = SkillBotAgent()
        assert agent.name == "skillbot"
        assert agent.display_name == "🔧 SkillBot"


class TestHelpers:
    """Test utility functions"""
    
    def test_truncate_text(self):
        """Test text truncation"""
        from app.utils.helpers import truncate_text
        
        short = "Short text"
        assert truncate_text(short, 100) == short
        
        long = "A" * 5000
        truncated = truncate_text(long, 100)
        assert len(truncated) <= 100
        assert truncated.endswith("...")
    
    def test_escape_markdown(self):
        """Test markdown escaping"""
        from app.utils.helpers import escape_markdown
        
        text = "Hello_world"
        escaped = escape_markdown(text)
        assert "\\_" in escaped
    
    def test_parse_code_from_message(self):
        """Test code extraction"""
        from app.utils.helpers import parse_code_from_message
        
        # Test with python code block
        msg = "```python\nprint('hello')\n```"
        code = parse_code_from_message(msg)
        assert code is not None
        assert "print" in code
        
        # Test without code block
        msg = "Just text"
        code = parse_code_from_message(msg)
        assert code is None


class TestDatabaseModels:
    """Test database models"""
    
    def test_user_model(self):
        """Test User model structure"""
        from app.core.database import User
        
        # Check columns exist
        assert hasattr(User, 'id')
        assert hasattr(User, 'telegram_id')
        assert hasattr(User, 'username')
        assert hasattr(User, 'is_active')
    
    def test_message_model(self):
        """Test Message model structure"""
        from app.core.database import Message
        
        assert hasattr(Message, 'id')
        assert hasattr(Message, 'user_id')
        assert hasattr(Message, 'role')
        assert hasattr(Message, 'content')
        assert hasattr(Message, 'agent_mode')


@pytest.mark.integration
class TestIntegration:
    """Integration tests (require full setup)"""
    
    @pytest.mark.asyncio
    async def test_database_initialization(self):
        """Test database can be initialized"""
        from app.core.database import init_db
        
        try:
            await init_db()
            assert True
        except Exception as e:
            pytest.fail(f"Database initialization failed: {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
