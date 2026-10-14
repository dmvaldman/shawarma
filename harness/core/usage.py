from decimal import Decimal
from uuid import UUID

from harness.core.types import Usage
from harness.persistence.store import Store


class UsageLedger:
    """UsageMeter port; separate from the inbox repository's reserve operation."""

    def __init__(self, store: Store):
        self.store = store

    async def reserve(self, agent_id: UUID, amount_usd: Decimal) -> UUID:
        return await self.store.reserve_usage(agent_id, amount_usd)

    async def record(self, reservation_id: UUID, usage: Usage) -> None:
        await self.store.record(reservation_id, usage)
