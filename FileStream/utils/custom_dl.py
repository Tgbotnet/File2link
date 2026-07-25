import asyncio
import logging
from typing import Dict, List, Optional, Tuple, Union

from FileStream.bot import work_loads
from FileStream.config import Download
from pyrogram import Client, utils, raw
from .file_properties import get_file_ids
from pyrogram.session import Session, Auth
from pyrogram.errors import AuthBytesInvalid, FloodWait
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.types import Message

# Global ceiling on how many 1 MB chunk requests may be in flight across the whole
# process at once. Without this, N concurrent downloads x 40 parts each would
# happily try to buffer gigabytes of RAM.
_GLOBAL_INFLIGHT = asyncio.Semaphore(Download.MAX_TOTAL_PARTS)


class ByteStreamer:
    def __init__(self, client: Client):
        self.clean_timer = 30 * 60
        self.client: Client = client
        self.cached_file_ids: Dict[str, FileId] = {}
        # dc_id -> pool of media sessions, plus a round-robin cursor and a live
        # in-flight counter per connection (used to pick the idlest socket)
        self.session_pools: Dict[int, List[Session]] = {}
        self.__pool_cursor: Dict[int, int] = {}
        self._session_load: Dict[int, List[int]] = {}
        self.__pool_lock = asyncio.Lock()
        asyncio.create_task(self.clean_cache())

    async def get_file_properties(self, db_id: str, multi_clients) -> FileId:
        """
        Returns the properties of a media of a specific message in a FIleId class.
        if the properties are cached, then it'll return the cached results.
        or it'll generate the properties from the Message ID and cache them.
        """
        if db_id not in self.cached_file_ids:
            await self.generate_file_properties(db_id, multi_clients)
            logging.debug(f"Cached file properties for file with ID {db_id}")
        return self.cached_file_ids[db_id]

    async def generate_file_properties(self, db_id: str, multi_clients) -> FileId:
        """
        Generates the properties of a media file on a specific message.
        returns ths properties in a FIleId class.
        """
        file_id = await get_file_ids(self.client, db_id, multi_clients, Message)
        self.cached_file_ids[db_id] = file_id
        logging.debug(f"Cached media file with ID {db_id}")
        return self.cached_file_ids[db_id]

    # ------------------------------------------------------------------ sessions

    async def _new_media_session(
        self, client: Client, dc_id: int, auth_key: Optional[bytes] = None
    ) -> Tuple[Session, bytes]:
        """
        Opens one media connection to `dc_id`.

        The first connection for a foreign DC has to create an auth key and import
        the exported authorization. Every later connection in the same pool reuses
        that key, so we only pay for ExportAuthorization once (it is rate limited).
        """
        test_mode = await client.storage.test_mode()

        if dc_id != await client.storage.dc_id():
            needs_import = auth_key is None
            if needs_import:
                auth_key = await Auth(client, dc_id, test_mode).create()

            session = Session(client, dc_id, auth_key, test_mode, is_media=True)
            await session.start()

            if needs_import:
                for _ in range(6):
                    exported_auth = await client.invoke(
                        raw.functions.auth.ExportAuthorization(dc_id=dc_id)
                    )
                    try:
                        await session.invoke(
                            raw.functions.auth.ImportAuthorization(
                                id=exported_auth.id, bytes=exported_auth.bytes
                            )
                        )
                        break
                    except AuthBytesInvalid:
                        logging.debug(f"Invalid authorization bytes for DC {dc_id}")
                        continue
                else:
                    await session.stop()
                    raise AuthBytesInvalid
        else:
            auth_key = await client.storage.auth_key()
            session = Session(client, dc_id, auth_key, test_mode, is_media=True)
            await session.start()

        return session, auth_key

    async def get_session_pool(self, client: Client, file_id: FileId) -> List[Session]:
        """
        Returns a pool of live media sessions for the file's DC.

        A single MTProto connection serialises its own request queue, so one
        connection caps out well below the link speed. Spreading chunk requests
        over several connections is what actually multiplies throughput.
        """
        dc_id = file_id.dc_id
        pool = self.session_pools.get(dc_id)
        if pool:
            return pool

        async with self.__pool_lock:
            pool = self.session_pools.get(dc_id)
            if pool:
                return pool

            first, auth_key = await self._new_media_session(client, dc_id)
            pool = [first]

            extra = Download.SESSIONS_PER_DC - 1
            if extra > 0:
                results = await asyncio.gather(
                    *[
                        self._new_media_session(client, dc_id, auth_key)
                        for _ in range(extra)
                    ],
                    return_exceptions=True,
                )
                for res in results:
                    if isinstance(res, BaseException):
                        logging.warning(f"Could not open extra media session: {res}")
                    else:
                        pool.append(res[0])

            # keep Pyrogram's own cache in sync so the rest of the library still works
            client.media_sessions[dc_id] = pool[0]
            self.session_pools[dc_id] = pool
            self.__pool_cursor[dc_id] = 0
            logging.info(f"Opened {len(pool)} media connection(s) to DC {dc_id}")
            return pool

    def _next_session(self, dc_id: int, pool: List[Session]) -> Session:
        cursor = self.__pool_cursor.get(dc_id, 0)
        self.__pool_cursor[dc_id] = (cursor + 1) % len(pool)
        return pool[cursor % len(pool)]

    def _loads(self, dc_id: int, pool: List[Session]) -> List[int]:
        # Built lazily rather than assumed, so an instance created without
        # __init__ (as test harnesses do) still works.
        store = getattr(self, "_session_load", None)
        if store is None:
            store = self._session_load = {}
        loads = store.get(dc_id)
        if loads is None or len(loads) != len(pool):
            loads = store[dc_id] = [0] * len(pool)
        return loads

    def _pick_session(self, dc_id: int, pool: List[Session]) -> Tuple[int, Session]:
        """
        Pick the connection with the fewest requests already queued on it.

        Round-robin looks fair but is not: a socket that hit a slow chunk keeps
        being handed new work at the same rate as the idle ones, so its queue
        grows and everything behind it arrives late. Least-loaded keeps the
        sockets even, which is most of what turns a sawtooth into a flat line.
        """
        loads = self._loads(dc_id, pool)
        idx = min(range(len(pool)), key=loads.__getitem__)
        return idx, pool[idx]

    # ------------------------------------------------------------------ location

    @staticmethod
    async def get_location(file_id: FileId) -> Union[raw.types.InputPhotoFileLocation,
                                                     raw.types.InputDocumentFileLocation,
                                                     raw.types.InputPeerPhotoFileLocation,]:
        """
        Returns the file location for the media file.
        """
        file_type = file_id.file_type

        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=file_id.chat_id, access_hash=file_id.chat_access_hash
                )
            else:
                if file_id.chat_access_hash == 0:
                    peer = raw.types.InputPeerChat(chat_id=-file_id.chat_id)
                else:
                    peer = raw.types.InputPeerChannel(
                        channel_id=utils.get_channel_id(file_id.chat_id),
                        access_hash=file_id.chat_access_hash,
                    )

            location = raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                volume_id=file_id.volume_id,
                local_id=file_id.local_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
            )
        elif file_type == FileType.PHOTO:
            location = raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        else:
            location = raw.types.InputDocumentFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        return location

    # ------------------------------------------------------------------ streaming

    async def yield_file(
        self,
        file_id: FileId,
        index: int,
        offset: int,
        first_part_cut: int,
        last_part_cut: int,
        part_count: int,
        chunk_size: int,
        parallel: int = 1,
    ) -> Union[str, None]:
        """
        Yields the bytes of the media file, in order, while fetching up to
        `parallel` chunks from Telegram at the same time.

        Telegram caps upload.GetFile at 1 MB per call, so the only way to go faster
        than one-round-trip-per-megabyte is to keep many requests in flight and
        reassemble them in order. This keeps a sliding window of `parallel`
        outstanding requests: as soon as the caller consumes chunk N, chunk
        N + parallel is scheduled.

        Chunked download logic modded from
        <https://github.com/eyaadh/megadlbot_oss/blob/master/mega/telegram/utils/custom_download.py#L20>
        Thanks to Eyaadh <https://github.com/eyaadh>
        """
        client = self.client
        work_loads[index] += 1

        parallel = max(1, min(parallel, Download.MAX_PARALLEL_PARTS, part_count))
        dc_id = file_id.dc_id

        try:
            pool = await self.get_session_pool(client, file_id)
            location = await self.get_location(file_id)
        except Exception:
            work_loads[index] -= 1
            raise

        # Never queue more than the pool can actually absorb; the surplus just
        # sits behind a busy socket and adds latency rather than throughput.
        parallel = max(1, min(parallel, len(pool) * Download.MAX_INFLIGHT_PER_SESSION))
        loads = self._loads(dc_id, pool)
        hedge_after = Download.HEDGE_AFTER_MS / 1000.0

        logging.debug(
            f"Streaming {part_count} part(s) via client {index} "
            f"with {parallel} parallel request(s) over {len(pool)} connection(s)"
        )

        async def one_request(part_offset: int) -> bytes:
            """A single GetFile on the idlest connection, with load accounting."""
            idx, session = self._pick_session(dc_id, pool)
            loads[idx] += 1
            try:
                r = await session.invoke(
                    raw.functions.upload.GetFile(
                        location=location,
                        offset=part_offset,
                        limit=chunk_size,
                    )
                )
                if isinstance(r, raw.types.upload.File):
                    return r.bytes
                return b""
            finally:
                loads[idx] -= 1

        async def hedged(part_offset: int) -> bytes:
            """
            Ask one connection; if it is slow, ask a second and take whichever
            answers first.

            Chunks have to be yielded in order, so one straggler stalls the
            entire stream regardless of how many later parts are already done.
            A duplicate request wastes at most one megabyte and removes the
            stall, which is the difference between 'averages 30 MB/s' and
            'holds 30 MB/s'.
            """
            first = asyncio.create_task(one_request(part_offset))
            if hedge_after <= 0:
                return await first

            done, _ = await asyncio.wait({first}, timeout=hedge_after)
            if first in done:
                return first.result()

            second = asyncio.create_task(one_request(part_offset))
            racers = {first, second}
            last_error: Optional[BaseException] = None
            try:
                while racers:
                    done, racers = await asyncio.wait(
                        racers, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        error = task.exception()
                        if error is None:
                            return task.result()
                        last_error = error
                if last_error is not None:
                    raise last_error
                return b""
            finally:
                for task in (first, second):
                    if not task.done():
                        task.cancel()

        async def fetch(part: int) -> bytes:
            """Fetch a single 1 MB chunk, hedged, with retries."""
            part_offset = offset + part * chunk_size
            for attempt in range(3):
                try:
                    async with _GLOBAL_INFLIGHT:
                        return await hedged(part_offset)
                except FloodWait as e:
                    # Sleep OUTSIDE the semaphore. Holding a global slot while
                    # waiting out a flood limit starves every other download.
                    wait = min(int(getattr(e, "value", 1)), 20)
                    logging.warning(f"FloodWait {wait}s on part {part}")
                    await asyncio.sleep(wait)
                except (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError):
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.35 * (attempt + 1))
            return b""

        tasks: Dict[int, asyncio.Task] = {}
        next_part = 0

        def top_up():
            """Keep the in-flight window full."""
            nonlocal next_part
            while next_part < part_count and len(tasks) < parallel:
                tasks[next_part] = asyncio.create_task(fetch(next_part))
                next_part += 1

        try:
            top_up()
            for current in range(part_count):
                task = tasks.pop(current, None)
                if task is None:
                    break
                chunk = await task
                top_up()

                if not chunk:
                    break
                if part_count == 1:
                    yield chunk[first_part_cut:last_part_cut]
                elif current == 0:
                    yield chunk[first_part_cut:]
                elif current == part_count - 1:
                    yield chunk[:last_part_cut]
                else:
                    yield chunk
        except (TimeoutError, AttributeError, asyncio.CancelledError):
            pass
        finally:
            for task in tasks.values():
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks.values(), return_exceptions=True)
            work_loads[index] -= 1
            logging.debug("Finished yielding file.")

    async def clean_cache(self) -> None:
        """
        function to clean the cache to reduce memory usage
        """
        while True:
            await asyncio.sleep(self.clean_timer)
            self.cached_file_ids.clear()
            logging.debug("Cleaned the cache")
