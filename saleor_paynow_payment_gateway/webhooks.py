from logging import getLogger
from typing import Optional

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.http import HttpResponse

from saleor.checkout.calculations import calculate_checkout_total_with_gift_cards
from saleor.checkout.complete_checkout import complete_checkout
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.checkout.models import Checkout
from saleor.core.transactions import transaction_with_commit_on_errors
from saleor.discount.utils import fetch_active_discounts
from saleor.order.actions import order_captured
from saleor.payment import ChargeStatus, TransactionKind
from saleor.payment.gateway import payment_refund_or_void
from saleor.payment.interface import GatewayResponse
from saleor.payment.models import Payment
from saleor.payment.utils import (
    price_from_minor_unit,
    create_transaction,
    update_payment_charge_status,
    gateway_postprocess,
)
from saleor.plugins.manager import get_plugins_manager
from saleor_paynow_payment_gateway.types import PaynowObject, PaymentStatus

logger = getLogger(__name__)


def _get_payment(payment_intent_id: str) -> Optional[Payment]:
    return (
        Payment.objects.prefetch_related(
            "checkout",
        )
        .select_for_update(of=("self",))
        .filter(transactions__token=payment_intent_id, is_active=True)
        .first()
    )


def _get_checkout(payment_id: int) -> Optional[Checkout]:
    return (
        Checkout.objects.prefetch_related("payments")
        .select_for_update(of=("self",))
        .filter(payments__id=payment_id, payments__is_active=True)
        .first()
    )


def _update_payment_with_new_transaction(
    payment: Payment,
    paynow_object: PaynowObject,
    kind: str,
):
    gateway_response = GatewayResponse(
        kind=kind,
        action_required=False,
        transaction_id=paynow_object.payment_id,
        is_success=True,
        amount=payment.total,
        currency=payment.currency,
        error=None,
        # raw_response=payment.last_response,
        psp_reference=paynow_object.payment_id,
    )
    transaction = create_transaction(
        payment,
        kind=kind,
        payment_information=None,  # type: ignore
        action_required=False,
        gateway_response=gateway_response,
    )
    gateway_postprocess(transaction, payment)

    return transaction


def _finalize_checkout(
    checkout: Checkout,
    payment: Payment,
    payment_intent: PaynowObject,
    kind: str,
):
    gateway_response = GatewayResponse(
        kind=kind,
        action_required=False,
        transaction_id=payment_intent.payment_id,
        is_success=True,
        amount=payment.total,
        currency=payment.currency,
        error=None,
        # raw_response=payment_intent.last_response,
        psp_reference=payment_intent.payment_id,
    )

    transaction = create_transaction(
        payment,
        kind=kind,
        payment_information=None,  # type: ignore
        action_required=False,
        gateway_response=gateway_response,
    )

    # To avoid zombie payments we have to update payment `charge_status` without
    # changing `to_confirm` flag. In case when order cannot be created then
    # payment will be refunded.
    update_payment_charge_status(payment, transaction)
    payment.refresh_from_db()
    checkout.refresh_from_db()

    manager = get_plugins_manager()
    discounts = fetch_active_discounts()
    lines, unavailable_variant_pks = fetch_checkout_lines(checkout)
    if unavailable_variant_pks:
        raise ValidationError("Some of the checkout lines variants are unavailable.")
    checkout_info = fetch_checkout_info(checkout, lines, discounts, manager)
    checkout_total = calculate_checkout_total_with_gift_cards(
        manager=manager,
        checkout_info=checkout_info,
        lines=lines,
        address=checkout.shipping_address or checkout.billing_address,
        discounts=discounts,
    )

    try:
        # when checkout total value is different than total amount from payments
        # it means that some products has been removed during the payment was completed
        if checkout_total.gross.amount != payment.total:
            payment_refund_or_void(payment, manager, checkout_info.channel.slug)
            raise ValidationError(
                "Cannot complete checkout - some products do not exist anymore."
            )

        order, _, _ = complete_checkout(
            manager=manager,
            checkout_info=checkout_info,
            lines=lines,
            payment_data={},
            store_source=False,
            discounts=discounts,
            user=checkout.user or AnonymousUser(),
            app=None,
        )
    except ValidationError as e:
        logger.info("Failed to complete checkout %s.", checkout.pk, extra={"error": e})
        return None


def handle_processing_payment_intent(
    payment_intent: PaynowObject, gateway_config: "GatewayConfig", _channel_slug: str
):
    payment = _get_payment(payment_intent.payment_id)

    if not payment:
        logger.warning(
            "Payment for PaymentIntent was not found",
            extra={"payment_intent": payment_intent.id},
        )
        return
    if payment.order_id:
        # Order already created
        return

    if payment.checkout_id:
        checkout = _get_checkout(payment_id=payment.pk)
        if checkout:
            _finalize_checkout(
                checkout=checkout,
                payment=payment,
                payment_intent=payment_intent,
                kind=TransactionKind.PENDING,
            )


def handle_successful_payment_intent(
    payment_intent: PaynowObject, gateway_config: "GatewayConfig", channel_slug: str
):
    payment = _get_payment(payment_intent.payment_id)

    if not payment:
        logger.warning(
            "Payment for PaymentIntent was not found",
            extra={"payment_intent": payment_intent.payment_id},
        )
        return

    if payment.order_id:
        if payment.charge_status in [ChargeStatus.PENDING, ChargeStatus.NOT_CHARGED]:
            capture_transaction = _update_payment_with_new_transaction(
                payment,
                payment_intent,
                TransactionKind.CAPTURE,
            )
            order_captured(
                payment.order,  # type: ignore
                None,
                None,
                capture_transaction.amount,
                payment,
                get_plugins_manager(),
            )
        return

    if payment.checkout_id:
        checkout = _get_checkout(payment_id=payment.pk)
        if checkout:
            _finalize_checkout(
                checkout=checkout,
                payment=payment,
                payment_intent=payment_intent,
                kind=TransactionKind.CAPTURE,
            )
        # _process_payment_with_checkout(
        #     payment,
        #     payment_intent,
        #     TransactionKind.CAPTURE,
        #     amount=payment_intent.amount_received,
        #     currency=payment_intent.currency,
        # )


@transaction_with_commit_on_errors()
def handle_webhook(payment, status: PaymentStatus, config, slug):
    if status == PaymentStatus.CONFIRMED:
        handle_successful_payment_intent(payment, config, slug)
    elif status == PaymentStatus.PENDING:
        handle_processing_payment_intent(payment, config, slug)
    return HttpResponse(status=200)
