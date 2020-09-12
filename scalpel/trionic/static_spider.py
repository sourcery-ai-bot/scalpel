import logging
import math
import platform
from asyncio import iscoroutinefunction
from typing import Callable, Optional, Any, Union

import attr
import httpx
import trio
from rfc3986 import uri_reference

from scalpel.core.spider import Spider
from .files import write_mp
from .response import StaticResponse
from .robots import RobotsAnalyzer
from .utils.queue import Queue

logger = logging.getLogger('scalpel')


@attr.s(slots=True)
class StaticSpider(Spider):
    # order is important here, http_client must come before robots_analyzer since the latter used the former
    _start_time: float = attr.ib(init=False, repr=False, factory=trio.current_time)
    _http_client: httpx.AsyncClient = attr.ib(init=False, repr=False)
    _robots_analyser: RobotsAnalyzer = attr.ib(init=False, repr=False)
    _fetch: Callable = attr.ib(init=False, repr=False)
    _lock: trio.Lock = attr.ib(init=False, repr=False, factory=trio.Lock)
    _queue: Queue = attr.ib(init=False, repr=False)

    def __attrs_post_init__(self):

        async def _get_fetch(url: str) -> httpx.Response:
            return await self._http_client.get(url)

        self._fetch = _get_fetch
        for middleware in self.config.response_middlewares:
            self._fetch = middleware(self._fetch)

    @_http_client.default
    def _get_http_client(self) -> httpx.AsyncClient:
        headers = {'User-Agent': self.config.user_agent}
        logger.debug('getting a default httpx client with user agent: %s', self.config.user_agent)
        return httpx.AsyncClient(headers=headers)

    @_robots_analyser.default
    def _get_robots_analyzer(self) -> RobotsAnalyzer:
        logger.debug('getting a default robots analyzer')
        return RobotsAnalyzer(
            http_client=self._http_client,
            robots_cache=trio.Path(self.config.robots_cache_folder),
            user_agent=self.config.user_agent
        )

    @_queue.default
    def _get_queue(self) -> Queue:
        logger.debug('getting a default queue')
        return Queue(size=math.inf, items=self.urls)

    def _get_static_response(
            self, url: str = '', text: str = '', httpx_response: httpx.Response = None
    ) -> StaticResponse:
        logger.debug(
            'returning StaticResponse object with url: %s, text: %s and httpx_response: %s', url, text, httpx_response
        )
        return StaticResponse(
            self.reachable_urls, self.followed_urls, self._queue, url=url, text=text,
            httpx_response=httpx_response
        )

    # noinspection PyBroadException
    async def _handle_url(self, url: str) -> None:
        if url in self.reachable_urls or url in self.unreachable_urls or url in self.robots_excluded_urls:
            logger.debug('url %s has already been processed', url)
            self._queue.task_done()
            return

        static_url = text = ''
        response: Optional[httpx.Response] = None
        ur = uri_reference(url)
        if ur.scheme == 'file':
            static_url = url
            logger.debug('url %s is a file url so we attempt to read its content')
            file_path = ur.path[1:] if platform.system() == 'Windows' else ur.path
            try:
                async with await trio.open_file(file_path) as f:
                    text = await f.read()
            except OSError:
                logger.exception('unable to open file %s', url)
                self.unreachable_urls.add(url)
                return
            self.reachable_urls.add(url)
        else:
            response: httpx.Response = await self._fetch(url)
            if response.is_error:
                logger.info('fetching url %s returns an error with status code %s', url, response.status_code)
                self.unreachable_urls.add(url)
                return
            # we update some variables for statistics
            self.request_counter += 1
            self.reachable_urls.add(url)
            self._total_fetch_time += response.elapsed.total_seconds()

        try:
            await self.parse(self, self._get_static_response(static_url, text, response))
        except Exception:
            logger.exception('something unexpected happened while parsing the content at url %s', url)
            if not self._ignore_errors:
                self._queue.task_done()
                raise
        self._queue.task_done()
        logger.info('content at url %s has been processed', url)

    async def save_item(self, item: Any) -> None:
        item_rejected = False
        original_item = item
        for processor in self.config.item_processors:
            if iscoroutinefunction(processor):
                item = await processor(item)
            else:
                item = processor(item)
            if item is None:
                item_rejected = True
                break
        if item_rejected:
            logger.debug('item %s was rejected', original_item)
            return

        logger.debug('writing item %s to file %s', item, self.config.backup_filename)
        async with self._lock:
            await write_mp(self.config.backup_filename, item, mode='a', encoder=self.config.msgpack_encoder)

    async def _get_request_delay(self, url: str) -> Union[int, float]:
        if self.config.follow_robots_txt:
            return await self._robots_analyser.get_request_delay(url, self.config.request_delay)
        await trio.sleep(0)  # checkpoint to ensure an async function in every case
        return self.config.request_delay

    async def worker(self, nursery: trio.Nursery) -> None:
        while True:
            url = await self._queue.get()
            request_delay = await self._get_request_delay(url)
            if request_delay == -1:  # url is not accessible
                self.robots_excluded_urls.add(url)
                self._queue.task_done()
                continue
            nursery.start_soon(self._handle_url, url)
            await trio.sleep(request_delay)

    async def run(self):
        """
        The spider main loop where all downloads, parsing happens.
        """
        async with trio.open_nursery() as nursery:
            nursery.start_soon(self.worker, nursery)
            await self._queue.join()
            # at this point, all the urls were handled, so the only remaining task is the worker
            nursery.cancel_scope.cancel()

        await self._http_client.aclose()
        self._duration = trio.current_time() - self._start_time
