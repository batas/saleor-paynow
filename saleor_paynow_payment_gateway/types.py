import enum
from dataclasses import dataclass
from datetime import datetime

import json

from json import JSONDecodeError


class PaymentStatus(enum.Enum):
    NEW = "NEW"
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"
    ABANDONED = "ABANDONED"


class PaynowException(Exception):
    pass


@dataclass
class PaynowObject:
    payment_id: str
    external_id: str
    status: PaymentStatus
    modified_at: datetime

    @staticmethod
    def from_json(data):
        try:
            data = json.loads(data)
        except JSONDecodeError:
            raise PaynowException()
        return PaynowObject(
            payment_id=data["paymentId"],
            external_id=data["externalId"],
            status=PaymentStatus(data["status"]),
            modified_at=datetime.strptime(data["modifiedAt"], "%Y-%m-%dT%H:%M:%S"),
        )
