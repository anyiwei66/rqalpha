# -*- coding: utf-8 -*-
#
# Copyright 2017 Ricequant, Inc
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import six

from rqalpha.environment import Environment
from rqalpha.const import POSITION_DIRECTION, POSITION_EFFECT, SIDE
from rqalpha.events import EVENT
from rqalpha.utils.repr import property_repr
from rqalpha.model.instrument import Instrument


class BookingModel(object):
    def __init__(self, data_proxy, long_positions=None, short_positions=None, backward_trade_set=None):
        self._data_proxy = data_proxy

        if long_positions is None:
            long_positions = BookingPositions(self._data_proxy, POSITION_DIRECTION.LONG)
        if short_positions is None:
            short_positions = BookingPositions(self._data_proxy, POSITION_DIRECTION.SHORT)

        self._positions_dict = {POSITION_DIRECTION.LONG: long_positions, POSITION_DIRECTION.SHORT: short_positions}
        self._backward_trade_set = backward_trade_set or set()

    def get_position(self, order_book_id, direction):
        return self._positions_dict[direction][order_book_id]

    def get_positions(self):
        total_positions = []
        for direction, positions in six.iteritems(self._positions_dict):
            for order_book_id, position in six.iteritems(positions):
                if position.quantity != 0:
                    total_positions.append(position)
        return total_positions

    def apply_settlement(self, trading_date):
        delete_list = []

        for direction, positions in six.iteritems(self._positions_dict):
            for order_book_id, position in six.iteritems(positions):
                if position.is_de_listed(trading_date) or position.quantity == 0:
                    delete_list.append((order_book_id, direction))
                else:
                    position.apply_settlement(trading_date)

        for order_book_id, direction in delete_list:
            self._positions_dict[direction].pop(order_book_id)

    def apply_trade(self, trade):
        if trade.exec_id in self._backward_trade_set:
            return
        order_book_id = trade.order_book_id

        if trade.position_effect == POSITION_EFFECT.OPEN:
            if trade.side == SIDE.BUY:
                position = self.get_position(order_book_id, POSITION_DIRECTION.LONG)
            elif trade.side == SIDE.SELL:
                position = self.get_position(order_book_id, POSITION_DIRECTION.SHORT)
            else:
                raise RuntimeError("unknown side, trade {}".format(trade))
        elif trade.position_effect in (POSITION_EFFECT.CLOSE, POSITION_EFFECT.CLOSE_TODAY):
            if trade.side == SIDE.BUY:
                position = self.get_position(order_book_id, POSITION_DIRECTION.SHORT)
            elif trade.side == SIDE.SELL:
                position = self.get_position(order_book_id, POSITION_DIRECTION.LONG)
            else:
                raise RuntimeError("unknown side, trade {}".format(trade))
        else:
            # NOTE: 股票如果没有position_effect就特殊处理
            position = self.get_position(order_book_id, POSITION_DIRECTION.LONG)

        position.apply_trade(trade)
        self._backward_trade_set.add(trade.exec_id)


class BookingPositions(dict):
    def __init__(self, data_proxy, direction):
        super(BookingPositions, self).__init__()
        self.direction = direction
        self._data_proxy = data_proxy

    def __missing__(self, key):
        self[key] = BookingPosition(self._data_proxy, key, self.direction)
        return self[key]

    def get_or_create(self, key):
        return self[key]


