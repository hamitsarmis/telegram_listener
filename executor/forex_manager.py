import asyncio
import concurrent.futures
import logging
import os
import random
import threading
import uuid
from typing import Optional, Literal, Dict, Any
import traceback
from datetime import datetime, timezone

from metaapi_cloud_sdk import MetaApi
from metaapi_cloud_sdk.clients.error_handler import ValidationException

Side = Literal["buy", "sell"]

# Set initial SL/TP on a freshly opened market order. Off by default — the
# trade-manager applies trailing/breakeven on its own cycle, so the broker-side
# stop is opt-in via env var.
ENABLE_INITIAL_SL_TP = os.getenv("ENABLE_INITIAL_SL_TP", "").lower() in ("1", "true", "yes", "on")


class _ConnectionWorker:
  """
  Owns a dedicated OS thread, asyncio event loop, MetaApi client, and RPC
  connection. Used to isolate background work (heartbeat, trade manager) from
  the trading-path connection so RPCs never queue behind monitoring traffic
  on a shared socket.

  Thread/loop lifecycle:
    - start_thread()              : spawn the thread, block until its loop is up
    - submit(coro)                : schedule a coroutine on this worker's loop
                                    from any thread; returns concurrent Future
    - stop_thread_and_join()      : disconnect, stop the loop, join the thread
  """

  def __init__(
    self,
    name: str,
    token: str,
    account_id: str,
    log: logging.Logger,
    parent_stop: threading.Event,
    *,
    rpc_timeout_sec: float,
    ready_wait_timeout_sec: float,
    reconnect_base_delay_sec: float,
    reconnect_max_delay_sec: float,
  ):
    self.name = name
    self.token = token
    self.account_id = account_id
    self.log = log
    self.parent_stop = parent_stop

    self.rpc_timeout_sec = rpc_timeout_sec
    self.ready_wait_timeout_sec = ready_wait_timeout_sec
    self.reconnect_base_delay_sec = reconnect_base_delay_sec
    self.reconnect_max_delay_sec = reconnect_max_delay_sec

    self.loop: Optional[asyncio.AbstractEventLoop] = None
    self.thread: Optional[threading.Thread] = None
    self._loop_started = threading.Event()

    self._api: Optional[MetaApi] = None
    self._account = None
    self.conn = None

    # Created on the worker's own loop in _run_loop.
    self._ready: Optional[asyncio.Event] = None
    self._reconnect_lock: Optional[asyncio.Lock] = None

  def start_thread(self, timeout: float = 5.0) -> None:
    self.thread = threading.Thread(
      target=self._run_loop,
      name=f"forexmanager-{self.name}",
      daemon=True,
    )
    self.thread.start()
    if not self._loop_started.wait(timeout=timeout):
      raise RuntimeError(f"[{self.name}] worker loop failed to start within {timeout}s")

  def _run_loop(self) -> None:
    self.loop = asyncio.new_event_loop()
    asyncio.set_event_loop(self.loop)
    self._ready = asyncio.Event()
    self._reconnect_lock = asyncio.Lock()
    self._loop_started.set()
    try:
      self.loop.run_forever()
    finally:
      try:
        pending = asyncio.all_tasks(self.loop)
        for t in pending:
          t.cancel()
        if pending:
          self.loop.run_until_complete(
            asyncio.gather(*pending, return_exceptions=True)
          )
      except Exception:
        pass
      self.loop.close()

  def submit(self, coro) -> concurrent.futures.Future:
    if self.loop is None:
      raise RuntimeError(f"[{self.name}] worker loop not started")
    return asyncio.run_coroutine_threadsafe(coro, self.loop)

  async def connect_and_sync(self) -> None:
    self._ready.clear()
    self.log.warning("[%s] Connecting to MetaApi...", self.name)
    self._api = MetaApi(self.token)
    self._account = await self._api.metatrader_account_api.get_account(self.account_id)
    if self._account.state != "DEPLOYED":
      self.log.warning("[%s] Account not deployed, deploying...", self.name)
      await self._account.deploy()
    self.log.warning("[%s] Waiting for MetaApi server connection...", self.name)
    await self._account.wait_connected()
    self.log.warning("[%s] Creating RPC connection...", self.name)
    self.conn = self._account.get_rpc_connection()
    await self.conn.connect()
    self.log.warning("[%s] Waiting for synchronization...", self.name)
    await self.conn.wait_synchronized()
    self.log.warning("[%s] READY ✅", self.name)
    self._ready.set()

  async def disconnect(self) -> None:
    self._ready.clear()
    try:
      if self.conn:
        await self.conn.close()
        await asyncio.sleep(1)
    except Exception:
      pass
    self.conn = None
    self._account = None
    self._api = None

  async def trigger_reconnect(self) -> None:
    async with self._reconnect_lock:
      if self.parent_stop.is_set():
        return
      if self._ready.is_set():
        return
      self._ready.clear()
      delay = self.reconnect_base_delay_sec
      while not self.parent_stop.is_set():
        try:
          await self.disconnect()
          await self.connect_and_sync()
          return
        except Exception as e:
          jitter = random.uniform(0, 0.5)
          sleep_for = min(self.reconnect_max_delay_sec, delay + jitter)
          self.log.warning(
            "[%s] Reconnect failed (%s). Retrying in %.1fs...",
            self.name, repr(e), sleep_for,
          )
          await asyncio.sleep(sleep_for)
          delay = min(self.reconnect_max_delay_sec, delay * 2)

  async def ensure_ready(self) -> None:
    if self._ready.is_set():
      return
    try:
      await asyncio.wait_for(self._ready.wait(), timeout=self.ready_wait_timeout_sec)
    except asyncio.TimeoutError:
      raise RuntimeError(
        f"[{self.name}] not ready after {self.ready_wait_timeout_sec}s"
      )

  async def stop_thread_and_join(self, timeout: float = 10.0) -> None:
    """Disconnect, stop the loop, join the thread. Safe to call from main loop."""
    if self.loop and self.loop.is_running():
      try:
        fut = asyncio.run_coroutine_threadsafe(self.disconnect(), self.loop)
        await asyncio.wait_for(asyncio.wrap_future(fut), timeout=timeout)
      except Exception:
        pass
      self.loop.call_soon_threadsafe(self.loop.stop)
    if self.thread:
      await asyncio.to_thread(self.thread.join, timeout)


