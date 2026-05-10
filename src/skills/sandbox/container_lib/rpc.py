"""Bidirectional JSON-lines RPC over pipes.

`Proxy.__call__` is uniform regardless of caller context or remote
shape: it fires the wire call and returns a _ProxyCall wrapper. The
caller resolves it via `await` (which yields to the event loop — so
replies aren't blocked by the caller even when caller and remote share
a loop) or `.wait()` from purely sync code.

Under the hood:
  * remote sync function  → reply "ok"     → value, resolved on await.
  * remote async function → reply "ok_fut" → RemoteFuture; final value
    arrives later via "fut_done" and is auto-chained on await/.wait().

Local syntax is uniform:
  * `x = await proxy.method(...)`            — any remote, in async code
  * `x = proxy.method(...).wait()`           — any remote, from sync code
  * `task = asyncio.create_task(proxy....)`  — schedule concurrently

The wire call is sent eagerly before _ProxyCall is returned, so
fire-and-forget still works (ignoring the wrapper is harmless).
_ProxyCall implements the Coroutine ABC so asyncio.create_task accepts it.

Threading model:
  * reader  — blocking readline loop. Each incoming "call" is handed off
              to a fresh daemon worker thread; reader never runs handlers.
  * worker  — one per incoming call, daemon thread. Sync handler runs
              directly. Async handler is scheduled on `async_loop` via
              run_coroutine_threadsafe (if provided) — the worker returns
              the Future immediately, _send_ok turns it into "ok_fut",
              and a done-callback delivers the final value via "fut_done".
              Without async_loop, the worker drives the coroutine on a
              fresh per-call event loop to completion (legacy path).

Why this shape:
  * Daemon threads → Ctrl+C / interpreter exit actually works even if
    a handler is "pinned" forever (e.g. Agent.loop()).
  * `async_loop` lets the host pin all async handlers to its main loop
    so things like aiogram/uvicorn (whose sessions are loop-bound) keep
    working when sandbox calls back into `transport.send_message`.
  * `max_workers` caps in-flight calls. When saturated the next call is
    rejected with an error (not queued) — silent queuing is the classic
    RPC-deadlock setup (pinned threads waiting on a reply that's stuck
    behind them in the backlog).

Objects of allowed classes are passed by reference (ref ID). The other
side gets a Proxy that forwards calls back.
"""

import asyncio, collections.abc, concurrent.futures, json, logging, threading, traceback

log = logging.getLogger(__name__)

# Set by Channel.start(). Sandbox has exactly one channel per process, so
# shims like src.transport.multi.MultiTransport can locate it without
# threading a reference through every call site.
_active_channel = None


def active_channel():
    return _active_channel


class RpcFuture:
    """Result of an RPC call. Both awaitable and blocking.

        result = await fut       # async code
        result = fut.wait()      # sync code

    Resolved from the reader thread via _set_result / _set_error.

    `wait()` and `__await__` auto-chain through RemoteFuture so direct
    users (tests, non-Proxy callsites) see the final value transparently.
    Proxy.__call__ doesn't go through this — it peeks the raw first reply
    via `_wait_raw()` to decide sync vs async by remote's response.
    """

    def __init__(self):
        self._done = threading.Event()
        self._value = None
        self._error = None
        self._lock = threading.Lock()
        self._waiters = []  # [(asyncio.Future, loop)] registered by __await__

    def _set_result(self, value):
        with self._lock:
            if self._done.is_set():
                return
            self._value = value
            self._done.set()
            waiters = self._waiters
            self._waiters = []
        for afut, loop in waiters:
            self._push(afut, loop)

    def _set_error(self, err):
        with self._lock:
            if self._done.is_set():
                return
            self._error = err
            self._done.set()
            waiters = self._waiters
            self._waiters = []
        for afut, loop in waiters:
            self._push(afut, loop)

    def _push(self, afut, loop):
        def _apply():
            if afut.done():
                return
            if self._error is not None:
                afut.set_exception(RuntimeError(self._error))
            else:
                afut.set_result(self._value)
        loop.call_soon_threadsafe(_apply)

    def __await__(self):
        loop = asyncio.get_running_loop()
        afut = loop.create_future()
        with self._lock:
            resolved = self._done.is_set()
            if not resolved:
                self._waiters.append((afut, loop))
        if resolved:
            self._push(afut, loop)
        v = yield from afut.__await__()
        while isinstance(v, RemoteFuture):
            v = yield from v.__await__()
        return v

    def wait(self, timeout=None):
        v = self._wait_raw(timeout)
        while isinstance(v, RemoteFuture):
            v = v._wait_raw(timeout)
        return v

    def _wait_raw(self, timeout=None):
        """Block on the first wire reply without auto-chain. For Proxy."""
        if not self._done.wait(timeout):
            raise TimeoutError("rpc call timed out")
        if self._error is not None:
            raise RuntimeError(self._error)
        return self._value



