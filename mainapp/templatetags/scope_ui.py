from django import template
from mainapp.roles import ROLE_LABELS
import re

register = template.Library()


@register.filter
def role_label(value):
    return ROLE_LABELS.get(value, value)


@register.filter
def readable_money(value):
    match = re.fullmatch(r"(\d+)\.(\d{2}) (RUB|USD|EUR)", str(value))
    if not match:
        return value
    integer, cents, currency = match.groups()
    number = f"{int(integer):,}".replace(",", " ")
    if cents != "00":
        number += "," + cents
    return number + " " + {"RUB": "₽", "USD": "$", "EUR": "€"}[currency]
