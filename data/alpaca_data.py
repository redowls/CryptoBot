"""Alpaca crypto market data + account access.

CryptoHistoricalDataClient does not require API keys for historical bars/quotes,
but TradingClient does. Both are wrapped here so callers have one entry point.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient

BAR_COLUMNS = ["ts", "open", "high", "low", "close", "volume", "trade_count", "vwap"]


class AlpacaDataClient:
    def __init__(self, api_key: str | None = None, secret_key: str | None = None, paper: bool = True) -> None:
        self._api_key = api_key or os.getenv("ALPACA_API_KEY")
        self._secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY")
        self._paper = paper
        # Historical client works without keys but accepts them if provided.
        self._data_client = CryptoHistoricalDataClient(
            api_key=self._api_key or None,
            secret_key=self._secret_key or None,
        )
        self._trading_client: TradingClient | None = None

    def _trading(self) -> TradingClient:
        if self._trading_client is None:
            if not self._api_key or not self._secret_key:
                raise RuntimeError(
                    "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set for trading API calls"
                )
            self._trading_client = TradingClient(
                api_key=self._api_key,
                secret_key=self._secret_key,
                paper=self._paper,
            )
        return self._trading_client

    def get_bars(self, symbol: str, timeframe: TimeFrame, lookback_days: int) -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days)
        return self.get_bars_range(symbol, timeframe, start, end)

    def get_bars_range(
        self,
        symbol: str,
        timeframe: TimeFrame,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        request = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
        )
        barset = self._data_client.get_crypto_bars(request)
        df = barset.df
        if df.empty:
            return pd.DataFrame(columns=BAR_COLUMNS)

        # BarSet.df has a MultiIndex (symbol, timestamp). Flatten to a flat frame
        # with timestamp as a UTC column named `ts`.
        df = df.reset_index()
        if "symbol" in df.columns:
            df = df.drop(columns=["symbol"])
        df = df.rename(columns={"timestamp": "ts"})
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.sort_values("ts").reset_index(drop=True)
        # Some alpaca-py versions omit vwap/trade_count on certain pairs — fill if so.
        for col in ("trade_count", "vwap"):
            if col not in df.columns:
                df[col] = pd.NA
        return df[BAR_COLUMNS]

    def get_latest_quote(self, symbol: str):
        request = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes = self._data_client.get_crypto_latest_quote(request)
        return quotes[symbol]

    def get_account_info(self):
        return self._trading().get_account()
