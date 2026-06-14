from freqtrade.persistence import Order
from pandas import DataFrame
import logging
from datetime import datetime, timedelta, timezone
from freqtrade.persistence import Trade
from freqtrade.strategy import (merge_informative_pair, DecimalParameter,
IStrategy, IntParameter, CategoricalParameter)
from typing import Optional
import talib.abstract as ta
import warnings
import pytz

warnings.filterwarnings("ignore", message="DataFrame is highly fragmented")
logger = logging.getLogger(__name__)


class ArbiStrategy(IStrategy):
    can_short: bool = False
    INTERFACE_VERSION = 3
    startup_candle_count = 0
    timeframe = '5m'
    process_only_new_candles = False
    position_adjustment_enable = False
    use_custom_stoploss = False
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = True
    trailing_stop = False
    stoploss = -0.99
    custom_info = {}

    plot_config = {
        'main_plot': {
            'ema21_1h': {'color': 'blue'},
            'ema55_1h': {'color': 'red'}
        }
    }

    def bot_start(self, **kwargs) -> None:
        open_trades = Trade.get_open_trades()
        for trade in open_trades:
            self.custom_info[trade.pair] = {'has_open_trade': 1}

    def bot_loop_start(self, **kwargs) -> None:
        for pair in self.dp.current_whitelist():
            self.custom_info[pair] = {'has_open_trade': 0}
        open_trades = Trade.get_open_trades()
        for trade in open_trades:
            self.custom_info[trade.pair] = {'has_open_trade': 1}

    def informative_pairs(self):
        pairs = self.dp.current_whitelist()
        informative_pairs = [(pair, '1h') for pair in pairs]
        return informative_pairs

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['ema21'] = ta.EMA(dataframe, timeperiod=21)
        dataframe['ema55'] = ta.EMA(dataframe, timeperiod=55)

        pair = metadata['pair']
        has_open_trade = self.custom_info.get(pair, {}).get('has_open_trade', 0)
        dataframe['has_open_trade'] = has_open_trade

        informative_1h = self.dp.get_pair_dataframe(pair=metadata['pair'], timeframe='1h')

        for indicator in [21, 55, 100]:
            informative_1h[f'ema{indicator}'] = ta.EMA(informative_1h, timeperiod=indicator)

        dataframe = merge_informative_pair(dataframe, informative_1h, self.timeframe, '1h', ffill=True)

        trend_window = 7
        mid_trend_threshold = 0.015

        dataframe['ema21_trend'] = dataframe['ema21_1h'].diff()
        dataframe['ema21_change'] = dataframe['ema21_1h'].pct_change(trend_window)
        dataframe['ema21_trend_sma'] = dataframe['ema21_trend'].rolling(window=trend_window).mean()
        dataframe['ema21_short_downtrend'] = dataframe['ema21_trend_sma'] < 0
        dataframe['ema21_short_uptrend'] = dataframe['ema21_trend_sma'] > 0
        dataframe['ema21_mid_change'] = dataframe['ema21_change'].rolling(window=trend_window).sum()
        dataframe['ema21_mid_downtrend'] = dataframe['ema21_mid_change'] < -mid_trend_threshold
        dataframe['ema21_mid_uptrend'] = dataframe['ema21_mid_change'] > mid_trend_threshold
        dataframe['ema21_downtrend'] = dataframe['ema21_short_downtrend'] | dataframe['ema21_mid_downtrend']
        dataframe['ema21_uptrend'] = dataframe['ema21_short_uptrend'] | dataframe['ema21_mid_uptrend']

        dataframe['ema55_trend'] = dataframe['ema55_1h'].diff()
        dataframe['ema55_change'] = dataframe['ema55_1h'].pct_change(trend_window)
        dataframe['ema55_trend_sma'] = dataframe['ema55_trend'].rolling(window=trend_window).mean()
        dataframe['ema55_short_downtrend'] = dataframe['ema55_trend_sma'] < 0
        dataframe['ema55_short_uptrend'] = dataframe['ema55_trend_sma'] > 0
        dataframe['ema55_mid_change'] = dataframe['ema55_change'].rolling(window=trend_window).sum()
        dataframe['ema55_mid_downtrend'] = dataframe['ema55_mid_change'] < -mid_trend_threshold
        dataframe['ema55_mid_uptrend'] = dataframe['ema55_mid_change'] > mid_trend_threshold
        dataframe['ema55_downtrend'] = dataframe['ema55_short_downtrend'] | dataframe['ema55_mid_downtrend']
        dataframe['ema55_uptrend'] = dataframe['ema55_short_uptrend'] | dataframe['ema55_mid_uptrend']

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe['volume'] > 0) &
            (dataframe['has_open_trade'] == 0),
            'enter_long'] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe['volume'] > 0) &
            (dataframe['has_open_trade'] == 1),
            'exit_long'] = 1
        return dataframe

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                            time_in_force: str, current_time: datetime, entry_tag: Optional[str],
                            side: str, **kwargs) -> bool:
        dataframe, last_updated = self.dp.get_analyzed_dataframe(pair=pair,
                                                                 timeframe=self.timeframe)
        ema_price = dataframe["ema55_1h"].iat[-1]
        if rate - ema_price >= 0.0001:
            logger.info(f"{pair}价格 大于 {ema_price}, 拒单")
            return False
        self.custom_info[pair] = {'has_open_trade': 1}
        return True

    def custom_entry_price(self, pair: str, current_time: datetime, proposed_rate: float,
                           entry_tag: Optional[str], **kwargs) -> float:
        dataframe, last_updated = self.dp.get_analyzed_dataframe(pair=pair,
                                                                 timeframe=self.timeframe)
        is_down = dataframe['ema21_downtrend'].iat[-1]

        closed_trades = Trade.get_trades(Trade.is_open.is_(False)).all()
        closed_trades.sort(key=lambda x: x.close_date, reverse=True)
        lastest_trade_close_date = closed_trades[0].close_date
        cutoff_date = lastest_trade_close_date + timedelta(minutes=30)

        ema_price = dataframe["ema55_1h"].iat[-1]

        if proposed_rate > ema_price:
            adjusted_rate = ema_price
            if is_down:
                adjusted_rate -= 0.0001
            logger.info(f"{pair}价格 调整为 {adjusted_rate}")
            return round(adjusted_rate, 4)
        else:
            adjusted_rate = proposed_rate
            if is_down:
                adjusted_rate -= 0.0001
        return round(adjusted_rate, 4)

    def custom_exit_price(self, pair: str, trade: Trade, current_time: datetime,
                          proposed_rate: float, current_profit: float, exit_tag: Optional[str], **kwargs) -> float:
        dataframe, last_updated = self.dp.get_analyzed_dataframe(pair=pair,
                                                                 timeframe=self.timeframe)
        buy_price = trade.open_rate
        filled_entries = trade.select_filled_orders(trade.entry_side)
        last_filled_utc = filled_entries[-1].order_filled_utc
        cutoff_date = last_filled_utc.date() + timedelta(days=2)
        cutoff_time = datetime.combine(cutoff_date, datetime.min.time(), tzinfo=pytz.UTC)

        is_up = dataframe['ema55_uptrend'].iat[-1]
        if is_up:
            adjusted_rate = buy_price + 0.0002
        else:
            adjusted_rate = buy_price + 0.0001
        if current_time.replace(tzinfo=pytz.UTC) > cutoff_time:
            if adjusted_rate > buy_price + 0.0001:
                return buy_price + 0.0001
        if buy_price > proposed_rate:
            adjusted_rate = proposed_rate
        if buy_price - adjusted_rate >= 0.00019:
            return None
        return round(adjusted_rate, 4)

    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, sell_reason: str,
                           current_time: datetime, **kwargs) -> bool:
        buy_price = trade.open_rate
        filled_entries = trade.select_filled_orders(trade.entry_side)
        last_filled_utc = filled_entries[-1].order_filled_utc
        cutoff_date = last_filled_utc.date() + timedelta(days=2)
        cutoff_time = datetime.combine(cutoff_date, datetime.min.time(), tzinfo=pytz.UTC)
        cutoff_date_long = last_filled_utc.date() + timedelta(days=3)
        cutoff_time_long = datetime.combine(cutoff_date_long, datetime.min.time(), tzinfo=pytz.UTC)

        if rate - buy_price >= 0.00019:
            logger.info(f"允许退出：{pair} spread {rate - buy_price}")
            self.custom_info[pair] = {'has_open_trade': 0}
            return True
        if current_time.replace(tzinfo=pytz.UTC) < cutoff_time:
            logger.info(f"禁止退出：{pair} 允许退出时间为 {cutoff_time.isoformat()}")
            return False
        if current_time.replace(tzinfo=pytz.UTC) > cutoff_time_long and rate <= buy_price:
            self.custom_info[pair] = {'has_open_trade': 0}
            return True
        if rate <= buy_price:
            logger.info(f"拒绝交易 {pair}: 卖出价格 ({rate}) 小于或等于买入价格 ({buy_price})")
            return False
        self.custom_info[pair] = {'has_open_trade': 0}
        return True
