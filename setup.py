from setuptools import setup

setup(
    name="saleor-paynow-payment-gateway",
    version="1.0",
    description="Paynow Saleor Gateway",
    author="Mateusz Sabat",
    author_email="mateusz@sabat.biz",
    entry_points={
        "saleor.plugins": [
            "saleor_paynow_payment_gateway = saleor_paynow_payment_gateway.plugin:PayNowPlugin"
        ]
    },
    requires=[
        "requests",
    ],
)