class RemoteFuture(RpcFuture):
    """Handle to an in-flight remote async call.

    Returned by Proxy when the remote function returned a coroutine that
    hasn't completed yet. Same interface as RpcFuture (await, .wait()),
    resolved by a "fut_done" message from the remote side.
    """
    pass


class _ProxyCall(collections.abc.Coroutine):
    """Wrapper Proxy.__call__ returns when the remote went async.

    The RPC has already been fired AND the first wire reply already
    arrived (it was "ok_fut"). This wraps the in-flight RemoteFuture
    so the caller can observe the final value however they want:

      * `await wrapper`                — async wait
      * `wrapper.wait()`               — sync wait
      * `asyncio.create_task(wrapper)` — schedule concurrently (via
                                         Coroutine protocol)
      * sync dunders (__iter__, __bool__, etc.) — block on wait() and
                                         forward to the unpacked value.
                                         Safe because each incoming RPC
                                         call runs on its own daemon
                                         thread, so blocking here never
                                         stops another handler.

    These dunders live here (not on RpcFuture) because asyncio probes
    futures with truthiness checks; a __bool__ that blocks on .wait()
    deadlocks the event loop.
    """

    __slots__ = ("_rfut", "_coro_iter")

    def __init__(self, rfut):
        self._rfut = rfut
        self._coro_iter = None

    def __await__(self):
        return self._rfut.__await__()

    def wait(self, timeout=None):
        return self._rfut.wait(timeout)

    # Coroutine protocol
    def _iter(self):
        if self._coro_iter is None:
            self._coro_iter = self.__await__()
        return self._coro_iter

    def send(self, value):
        return self._iter().send(value)

    def throw(self, *args, **kwargs):
        return self._iter().throw(*args, **kwargs)

    def close(self):
        if self._coro_iter is not None:
            self._coro_iter.close()

    # Sync dunders: block and forward to the resolved value.
    def __iter__(self): return iter(self._rfut.wait())
    def __bool__(self): return bool(self._rfut.wait())
    def __len__(self): return len(self._rfut.wait())
    def __getitem__(self, k): return self._rfut.wait()[k]
    def __contains__(self, item): return item in self._rfut.wait()


class Proxy:
    """Remote-object reference. Dotted attribute access builds a method path.

    Calling a Proxy always blocks on the first wire reply (fast — reader
    thread acknowledges whether the remote handler was sync or async,
    independent of the caller's event loop). Decision is based on the
    remote's response, not caller context:

        * remote was sync (ok)      → returns the unpacked value
        * remote was async (ok_fut) → returns _ProxyCall wrapping the
                                       in-flight RemoteFuture

    Callers use the result uniformly:

        x = proxy.sync_method(...)          # value immediately
        x = await proxy.async_method(...)   # async wait
        x = proxy.async_method(...).wait()  # sync wait from any thread
    """

    def __init__(self, ch, ref, path=""):
        object.__setattr__(self, '_ch', ch)
        object.__setattr__(self, '_ref', ref)
        object.__setattr__(self, '_path', path)

    def __getattr__(self, name):
        p = f"{self._path}.{name}" if self._path else name
        return Proxy(self._ch, self._ref, p)

    def __call__(self, *args, **kwargs):
        rfut = self._ch.call(self._ref, self._path, *args, **kwargs)
        v = rfut._wait_raw()
        if isinstance(v, RemoteFuture):
            return _ProxyCall(v)
        return v

    def __repr__(self):
        p = f".{self._path}" if self._path else ""
        return f"<Proxy {self._ref}{p}>"


