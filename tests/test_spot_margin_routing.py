"""Pure market-selection contract for profile routes that include isolated spot margin."""

import pytest
from cryptonite_schemas.mandate import Mandate, risk_adding_change
from cryptonite_schemas.profile import Market, select_market


@pytest.mark.parametrize(
    "side,leverage,markets,expected",
    [
        ("LONG", 1, [Market.SPOT, Market.SPOT_MARGIN], Market.SPOT),
        ("LONG", 3, [Market.SPOT, Market.SPOT_MARGIN], Market.SPOT_MARGIN),
        ("SHORT", 3, [Market.SPOT, Market.SPOT_MARGIN], Market.SPOT_MARGIN),
    ],
)
def test_profile_selection_routes_margin_sides_to_the_margin_instrument(side, leverage, markets, expected):
    assert select_market(side, leverage, markets) is expected


def test_adding_spot_margin_to_a_mandate_is_a_totp_risk_increase():
    old = Mandate(
        id="00000000-0000-0000-0000-000000000001", name="spot", capital_budget_usd="10000",
        instruments=["BTCUSDT"], max_position_pct=25, max_drawdown_pct=20, markets=[Market.SPOT],
    )
    new = old.model_copy(update={"markets": old.markets | {Market.SPOT_MARGIN}})
    assert risk_adding_change(old, new)
