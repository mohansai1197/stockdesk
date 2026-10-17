"""Validated product and sale inputs; prices are converted to integer paise."""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

SHOP_TIMEZONE = ZoneInfo("Asia/Kolkata")
MAX_STOCK = 1_000_000


def shop_today():
    return datetime.now(SHOP_TIMEZONE).date()


def to_paise(value: str) -> int:
    """Store ₹19.99 as 1999, so money never relies on floating-point arithmetic."""
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("Price must be a number, for example 149.50") from None
    if not number.is_finite() or not Decimal("0") < number <= Decimal("1000000"):
        raise ValueError("Price must be between ₹0.01 and ₹10,00,000")
    paise = number * 100
    if paise != paise.to_integral_value():
        raise ValueError("Price can have at most two decimal places")
    return int(paise)


class ProductInput(BaseModel):
    sku: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=50)
    price: str = Field(max_length=24, strict=True)
    stock: int = Field(ge=0, le=MAX_STOCK, strict=True)
    low_stock_at: int = Field(default=5, ge=0, le=MAX_STOCK, strict=True)
    cost: str | None = Field(default=None, max_length=24)
    supplier_id: int | None = Field(default=None, gt=0, strict=True)
    location_id: int = Field(default=1, gt=0, strict=True)

    @field_validator("sku")
    @classmethod
    def valid_sku(cls, value):
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,31}", value):
            raise ValueError("SKU must use letters, numbers, hyphens, or underscores")
        return value

    @field_validator("name", "category")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank")
        return value

    @field_validator("cost")
    @classmethod
    def valid_cost(cls, value):
        if value is not None:
            to_paise(value)
        return value

    @field_validator("price")
    @classmethod
    def valid_price(cls, value):
        to_paise(value)
        return value


class RestockInput(BaseModel):
    quantity: int = Field(gt=0, le=MAX_STOCK, strict=True)
    location_id: int = Field(default=1, gt=0, strict=True)
    cost: str | None = Field(default=None, max_length=24)


class SaleItemInput(BaseModel):
    product_id: int = Field(gt=0, strict=True)
    quantity: int = Field(gt=0, le=MAX_STOCK, strict=True)


class SaleInput(BaseModel):
    location_id: int = Field(default=1, gt=0, strict=True)
    items: list[SaleItemInput] = Field(min_length=1, max_length=20)
    sale_date: date = Field(default_factory=shop_today)

    @field_validator("sale_date")
    @classmethod
    def valid_date(cls, value):
        if value > shop_today():
            raise ValueError("A sale date cannot be in the future")
        return value

    @field_validator("items")
    @classmethod
    def unique_products(cls, items):
        if len({item.product_id for item in items}) != len(items):
            raise ValueError("Combine repeated products into one sale line")
        return items