class BookingPosition(object):
    __repr__ = property_repr

    def __init__(
        self,
        data_proxy,
        order_book_id,
        direction,
        old_quantity=0,
        today_quantity=0,
        avg_price=0,
        prev_settlement_price=0,
    ):
        self._data_proxy = data_proxy

        self._order_book_id = order_book_id
        self._direction = direction
        self._old_quantity = old_quantity
        self._logical_old_quantity = old_quantity
        self._today_quantity = today_quantity
        self._avg_price = avg_price
        self._prev_settlement_price = prev_settlement_price

        self._trades = {}

    @property
    def order_book_id(self):
        """
        [Required]

        返回当前持仓的 order_book_id
        """
        return self._order_book_id

    @property
    def direction(self):
        """
        [Required]

        持仓方向
        """
        return self._direction

    @property
    def quantity(self):
        """
        [float] 总仓位
        """
        return self.old_quantity + self.today_quantity

    @property
    def old_quantity(self):
        """
        [float] 昨仓
        """
        return self._old_quantity

    @property
    def today_quantity(self):
        """
        [float] 今仓
        """
        return self._today_quantity

    @property
    def avg_price(self):
        """
        [float] 平均开仓价格
        """
        return self._avg_price

    @property
    def last_price(self):
        return self._data_proxy.current_snapshot(self._order_book_id).last

    @property
    def prev_settlement_price(self):
        return self._prev_settlement_price

    @property
    def trading_pnl(self):
        pnl = 0

        for trade in six.itervalues(self._trades):
            if trade.side == SIDE.BUY:
                price_spread = self.last_price - trade.last_price
            else:
                price_spread = trade.last_price - self.last_price

            contract_multiplier = self._data_proxy.instruments(self._order_book_id).contract_multiplier
            pnl += trade.last_quantity * contract_multiplier * price_spread

        return pnl

    @property
    def position_pnl(self):
        if self._logical_old_quantity == 0:
            return 0

        if self._direction == POSITION_DIRECTION.LONG:
            price_spread = self.last_price - self._prev_settlement_price
        else:
            price_spread = self._prev_settlement_price - self.last_price

        contract_multiplier = self._data_proxy.instruments(self._order_book_id).contract_multiplier

        return self._logical_old_quantity * contract_multiplier * price_spread

    def apply_settlement(self, trading_date):
        next_trading_date = self._data_proxy.get_next_trading_date(trading_date).date()
        # 今仓变昨仓
        self._old_quantity += self._today_quantity
        self._logical_old_quantity = self._old_quantity
        self._today_quantity = 0
        # 处理拆分
        split_ratio = self._data_proxy.get_split_by_ex_date(self._order_book_id, next_trading_date)
        if split_ratio:
            self._old_quantity *= split_ratio
            self._avg_price /= split_ratio
        # 清空缓存的交易
        self._trades.clear()
        # 更新结算价
        if self._data_proxy.instruments(self._order_book_id).type == "Future":
            self._prev_settlement_price = self._data_proxy.get_settle_price(self._order_book_id, trading_date)
        else:
            self._prev_settlement_price = self._data_proxy.history_bars(
                order_book_id=self._order_book_id, bar_count=1, frequency="1d", field="close", dt=trading_date
            )[0]

    def apply_trade(self, trade):
        if trade.exec_id in self._trades:
            return

        position_effect = self._get_position_effect(trade.side, trade.position_effect)

        if position_effect == POSITION_EFFECT.OPEN:
            if self.quantity < 0:
                if trade.last_quantity <= -1 * self.quantity:
                    self._avg_price = 0
                else:
                    self._avg_price = trade.last_price
            else:
                self._avg_price = (self.quantity * self._avg_price + trade.last_quantity * trade.last_price) / (
                    self.quantity + trade.last_quantity
                )
            self._today_quantity += trade.last_quantity
        elif position_effect == POSITION_EFFECT.CLOSE_TODAY:
            self._today_quantity -= trade.last_quantity
        elif position_effect == POSITION_EFFECT.CLOSE:
            # 先平昨，后平今
            self._old_quantity -= trade.last_quantity
            if self._old_quantity < 0:
                self._today_quantity += self._old_quantity
                self._old_quantity = 0
        else:
            raise RuntimeError("Unknown side when apply trade: ")

        self._trades[trade.exec_id] = trade

    def is_de_listed(self, trading_date):
        instrument = self._data_proxy.instruments(self._order_book_id)
        if instrument.de_listed_date is None or instrument.de_listed_date == Instrument.DEFAULT_DE_LISTED_DATE:
            return False
        if instrument.type == "Future":
            return instrument.de_listed_date.date() <= trading_date
        else:
            return self._data_proxy.get_previous_trading_date(instrument.de_listed_date).date() <= trading_date

    @staticmethod
    def _get_position_effect(side, position_effect):
        if position_effect is None:
            # NOTE: 股票如果没有position_effect就特殊处理
            if side == SIDE.BUY:
                return POSITION_EFFECT.OPEN
            elif side == SIDE.SELL:
                return POSITION_EFFECT.CLOSE
        return position_effect


class Booking(BookingModel):
    def __init__(self, long_positions=None, short_positions=None, backward_trade_set=None):
        self._env = Environment.get_instance()
        super(Booking, self).__init__(self._env.data_proxy, long_positions, short_positions, backward_trade_set)
        self.register_event()

    def register_event(self):
        event_bus = Environment.get_instance().event_bus
        event_bus.prepend_listener(EVENT.POST_SETTLEMENT, lambda e: self.apply_settlement(self._env.trading_dt.date()))
        event_bus.add_listener(EVENT.TRADE, lambda e: self.apply_trade(e.trade))

    def apply_trade(self, trade):
        if trade.exec_id in self._backward_trade_set:
            return

        order_book_id = trade.order_book_id

        long_positions = self._positions_dict[POSITION_DIRECTION.LONG]
        short_positions = self._positions_dict[POSITION_DIRECTION.SHORT]

        if trade.position_effect == POSITION_EFFECT.OPEN:
            if trade.side == SIDE.BUY:
                position = long_positions.get_or_create(order_book_id)
            elif trade.side == SIDE.SELL:
                position = short_positions.get_or_create(order_book_id)
            else:
                raise RuntimeError("unknown side, trade {}".format(trade))
        elif trade.position_effect in (POSITION_EFFECT.CLOSE, POSITION_EFFECT.CLOSE_TODAY):
            if trade.side == SIDE.BUY:
                position = short_positions.get_or_create(order_book_id)
            elif trade.side == SIDE.SELL:
                position = long_positions.get_or_create(order_book_id)
            else:
                raise RuntimeError("unknown side, trade {}".format(trade))
        else:
            # NOTE: 股票如果没有position_effect就特殊处理
            position = long_positions.get_or_create(order_book_id)

        position.apply_trade(trade)
        self._backward_trade_set.add(trade.exec_id)
