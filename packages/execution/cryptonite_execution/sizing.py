"""Entry sizing by setup grade, shared by debate routing and Brain order cards (H2, O2)."""

GRADE_SIZE_FACTOR = {"A": 1.0, "B": 1.0, "C": 0.5}
UNKNOWN_GRADE_FACTOR = 0.5  # C, NO_TRADE and an unknown grade size at half


def grade_size_pct(grade: str | None, max_position_pct: float, room: float) -> float:
    """The entry size, in percent of the mandate: the profile's max position scaled by the setup grade (A or B full, C
    half, an unknown grade half), never more than the room left. The model's allocation does not size an entry."""
    return round(min(float(max_position_pct) * GRADE_SIZE_FACTOR.get(str(grade), UNKNOWN_GRADE_FACTOR), float(room)), 2)
