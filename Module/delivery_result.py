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
        eligible = [delivery for delivery in self.deliveries if delivery.ack_eligible]
        return bool(eligible) and all(
            delivery.outcome is DeliveryOutcome.SUCCEEDED for delivery in eligible
        )

    def media_acknowledged(self, media_id: str) -> bool:
        eligible = [
            d for d in self.deliveries
            if d.ack_eligible and media_id in d.requested_media
        ]
        return bool(eligible) and all(media_id in d.delivered_media for d in eligible)

    def merge(self, *others: DeliveryResult) -> DeliveryResult:
        return DeliveryResult(
            self.deliveries
            + tuple(
                delivery
                for result in others
                for delivery in result.deliveries
            )
        )
