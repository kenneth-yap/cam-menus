"""Data schema — the single source of truth for what a menu looks like.

This is the contract every college's data must conform to, no matter how messy
its source page was. It is used in three places at once:
  1. As the LLM's response_schema, constraining what the model may return.
  2. As runtime validation (Pydantic rejects malformed model output).
  3. As the blueprint the Postgres tables mirror one-to-one.

Defining the destination format ONCE, here, is what lets every source be
interchangeable downstream.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Meal(str, Enum):
    """Controlled vocabulary for meal sittings. The model must pick from these.

    Inheriting from `str` as well as `Enum` means each member behaves like its
    string value, which keeps JSON serialisation and database writes simple.
    """
    BREAKFAST = "breakfast"
    BRUNCH = "brunch"          # typically weekends
    LUNCH = "lunch"
    DINNER = "dinner"


class Dish(BaseModel):
    """A single item on the menu."""
    name: str = Field(description="Dish name, cleaned of allergen codes.")
    course: Optional[str] = Field(
        default=None,
        description="starter | main | side | dessert if indicated, else null.",
    )
    dietary: list[str] = Field(
        default_factory=list,
        description="Normalised tags: any of vegan, vegetarian, halal, gluten-free.",
    )


class MealSitting(BaseModel):
    """All dishes for one meal on one day."""
    meal: Meal
    dishes: list[Dish] = Field(default_factory=list)


class DayMenu(BaseModel):
    """One day's worth of meals.

    `day` is the weekday NAME as the LLM reads it from the page ('Monday').
    `menu_date` is the real calendar date, filled in later by the pipeline's
    resolve_dates() — the LLM never computes dates. It starts as None.
    """
    day: str = Field(description="Day of week, e.g. 'Monday'.")
    date_text: Optional[str] = Field(
        default=None,
        description="Raw date string if shown on the page, else null.",
    )
    menu_date: Optional[date] = Field(
        default=None,
        description="Resolved calendar date. Set by the pipeline, not the LLM.",
    )
    meals: list[MealSitting] = Field(default_factory=list)


class CollegeMenu(BaseModel):
    """The unified output for one college. This is what the website consumes."""
    college: str
    week_commencing: Optional[str] = Field(
        default=None,
        description="The Monday the menu week starts, as YYYY-MM-DD if derivable.",
    )
    days: list[DayMenu] = Field(default_factory=list)