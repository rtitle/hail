from typing import BinaryIO, Optional, Tuple, Type
from types import TracebackType
import abc
import io
import os
from concurrent.futures import ThreadPoolExecutor
import janus
from hailtop.utils import blocking_to_async


class ReadableStream(abc.ABC):
    def __init__(self):
        self._closed = False
        self._waited_closed = False

    async def read(self, n: int = -1) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        self._closed = True

    @abc.abstractmethod
    async def _wait_closed(self) -> None:
        pass

    async def wait_closed(self) -> None:
        self._closed = True
        if not self._waited_closed:
            try:
                await self._wait_closed()
            finally:
                self._waited_closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    async def __aenter__(self) -> 'ReadableStream':
        return self

    async def __aexit__(
            self, exc_type: Optional[Type[BaseException]] = None,
            exc_value: Optional[BaseException] = None,
            exc_traceback: Optional[TracebackType] = None) -> None:
        await self.wait_closed()


class WritableStream(abc.ABC):
    def __init__(self):
        self._closed = False
        self._waited_closed = False

    def writable(self) -> bool:  # pylint: disable=no-self-use
        return False

    async def write(self, b: bytes) -> int:
        raise NotImplementedError

    def close(self) -> None:
        self._closed = True

    @abc.abstractmethod
    async def _wait_closed(self) -> None:
        pass

    async def wait_closed(self) -> None:
        self._closed = True
        if not self._waited_closed:
            try:
                await self._wait_closed()
            finally:
                self._waited_closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    async def __aenter__(self) -> 'WritableStream':
        return self

    async def __aexit__(
            self, exc_type: Optional[Type[BaseException]] = None,
            exc_value: Optional[BaseException] = None,
            exc_traceback: Optional[TracebackType] = None) -> None:
        await self.wait_closed()


class _ReadableStreamFromBlocking(ReadableStream):
    _thread_pool: ThreadPoolExecutor
    _f: BinaryIO

    def __init__(self, thread_pool: ThreadPoolExecutor, f: BinaryIO):
        super().__init__()
        self._thread_pool = thread_pool
        self._f = f

    async def read(self, n: int = -1) -> bytes:
        if n == -1:
            return await blocking_to_async(self._thread_pool, self._f.read)
        return await blocking_to_async(self._thread_pool, self._f.read, n)

    async def _wait_closed(self) -> None:
        await blocking_to_async(self._thread_pool, self._f.close)
        del self._f


class _WritableStreamFromBlocking(WritableStream):
    _thread_pool: ThreadPoolExecutor
    _f: BinaryIO

    def __init__(self, thread_pool: ThreadPoolExecutor, f: BinaryIO):
        super().__init__()
        self._thread_pool = thread_pool
        self._f = f

    def writable(self) -> bool:
        return self._f.writable()

    async def write(self, b: bytes) -> int:
        return await blocking_to_async(self._thread_pool, self._f.write, b)

    async def _wait_closed(self) -> None:
        await blocking_to_async(self._thread_pool, self._f.flush)
        await blocking_to_async(self._thread_pool, os.fsync, self._f.fileno())
        await blocking_to_async(self._thread_pool, self._f.close)
        del self._f


def blocking_readable_stream_to_async(thread_pool: ThreadPoolExecutor, f: BinaryIO) -> _ReadableStreamFromBlocking:
    return _ReadableStreamFromBlocking(thread_pool, f)


def blocking_writable_stream_to_async(thread_pool: ThreadPoolExecutor, f: BinaryIO) -> _WritableStreamFromBlocking:
    return _WritableStreamFromBlocking(thread_pool, f)


class _Closable:
    def __init__(self):
        super().__init__()
        self._closed = False


class BlockingQueueReadableStream(io.RawIOBase, _Closable):
    # self.closed and self.close() must be multithread safe, because
    # they can be accessed by both the stream reader and writer which
    # are in different threads.
    def __init__(self, q: janus.Queue):
        super().__init__()
        self._q = q
        self._saw_eos = False
        self._closed = False
        self._unread = memoryview(b'')
        self._off = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray) -> int:
        if self._closed:
            raise ValueError('read on closed stream')
        if self._saw_eos:
            return 0

        # If readinto only partially fills b without hitting the end
        # of stream, then the upload_obj returns an EntityTooSmall
        # error in some cases.
        total = 0
        while total < len(b):
            if self._off == len(self._unread):
                self._unread = self._q.sync_q.get()
                if self._unread is None:
                    self._saw_eos = True
                    return total
                self._off = 0
                self._unread = memoryview(self._unread)

            n = min(len(self._unread) - self._off, len(b) - total)
            b[total:total + n] = self._unread[self._off:self._off + n]
            self._off += n
            total += n
            assert total == len(b) or self._off == len(self._unread)

        return total

    def close(self):
        self._closed = True
        # drain the q so the writer doesn't deadlock
        while not self._saw_eos:
            c = self._q.sync_q.get()
            if c is None:
                self._saw_eos = True


class AsyncQueueWritableStream(WritableStream):
    def __init__(self, q: janus.Queue, reader: _Closable):
        super().__init__()
        self._sent_eos = False
        self._q = q
        self._reader = reader

    async def write(self, b: bytes) -> int:
        if self._reader._closed:
            if not self._sent_eos:
                await self._q.async_q.put(None)
                self._sent_eos = True
            raise ValueError('reader closed')
        if b:
            await self._q.async_q.put(b)
        return len(b)

    async def _wait_closed(self) -> None:
        if not self._sent_eos:
            await self._q.async_q.put(None)
            self._sent_eos = True


def async_writable_blocking_readable_stream_pair() -> Tuple[AsyncQueueWritableStream, BlockingQueueReadableStream]:
    q: janus.Queue = janus.Queue(maxsize=1)
    blocking_readable = BlockingQueueReadableStream(q)
    async_writable = AsyncQueueWritableStream(q, blocking_readable)
    return async_writable, blocking_readable


class BlockingCollect(_Closable):
    def __init__(self, q: janus.Queue, size_hint: int):
        super().__init__()
        self._q = q
        self._closed = False
        self._size_hint = size_hint

    def get(self) -> bytes:
        n = self._size_hint
        buf = bytearray(n)
        off = 0
        while True:
            b = self._q.sync_q.get()
            if b is None:
                self._closed = True

                # Unfortunately, we can't use a memory view here, we
                # need to slice/copy buf, since the S3 upload_part API
                # call requires byte or bytearray:
                # Invalid type for parameter Body, value: <memory at 0x7f512801b6d0>, type: <class 'memoryview'>, valid types: <class 'bytes'>, <class 'bytearray'>, file-like object
                if off == len(buf):
                    return buf
                return buf[:off]

            k = len(b)
            if k > n - off:
                new_n = n * 2
                while k > new_n - off:
                    new_n *= 2
                new_buf = bytearray(new_n)
                view = memoryview(buf)
                new_buf[:off] = view[:off]
                buf = new_buf
                n = new_n
            assert k <= n - off

            buf[off:off + k] = b
            off += k


def async_writable_blocking_collect_pair(size_hint: int) -> Tuple[AsyncQueueWritableStream, BlockingCollect]:
    q: janus.Queue = janus.Queue(maxsize=1)
    blocking_collect = BlockingCollect(q, size_hint)
    async_writable = AsyncQueueWritableStream(q, blocking_collect)
    return async_writable, blocking_collect