class Channel:
    """Bidirectional RPC channel. Both sides can send calls and receive callbacks.

    Arguments:
        readline:   blocking readline() → str (empty string = EOF)
        writeline:  blocking writeline(dict) → None
        allowed:    {type: {"method_or_attr", ...} | None} — whitelist per
                    class. None means "all attributes allowed".
        ref_prefix: prefix for auto-generated ref ids (avoids clashes when
                    two channels share id space, e.g. host "$" vs sandbox "s").
        async_loop: if provided, async handlers run on this loop via
                    run_coroutine_threadsafe. Use on the host side to pin
                    handlers to the main asyncio loop so libraries whose
                    sessions are loop-bound (aiogram, uvicorn, etc.) keep
                    working when called back from sandbox. If None, async
                    handlers get a fresh per-call loop on the worker thread.
        max_workers: max in-flight incoming calls. Calls beyond this cap
                    are rejected with an error rather than queued.
    """

    def __init__(self, readline, writeline, allowed=None, ref_prefix="$",
                 async_loop=None, max_workers=32):
        self._readline = readline
        self._writeline = writeline
        self._allowed = allowed or {}
        self._refs = {}
        self._pending = {}                  # cid → RpcFuture
        self._futures = {}                  # fref → RemoteFuture (in-flight remote async)
        self._next_id = 0
        self._next_ref = 0
        self._next_fref = 0
        self._ref_prefix = ref_prefix

        self._write_lock = threading.Lock()
        self._async_loop = async_loop
        self._max_workers = max_workers
        self._active = 0
        self._active_lock = threading.Lock()

        self._reader_thread = None
        self._closed = threading.Event()

    # --- lifecycle ---

    def start(self):
        global _active_channel
        _active_channel = self
        self._reader_thread = threading.Thread(target=self._reader, daemon=True, name="rpc-reader")
        self._reader_thread.start()

    def close(self):
        self._closed.set()

    def join(self):
        if self._reader_thread:
            self._reader_thread.join()

    def register(self, key, obj):
        self._refs[key] = obj

    # --- writing (thread-safe) ---

    def _send(self, msg):
        with self._write_lock:
            self._writeline(msg)

    def _next_cid(self):
        self._next_id += 1
        return self._next_id

    # --- outgoing calls ---

    def call(self, ref, method, *args, **kwargs):
        """Issue an RPC call. Returns RpcFuture resolving to the first
        wire reply: for a sync remote that's the value, for an async
        remote that's a RemoteFuture (in-flight; await/.wait() it for
        the final value). Normal callers go through Proxy, which hides
        this distinction; direct ch.call(...) users must handle it.
        """
        cid = self._next_cid()
        rfut = RpcFuture()
        self._pending[cid] = rfut
        self._send({
            "t": "call", "i": cid, "r": ref, "m": method,
            "a": [self._pack(v) for v in args],
            "k": {k: self._pack(v) for k, v in kwargs.items()},
        })
        return rfut

    # --- reader thread ---

    def _reader(self):
        try:
            while not self._closed.is_set():
                line = self._readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except Exception:
                    log.warning("[rpc] bad line: %r", line)
                    continue
                t = msg.get("t")
                if t == "call":
                    self._dispatch_call(msg)
                elif t in ("ok", "err", "ok_fut"):
                    self._resolve(msg)
                elif t == "fut_done":
                    self._resolve_future(msg)
        finally:
            self._fail_pending("channel closed")

    def _fail_pending(self, err):
        for rfut in list(self._pending.values()):
            rfut._set_error(err)
        self._pending.clear()
        for rfut in list(self._futures.values()):
            rfut._set_error(err)
        self._futures.clear()

    def _resolve(self, msg):
        rfut = self._pending.pop(msg["i"], None)
        if not rfut:
            return
        if msg["t"] == "err":
            rfut._set_error(msg["e"])
        elif msg["t"] == "ok_fut":
            # Remote returned a coroutine — register a RemoteFuture, the
            # remote side will deliver the final value via "fut_done".
            remote = RemoteFuture()
            self._futures[msg["f"]] = remote
            rfut._set_result(remote)
        else:
            rfut._set_result(self._unpack(msg.get("v")))

    def _resolve_future(self, msg):
        remote = self._futures.pop(msg["f"], None)
        if not remote:
            return
        if "e" in msg:
            remote._set_error(msg["e"])
        else:
            remote._set_result(self._unpack(msg.get("v")))

    def _dispatch_call(self, msg):
        """Resolve target and run the handler on its own daemon worker thread.

        If all workers are busy, reject the call with an error instead of
        queuing. Silent queuing is the classic RPC-deadlock setup: a pinned
        thread waits for a reply that's stuck behind it in the queue.
        """
        try:
            obj = self._refs[msg["r"]]
            # Empty path → ref itself is the callable (auto-registered
            # function/lambda/method/class passed as an argument).
            if msg["m"]:
                for attr in msg["m"].split("."):
                    if not self._check(obj, attr):
                        raise PermissionError(f"{type(obj).__name__}.{attr}")
                    obj = getattr(obj, attr)
            args = [self._unpack(a) for a in msg.get("a", [])]
            kwargs = {k: self._unpack(v) for k, v in msg.get("k", {}).items()}
        except Exception:
            self._send({"t": "err", "i": msg["i"], "e": traceback.format_exc()})
            return

        cid = msg["i"]
        with self._active_lock:
            if self._active >= self._max_workers:
                self._send({"t": "err", "i": cid,
                            "e": f"rpc pool exhausted ({self._max_workers} workers pinned)"})
                return
            self._active += 1

        def run():
            try:
                result = self._run_call(obj, args, kwargs)
                self._send_ok(cid, result)
            except Exception:
                tb = traceback.format_exc()
                log.warning("[rpc] %s", tb.rstrip())
                self._send({"t": "err", "i": cid, "e": tb})
            finally:
                with self._active_lock:
                    self._active -= 1

        threading.Thread(target=run, daemon=True, name=f"rpc-call-{cid}").start()

    def _run_call(self, fn, args, kwargs):
        """Run a handler. Sync call returns the value directly. Async call
        returns a concurrent.futures.Future (still in flight) — _send_ok
        recognizes this and turns it into "ok_fut", so the caller's worker
        thread doesn't block waiting for the coroutine to complete.

        Without `async_loop`, falls back to driving the coroutine on a
        fresh per-call event loop in a background thread (so the worker
        can return the Future immediately)."""
        result = fn(*args, **kwargs)
        if not asyncio.iscoroutine(result):
            return result
        if self._async_loop is not None:
            return asyncio.run_coroutine_threadsafe(result, self._async_loop)
        cfut = concurrent.futures.Future()
        def _drive():
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                cfut.set_result(loop.run_until_complete(result))
            except BaseException as e:
                cfut.set_exception(e)
            finally:
                asyncio.set_event_loop(None)
                loop.close()
        threading.Thread(target=_drive, daemon=True, name="rpc-coro").start()
        return cfut

    def _new_fref(self):
        self._next_fref += 1
        return f"f{self._ref_prefix}{self._next_fref}"

    def _send_ok(self, cid, result):
        if isinstance(result, concurrent.futures.Future):
            fref = self._new_fref()
            self._send({"t": "ok_fut", "i": cid, "f": fref})
            # done-callback от concurrent future, полученного через
            # run_coroutine_threadsafe, гарантированно прилетает в тред
            # _async_loop'а. Контракт writeline — sync блокирующая, не из loop.
            # Переселяем в дефолтный executor loop'а (shared thread pool).
            loop = self._async_loop
            if loop is not None:
                result.add_done_callback(
                    lambda f: loop.run_in_executor(None, self._send_fut_done, fref, f)
                )
            else:
                result.add_done_callback(lambda f: self._send_fut_done(fref, f))
            return
        self._send({"t": "ok", "i": cid, "v": self._pack(result)})

    def _send_fut_done(self, fref, fut):
        try:
            v = fut.result()
        except BaseException:
            self._send({"t": "fut_done", "f": fref, "e": traceback.format_exc()})
            return
        self._send({"t": "fut_done", "f": fref, "v": self._pack(v)})

    def _check(self, obj, name):
        return any(m is None or name in m for cls, m in self._allowed.items() if isinstance(obj, cls))

    # --- serialization ---

    def _pack(self, v):
        if v is None or isinstance(v, (str, int, float, bool)):
            return v
        if isinstance(v, Proxy):
            d = {"_r": v._ref}
            if v._path:
                d["_p"] = v._path
            return d
        if isinstance(v, dict):
            return {k: self._pack(val) for k, val in v.items()}
        if isinstance(v, (list, tuple)):
            return [self._pack(x) for x in v]
        for cls in self._allowed:
            if isinstance(v, cls):
                return {"_r": self._new_ref(v)}
        # Any remaining callable (function, lambda, bound method, class)
        # gets auto-registered as a ref. The peer receives a Proxy whose
        # __call__ with empty path fires back to invoke the callable here,
        # preserving closures. Whitelist is bypassed deliberately: caller
        # explicitly handed over this callable, it's opt-in by reference.
        if callable(v):
            return {"_r": self._new_ref(v)}
        return str(v)

    def _unpack(self, v):
        if isinstance(v, dict):
            if "_r" in v:
                ref = v["_r"]
                path = v.get("_p", "")
                if ref in self._refs:
                    obj = self._refs[ref]
                    for attr in (path.split(".") if path else ()):
                        obj = getattr(obj, attr)
                    return obj
                return Proxy(self, ref, path)
            return {k: self._unpack(val) for k, val in v.items()}
        if isinstance(v, list):
            return [self._unpack(x) for x in v]
        return v

    def _new_ref(self, obj):
        self._next_ref += 1
        ref = f"{self._ref_prefix}{self._next_ref}"
        self._refs[ref] = obj
        return ref
