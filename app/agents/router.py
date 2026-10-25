"""
Agent router - routes messages to appropriate agent
"""
import logging
from typing import Any, Dict, Optional

from app.agents.fastbot import fastbot
from app.agents.planbot import planbot
from app.agents.skillbot import skillbot
from app.utils.states import BotMode

logger = logging.getLogger(__name__)


class AgentRouter:
    """Routes messages to appropriate agent based on mode and context"""
    
    def __init__(self):
        self.agents = {
            BotMode.FASTBOT.value: fastbot,
            BotMode.PLANBOT.value: planbot,
            BotMode.SKILLBOT.value: skillbot,
        }
        self.default_agent = BotMode.FASTBOT.value
    
    async def route_message(
        self,
        user_id: int,
        message: str,
        agent_mode: Optional[str] = None,
        conversation_history: Optional[list] = None,
        on_status: Optional[Any] = None
    ) -> str:
        """
        Route message to appropriate agent

        Args:
            user_id: User's Telegram ID
            message: User's message
            agent_mode: Force specific agent mode (optional)
            conversation_history: Previous messages
            on_status: async-callback стадий выполнения (поддерживает PlanBot)

        Returns:
            Agent's response
        """
        # Determine agent to use
        mode = agent_mode or self.default_agent
        
        # Get agent instance
        agent = self.agents.get(mode, self.agents[self.default_agent])
        
        logger.info(f"Routing message to {agent.name} for user {user_id}")
        
        # Process through agent
        try:
            if on_status is not None and getattr(agent, "name", "") == "planbot":
                response = await agent.process_message(
                    user_id=user_id,
                    message=message,
                    conversation_history=conversation_history,
                    on_status=on_status
                )
            else:
                response = await agent.process_message(
                    user_id=user_id,
                    message=message,
                    conversation_history=conversation_history
                )
            return response
        except Exception as e:
            logger.error(f"Agent {agent.name} failed: {e}")
            return "⚠️ Извините, у меня возникли проблемы с обработкой вашего запроса. Пожалуйста, попробуйте еще раз."
    
    def get_agent(self, mode: str) -> Optional[Any]:
        """Get agent by mode"""
        return self.agents.get(mode)
    
    def list_agents(self) -> Dict[str, Any]:
        """List all available agents"""
        return self.agents.copy()
    
    def get_agent_info(self, mode: str) -> Optional[Dict[str, str]]:
        """Get agent information"""
        agent = self.agents.get(mode)
        if agent:
            return {
                "name": agent.name,
                "display_name": agent.display_name,
                "description": agent.description
            }
        return None


# Global router instance
agent_router = AgentRouter()
