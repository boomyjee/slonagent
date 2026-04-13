"""Bidirectional JSON-lines RPC over pipes.

Works from both sync and async code on either side.

Threading model:
  * reader  — blocking readline loop. Each incoming "call" is handed off
              to a fresh daemon worker thread; reader never runs handlers.
  * worker  — one per incoming call, daemon thread. Either runs the sync
              handler directly, or drives the async handler on `async_loop`
              via run_coroutine_threadsafe (if provided) else on a fresh
              per-call event loop.

Why this shape:
  * Daemon threads → Ctrl+C / interpreter exit actually works even if
    a handler is "pinned" forever (e.g. Agent.loop()).
  * `async_loop` lets the host pin all async handlers to its main loop
    so things like aiogram/uvicorn (whose sessions are loop-bound) keep
    working when sandbox calls back into `transport.send_message`.
  * A per-call fresh loop is fine for the sandbox side (no libs bound
    to a shared loop) and gives maximum parallelism.
  * `max_workers` caps in-flight calls. When saturated the next call is
    rejected with an error (not queued) — silent queuing is the classic
    RPC-deadlock setup (pinned threads waiting on a reply that's stuck
    behind them in the backlog).

Outgoing calls:
  * ch.call(ref, method, *a, **kw) → RpcFuture: both awaitable and .wait()-able.
  * proxy.method(...)              → if caller has a running loop, returns
                                     a coroutine-compatible _ProxyCall
                                     (await, or use sync dunders like
                                     `for x in proxy.method()`). Otherwise
                                     blocks and returns the unpacked value.

Objects of allowed classes are passed by reference (ref ID). The other
side gets a Proxy that forwards calls back.
"""

import asyncio, json, logging, threading, traceback

log = logging.getLogger(__name__)


class RpcFuture:
    """Result of an RPC call. Both awaitable and blocking.

        result = await fut       # async code
        result = fut.wait()      # sync code

    Resolved from the reader thread via _set_result / _set_error.
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
        return afut.__await__()

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise TimeoutError("rpc call timed out")
        if self._error is not None:
            raise RuntimeError(self._error)
        return self._value


class _ProxyCall:
    """Coroutine-compatible wrapper over an already-fired RPC call.

    The RPC fires the moment Proxy(...) is invoked — not when this is
    awaited. So `proxy.method(x)` without await still fires the call
    (fire-and-forget), with no "never awaited" warning because this
    object implements the coroutine protocol.

    Also supports sync access (iter/bool/len/getitem) by blocking on
    the future. Safe because each incoming call runs on its own daemon
    thread — blocking here never stops another handler from running.
    """

    __slots__ = ("_rfut", "_it")

    def __init__(self, rfut):
        self._rfut = rfut
        self._it = None

    def __await__(self):
        return self._rfut.__await__()

    def _iter(self):
        if self._it is None:
            self._it = self._rfut.__await__()
        return self._it

    def send(self, value):
        return self._iter().send(value)

    def throw(self, typ, val=None, tb=None):
        return self._iter().throw(typ, val, tb)

    def close(self):
        if self._it is not None and hasattr(self._it, "close"):
            self._it.close()

    def wait(self, timeout=None):
        return self._rfut.wait(timeout)

    # Sync-access dunders: let callers that don't `await` still get the
    # value by iterating/testing/indexing.
    def __iter__(self):
        return iter(self._rfut.wait())

    def __bool__(self):
        return bool(self._rfut.wait())

    def __len__(self):
        return len(self._rfut.wait())

    def __getitem__(self, key):
        return self._rfut.wait()[key]


class Proxy:
    """Remote-object reference. Dotted attribute access builds a method path.

    Calling a Proxy fires the RPC immediately, then:
      * If there's a running event loop (async context) → return a
        _ProxyCall. The caller can `await proxy.method(...)` or use
        sync dunders (`for x in proxy.method()`).
      * Otherwise (no running loop) → block on .wait() and return
        the unpacked value directly.
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
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return rfut.wait()
        return _ProxyCall(rfut)

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
        self._next_id = 0
        self._next_ref = 0
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
        """Issue an RPC call. Returns RpcFuture — `await` or `.wait()`."""
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
                elif t in ("ok", "err"):
                    self._resolve(msg)
        finally:
            self._fail_pending("channel closed")

    def _fail_pending(self, err):
        for rfut in list(self._pending.values()):
            rfut._set_error(err)
        self._pending.clear()

    def _resolve(self, msg):
        rfut = self._pending.pop(msg["i"], None)
        if not rfut:
            return
        if msg["t"] == "err":
            rfut._set_error(msg["e"])
        else:
            value = Proxy(self, msg["r"]) if "r" in msg else self._unpack(msg.get("v"))
            rfut._set_result(value)

    def _dispatch_call(self, msg):
        """Resolve target and run the handler on its own daemon worker thread.

        If all workers are busy, reject the call with an error instead of
        queuing. Silent queuing is the classic RPC-deadlock setup: a pinned
        thread waits for a reply that's stuck behind it in the queue.
        """
        try:
            obj = self._refs[msg["r"]]
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
        """Run a handler. Sync call returns directly. Async call is driven
        on `async_loop` (if provided) via run_coroutine_threadsafe, else on
        a fresh per-call event loop owned by this worker thread."""
        result = fn(*args, **kwargs)
        if not asyncio.iscoroutine(result):
            return result
        if self._async_loop is not None:
            cfut = asyncio.run_coroutine_threadsafe(result, self._async_loop)
            return cfut.result()
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(result)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def _send_ok(self, cid, result):
        packed = self._pack(result)
        if isinstance(packed, dict) and "_r" in packed:
            self._send({"t": "ok", "i": cid, "r": packed["_r"]})
        else:
            self._send({"t": "ok", "i": cid, "v": packed})

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
