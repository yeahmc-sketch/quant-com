from dataclasses import asdict, dataclass
from typing import Dict, List, Optional


@dataclass
class PendingOrder:
    code: str
    signal_date: str
    execute_date: str
    target_weight: float
    signal_score: float
    is_strong: bool
    stage: int = 1
    allow_add: bool = False

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class Position:
    code: str
    entry_date: str
    entry_price: float
    quantity: float
    gross_amount: float
    entry_fee: float
    signal_score: float
    is_strong: bool
    hold_days: int = 0
    peak_price: float = 0.0
    add_on_count: int = 1

    @property
    def total_cost(self) -> float:
        return self.gross_amount + self.entry_fee

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class TradeRecord:
    trade_date: str
    side: str
    code: str
    price: float
    quantity: float
    gross_amount: float
    fee: float
    net_amount: float
    reason: str
    signal_score: float
    realized_pnl: float = 0.0
    realized_return: float = 0.0

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class Portfolio:
    def __init__(
        self,
        initial_cash: float,
        max_positions: int,
        commission_rate: float,
        min_commission: float,
        stamp_tax_rate: float,
        transfer_fee_rate: float,
        lot_size: Optional[int] = None,
    ) -> None:
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.max_positions = max_positions
        self.commission_rate = commission_rate
        self.min_commission = min_commission
        self.stamp_tax_rate = stamp_tax_rate
        self.transfer_fee_rate = transfer_fee_rate
        self.lot_size = lot_size
        self.positions: Dict[str, Position] = {}
        self.pending_orders: List[PendingOrder] = []
        self.trades: List[TradeRecord] = []
        self.consecutive_losses: int = 0

    def fee_for(self, side: str, gross_amount: float) -> float:
        commission = max(self.min_commission, gross_amount * self.commission_rate)
        transfer_fee = gross_amount * self.transfer_fee_rate
        stamp_tax = gross_amount * self.stamp_tax_rate if side == "SELL" else 0.0
        return commission + transfer_fee + stamp_tax

    def equity(self, price_lookup: Dict[str, float]) -> float:
        market_value = 0.0
        for code, position in self.positions.items():
            price = price_lookup.get(code, position.entry_price)
            market_value += position.quantity * price
        return self.cash + market_value

    def available_slots(self) -> int:
        pending_new_positions = sum(1 for order in self.pending_orders if not order.allow_add)
        return max(0, self.max_positions - len(self.positions) - pending_new_positions)

    def has_pending(self, code: str) -> bool:
        return any(order.code == code for order in self.pending_orders)

    def queue_buy(self, order: PendingOrder) -> None:
        if self.has_pending(order.code):
            return
        if order.code in self.positions and not order.allow_add:
            return
        self.pending_orders.append(order)

    def pop_orders(self, execute_date: str) -> List[PendingOrder]:
        ready = [order for order in self.pending_orders if order.execute_date == execute_date]
        self.pending_orders = [order for order in self.pending_orders if order.execute_date != execute_date]
        return ready

    def _calc_buy_lot(self, target_amount: float, price: float) -> float:
        if self.lot_size:
            return int(target_amount / price / self.lot_size) * self.lot_size
        return target_amount / price

    def buy(
        self,
        order: PendingOrder,
        trade_date: str,
        price: float,
        reference_equity: float,
    ) -> Optional[TradeRecord]:
        if price <= 0:
            return None

        existing = self.positions.get(order.code)
        if existing and not order.allow_add:
            return None
        if existing and existing.add_on_count >= max(order.stage, 2):
            return None

        target_amount = min(self.cash, max(reference_equity, self.cash) * order.target_weight)
        if target_amount <= price:
            return None

        quantity = self._calc_buy_lot(target_amount, price)
        if quantity <= 0:
            return None

        gross_amount = quantity * price
        fee = self.fee_for("BUY", gross_amount)

        if gross_amount + fee > self.cash:
            if self.lot_size:
                while quantity > 0 and gross_amount + fee > self.cash:
                    quantity -= self.lot_size
                    gross_amount = quantity * price
                    fee = self.fee_for("BUY", gross_amount) if quantity > 0 else 0.0
            else:
                quantity = max(0.0, (self.cash - self.min_commission) / price)
                gross_amount = quantity * price
                fee = self.fee_for("BUY", gross_amount) if quantity > 0 else 0.0

        if quantity <= 0:
            return None

        self.cash -= gross_amount + fee

        if existing:
            total_quantity = existing.quantity + quantity
            total_gross = existing.gross_amount + gross_amount
            total_fee = existing.entry_fee + fee
            existing.entry_price = total_gross / total_quantity if total_quantity > 0 else existing.entry_price
            existing.quantity = total_quantity
            existing.gross_amount = total_gross
            existing.entry_fee = total_fee
            existing.signal_score = max(existing.signal_score, order.signal_score)
            existing.is_strong = existing.is_strong or order.is_strong
            existing.peak_price = max(existing.peak_price, price)
            existing.add_on_count = max(existing.add_on_count, order.stage)
            reason = f"add_on_stage{order.stage}@{order.signal_date}"
        else:
            self.positions[order.code] = Position(
                code=order.code,
                entry_date=trade_date,
                entry_price=price,
                quantity=quantity,
                gross_amount=gross_amount,
                entry_fee=fee,
                signal_score=order.signal_score,
                is_strong=order.is_strong,
                peak_price=price,
                add_on_count=max(1, order.stage),
            )
            reason = f"signal_stage{order.stage}@{order.signal_date}"

        trade = TradeRecord(
            trade_date=trade_date,
            side="BUY",
            code=order.code,
            price=price,
            quantity=quantity,
            gross_amount=gross_amount,
            fee=fee,
            net_amount=-(gross_amount + fee),
            reason=reason,
            signal_score=order.signal_score,
        )
        self.trades.append(trade)
        return trade

    def sell(self, code: str, trade_date: str, price: float, reason: str) -> Optional[TradeRecord]:
        position = self.positions.get(code)
        if not position or price <= 0:
            return None

        gross_amount = position.quantity * price
        fee = self.fee_for("SELL", gross_amount)
        net_amount = gross_amount - fee
        realized_pnl = net_amount - position.total_cost
        realized_return = realized_pnl / position.total_cost if position.total_cost else 0.0

        self.cash += net_amount
        del self.positions[code]
        self.consecutive_losses = 0 if realized_pnl > 0 else self.consecutive_losses + 1

        trade = TradeRecord(
            trade_date=trade_date,
            side="SELL",
            code=code,
            price=price,
            quantity=position.quantity,
            gross_amount=gross_amount,
            fee=fee,
            net_amount=net_amount,
            reason=reason,
            signal_score=position.signal_score,
            realized_pnl=realized_pnl,
            realized_return=realized_return,
        )
        self.trades.append(trade)
        return trade


__all__ = ["PendingOrder", "Portfolio", "Position", "TradeRecord"]
