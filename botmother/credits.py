from __future__ import annotations

from dataclasses import dataclass


ACTION_NEW_BOT = "new_bot"
ACTION_EDIT = "edit"
ACTION_REVISE = "revise"
ACTION_AUTOFIX = "autofix"
ACTION_ASK = "ask"
ACTION_RUNTIME = "runtime"


ACTION_LABELS = {
    ACTION_NEW_BOT: "New Bot",
    ACTION_EDIT: "Edit",
    ACTION_REVISE: "Revise",
    ACTION_AUTOFIX: "Auto Fix",
    ACTION_ASK: "Ask Bot",
    ACTION_RUNTIME: "Runtime",
}


@dataclass(frozen=True)
class CreditGateResult:
    ok: bool
    action: str
    cost: int
    balance: int | None = None
    reservation_id: int | None = None
    exempt: bool = False
    message: str = ""


@dataclass(frozen=True)
class RuntimeChargeResult:
    user_id: int
    charged: int
    balance: int
    should_stop: bool
    due: int
