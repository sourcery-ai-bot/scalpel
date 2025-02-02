from datetime import datetime
from pathlib import Path

import gevent
import httpx
import pytest
import respx
from gevent.lock import RLock
from gevent.pool import Pool
from gevent.queue import JoinableQueue

from scalpel.core.config import Configuration
from scalpel.core.message_pack import datetime_decoder
from scalpel.core.spider import SpiderStatistics
from scalpel.green.files import read_mp
from scalpel.green.response import StaticResponse
from scalpel.green.robots import RobotsAnalyzer
from scalpel.green.static_spider import StaticSpider


@pytest.fixture(scope='module')
def green_spider():
    return StaticSpider(
        urls=['http://foo.com', 'http://bar.com', 'file:///home/kevin/page.html'], parse=lambda x, y: None
    )


class TestStaticSpider:
    """Tests class StaticSpider"""

    def test_specific_static_attributes_are_correctly_instantiated(self):
        config = Configuration(user_agent='mozilla/5.0')
        spider = StaticSpider(urls=['http://foo.com'], parse=lambda x, y: None, config=config)

        assert isinstance(spider._start_time, float)
        assert isinstance(spider._http_client, httpx.Client)
        assert isinstance(spider._robots_analyser, RobotsAnalyzer)
        assert config == spider._config
        assert isinstance(spider._lock, RLock)
        assert isinstance(spider._queue, JoinableQueue)
        assert len(spider.urls) == spider._queue.qsize()
        assert isinstance(spider._pool, Pool)

    # _fetch tests

    @respx.mock
    def test_fetch_method_returns_httpx_response(self, green_spider):
        url = 'http://foo.com'
        respx.get('http://foo.com') % {'text': 'content'}
        response = green_spider._fetch(url)

        assert 'content' == response.text
        assert 200 == response.status_code
        assert url == f'{response.url}'

    @respx.mock
    def test_middlewares_are_applied_when_fetching_resources(self, capsys):
        def log_middleware(fetch):
            def wrapper(*args, **kwargs):
                print('before fetching')
                return fetch(*args, **kwargs)

            print('after fetching')
            return wrapper

        url = 'http://foo.com'
        respx.get(url)
        config = Configuration(response_middlewares=[log_middleware])
        spider = StaticSpider(urls=[url], parse=lambda x, y: None, config=config)
        response = spider._fetch(url)

        assert 200 == response.status_code
        out, _ = capsys.readouterr()
        assert 'before fetching' in out
        assert 'after fetching' in out

    # _get_static_response test

    @pytest.mark.parametrize(
        ('url', 'text', 'httpx_response'),
        [
            ('file:///home/kevin/page.html', 'hello world', None),
            ('', '', httpx.Response(200, request=httpx.Request('GET', 'http://foo.com'))),
        ],
    )
    def test_should_return_static_response_when_giving_correct_input(self, url, text, httpx_response, green_spider):
        static_response = green_spider._get_static_response(url, text, httpx_response)

        assert isinstance(static_response, StaticResponse)
        assert static_response._reachable_urls is green_spider.reachable_urls
        assert static_response._followed_urls is green_spider.followed_urls
        assert static_response._queue is green_spider._queue
        assert static_response._url == url
        assert static_response._text == text
        assert static_response._httpx_response is httpx_response

    # _handle_url tests

    @pytest.mark.parametrize(
        ('reachable_urls', 'unreachable_urls', 'robots_excluded_urls'),
        [(set(), set(), {'http://foo.com'}), (set(), {'http://foo.com'}, set()), ({'http://foo.com'}, set(), set())],
    )
    def test_should_do_nothing_if_url_is_already_present_in_one_url_set(
        self, mocker, green_spider, reachable_urls, unreachable_urls, robots_excluded_urls
    ):
        url = 'http://foo.com'
        logger_mock = mocker.patch('logging.Logger.debug')
        green_spider.reachable_urls = reachable_urls
        green_spider.unreachable_urls = unreachable_urls
        green_spider.robots_excluded_urls = robots_excluded_urls
        green_spider._handle_url(url)

        logger_mock.assert_any_call('url %s has already been processed', url)

    def test_should_read_file_content_when_giving_a_file_url(self, tmp_path):
        parse_args = []
        hello_file = tmp_path / 'hello.txt'
        hello_file.write_text('hello world')
        file_url = hello_file.resolve().as_uri()

        def parse(spider, response):
            parse_args.extend([spider, response])

        static_spider = StaticSpider(urls=[file_url], parse=parse)
        static_spider._handle_url(file_url)

        assert parse_args[0] is static_spider
        static_response = parse_args[1]
        assert isinstance(static_response, StaticResponse)
        assert file_url == static_response._url
        assert 'hello world' == static_response._text
        assert static_response._httpx_response is None
        assert {file_url} == static_spider.reachable_urls

    def test_should_not_called_parse_method_when_file_cannot_be_opened(self, tmp_path, mocker):
        logger_mock = mocker.patch('logging.Logger.exception')
        hello_file = tmp_path / 'hello.txt'
        file_url = hello_file.resolve().as_uri()
        parse_args = []

        def parse(spider, response):
            parse_args.extend([spider, response])

        static_spider = StaticSpider(urls=[file_url], parse=parse)
        static_spider._handle_url(file_url)

        assert [] == parse_args
        logger_mock.assert_any_call('unable to open file %s', file_url)
        assert {file_url} == static_spider.unreachable_urls
        assert set() == static_spider.reachable_urls

    @respx.mock
    @pytest.mark.parametrize('status_code', [404, 500])
    def test_should_not_called_parse_method_if_httpx_response_is_an_error_one(self, mocker, status_code):
        parse_args = []
        url = 'http://foo.com'

        def parse(spider, response):
            parse_args.extend([spider, response])

        respx.get(url) % status_code
        logger_mock = mocker.patch('logging.Logger.info')
        static_spider = StaticSpider(urls=[url], parse=parse)
        static_spider._handle_url(url)

        assert [] == parse_args
        logger_mock.assert_any_call('fetching url %s returns an error with status code %s', url, status_code)

    @respx.mock
    def test_should_not_called_parse_method_if_url_is_forbidden_by_robots_txt(self, mocker):
        parse_args = []
        url = 'http://foo.com'

        def parse(spider, response):
            parse_args.extend([spider, response])

        respx.get(f'{url}/robots.txt') % 401
        logger_mock = mocker.patch('logging.Logger.info')
        static_spider = StaticSpider(urls=[url], parse=parse, config=Configuration(follow_robots_txt=True))
        static_spider._handle_url(url)

        assert [] == parse_args
        assert {url} == static_spider.robots_excluded_urls
        logger_mock.assert_any_call(
            'robots.txt rule has forbidden the processing of url %s or the url is not reachable', url
        )

    @respx.mock
    def test_should_fetch_content_when_giving_http_url(self):
        parse_args = []
        url = 'http://foo.com'

        def parse(spider, response):
            parse_args.extend([spider, response])

        respx.get(url) % {'status_code': 200, 'text': 'http content'}
        static_spider = StaticSpider(urls=[url], parse=parse)
        static_spider._handle_url(url)

        assert parse_args[0] is static_spider
        static_response = parse_args[1]
        assert isinstance(static_response, StaticResponse)
        assert '' == static_response._url
        assert '' == static_response._text
        assert 200 == static_response._httpx_response.status_code
        assert 'http content' == static_response._httpx_response.text
        assert 1 == static_spider.request_counter
        assert static_spider._total_fetch_time > 0

    @respx.mock
    def test_should_raise_error_if_parse_function_raises_error_and_ignore_errors_is_false(self):
        def parse(*_):
            raise ValueError('just a test')

        url = 'http://foo.com'
        respx.get(url)
        static_spider = StaticSpider(urls=[url], parse=parse, ignore_errors=False)

        with pytest.raises(ValueError) as exc_info:
            static_spider._handle_url(url)

        assert 'just a test' == str(exc_info.value)

    @respx.mock
    def test_should_not_raise_error_if_parse_function_raises_error_and_ignore_errors_is_true(self):
        def parse(*_):
            raise ValueError('just a test')

        url = 'http://foo.com'
        respx.get(url)
        static_spider = StaticSpider(urls=[url], parse=parse, ignore_errors=True)

        try:
            static_spider._handle_url(url)
        except ValueError:
            pytest.fail('unexpected ValueError raised when ignore_errors is set to true')

    # test _error_callback

    def test_should_log_error_when_task_name_is_worker(self, mocker):
        logger_mock = mocker.patch('logging.Logger.error')

        def raise_error():
            raise ValueError

        task = gevent.spawn(raise_error)
        task.name = 'worker'
        task.link_exception(StaticSpider._error_callback)
        task.join()

        logger_mock.assert_called_once()

    def test_should_not_log_error_when_task_name_is_different_from_worker(self, mocker):
        logger_mock = mocker.patch('logging.Logger.error')

        def raise_error():
            raise ValueError

        task = gevent.spawn(raise_error)
        task.link_exception(StaticSpider._error_callback)

        logger_mock.assert_not_called()

    # save_item tests

    def test_should_call_item_processors_and_reject_item_if_one_processor_returns_none(self, capsys, mocker):
        logger_mock = mocker.patch('logging.Logger.debug')
        data = {'banana': True}

        def processor_1(item):
            print("I'm a processor")
            return item

        def processor_2(item):
            if 'banana' in item:
                return
            return item

        config = Configuration(item_processors=[processor_1, processor_2])
        static_spider = StaticSpider(urls=['http://foo.com'], parse=lambda x, y: None, config=config)
        static_spider.save_item(data)

        logger_mock.assert_any_call('item %s was rejected', data)
        out, _ = capsys.readouterr()
        assert "I'm a processor" in out

    def test_should_save_content_to_backup_file(self, tmp_path, capsys):
        def processor(item):
            print("I'm a processor")
            return item

        backup = tmp_path / 'backup.mp'
        fruit_1 = {'fruit': 'pineapple'}
        fruit_2 = {'fruit': 'apple'}
        config = Configuration(backup_filename=f'{backup.resolve()}', item_processors=[processor])
        static_spider = StaticSpider(urls=['https://foo.com'], parse=lambda x, y: None, config=config)
        static_spider.save_item(fruit_1)
        static_spider.save_item(fruit_2)
        out, _ = capsys.readouterr()

        assert [fruit_1, fruit_2] == list(read_mp(f'{backup.resolve()}'))
        assert "I'm a processor" in out

    # simple test of run and statistics methods, more reliable tests are below

    @respx.mock
    def test_should_return_correct_statistics_after_running_spider(self):
        url1 = 'http://foo.com'
        url2 = 'http://bar.com'
        respx.get(url1, path='/')
        respx.get(url1, path='/robots.txt') % 404
        respx.get(f'{url2}/robots.txt') % 401

        config = Configuration(follow_robots_txt=True)
        static_spider = StaticSpider(urls=[url1, url2], parse=lambda x, y: None, config=config)
        static_spider.run()
        stats = static_spider.statistics()

        assert stats.reachable_urls == {url1}
        assert stats.unreachable_urls == set()
        assert stats.followed_urls == set()
        assert stats.robot_excluded_urls == {url2}
        assert stats.request_counter == 1
        assert stats.total_time > 0
        assert stats.average_fetch_time > 0


