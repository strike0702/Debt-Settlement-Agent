"""Registry of creditor-rule fields the agent may ask about or assume.

Each ``FieldSpec`` carries ask/readback copy, kind, defaults, and optional
prior ranges. ``FIELD_REGISTRY`` / ``REQUIRED_FIELDS`` drive belief seeding
and ``NeedsInfo`` when the engine cannot yet build ``CreditorRules``.
``label`` is human-speakable (NLG / UI); ``name`` stays the machine id.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

FieldKind = Literal["int", "cents", "enum", "date", "tiers"]
PaymentStructure = Literal["even", "balloon", "flexible"]

PAYMENT_STRUCTURES: tuple[PaymentStructure, ...] = ("even", "balloon", "flexible")


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: FieldKind
    label: str
    ask_text: str
    readback_text: str
    required: bool
    default_factory: Callable[[], Any] | None
    prior_range: tuple[Any, ...] | None


FIELD_REGISTRY: list[FieldSpec] = [
    FieldSpec(
        name="max_payments",
        kind="int",
        label="maximum number of payments",
        ask_text="What is the maximum number of payments you can accept?",
        readback_text="So the maximum number of payments is {value}?",
        required=True,
        default_factory=None,
        prior_range=(1, 60),
    ),
    FieldSpec(
        name="min_payment_cents",
        kind="cents",
        label="minimum payment",
        ask_text="What's the minimum payment amount?",
        readback_text="So the minimum payment is {value}?",
        required=True,
        default_factory=None,
        prior_range=(1000, 100000),
    ),
    FieldSpec(
        name="payment_structure",
        kind="enum",
        label="payment structure",
        ask_text="Do you need even payments, or can you allow a balloon or a flexible schedule?",
        readback_text="So the payment structure is {value}?",
        required=True,
        default_factory=None,
        prior_range=PAYMENT_STRUCTURES,
    ),
    FieldSpec(
        name="first_payment_date",
        kind="date",
        label="initial payment date",
        ask_text="When should the first payment be due?",
        readback_text="So the initial payment date is {value}?",
        required=False,
        default_factory=None,  # BeliefState seeds from default_first_payment_date(client)
        prior_range=None,
    ),
    FieldSpec(
        name="max_segments",
        kind="int",
        label="payment levels",
        ask_text="How many distinct payment levels can the schedule have?",
        readback_text="So at most {value} payment levels?",
        required=False,
        default_factory=lambda: 2,
        prior_range=(1, 10),
    ),
    FieldSpec(
        name="max_token_pays",
        kind="int",
        label="token payments",
        ask_text="How many token payments are allowed?",
        readback_text="So at most {value} token payments?",
        required=False,
        # Default is max_payments (no limit); BeliefState syncs while ASSUMED.
        default_factory=None,
        prior_range=(0, 60),
    ),
    FieldSpec(
        name="min_payment_tiers",
        kind="tiers",
        label="payment tiers",
        ask_text="Are there any tiered minimum payment floors?",
        readback_text="So the payment tiers are {value}?",
        required=False,
        default_factory=lambda: [],
        prior_range=None,
    ),
]

FIELDS_BY_NAME: dict[str, FieldSpec] = {spec.name: spec for spec in FIELD_REGISTRY}

REQUIRED_FIELDS: list[str] = [spec.name for spec in FIELD_REGISTRY if spec.required]