class ForexManager:
  """
  Using MetaApi Cloud SDK:
    - Connect / Sync
    - Open position with market order
    - Close position with market order (using positionId)
    - Check connection using heartbeat, reconnect if needed

  Requirements:
    pip install metaapi-cloud-sdk
  """

  def __init__(
    self,
    token: str,
    account_id: str,
    *,
    heartbeat_interval_sec: int = 10,
    reconnect_base_delay_sec: float = 2.0,
    reconnect_max_delay_sec: float = 30.0,
    rpc_timeout_sec: float = 30.0,
    ready_wait_timeout_sec: float = 60.0,
    enable_trade_manager: bool = False,
    logger: Optional[logging.Logger] = None
  ):
    self.token = token
    self.account_id = account_id

    self.heartbeat_interval_sec = heartbeat_interval_sec
    self.reconnect_base_delay_sec = reconnect_base_delay_sec
    self.reconnect_max_delay_sec = reconnect_max_delay_sec
    self.rpc_timeout_sec = rpc_timeout_sec
    self.ready_wait_timeout_sec = ready_wait_timeout_sec
    self.enable_trade_manager = enable_trade_manager

    self.log = logger or logging.getLogger("ForexManager")
    self.log.setLevel(logging.WARNING)
    
    # Add handlers if not already present
    if not self.log.handlers:
      formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
      
      # Console handler
      console_handler = logging.StreamHandler()
      console_handler.setLevel(logging.INFO)
      console_handler.setFormatter(formatter)
      self.log.addHandler(console_handler)
      
      # File handler - log to app.log
      file_handler = logging.FileHandler('app.log')
      file_handler.setLevel(logging.WARNING)
      file_handler.setFormatter(formatter)
      self.log.addHandler(file_handler)

    self._api: Optional[MetaApi] = None
    self._account = None
    self._conn = None

    self._ready = asyncio.Event()
    # threading.Event so the heartbeat / trade-manager threads can read it.
    self._stop = threading.Event()
    self._heartbeat_task: Optional[concurrent.futures.Future] = None

    # only one reconnect attempt at a time (trading conn)
    self._reconnect_lock = asyncio.Lock()

    # Cross-thread protection for shared state mutated by trading thread
    # (open_market_order, close_all_positions) AND trade-manager thread
    # (_manage_all_positions, _check_closed_positions_profit).
    self._state_lock = threading.Lock()

    # Dedicated workers (own thread + loop + RPC connection) so monitoring
    # traffic never queues behind trading RPCs on the shared socket.
    self._hb_worker = _ConnectionWorker(
      name="heartbeat",
      token=token,
      account_id=account_id,
      log=self.log,
      parent_stop=self._stop,
      rpc_timeout_sec=self.rpc_timeout_sec,
      ready_wait_timeout_sec=self.ready_wait_timeout_sec,
      reconnect_base_delay_sec=self.reconnect_base_delay_sec,
      reconnect_max_delay_sec=self.reconnect_max_delay_sec,
    )
    self._tm_worker = _ConnectionWorker(
      name="trade-manager",
      token=token,
      account_id=account_id,
      log=self.log,
      parent_stop=self._stop,
      rpc_timeout_sec=self.rpc_timeout_sec,
      ready_wait_timeout_sec=self.ready_wait_timeout_sec,
      reconnect_base_delay_sec=self.reconnect_base_delay_sec,
      reconnect_max_delay_sec=self.reconnect_max_delay_sec,
    )

    # Trade management state: {position_id: {"open_price", "direction", "symbol", "volume", "peak_profit"}}
    self._trade_state: Dict[str, Dict[str, Any]] = {}
    self._trade_manager_task: Optional[concurrent.futures.Future] = None
    self._trade_manager_interval_sec = 5  # Check every 5 seconds

    # --- MFE/MAE-optimized parameters from 90-day backtest ---
    self.initial_sl_distance = 15.0
    self.breakeven_profit = 15.0
    self.trail_activation = 20.0
    self.trail_distance = 5.0

    # Pending re-entry after SL: {symbol: {"entry_price", "direction", "volume"}}
    self._pending_reentry: Dict[str, Dict[str, Any]] = {}

    # Last signal tracking
    self.last_signal: Optional[Dict[str, Any]] = None

    # Concurrency: per-symbol locks + close-all barrier.
    # - Opens for different symbols run in parallel.
    # - Opens for the same symbol serialize.
    # - close_all_positions() drains every symbol lock before running, and
    #   sets _close_all_in_progress so newly arriving opens wait it out.
    self._symbol_locks: Dict[str, asyncio.Lock] = {}
    self._close_all_lock = asyncio.Lock()
    self._close_all_in_progress = asyncio.Event()

  # ----------------------------
  # Public lifecycle
  # ----------------------------
  async def start(self) -> None:
    """Connects trading conn, then spawns heartbeat & trade-manager workers
    each on its own dedicated thread + event loop + RPC connection."""
    self._stop.clear()

    # 1. Trading connection — lives on the caller's (main) event loop.
    await self._connect_and_sync()

    # 2. Heartbeat worker — own thread, own connection.
    self._hb_worker.start_thread()
    await asyncio.wrap_future(self._hb_worker.submit(self._hb_worker.connect_and_sync()))
    self._heartbeat_task = self._hb_worker.submit(self._heartbeat_loop())

    # 3. Trade manager worker — own thread, own connection.
    if self.enable_trade_manager:
      self._tm_worker.start_thread()
      await asyncio.wrap_future(self._tm_worker.submit(self._tm_worker.connect_and_sync()))
      self._trade_manager_task = self._tm_worker.submit(self._trade_manager_loop())
    else:
      self.log.warning("Trade manager disabled (set ENABLE_TRADE_MANAGER=1 to enable)")

  async def stop(self) -> None:
    """Stops heartbeat & trade-manager workers and closes trading connection."""
    self._stop.set()

    if self._heartbeat_task is not None:
      self._heartbeat_task.cancel()
      self._heartbeat_task = None
    await self._hb_worker.stop_thread_and_join()

    if self._trade_manager_task is not None:
      self._trade_manager_task.cancel()
      self._trade_manager_task = None
    await self._tm_worker.stop_thread_and_join()

    await self._disconnect()

  async def wait_ready(self, timeout: Optional[float] = None) -> bool:
    """Waits until bot is ready and synchronized."""
    try:
      await asyncio.wait_for(self._ready.wait(), timeout=timeout)
      return True
    except asyncio.TimeoutError:
      return False

  # ----------------------------
  # Trading ops
  # ----------------------------
  @staticmethod
  def _current_task_is_cancelling() -> bool:
    """True iff our current asyncio task is being cancelled via task.cancel().

    Distinguishes a legitimate outer cancel (e.g. shutdown) from the SDK
    cancelling an in-flight RPC on its internal socket — the latter raises
    CancelledError inside our await without marking our task as cancelling.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0

  def _get_symbol_lock(self, symbol: str) -> asyncio.Lock:
    """Returns (and lazily creates) the asyncio.Lock for a symbol."""
    lock = self._symbol_locks.get(symbol)
    if lock is None:
      lock = asyncio.Lock()
      self._symbol_locks[symbol] = lock
    return lock

  async def _wait_for_close_all_done(self) -> None:
    """If a close-all is in progress, block until it finishes."""
    while self._close_all_in_progress.is_set():
      await asyncio.sleep(0.01)

  async def open_market_order(
    self,
    symbol: str,
    side: Side,
    volume: float,
    *,
    magic: Optional[int] = None,
    slippage: Optional[float] = None,
    update_last_signal: bool = True,
  ) -> Dict[str, Any]:

    await self._ensure_ready()
    await self._wait_for_close_all_done()

    # Per-call client_id lets us detect on retry whether the first attempt's
    # order actually reached the broker — guards against duplicate positions
    # when the SDK cancels a request after the broker has already accepted it.
    # MetaApi enforces a three-part `strategy_position_order` underscore
    # format (alphanumeric segments). 23 chars, 80 bits of entropy.
    # See https://metaapi.cloud/docs/client/clientIdUsage/
    hex_id = uuid.uuid4().hex
    client_id = f"sp_{hex_id[:10]}_{hex_id[10:20]}"

    # MetaApi SDK takes clientId/magic/slippage via the `options` dict
    # (CreateMarketTradeOptions), not as top-level kwargs.
    options: Dict[str, Any] = {"clientId": client_id}
    if magic is not None:
        options["magic"] = magic
    if slippage is not None:
        options["slippage"] = slippage

    params = {
        "symbol": symbol,
        "volume": volume,
        "options": options,
    }

    sym_lock = self._get_symbol_lock(symbol)
    async with sym_lock:
      self.log.info(
          "open_market_order -> MetaApi: side=%s params=%r", side, params,
      )
      try:
        return await self._execute_market_order(side, params, symbol, volume, update_last_signal)
      except asyncio.CancelledError:
        if self._current_task_is_cancelling():
          raise
        # SDK cancelled our RPC during an internal reconnect — transient.
        # Clear _ready so _trigger_reconnect actually rebuilds the connection
        # instead of short-circuiting on a stale ready flag.
        self.log.warning("open_market_order cancelled mid-RPC (SDK reconnect). Triggering reconnect...")
        self._ready.clear()
      except ValidationException as e:
        # Server rejected the payload (HTTP 400). Retrying won't help — surface
        # the field-level details so we can fix the request, then propagate.
        self.log.error(
            "open_market_order rejected by MetaApi (side=%s params=%r): %s | details=%r",
            side, params, e, e.details,
        )
        raise
      except Exception as e:
        self.log.warning(
            "open_market_order failed (%s). Triggering reconnect...", repr(e)
        )
        self._ready.clear()

      await self._trigger_reconnect()

      # Did the first attempt actually reach the broker? Polling get_positions
      # by client_id avoids placing a duplicate when the SDK cancellation
      # happened *after* the order was accepted.
      existing = await self._find_position_by_client_id(client_id)
      if existing is not None:
        self.log.warning(
            "First attempt succeeded (position %s found via client_id=%s); skipping retry.",
            existing.get("id"), client_id,
        )
        await self._finalize_market_order(existing, side, symbol, volume, update_last_signal)
        return {"positionId": existing.get("id")}

      # First attempt didn't reach the broker — safe to retry.
      try:
          self.log.warning("Retrying open_market_order after reconnect...")
          return await self._execute_market_order(side, params, symbol, volume, update_last_signal)
      except asyncio.CancelledError:
          if self._current_task_is_cancelling():
            raise
          # Inner retry was also cancelled mid-RPC. Clear _ready so any outer
          # retry layer (listen_signals) gets a fresh reconnect on its next try.
          self.log.error("Retry of open_market_order also cancelled mid-RPC. Giving up.")
          self._ready.clear()
          raise
      except Exception as retry_e:
          self.log.warning("Retry also failed (%s). Giving up.", repr(retry_e))
          raise

  async def _execute_market_order(self, side, params, symbol, volume, update_last_signal):
    if side == "buy":
        response = await asyncio.wait_for(
            self._conn.create_market_buy_order(**params),
            timeout=self.rpc_timeout_sec,
        )
    else:
        response = await asyncio.wait_for(
            self._conn.create_market_sell_order(**params),
            timeout=self.rpc_timeout_sec,
        )

    position_id = response["positionId"]

    # Poll for the position to appear instead of a fixed sleep.
    # MetaApi can take 50-300ms to reflect a new market order.
    position = await self._poll_for_position(position_id)

    await self._finalize_market_order(position, side, symbol, volume, update_last_signal)
    return response

  async def _finalize_market_order(self, position, side, symbol, volume, update_last_signal):
    """Set SL/TP and update last_signal/trade_state for a freshly opened position.

    Safe to call twice for the same logical entry: SL/TP failure is logged
    but does NOT prevent _trade_state from being written (so the trade-manager
    will pick up the position and apply trailing on the next cycle), and
    last_signal is only (re)written if it doesn't already match this entry —
    avoiding date_created drift that would shift the deal-history window
    consulted by _check_closed_positions_profit.
    """
    position_id = position["id"]
    open_price = position["openPrice"]

    sl_distance = self.initial_sl_distance

    if update_last_signal and ENABLE_INITIAL_SL_TP:
        if side == "buy":
            sl = open_price - sl_distance
            tp = open_price + 2.0
        else:
            sl = open_price + sl_distance
            tp = open_price - 2.0
        try:
            await asyncio.wait_for(
                self._conn.modify_position(position_id, stop_loss=sl, take_profit=tp),
                timeout=self.rpc_timeout_sec,
            )
            self.log.warning(f"Set SL={sl:.2f}, TP={tp:.2f} for position {position_id} (distance={sl_distance:.1f})")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.log.error(
                "CRITICAL: failed to set SL/TP on position %s (%s %s vol=%s open=%s): %s. "
                "Position is OPEN WITHOUT PROTECTIVE STOP. Recording in trade_state "
                "so trade-manager applies trailing on the next cycle.",
                position_id, side, symbol, volume, open_price, repr(e),
            )

    if update_last_signal:
        with self._state_lock:
          # Skip rewrite if last_signal already matches this entry — preserves
          # date_created so _check_closed_positions_profit's
          # get_deals_by_time_range window still covers the original deals.
          already_set = (
              self.last_signal is not None
              and self.last_signal.get("symbol") == symbol
              and self.last_signal.get("first_entry_price") == open_price
              and self.last_signal.get("action") == side
          )
          if not already_set:
            self.last_signal = {
                "date_created": datetime.now(timezone.utc).isoformat(),
                "action": side,
                "price": open_price,
                "first_entry_price": open_price,
                "symbol": symbol,
                "volume": volume,
                "has_profit": False,
                "has_pending_order": False,
            }
        if not already_set:
          try:
            await self.delete_all_pending_orders()
          except asyncio.CancelledError:
            raise
          except Exception as e:
            self.log.warning(
              "delete_all_pending_orders raised in _finalize_market_order: %s", repr(e),
            )
          self.log.warning(f"Last signal set: {self.last_signal}")
        else:
          self.log.warning(
            "last_signal already set for this entry (symbol=%s action=%s first_entry_price=%s); "
            "skipping rewrite.", symbol, side, open_price,
          )

    # Always record trade_state so the trade-manager can manage the position,
    # regardless of whether SL/TP set above succeeded.
    with self._state_lock:
      self._trade_state[position_id] = {
          "open_price": open_price,
          "direction": side,
          "symbol": symbol,
          "volume": volume,
          "peak_profit": 0.0,
      }
    self.log.warning(
        f"Trade state for {position_id}: entry={open_price}, dir={side}, "
        f"SL={sl_distance:.1f}, BE={self.breakeven_profit:.1f}, "
        f"trail@{self.trail_activation:.1f} dist={self.trail_distance:.1f}"
    )

  async def open_pending_order(
    self,
    symbol: str,
    side: Side,
    volume: float,
    *,
    entry_price: float,
    sl_price: Optional[float] = None,
    tp_price: Optional[float] = None,
  ) -> Dict[str, Any]:
    """Place a pending entry order at `entry_price`.

    MetaApi has separate limit/stop methods depending on which side of the
    current price the entry sits on:
      - buy: limit if entry < ask, stop if entry >= ask
      - sell: limit if entry > bid, stop if entry <= bid
    We pick the right one automatically — the broker rejects mismatches.
    """
    await self._ensure_ready()
    await self._wait_for_close_all_done()

    sym_lock = self._get_symbol_lock(symbol)
    async with sym_lock:
      price_data = await asyncio.wait_for(
          self._conn.get_symbol_price(symbol),
          timeout=self.rpc_timeout_sec,
      )
      current_ask = price_data["ask"]
      current_bid = price_data["bid"]

      kwargs: Dict[str, Any] = {
          "symbol": symbol,
          "volume": volume,
          "open_price": entry_price,
      }
      if sl_price is not None:
        kwargs["stop_loss"] = sl_price
      if tp_price is not None:
        kwargs["take_profit"] = tp_price

      if side == "buy":
        is_limit = entry_price < current_ask
        method = (
            self._conn.create_limit_buy_order if is_limit
            else self._conn.create_stop_buy_order
        )
      else:
        is_limit = entry_price > current_bid
        method = (
            self._conn.create_limit_sell_order if is_limit
            else self._conn.create_stop_sell_order
        )

      kind = "limit" if is_limit else "stop"
      self.log.info(
          "open_pending_order -> MetaApi: %s %s %s vol=%s @ %s (ask=%s bid=%s)",
          kind, side, symbol, volume, entry_price, current_ask, current_bid,
      )

      try:
        response = await asyncio.wait_for(method(**kwargs), timeout=self.rpc_timeout_sec)
      except ValidationException as e:
        self.log.error(
            "open_pending_order rejected by MetaApi (%s %s %s @ %s): %s | details=%r",
            kind, side, symbol, entry_price, e, e.details,
        )
        raise

      self.log.warning("Pending order created (%s %s %s @ %s): %s",
                       kind, side, symbol, entry_price, response)
      return response

  async def modify_positions_for_symbol(
    self,
    symbol: str,
    *,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
  ) -> int:
    """Apply the same SL and/or TP to every open position on `symbol`.

    Returns the number of positions modified. Logs and continues on per-position
    failure so a partial outage doesn't drop the rest of the batch.
    """
    if stop_loss is None and take_profit is None:
      return 0

    await self._ensure_ready()
    positions = await self.get_positions()
    matches = [p for p in positions if p.get("symbol") == symbol]
    if not matches:
      self.log.warning("modify_positions_for_symbol: no open positions for %s", symbol)
      return 0

    kwargs: Dict[str, Any] = {}
    if stop_loss is not None:
      kwargs["stop_loss"] = stop_loss
    if take_profit is not None:
      kwargs["take_profit"] = take_profit

    modified = 0
    for pos in matches:
      pid = pos["id"]
      try:
        await asyncio.wait_for(
            self._conn.modify_position(pid, **kwargs),
            timeout=self.rpc_timeout_sec,
        )
        self.log.warning("modify_position %s (%s): %s", pid, symbol, kwargs)
        modified += 1
      except Exception as e:
        self.log.error("modify_position %s failed: %s", pid, repr(e))
    return modified

  async def move_stop_to_entry_for_symbol(self, symbol: str) -> int:
    """Move SL to each position's own openPrice (breakeven)."""
    await self._ensure_ready()
    positions = await self.get_positions()
    matches = [p for p in positions if p.get("symbol") == symbol]
    if not matches:
      self.log.warning("move_stop_to_entry_for_symbol: no open positions for %s", symbol)
      return 0

    modified = 0
    for pos in matches:
      pid = pos["id"]
      open_price = pos.get("openPrice")
      if open_price is None:
        self.log.warning("position %s has no openPrice; skipping", pid)
        continue
      try:
        await asyncio.wait_for(
            self._conn.modify_position(pid, stop_loss=open_price),
            timeout=self.rpc_timeout_sec,
        )
        self.log.warning("move_stop_to_entry: position %s SL→%s", pid, open_price)
        modified += 1
      except Exception as e:
        self.log.error("modify_position (move_to_entry) %s failed: %s", pid, repr(e))
    return modified

  async def partial_close_for_symbol(self, symbol: str, fraction: float) -> int:
    """Close `fraction` (0..1) of each open position's volume on `symbol`."""
    if not (0 < fraction <= 1):
      self.log.warning("partial_close_for_symbol: invalid fraction %r", fraction)
      return 0

    await self._ensure_ready()
    positions = await self.get_positions()
    matches = [p for p in positions if p.get("symbol") == symbol]
    if not matches:
      self.log.warning("partial_close_for_symbol: no open positions for %s", symbol)
      return 0

    closed = 0
    for pos in matches:
      pid = pos["id"]
      vol = pos.get("volume") or 0
      close_vol = round(vol * fraction, 2)
      if close_vol <= 0:
        self.log.warning("partial_close: position %s volume too small (%s × %s)", pid, vol, fraction)
        continue
      try:
        await asyncio.wait_for(
            self._conn.close_position_partially(pid, close_vol),
            timeout=self.rpc_timeout_sec,
        )
        self.log.warning("partial_close: position %s closed %s of %s", pid, close_vol, vol)
        closed += 1
      except Exception as e:
        self.log.error("partial_close %s failed: %s", pid, repr(e))
    return closed

  async def update_takeprofit_from_pips_for_symbol(
    self, symbol: str, base_price: float, distance: float
  ) -> int:
    """Set TP = base_price ± distance per position, based on its direction.

    `distance` is in price units (executor multiplies pips × pip_value).
    Buy positions get base+distance; sell positions get base-distance.
    """
    await self._ensure_ready()
    positions = await self.get_positions()
    matches = [p for p in positions if p.get("symbol") == symbol]
    if not matches:
      self.log.warning("update_takeprofit_from_pips: no open positions for %s", symbol)
      return 0

    modified = 0
    for pos in matches:
      pid = pos["id"]
      # MetaApi position type: "POSITION_TYPE_BUY" or "POSITION_TYPE_SELL"
      pos_type = pos.get("type", "")
      is_buy = "BUY" in pos_type.upper()
      tp = base_price + distance if is_buy else base_price - distance
      try:
        await asyncio.wait_for(
            self._conn.modify_position(pid, take_profit=tp),
            timeout=self.rpc_timeout_sec,
        )
        self.log.warning("update_takeprofit_from_pips: position %s (%s) TP→%s", pid, pos_type, tp)
        modified += 1
      except Exception as e:
        self.log.error("modify_position (tp_from_pips) %s failed: %s", pid, repr(e))
    return modified

  async def _find_position_by_client_id(
    self,
    client_id: str,
    *,
    max_wait_sec: float = 5.0,
    poll_interval_sec: float = 0.1,
  ) -> Optional[Dict[str, Any]]:
    """Poll get_positions until a position with the given clientId appears,
    or timeout. Returns the position dict or None on a confirmed-absent result.

    Raises RuntimeError if every poll attempt failed — in that case we cannot
    distinguish "broker has no such position" from "we couldn't ask", and the
    caller must NOT retry the order (would risk a duplicate position).

    Note: MT4 brokers may strip clientId on the position; this dedupe relies
    on the broker preserving it (MT5 generally does).
    """
    deadline = asyncio.get_event_loop().time() + max_wait_sec
    ever_succeeded = False
    last_err: Optional[BaseException] = None
    while True:
      try:
        positions = await asyncio.wait_for(
          self._conn.get_positions(),
          timeout=self.rpc_timeout_sec,
        )
        ever_succeeded = True
        for pos in (positions or []):
          if pos.get("clientId") == client_id:
            return pos
      except asyncio.CancelledError:
        raise
      except Exception as e:
        last_err = e
        self.log.warning(
          "_find_position_by_client_id poll failed for client_id=%s: %s",
          client_id, repr(e),
        )
      if asyncio.get_event_loop().time() >= deadline:
        if not ever_succeeded:
          raise RuntimeError(
            f"Could not verify whether order with client_id={client_id} reached "
            f"broker after {max_wait_sec}s (last poll error: {last_err!r}). "
            f"Refusing to retry to avoid duplicate position."
          )
        return None
      await asyncio.sleep(poll_interval_sec)

  async def close_position_market(
    self,
    position_id: str,
    *,
    volume: Optional[float] = None,
  ) -> Dict[str, Any]:
    """
    Closes position with market order.
    MetaApi RPC: close_position (using positionId)

    If volume is None closes the whole position (if account/broker allows it).
    """
    await self._ensure_ready()

    try:
      if volume is not None:
        return await asyncio.wait_for(
          self._conn.close_position(position_id, volume),
          timeout=self.rpc_timeout_sec,
        )
      else:
        return await asyncio.wait_for(
          self._conn.close_position(position_id),
          timeout=self.rpc_timeout_sec,
        )
    except asyncio.CancelledError:
      if self._current_task_is_cancelling():
        raise
      self.log.warning("close_position_market cancelled mid-RPC (SDK reconnect). Triggering reconnect...")
      self._ready.clear()
      await self._trigger_reconnect()
      raise
    except Exception as e:
      self.log.warning("close_position_market failed (%s). Triggering reconnect...", repr(e))
      self._ready.clear()
      await self._trigger_reconnect()
      raise

  async def get_positions(self) -> Dict[str, Any]:
    """Retrieves all open positions from MetaApi."""
    await self._ensure_ready()
    try:
      return await asyncio.wait_for(
        self._conn.get_positions(),
        timeout=self.rpc_timeout_sec,
      )
    except asyncio.CancelledError:
      if self._current_task_is_cancelling():
        raise
      self.log.warning("get_positions cancelled mid-RPC (SDK reconnect). Triggering reconnect...")
      self._ready.clear()
      await self._trigger_reconnect()
      raise
    except Exception as e:
      self.log.warning("get_positions failed (%s). Triggering reconnect...", repr(e))
      self._ready.clear()
      await self._trigger_reconnect()
      raise

  async def _poll_for_position(
    self,
    position_id: str,
    *,
    poll_interval_sec: float = 0.05,
    max_wait_sec: float = 3.0,
  ) -> Dict[str, Any]:
    """Poll get_position until it returns, or max_wait_sec elapses."""
    deadline = asyncio.get_event_loop().time() + max_wait_sec
    last_err: Optional[BaseException] = None
    while True:
      try:
        return await asyncio.wait_for(
          self._conn.get_position(position_id),
          timeout=self.rpc_timeout_sec,
        )
      except Exception as e:
        last_err = e
        if asyncio.get_event_loop().time() >= deadline:
          raise
        await asyncio.sleep(poll_interval_sec)

  async def close_positions_for_symbol(self, symbol: str) -> None:
    """
    Atomically closes every open position for `symbol` and deletes pending
    orders for that symbol. Other symbols are unaffected. Serializes with
    open_market_order(symbol) via the per-symbol lock and waits for any
    in-progress close_all_positions() to finish first.
    """
    await self._ensure_ready()
    await self._wait_for_close_all_done()

    sym_lock = self._get_symbol_lock(symbol)
    async with sym_lock:
      positions = await self.get_positions()
      symbol_positions = [p for p in (positions or []) if p.get("symbol") == symbol]
      if symbol_positions:
        await asyncio.gather(
          *(self.close_position_market(pos["id"]) for pos in symbol_positions),
          return_exceptions=True,
        )

      try:
        orders = await asyncio.wait_for(
          self._conn.get_orders(),
          timeout=self.rpc_timeout_sec,
        )
        symbol_order_ids = [
          o["id"] for o in (orders or [])
          if o.get("symbol") == symbol and o.get("id")
        ]
        if symbol_order_ids:
          async def _cancel(oid: str):
            try:
              await asyncio.wait_for(
                self._conn.cancel_order(oid),
                timeout=self.rpc_timeout_sec,
              )
              self.log.warning(f"Deleted pending order: {oid}")
            except Exception as e:
              self.log.warning(f"Failed to delete order {oid}: {e}")
          await asyncio.gather(*(_cancel(oid) for oid in symbol_order_ids))
      except Exception as e:
        self.log.warning(f"Error cancelling pending orders for {symbol}: {e}")

      with self._state_lock:
        if self.last_signal is not None and self.last_signal.get("symbol") == symbol:
          self.last_signal = None
        self._pending_reentry.pop(symbol, None)

  async def close_all_positions(self) -> None:
    """
    Atomically closes every open position and deletes pending orders.
    Drains in-flight per-symbol opens before running so a racing open
    cannot create a position the close-all snapshot would miss.
    """
    await self._ensure_ready()
    async with self._close_all_lock:
      self._close_all_in_progress.set()
      try:
        # Acquire every known symbol lock so any in-flight open finishes
        # before we snapshot positions. New opens will see the flag and wait.
        sym_locks = list(self._symbol_locks.values())
        acquired: list = []
        try:
          for lock in sym_locks:
            await lock.acquire()
            acquired.append(lock)

          positions = await self.get_positions()
          if positions:
            await asyncio.gather(
              *(self.close_position_market(pos["id"]) for pos in positions),
              return_exceptions=True,
            )
          await self.delete_all_pending_orders()
          with self._state_lock:
            self.last_signal = None
        finally:
          for lock in acquired:
            lock.release()
      finally:
        self._close_all_in_progress.clear()

  # ----------------------------
  # Internals: connect / sync / heartbeat
  # ----------------------------
  async def _connect_and_sync(self) -> None:
    self._ready.clear()

    self.log.warning("Connecting to MetaApi...")
    self._api = MetaApi(self.token)

    self._account = await self._api.metatrader_account_api.get_account(self.account_id)

    if self._account.state != "DEPLOYED":
      self.log.warning("Account not deployed, deploying...")
      await self._account.deploy()

    self.log.warning("Waiting for MetaApi server connection...")
    await self._account.wait_connected()

    self.log.warning("Creating RPC connection...")
    self._conn = self._account.get_rpc_connection()  # ✅ no await

    await self._conn.connect()  # ✅ await needed
    self.log.warning("Waiting for synchronization...")
    await self._conn.wait_synchronized()  # ✅ await needed

    self.log.warning("READY ✅")
    self._ready.set()

  async def _ensure_ready(self) -> None:
    if self._ready.is_set():
      return
    try:
      await asyncio.wait_for(self._ready.wait(), timeout=self.ready_wait_timeout_sec)
    except asyncio.TimeoutError:
      raise RuntimeError(
        f"ForexManager not ready after {self.ready_wait_timeout_sec}s "
        "(reconnect in progress or start() never called)."
      )

  async def _heartbeat_loop(self) -> None:
    """
    Runs on the heartbeat worker's own thread+loop, using its dedicated RPC
    connection — never queues behind trading or trade-manager traffic.
    """
    self.log.warning("Heartbeat started (%ss interval)", self.heartbeat_interval_sec)

    while not self._stop.is_set():
      await asyncio.sleep(self.heartbeat_interval_sec)

      if not self._hb_worker._ready.is_set() or self._hb_worker.conn is None:
        continue

      try:
        await asyncio.wait_for(
          self._hb_worker.conn.get_account_information(),
          timeout=self.rpc_timeout_sec,
        )
      except Exception as e:
        self.log.warning("Heartbeat check failed (%s). Reconnecting...", repr(e))
        await self._hb_worker.trigger_reconnect()

    self.log.warning("Heartbeat stopped.")

  # ----------------------------
  # Trade Management
  # ----------------------------
  async def _trade_manager_loop(self) -> None:
    """
    Runs on the trade-manager worker's own thread+loop, using its dedicated
    RPC connection — never queues behind trading or heartbeat traffic.
    """
    self.log.warning("Trade manager started (%ss interval)", self._trade_manager_interval_sec)

    while not self._stop.is_set():
      try:
        await asyncio.sleep(self._trade_manager_interval_sec)

        if not self._tm_worker._ready.is_set():
          self.log.warning("Trade manager: tm conn not ready")
          continue

        await asyncio.wait_for(self._manage_all_positions(), timeout=30)
      except asyncio.CancelledError:
        self.log.warning("Trade manager task cancelled, exiting loop")
        raise
      except asyncio.TimeoutError:
        self.log.warning("Trade manager timed out after 30s, will retry next cycle")
      except BaseException as e:
        self.log.warning("Trade manager error (will retry): %s", repr(e))
        traceback.print_exc()

    self.log.warning("Trade manager stopped.")

  async def _manage_all_positions(self) -> None:
    """Check all open positions and apply trailing stop logic.
    Runs on the trade-manager worker's loop using `_tm_worker.conn`."""
    if not self._tm_worker._ready.is_set() or self._tm_worker.conn is None:
      return

    try:
      positions = await self._tm_worker.conn.get_positions()
    except Exception as e:
      self.log.warning("Failed to get positions for management: %s", repr(e))
      return

    # Adjust polling interval based on whether positions exist
    if len(positions) == 0:
      if self._trade_manager_interval_sec != 5:
        self._trade_manager_interval_sec = 5
        self.log.warning("No positions - trade manager interval set to 5s")

      # Check closed positions profit if last_signal exists and has_profit is False
      with self._state_lock:
        signal = self.last_signal
        should_check = signal is not None and not signal.get("has_profit")
      if should_check:
        await self._check_closed_positions_profit()
    else:
      if self._trade_manager_interval_sec != 1:
        self._trade_manager_interval_sec = 1
        self.log.warning(f"Positions found ({len(positions)}) - trade manager interval set to 1s")

      # If a position exists, any prior "pending order" should be considered resolved.
      with self._state_lock:
        if self.last_signal is not None:
          self.last_signal["has_pending_order"] = False

    # Clean up state for closed positions + register re-entries (single critical section).
    open_position_ids = {p.get("id") for p in positions}
    with self._state_lock:
      closed_ids = [pid for pid in self._trade_state if pid not in open_position_ids]
      for pid in closed_ids:
        del self._trade_state[pid]
        self.log.warning(f"Position {pid} closed, removed from trade state")

      for position in positions:
        pos_id = position.get("id")
        if pos_id and pos_id not in self._trade_state:
          symbol = position.get("symbol")
          if symbol in self._pending_reentry:
            reentry = self._pending_reentry.pop(symbol)
            self._trade_state[pos_id] = {
                "open_price": reentry["entry_price"],
                "direction": reentry["direction"],
                "symbol": symbol,
                "volume": reentry["volume"],
                "peak_profit": 0.0,
            }
            self.log.warning(f"Re-entry detected for {symbol}: position {pos_id} initialized with entry={reentry['entry_price']}")

    for position in positions:
      await self._manage_single_position(position)

  async def _manage_single_position(self, position: Dict[str, Any]) -> None:
    """
    MFE/MAE-optimized trailing stop logic (from 90-day backtest).

    Strategy:
      1. Initial SL at entry ± 15 (set on open, catches 75% of winner MAE dips)
      2. profit >= $15 → SL moves to breakeven (entry price)
      3. profit >= $20 → trailing activates: SL follows peak_profit - $5
         This captures the large MFE moves (median winner MFE = $57)
         while giving $5 breathing room to avoid premature exits.

    The trail only ratchets forward (never loosens).
    Re-entry on SL hit is handled by _check_closed_positions_profit.
    """
    position_id = position.get("id")
    if not position_id:
      self.log.warning(f"Position id {position_id} doesn't exist, returning...")
      return

    with self._state_lock:
      if position_id not in self._trade_state:
        self.log.warning(f"Position id {position_id} isn't in trade state, returning...")
        return
      state = self._trade_state[position_id]
      open_price = state["open_price"]
      direction = state["direction"]
      current_profit = position.get("profit", 0) or 0

      if current_profit > state["peak_profit"]:
        state["peak_profit"] = current_profit
        self.log.info(f"New peak profit for {position_id}: {current_profit:.2f}")

      peak_profit = state["peak_profit"]

    self.log.info(f"Managing {position_id}: entry={open_price}, dir={direction}, profit={current_profit:.2f}, peak={peak_profit:.2f}")

    be_threshold = self.breakeven_profit
    trail_act = self.trail_activation
    trail_dist = self.trail_distance

    # Direction multiplier: buy = +1 (SL below entry), sell = -1 (SL above entry)
    d = 1 if direction == "buy" else -1

    if peak_profit >= trail_act:
      # Trailing mode: SL follows peak profit with trail_distance gap
      trail_offset = peak_profit - trail_dist
      new_sl = open_price + (trail_offset * d)
    elif peak_profit >= be_threshold:
      # Breakeven mode: lock in entry price
      new_sl = open_price
    else:
      # Below breakeven threshold — initial SL is already set, nothing to adjust
      return

    # Only ratchet forward (never loosen the SL)
    current_sl = position.get("stopLoss")
    if current_sl is not None:
      if direction == "buy" and new_sl <= current_sl:
        return
      if direction == "sell" and new_sl >= current_sl:
        return

    try:
      await asyncio.wait_for(
        self._tm_worker.conn.modify_position(position_id, stop_loss=new_sl),
        timeout=self.rpc_timeout_sec,
      )
      self.log.warning(f"Trailing SL for {position_id}: profit={current_profit:.2f}, peak={peak_profit:.2f}, new SL={new_sl:.2f}")
    except Exception as e:
      self.log.error(f"Failed to modify SL for {position_id}: {e}")

  async def _check_closed_positions_profit(self) -> None:
    """
    Check closed positions since last signal.
    - If profit >= 100000: signal complete, clear last_signal.
    - If profit >= 0 but below threshold: under-performed signal, re-enter at
      last close price + 1 (momentum confirmation) to capture remaining move.
    - If SL was hit (profit < 0): place re-entry at first_entry_price with SL.
    - Repeats until a new signal arrives.

    Runs on the trade-manager worker's loop using `_tm_worker.conn`.
    """
    # Snapshot last_signal under lock — trading thread may mutate or clear it.
    with self._state_lock:
      if self.last_signal is None:
        self.log.warning("_check_closed_positions_profit: last_signal is None, returning")
        return
      sig = dict(self.last_signal)

    conn = self._tm_worker.conn
    if conn is None:
      return

    try:
      signal_date = datetime.fromisoformat(sig["date_created"])
      if signal_date.tzinfo is None:
        signal_date = signal_date.replace(tzinfo=timezone.utc)

      history = await conn.get_deals_by_time_range(signal_date, datetime.now(timezone.utc))

      if not history:
        self.log.warning("_check_closed_positions_profit: No history deals found, returning")
        return

      total_profit = 0.0
      deal_count = 0
      last_close_price = None

      if isinstance(history, dict):
        deals_list = history.get("deals", [])
      else:
        deals_list = history

      for deal in deals_list:
        if not isinstance(deal, dict):
          continue
        if deal.get("entryType") == "DEAL_ENTRY_OUT":
          profit = deal.get("profit", 0) or 0
          total_profit += profit
          deal_count += 1
          last_close_price = deal.get("price")

      if deal_count == 0:
        self.log.warning("_check_closed_positions_profit: No DEAL_ENTRY_OUT deals found, returning")
        return

      if total_profit >= 0:
        if total_profit >= 100000:
          # Signal complete
          with self._state_lock:
            if self.last_signal is not None:
              self.last_signal["has_profit"] = True
          self.log.warning(f"Profit: {total_profit:.2f}. Signal complete.")
          await self.delete_all_pending_orders(conn=conn)
          with self._state_lock:
            self.last_signal = None
          return
        else:
          # Profit below threshold — re-enter at close price + 1 for momentum confirmation
          self.log.warning(
            f"Profit {total_profit:.2f} below threshold. Re-entering for more."
          )
          # Fall through to re-entry logic below with close price

      # Re-entry logic (SL hit or under-target profit)
      if sig.get("has_pending_order"):
        # Verify the order still exists before trusting the flag
        try:
          orders = await conn.get_orders()
          if orders and len(orders) > 0:
            self.log.warning(f"Re-entry pending (profit={total_profit:.2f}), order still exists, returning")
            return
          else:
            with self._state_lock:
              if self.last_signal is not None:
                self.last_signal["has_pending_order"] = False
            self.log.warning("Pending order no longer exists, will re-create re-entry order")
        except Exception:
          return

      side = sig["action"]
      first_entry = sig["first_entry_price"]
      symbol = sig.get("symbol", "GOLD")
      volume = sig.get("volume", 0.01)

      sl_dist = self.initial_sl_distance

      # Choose re-entry price based on reason
      if total_profit >= 0 and last_close_price is not None:
        # Under-target profit: re-enter at close price + 1 (momentum confirmation)
        momentum_offset = 1.0
        if side == "buy":
          reentry_price = round(last_close_price + momentum_offset, 2)
        else:
          reentry_price = round(last_close_price - momentum_offset, 2)
        reason = "under-target"
      else:
        # SL hit: re-enter at first entry price (existing behavior)
        reentry_price = first_entry
        reason = "SL hit"

      if side == "buy":
        sl = reentry_price - sl_dist
      else:
        sl = reentry_price + sl_dist

      # Get current price to decide between limit and stop order
      price_data = await conn.get_symbol_price(symbol)
      current_ask = price_data["ask"]
      current_bid = price_data["bid"]

      self.log.warning(
        f"{reason} (profit={total_profit:.2f}). Re-entering: {side} {symbol} "
        f"at {reentry_price:.2f}, SL={sl:.2f}, ask={current_ask}, bid={current_bid}"
      )

      try:
        if side == "buy":
          if reentry_price < current_ask:
            order_response = await conn.create_limit_buy_order(
              symbol=symbol, volume=volume, open_price=reentry_price,
              stop_loss=sl,
            )
          else:
            order_response = await conn.create_stop_buy_order(
              symbol=symbol, volume=volume, open_price=reentry_price,
              stop_loss=sl,
            )
        else:
          if reentry_price > current_bid:
            order_response = await conn.create_limit_sell_order(
              symbol=symbol, volume=volume, open_price=reentry_price,
              stop_loss=sl,
            )
          else:
            order_response = await conn.create_stop_sell_order(
              symbol=symbol, volume=volume, open_price=reentry_price,
              stop_loss=sl,
            )

        with self._state_lock:
          if self.last_signal is not None:
            self.last_signal["has_pending_order"] = True
          self._pending_reentry[symbol] = {
              "entry_price": reentry_price,
              "direction": side,
              "volume": volume,
          }
        self.log.warning(f"Re-entry order created ({reason}): {order_response}")

      except Exception as order_error:
        self.log.warning(f"Failed to create re-entry order: {order_error}")
        traceback.print_exc()

    except Exception as e:
      self.log.warning(f"Error checking closed positions profit: {e}")
      traceback.print_exc()

  async def delete_all_pending_orders(self, conn=None) -> Dict[str, Any]:
    """
    Delete all pending/limit orders.

    `conn`: optional MetaApi RPC connection to use. If None (default), uses
    the trading connection — that's the case for trading-thread callers.
    Trade-manager-thread callers pass `self._tm_worker.conn` so deletion
    runs on its own socket without contending with trading RPCs.
    """
    is_trading_conn = conn is None
    if is_trading_conn:
      await self._ensure_ready()
      conn = self._conn

    try:
      # Get all pending orders
      orders = await asyncio.wait_for(
        conn.get_orders(),
        timeout=self.rpc_timeout_sec,
      )

      if not orders:
        self.log.warning("No pending orders to delete")
        return {"status": "success", "deleted_count": 0, "orders": []}

      order_ids = [o.get("id") for o in orders if o.get("id")]

      async def _cancel(oid: str):
        try:
          await asyncio.wait_for(
            conn.cancel_order(oid),
            timeout=self.rpc_timeout_sec,
          )
          self.log.warning(f"Deleted pending order: {oid}")
          return oid
        except Exception as e:
          self.log.warning(f"Failed to delete order {oid}: {e}")
          return None

      results = await asyncio.gather(*(_cancel(oid) for oid in order_ids))
      deleted_orders = [oid for oid in results if oid]

      # Reset has_pending_order flag in last_signal if exists
      with self._state_lock:
        if self.last_signal is not None:
          self.last_signal["has_pending_order"] = False

      self.log.warning(f"Deleted {len(deleted_orders)} pending orders")
      return {
        "status": "success",
        "deleted_count": len(deleted_orders),
        "orders": deleted_orders
      }

    except asyncio.TimeoutError as e:
      self.log.warning("delete_all_pending_orders timed out.")
      if is_trading_conn:
        await self._trigger_reconnect()
      return {"status": "error", "error": "timeout"}
    except Exception as e:
      self.log.warning(f"Error deleting pending orders: {e}")
      traceback.print_exc()
      return {"status": "error", "error": str(e)}

  async def _trigger_reconnect(self) -> None:
    """
    Makes sure no more than one reconnection attempt is in progress at a time.
    Applies backoff + jitter.
    """
    async with self._reconnect_lock:
      if self._stop.is_set():
        return

      # Another task already reconnected while we waited for the lock
      if self._ready.is_set():
        return

      self._ready.clear()

      delay = self.reconnect_base_delay_sec
      while not self._stop.is_set():
        try:
          await self._disconnect()
          await self._connect_and_sync()
          return
        except Exception as e:
          jitter = random.uniform(0, 0.5)
          sleep_for = min(self.reconnect_max_delay_sec, delay + jitter)
          self.log.warning("Reconnect failed (%s). Retrying in %.1fs...", repr(e), sleep_for)
          await asyncio.sleep(sleep_for)
          delay = min(self.reconnect_max_delay_sec, delay * 2)

  async def _disconnect(self) -> None:
    self._ready.clear()

    try:
      if self._conn:
        await self._conn.close()
        # Give MetaApi internal tasks (unsubscribe etc.) time to complete
        await asyncio.sleep(1)
    except Exception:
      pass

    self._conn = None
    self._account = None
    self._api = None


# ----------------------------
# Example usage
# ----------------------------
async def main():
  logging.basicConfig(level=logging.INFO)

  fm = ForexManager(
    token="YOUR_METAAPI_TOKEN",
    account_id="YOUR_ACCOUNT_ID",
    # region="agiliumtrade.agiliumtrade.ai",  # if needed
    heartbeat_interval_sec=10,
  )

  await fm.start()
  await fm.wait_ready(timeout=60)

  order = await fm.open_market_order(
    symbol="EURUSD",
    side="buy",
    volume=0.01,
    stop_loss=None,
  )
  print("ORDER:", order)

  pos = await fm.get_positions()
  print("POSITIONS:", pos)

  await fm.stop()


if __name__ == "__main__":
  asyncio.run(main())