class TestIntegrationStaticSpider:
    """More concrete tests of StaticSpider class"""

    @staticmethod
    def processor(item: dict) -> dict:
        item['date'] = datetime.now()
        return item

    @staticmethod
    def parse(spider: StaticSpider, response: StaticResponse) -> None:
        quotes = [quote.strip() for quote in response.xpath('//blockquote/p/text()').getall()]
        authors = [author.strip() for author in response.css('blockquote footer::text').getall()]
        for quote, author in zip(quotes, authors):
            spider.save_item({'quote': quote, 'author': author})

        link = response.xpath('//a[2][contains(@href, "page")]/@href').get()
        if link is not None:
            response.follow(link)

    @staticmethod
    def common_assert(stats: SpiderStatistics, backup_path: Path):
        assert stats.unreachable_urls == set()
        assert stats.robot_excluded_urls == set()
        assert stats.total_time > 0

        albert_count = 0
        for item in read_mp(backup_path, decoder=datetime_decoder):
            assert isinstance(item['date'], datetime)
            if item['author'] == 'Albert Einstein':
                albert_count += 1

        assert albert_count == 3

    def test_should_work_with_file_url(self, page_1_file_url, tmp_path):
        backup_path = tmp_path / 'backup.mp'
        config = Configuration(item_processors=[self.processor], backup_filename=f'{backup_path}')
        static_spider = StaticSpider(urls=[page_1_file_url], parse=self.parse, config=config)
        static_spider.run()
        stats = static_spider.statistics()
        followed_urls = {
            page_1_file_url.replace('1', '2').replace('///', '/'),
            page_1_file_url.replace('1', '3').replace('///', '/'),
        }

        assert stats.reachable_urls == {page_1_file_url} | followed_urls
        assert stats.followed_urls == followed_urls
        assert stats.request_counter == 3
        assert stats.average_fetch_time == static_spider._total_fetch_time / stats.request_counter
        self.common_assert(stats, backup_path)

    @respx.mock
    def test_should_work_with_http_url(self, page_content, tmp_path):
        url = 'http://quotes.com'
        respx.get(url, path='/robots.txt') % 404
        respx.get(url, path='/') % {'html': page_content('page1.html')}
        for i in range(2, 4):
            respx.get(url, path=f'/page{i}.html') % {'html': page_content(f'page{i}.html')}

        backup_path = tmp_path / 'backup.mp'
        config = Configuration(
            item_processors=[self.processor], backup_filename=f'{backup_path}', follow_robots_txt=True
        )
        static_spider = StaticSpider(urls=[url], parse=self.parse, config=config)
        static_spider.run()
        stats = static_spider.statistics()
        followed_urls = {f'{url}/page{i}.html' for i in range(2, 4)}

        assert stats.reachable_urls == {url} | followed_urls
        assert stats.followed_urls == followed_urls
        assert stats.request_counter == 3
        assert stats.average_fetch_time == static_spider._total_fetch_time / stats.request_counter
        self.common_assert(stats, backup_path)
