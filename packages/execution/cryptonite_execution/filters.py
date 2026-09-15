"""Dynamic Binance exchange filter validator.

Enforces:
1. Symbol trading status (must be 'TRADING').
2. PRICE_FILTER: minPrice, maxPrice, tickSize alignment.
3. LOT_SIZE: minQty, maxQty, stepSize alignment.
4. MIN_NOTIONAL / NOTIONAL: quantity * price >= minNotional.
5. PERCENT_PRICE / avgPriceMins: dynamic asymmetric collars against /api/v3/avgPrice.
6. myFilters: account-level or custom exchange filter validations.
"""

import math
from decimal import Decimal
from typing import Any, Dict, List, Optional


class ExchangeFilterValidationError(Exception):
    """Raised when an order violates Binance exchange trading filters."""


class DynamicExchangeFilterValidator:
    """Validates order intent against Binance symbol filters and dynamic price collars."""

    def __init__(self, symbol_info: Optional[Dict[str, Any]] = None):
        if symbol_info is None:
            symbol_info = {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "filters": [
                    {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000.00", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000.00", "stepSize": "0.00001"},
                    {"filterType": "NOTIONAL", "minNotional": "5.0"},
                ],
            }
        self._symbol = symbol_info.get("symbol", "")
        self._status = symbol_info.get("status", "TRADING")
        self._filters: Dict[str, Dict[str, Any]] = {
            f["filterType"]: f for f in symbol_info.get("filters", [])
        }

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def status(self) -> str:
        return self._status

    def validate_trading_status(self) -> None:
        """Ensure symbol is actively trading."""
        if self._status != "TRADING":
            raise ExchangeFilterValidationError(
                f"Symbol {self._symbol} is not trading. Status: {self._status}"
            )

    def validate_price(self, price: Decimal) -> None:
        """Validate price against PRICE_FILTER (minPrice, maxPrice, tickSize)."""
        pf = self._filters.get("PRICE_FILTER")
        if not pf:
            return

        min_price = Decimal(str(pf.get("minPrice", "0")))
        max_price = Decimal(str(pf.get("maxPrice", "1000000000")))
        tick_size = Decimal(str(pf.get("tickSize", "0.01")))

        if price < min_price:
            raise ExchangeFilterValidationError(
                f"Price {price} is below minPrice {min_price} for {self._symbol}"
            )
        if price > max_price:
            raise ExchangeFilterValidationError(
                f"Price {price} exceeds maxPrice {max_price} for {self._symbol}"
            )

        if tick_size > 0:
            remainder = (price - min_price) % tick_size
            # Tolerate standard micro-epsilon floating/decimal residue
            if remainder > Decimal("1e-12") and remainder < (tick_size - Decimal("1e-12")):
                raise ExchangeFilterValidationError(
                    f"Price {price} does not align with tickSize {tick_size} (minPrice: {min_price})"
                )

    def validate_lot_size(self, quantity: Decimal) -> None:
        """Validate quantity against LOT_SIZE (minQty, maxQty, stepSize)."""
        ls = self._filters.get("LOT_SIZE")
        if not ls:
            return

        min_qty = Decimal(str(ls.get("minQty", "0")))
        max_qty = Decimal(str(ls.get("maxQty", "1000000000")))
        step_size = Decimal(str(ls.get("stepSize", "0.0001")))

        if quantity < min_qty:
            raise ExchangeFilterValidationError(
                f"Quantity {quantity} is below minQty {min_qty} for {self._symbol}"
            )
        if quantity > max_qty:
            raise ExchangeFilterValidationError(
                f"Quantity {quantity} exceeds maxQty {max_qty} for {self._symbol}"
            )

        if step_size > 0:
            remainder = (quantity - min_qty) % step_size
            if remainder > Decimal("1e-12") and remainder < (step_size - Decimal("1e-12")):
                raise ExchangeFilterValidationError(
                    f"Quantity {quantity} does not align with stepSize {step_size} (minQty: {min_qty})"
                )

    def validate_notional(self, quantity: Decimal, price: Decimal) -> None:
        """Validate notional against MIN_NOTIONAL or NOTIONAL filter."""
        notional_filter = self._filters.get("NOTIONAL") or self._filters.get("MIN_NOTIONAL")
        if not notional_filter:
            return

        min_notional = Decimal(str(notional_filter.get("minNotional", "10.0")))
        max_notional = Decimal(str(notional_filter.get("maxNotional", "1000000000.0")))
        notional = quantity * price
        if notional < min_notional:
            raise ExchangeFilterValidationError(
                f"Order notional {notional} is below minNotional {min_notional} for {self._symbol}"
            )
        if notional > max_notional:
            raise ExchangeFilterValidationError(
                f"Order notional {notional} exceeds maxNotional {max_notional} for {self._symbol}"
            )

    def validate_market_lot_size(self, quantity: Decimal, order_type: str) -> None:
        if order_type != "MARKET":
            return
        ls = self._filters.get("MARKET_LOT_SIZE")
        if not ls:
            return
        min_qty = Decimal(str(ls.get("minQty", "0")))
        max_qty = Decimal(str(ls.get("maxQty", "1000000000")))
        step_size = Decimal(str(ls.get("stepSize", "0.0001")))

        if quantity < min_qty:
            raise ExchangeFilterValidationError(f"Quantity {quantity} is below minQty {min_qty} for MARKET_LOT_SIZE")
        if quantity > max_qty:
            raise ExchangeFilterValidationError(f"Quantity {quantity} exceeds maxQty {max_qty} for MARKET_LOT_SIZE")
        if step_size > 0:
            remainder = (quantity - min_qty) % step_size
            if remainder > Decimal("1e-12") and remainder < (step_size - Decimal("1e-12")):
                raise ExchangeFilterValidationError(f"Quantity {quantity} does not align with stepSize {step_size} for MARKET_LOT_SIZE")

    def validate_max_position(self, current_position: Decimal, quantity: Decimal) -> None:
        f = self._filters.get("MAX_POSITION")
        if not f:
            return
        max_pos = Decimal(str(f.get("maxPosition", "1000000000")))
        if max_pos > 0 and (current_position + quantity) > max_pos:
            raise ExchangeFilterValidationError(f"Order quantity {quantity} would exceed maxPosition {max_pos}")

    def validate_max_num_orders(self, current_orders: int) -> None:
        f = self._filters.get("MAX_NUM_ORDERS")
        if not f:
            return
        max_num = int(f.get("maxNumOrders", 10000))
        if current_orders >= max_num:
            raise ExchangeFilterValidationError(f"Current open orders {current_orders} >= MAX_NUM_ORDERS {max_num}")

    def validate_max_num_algo_orders(self, current_algo_orders: int) -> None:
        f = self._filters.get("MAX_NUM_ALGO_ORDERS")
        if not f:
            return
        max_num = int(f.get("maxNumAlgoOrders", 10000))
        if current_algo_orders >= max_num:
            raise ExchangeFilterValidationError(f"Current algo orders {current_algo_orders} >= MAX_NUM_ALGO_ORDERS {max_num}")

    def validate_max_num_order_lists(self, current_order_lists: int) -> None:
        f = self._filters.get("MAX_NUM_ORDER_LISTS")
        if not f:
            return
        max_num = int(f.get("maxNumOrderLists", 10000))
        if current_order_lists >= max_num:
            raise ExchangeFilterValidationError(f"Current order lists {current_order_lists} >= MAX_NUM_ORDER_LISTS {max_num}")

    def validate_max_asset(self, current_asset: Decimal, amount: Decimal) -> None:
        f = self._filters.get("MAX_ASSET")
        if not f:
            return
        max_asset = Decimal(str(f.get("maxAsset", "1000000000")))
        if max_asset > 0 and (current_asset + amount) > max_asset:
            raise ExchangeFilterValidationError(f"Order amount {amount} would exceed maxAsset {max_asset}")

    def validate_exchange_max_num_orders(self, current_exchange_orders: int) -> None:
        # Pseudo-filter for exchange-wide limit
        f = self._filters.get("EXCHANGE_MAX_NUM_ORDERS")
        if not f:
            return
        max_num = int(f.get("maxNumOrders", 100000))
        if current_exchange_orders >= max_num:
            raise ExchangeFilterValidationError(f"Current exchange open orders {current_exchange_orders} >= EXCHANGE_MAX_NUM_ORDERS {max_num}")

    def validate_price_collar(
        self,
        price: Decimal,
        side: str,
        avg_price: Decimal,
        bid_multiplier_up: Decimal = Decimal("1.2"),
        bid_multiplier_down: Decimal = Decimal("0.8"),
        ask_multiplier_up: Decimal = Decimal("1.2"),
        ask_multiplier_down: Decimal = Decimal("0.8"),
    ) -> None:
        """Validate dynamic price collars against /api/v3/avgPrice (avgPriceMins)."""
        if avg_price <= Decimal("0"):
            return

        # Check PERCENT_PRICE or PERCENT_PRICE_BY_SIDE if present in exchange symbol info
        percent_filter = self._filters.get("PERCENT_PRICE_BY_SIDE") or self._filters.get("PERCENT_PRICE")
        if percent_filter:
            bid_multiplier_up = Decimal(str(percent_filter.get("bidMultiplierUp", bid_multiplier_up)))
            bid_multiplier_down = Decimal(str(percent_filter.get("bidMultiplierDown", bid_multiplier_down)))
            ask_multiplier_up = Decimal(str(percent_filter.get("askMultiplierUp", ask_multiplier_up)))
            ask_multiplier_down = Decimal(str(percent_filter.get("askMultiplierDown", ask_multiplier_down)))

        if side.upper() == "BUY":
            max_allowed = avg_price * bid_multiplier_up
            min_allowed = avg_price * bid_multiplier_down
            if price > max_allowed:
                raise ExchangeFilterValidationError(
                    f"BUY price {price} exceeds upper price collar {max_allowed} "
                    f"(avgPrice: {avg_price}, multiplier: {bid_multiplier_up})"
                )
            if price < min_allowed:
                raise ExchangeFilterValidationError(
                    f"BUY price {price} is below lower price collar {min_allowed} "
                    f"(avgPrice: {avg_price}, multiplier: {bid_multiplier_down})"
                )
        elif side.upper() == "SELL":
            max_allowed = avg_price * ask_multiplier_up
            min_allowed = avg_price * ask_multiplier_down
            if price > max_allowed:
                raise ExchangeFilterValidationError(
                    f"SELL price {price} exceeds upper price collar {max_allowed} "
                    f"(avgPrice: {avg_price}, multiplier: {ask_multiplier_up})"
                )
            if price < min_allowed:
                raise ExchangeFilterValidationError(
                    f"SELL price {price} is below lower price collar {min_allowed} "
                    f"(avgPrice: {avg_price}, multiplier: {ask_multiplier_down})"
                )

    def validate_all(
        self,
        side: str,
        price: Decimal,
        quantity: Decimal,
        avg_price: Optional[Decimal] = None,
        order_type: str = "LIMIT",
        current_position: Decimal = Decimal("0"),
        current_orders: int = 0,
        current_algo_orders: int = 0,
        current_order_lists: int = 0,
        current_asset: Decimal = Decimal("0"),
        current_exchange_orders: int = 0,
    ) -> None:
        """Perform comprehensive filter validation."""
        self.validate_trading_status()
        self.validate_price(price)
        self.validate_lot_size(quantity)
        self.validate_notional(quantity, price)
        self.validate_market_lot_size(quantity, order_type)
        self.validate_max_position(current_position, quantity)
        self.validate_max_num_orders(current_orders)
        self.validate_max_num_algo_orders(current_algo_orders)
        self.validate_max_num_order_lists(current_order_lists)
        self.validate_max_asset(current_asset, quantity)
        self.validate_exchange_max_num_orders(current_exchange_orders)
        if avg_price is not None:
            self.validate_price_collar(price, side, avg_price)

    def validate_order(
        self,
        symbol: str,
        price: Decimal,
        quantity: Decimal,
        side: str,
        current_avg_price: Optional[Decimal] = None,
        order_type: str = "LIMIT",
    ) -> None:
        """Validate order parameters against exchange filters."""
        self.validate_all(
            side=side, price=price, quantity=quantity, avg_price=current_avg_price, order_type=order_type
        )
