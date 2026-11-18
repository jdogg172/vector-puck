"""Deterministic conversions. Volume is US customary, never ingredient mass."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import re


# Canonical unit -> dimension, SI multiplier, offset before multiplication.
UNITS = {
    "m": ("length", "1", "0"),
    "km": ("length", "1000", "0"),
    "cm": ("length", ".01", "0"),
    "mm": ("length", ".001", "0"),
    "in": ("length", ".0254", "0"),
    "ft": ("length", ".3048", "0"),
    "yd": ("length", ".9144", "0"),
    "mi": ("length", "1609.344", "0"),
    "g": ("mass", ".001", "0"),
    "kg": ("mass", "1", "0"),
    "mg": ("mass", ".000001", "0"),
    "oz": ("mass", ".028349523125", "0"),
    "lb": ("mass", ".45359237", "0"),
    "l": ("volume", "1", "0"),
    "ml": ("volume", ".001", "0"),
    "us_gal": ("volume", "3.785411784", "0"),
    "us_qt": ("volume", ".946352946", "0"),
    "us_pt": ("volume", ".473176473", "0"),
    "us_cup": ("volume", ".2365882365", "0"),
    "us_fl_oz": ("volume", ".0295735295625", "0"),
    "us_tbsp": ("volume", ".01478676478125", "0"),
    "us_tsp": ("volume", ".00492892159375", "0"),
    "m2": ("area", "1", "0"),
    "ft2": ("area", ".09290304", "0"),
    "acre": ("area", "4046.8564224", "0"),
    "ha": ("area", "10000", "0"),
    "m/s": ("speed", "1", "0"),
    "km/h": ("speed", "0.2777777777777777777777777778", "0"),
    "mph": ("speed", ".44704", "0"),
    "knot": ("speed", "0.5144444444444444444444444444", "0"),
    "pa": ("pressure", "1", "0"),
    "kpa": ("pressure", "1000", "0"),
    "bar": ("pressure", "100000", "0"),
    "psi": ("pressure", "6894.757293168361", "0"),
    "s": ("time", "1", "0"),
    "min": ("time", "60", "0"),
    "h": ("time", "3600", "0"),
}
ALIASES = {
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
    "kilometers": "km",
    "kilometres": "km",
    "centimeters": "cm",
    "millimeters": "mm",
    "inch": "in",
    "inches": "in",
    "feet": "ft",
    "foot": "ft",
    "yard": "yd",
    "yards": "yd",
    "mile": "mi",
    "miles": "mi",
    "gram": "g",
    "grams": "g",
    "kilogram": "kg",
    "kilograms": "kg",
    "milligrams": "mg",
    "ounce": "oz",
    "ounces": "oz",
    "pound": "lb",
    "pounds": "lb",
    "lbs": "lb",
    "liter": "l",
    "liters": "l",
    "litre": "l",
    "litres": "l",
    "milliliters": "ml",
    "millilitres": "ml",
    "gallon": "us_gal",
    "gallons": "us_gal",
    "quart": "us_qt",
    "quarts": "us_qt",
    "pint": "us_pt",
    "pints": "us_pt",
    "cup": "us_cup",
    "cups": "us_cup",
    "fluid ounce": "us_fl_oz",
    "fluid ounces": "us_fl_oz",
    "fl oz": "us_fl_oz",
    "tablespoon": "us_tbsp",
    "tablespoons": "us_tbsp",
    "tbsp": "us_tbsp",
    "teaspoon": "us_tsp",
    "teaspoons": "us_tsp",
    "tsp": "us_tsp",
    "square feet": "ft2",
    "square meters": "m2",
    "acres": "acre",
    "hectares": "ha",
    "knots": "knot",
    "kph": "km/h",
    "seconds": "s",
    "minutes": "min",
    "hours": "h",
    "celsius": "c",
    "fahrenheit": "f",
    "kelvin": "k",
}
LABELS = {
    "c": "degrees Celsius",
    "f": "degrees Fahrenheit",
    "k": "kelvin",
    "us_cup": "US cups",
    "us_gal": "US gallons",
    "us_qt": "US quarts",
    "us_pt": "US pints",
    "us_fl_oz": "US fluid ounces",
    "us_tbsp": "US tablespoons",
    "us_tsp": "US teaspoons",
    "oz": "ounces by weight",
}


def decimal(value) -> Decimal:
    if (
        isinstance(value, bool)
        or not isinstance(value, (str, int, float, Decimal))
        or len(str(value)) > 50
    ):
        raise ValueError("Invalid quantity")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid quantity") from None
    if not number.is_finite() or abs(number) > Decimal("1e12"):
        raise ValueError("Quantity out of range")
    return number


def display(value: Decimal, places=4) -> str:
    with localcontext() as ctx:
        ctx.prec = 40
        rounded = value.quantize(Decimal(1).scaleb(-places))
    return (
        format(rounded, "f").rstrip("0").rstrip(".")
        if "." in format(rounded, "f")
        else str(rounded)
    )


def unit(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        raise ValueError("Invalid unit")
    clean = re.sub(r"\s+", " ", value.lower().replace("°", "").strip())
    return ALIASES.get(clean, clean)


def convert(amount, source: str, target: str) -> dict:
    number, a, b = decimal(amount), unit(source), unit(target)
    with localcontext() as ctx:
        ctx.prec = 40
        if a in {"c", "f", "k"} and b in {"c", "f", "k"}:
            celsius = (
                (number - 32) * 5 / 9
                if a == "f"
                else number - Decimal("273.15")
                if a == "k"
                else number
            )
            if celsius < Decimal("-273.15"):
                raise ValueError("Temperature is below absolute zero")
            result = (
                celsius * 9 / 5 + 32
                if b == "f"
                else celsius + Decimal("273.15")
                if b == "k"
                else celsius
            )
        else:
            if a not in UNITS or b not in UNITS or UNITS[a][0] != UNITS[b][0]:
                raise ValueError(
                    "Unsupported or incompatible units; ingredient volume needs its density to become weight"
                )
            result = number * Decimal(UNITS[a][1]) / Decimal(UNITS[b][1])
    return {
        "input": str(number),
        "from": a,
        "to": b,
        "result": str(result),
        "spoken": f"{display(number)} {LABELS.get(a, a)} is about {display(result)} {LABELS.get(b, b)}.",
        "source": "Deterministic SI and US customary conversion",
        "stale": False,
    }
