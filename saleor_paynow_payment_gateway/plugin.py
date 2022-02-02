import json
import logging
from pprint import pprint
from typing import List, Optional

import requests
from django.core.handlers.wsgi import WSGIRequest
from django.http import (
    HttpResponse,
    JsonResponse,
    HttpResponseNotFound,
    HttpResponseServerError,
    HttpResponseBadRequest,
)

from saleor.checkout.models import Checkout
from saleor.core.transactions import transaction_with_commit_on_errors
from saleor.payment import TransactionKind
from saleor.payment.gateways.utils import require_active_plugin
from saleor.payment.interface import GatewayConfig, CustomerSource, PaymentGateway
from saleor.payment.interface import GatewayResponse
from saleor.payment.interface import PaymentData
from saleor.payment.models import Payment
from saleor.plugins.base_plugin import (
    BasePlugin,
    ConfigurationTypeField,
)
from saleor.plugins.models import PluginConfiguration
import hmac
import hashlib
import base64

from saleor_paynow_payment_gateway.types import PaymentStatus, PaynowObject
from saleor_paynow_payment_gateway.webhooks import handle_webhook

PLUGIN_NAME = "PayNow Payments"

log = logging.getLogger(__name__)


class PayNowPlugin(BasePlugin):
    PLUGIN_ID = "payments.paynow"
    PLUGIN_NAME = PLUGIN_NAME

    DEFAULT_CONFIGURATION = [
        {"name": "use_sandbox", "value": True},
        {"name": "api_key", "value": None},
        {"name": "signature_key", "value": ""},
        {"name": "supported_currencies", "value": "PLN"},
    ]

    CONFIG_STRUCTURE = {
        "api_key": {
            "type": ConfigurationTypeField.SECRET,
            "help_text": "Provide Stripe public API key.",
            "label": "API key",
        },
        "signature_key": {
            "type": ConfigurationTypeField.SECRET,
            "help_text": "Provide Stripe secret API key.",
            "label": "Signature key",
        },
        "use_sandbox": {
            "type": ConfigurationTypeField.BOOLEAN,
            "help_text": "Determines usage of production or sandbox environment.",
            "label": "Use sandbox",
        },
        "supported_currencies": {
            "type": ConfigurationTypeField.STRING,
            "help_text": "Determines currencies supported by gateway."
            " Please enter currency codes separated by a comma.",
            "label": "Supported currencies",
        },
    }

    API_URL = {
        "PRODUCTION": "api.paynow.pl",
        "SANDBOX": "api.sandbox.paynow.pl",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configuration = {item["name"]: item["value"] for item in self.configuration}

        self.config = GatewayConfig(
            gateway_name=PLUGIN_NAME,
            auto_capture=True,
            supported_currencies=configuration["supported_currencies"],
            connection_params={
                "api_key": configuration["api_key"],
                "signature_key": configuration["signature_key"],
                "api_host": self.API_URL["SANDBOX"]
                if configuration["use_sandbox"]
                else self.API_URL["PRODUCTION"],
            },
            store_customer=True,
        )

    @classmethod
    def check_plugin_id(cls, plugin_id: str) -> bool:
        return plugin_id.startswith(cls.PLUGIN_ID)

    def _calculate_hmac(self, data):
        if isinstance(data, str):
            data = data.encode()
        elif isinstance(data, bytes):
            pass
        else:
            data = json.dumps(data).encode()

        hashed_object = hmac.new(
            self.config.connection_params["signature_key"].encode(),
            data,
            hashlib.sha256,
        ).digest()
        return base64.b64encode(hashed_object)

    def _get_payment_methods(self):
        result = requests.get(
            f"https://{self.config.connection_params['api_host']}/v2/payments/paymentmethods",
            headers={
                "Accept": "application/json",
                "Api-Key": self.config.connection_params["api_key"],
            },
        )

        return result.json()

    @require_active_plugin
    def get_supported_currencies(self, previous_value):
        return ["PLN"]

    @require_active_plugin
    def process_payment(
        self, payment_information: "PaymentData", previous_value
    ) -> "GatewayResponse":
        data = {
            "amount": int(payment_information.amount * 100),
            "currency": payment_information.currency,
            "externalId": payment_information.checkout_token,
            "description": f"Zamówienie {payment_information.checkout_token}",
            "buyer": {
                "email": payment_information.customer_email,
                # "phone": payment_information.billing.phone,
            },
            "paymentMethodId": payment_information.gateway.split(".")[-1],
        }

        try:
            payment = Payment.objects.get(
                id=payment_information.payment_id,
                gateway=payment_information.gateway,
            )
            if payment and payment.return_url:
                data["continueUrl"] = payment.return_url
        except:
            pass

        pprint(payment_information)
        print(data)

        response = requests.post(
            f'https://{self.config.connection_params["api_host"]}/v1/payments',
            json=data,
            headers={
                "Api-Key": self.config.connection_params["api_key"],
                "Signature": self._calculate_hmac(data),
                "Idempotency-Key": payment_information.token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            payment_data = response.json()
        except:
            payment_data = {}

        print("status", response.status_code, response.content)
        if 200 <= response.status_code <= 299:
            error = False
        else:
            log.error(f"Failed create payment {str(payment_data)}")
            error = True

        action_required = True
        kind = TransactionKind.ACTION_TO_CONFIRM

        return GatewayResponse(
            is_success=True if not error else False,
            action_required=action_required,
            kind=kind,
            amount=payment_information.amount,
            currency=payment_information.currency,
            transaction_id=payment_data.get("paymentId", ""),
            error=None,
            raw_response=None,
            action_required_data={"redirect": payment_data.get("redirectUrl")},
            customer_id=None,
            psp_reference=None,
            # transaction_already_processed=True,
        )

    @require_active_plugin
    def confirm_payment(
        self, payment_information: "PaymentData", previous_value
    ) -> "GatewayResponse":
        pprint(payment_information)
        return GatewayResponse(
            is_success=True,
            action_required=False,
            kind=TransactionKind.CONFIRM,
            amount=payment_information.amount,
            currency=payment_information.currency,
            transaction_id=payment_information.payment_id,
            error=None,
            # error=error.user_message if error else None,
            # raw_response=raw_response,
            # psp_reference=payment_intent.id if payment_intent else None,
            # payment_method_info=payment_method_info,
        )

    @require_active_plugin
    def capture_payment(
        self, payment_information: "PaymentData", previous_value
    ) -> "GatewayResponse":
        pass

    @require_active_plugin
    def refund_payment(
        self, payment_information: "PaymentData", previous_value
    ) -> "GatewayResponse":
        pass

    @require_active_plugin
    def void_payment(
        self, payment_information: "PaymentData", previous_value
    ) -> "GatewayResponse":
        pass

    @require_active_plugin
    def list_payment_sources(
        self, customer_id: str, previous_value
    ) -> List[CustomerSource]:
        return previous_value

    @classmethod
    def pre_save_plugin_configuration(cls, plugin_configuration: "PluginConfiguration"):
        pass

    @require_active_plugin
    def get_payment_config(self, previous_value):
        return {}

    def get_payment_gateways(
        self, currency: Optional[str], checkout: Optional["Checkout"], previous_value
    ) -> List["PaymentGateway"]:
        payment_config = self.get_payment_config(previous_value)  # type: ignore
        payment_config = payment_config if payment_config != NotImplemented else []
        currencies = self.get_supported_currencies(previous_value=[])  # type: ignore
        currencies = currencies if currencies != NotImplemented else []
        if currency and currency not in currencies:
            return []

        gateways = []

        for payment_method in self._get_payment_methods():
            for method in payment_method["paymentMethods"]:
                gateway = PaymentGateway(
                    id=f"{self.PLUGIN_ID}.{method['id']}",
                    name=method["name"],
                    config=[
                        *payment_config,
                        {
                            "field": "image",
                            "value": method["image"],
                        },
                        {
                            "field": "type",
                            "value": payment_method["type"],
                        },
                        {
                            "field": "description",
                            "value": method["description"],
                        },
                    ],
                    currencies=currencies,
                )
                if method["status"] == "ENABLED":
                    gateways.append(gateway)

        return gateways

    @transaction_with_commit_on_errors()
    def webhook(self, request: WSGIRequest, path: str, previous_value) -> HttpResponse:
        if path == "/notification":
            try:
                body = json.loads(request.body)
                paynow_payment = PaynowObject.from_json(request.body)
            except:
                log.error("Failed to parse request body", exc_info=True)
                return HttpResponseServerError()

            signature = self._calculate_hmac(request.body).decode("utf-8")
            if request.headers.get("Signature") != signature:
                log.error(
                    "Invalid signature (%s != %s) data: %s",
                    request.headers.get("Signature"),
                    signature,
                    repr(body),
                )
                return HttpResponseBadRequest(b"invalid signature")

            payment_status = PaymentStatus(body["status"])  # todo check invalid status

            return handle_webhook(
                paynow_payment, payment_status, self.config, self.channel.slug
            )
        return HttpResponseNotFound()
