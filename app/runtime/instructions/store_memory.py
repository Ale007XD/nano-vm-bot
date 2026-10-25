"""
StoreMemoryInstruction — персистирует запись в VectorMemory (ChromaDB).

Params:
    content     (str, required)
    memory_type (str, default="note") — тип: note, trip, budget, plan, dialog
    metadata    (dict, optional)

Output: str — doc_id из ChromaDB
"""
from typing import Dict

from app.runtime.builder import StepResultBuilder
from app.runtime.context import VMContext
from app.runtime.instructions.base import BaseInstruction


class StoreMemoryInstruction(BaseInstruction):
    name = "store_memory"

    async def execute(self, step_id: str, params: Dict, ctx: VMContext):
        content: str = params["content"]
        memory_type: str = params.get("memory_type", "note")
        metadata: dict = params.get("metadata", {})

        doc_id = await ctx.memory.add_memory(
            content=content,
            user_id=ctx.user_id,
            memory_type=memory_type,
            metadata=metadata,
        )

        return (
            StepResultBuilder(step_id, self.name)
            .output(doc_id)
            .memory_write(
                collection="episodic",
                content=content,
                metadata={"doc_id": doc_id, "memory_type": memory_type, **metadata},
            )
            .build()
        )
