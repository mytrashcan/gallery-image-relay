from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class DeliveryOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    QUEUED = "queued"


@dataclass(frozen=True, slots=True)
class ChannelDelivery:
    transport: Literal["discord", "telegram", "web_gallery"]
    destination_id: str
    outcome: DeliveryOutcome
    requested_media: tuple[str, ...]
    delivered_media: tuple[str, ...]
    ack_eligible: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    deliveries: tuple[ChannelDelivery, ...]

    @property
    def acknowledged(self) -> bool:
        return any(
            delivery.ack_eligible and delivery.outcome is DeliveryOutcome.SUCCEEDED
            for delivery in self.deliveries
        )

    def merge(self, *others: DeliveryResult) -> DeliveryResult:
        return DeliveryResult(
            self.deliveries
            + tuple(
                delivery
                for result in others
                for delivery in result.deliveries
            )
        )
