import errno
import io
import socket
import sys
import array
import asyncio
import os
import re
import functools


import unittest

sys.path.insert(0, '..')
from yieldfrom_t.http import client
import testtcpserver as server
from testtcpserver import RECEIVE, FauxWriter

NotSocket = client.NotSocket

TestCase = unittest.TestCase

from test import support
support.use_resources = ['network']


here = os.path.dirname(__file__)
# Self-signed cert file for 'localhost'
CERT_localhost = os.path.join(here, 'keycert.pem')
# Self-signed cert file for 'fakehostname'
CERT_fakehostname = os.path.join(here, 'keycert2.pem')
# Root cert file (CA) for svn.python.org's cert
CACERT_svn_python_org = os.path.join(here, 'https_svn_python_org_root.pem')

# constants for testing chunked encoding
chunked_start = (
    'HTTP/1.1 200 OK\r\n'
    'Transfer-Encoding: chunked\r\n\r\n'
    'a\r\n'
    'hello worl\r\n'
    '3\r\n'
    'd! \r\n'
    '8\r\n'
    'and now \r\n'
    '22\r\n'
    'for something completely different\r\n'
)
chunked_expected = b'hello world! and now for something completely different'
chunk_extension = ";foo=bar"
last_chunk = "0\r\n"
last_chunk_extended = "0" + chunk_extension + "\r\n"
trailers = "X-Dummy: foo\r\nX-Dumm2: bar\r\n"
chunked_end = "\r\n"

HOST = support.HOST

CONNECT = ('127.0.0.1', 2222)
testLoop = asyncio.get_event_loop()


def async_test(f):

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        coro = asyncio.coroutine(f)
        future = coro(*args, **kwargs)
        testLoop.run_until_complete(future)
    return wrapper

async_test.__test__ = False # not a test


def open_socket_conn(host='127.0.0.1', port=2222):
    """  """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    sock.connect((host, port))
    return sock

def _prep_server(body, prime=False, reader=None):
    commands = []
    if type(body) == type([]):
        commands.extend(body)
    else:
        commands.extend([RECEIVE, body])
    srvr = server.AsyncioCommandServer(commands, testLoop if reader else None, reader, *CONNECT, verbose=False)
    if prime:
        sock = open_socket_conn(*CONNECT)
        sock.sendall(b' ')
        return srvr, sock
    else:
        return srvr, None

def _run_with_server(f, body='', srvr=None, sock=None):
    try:
        if srvr is None:
            srvr, sock = _prep_server(body, prime=True)
        testLoop.run_until_complete(f(sock))
    except:
        raise
    finally:
        srvr.stop()

def _run_with_server(f, body='', srvr=None):
    if srvr is None:
        srvr, _j = _prep_server(body)
    testLoop.run_until_complete(f(*CONNECT))
    srvr.stop()


class HeaderTests(TestCase):

    def test_auto_headers(self):
        # Some headers are added automatically, but should not be added by
        # .request() if they are explicitly set.

        class HeaderCountingBuffer(list):
            def __init__(self):
                self.count = {}
            def append(self, item):
                kv = item.split(b':')
                if len(kv) > 1:
                    # item is a 'Key: Value' header string
                    lcKey = kv[0].decode('ascii').lower()
                    self.count.setdefault(lcKey, 0)
                    self.count[lcKey] += 1
                list.append(self, item)

        @asyncio.coroutine
        def _run():
            for explicit_header in True, False:
                for header in 'Content-length', 'Host', 'Accept-encoding':
                    conn = client.HTTPConnection(*CONNECT)
                    #conn.sock = FakeSocket('blahblahblah')
                    conn._buffer = HeaderCountingBuffer()

                    body = 'spamspamspam'
                    headers = {}
                    if explicit_header:
                        headers[header] = str(len(body))
                    yield From (conn.request('POST', '/', body, headers))
                    self.assertEqual(conn._buffer.count[header.lower()], 1)

        srvr = server.CommandServer([RECEIVE, 'blahblahblah'], verbose=True)
        testLoop.run_until_complete(_run())
        srvr.stop()

    def test_content_length_0(self):

        class ContentLengthChecker(list):
            def __init__(self):
                list.__init__(self)
                self.content_length = None
            def append(self, item):
                kv = item.split(b':', 1)
                if len(kv) > 1 and kv[0].lower() == b'content-length':
                    self.content_length = kv[1].strip()
                list.append(self, item)

        def _run():
            # POST with empty body
            conn = client.HTTPConnection(*CONNECT)
            #conn.sock = FakeSocket(None)
            conn._buffer = ContentLengthChecker()
            yield From (asyncio.wait_for(conn.request('POST', '/', ''), 5.0))
            self.assertEqual(conn._buffer.content_length, b'0', 'Header Content-Length not set ')

            # PUT request with empty body
            conn = client.HTTPConnection(*CONNECT)
            #conn.sock = FakeSocket(None)
            conn._buffer = ContentLengthChecker()
            yield From (asyncio.wait_for(conn.request('PUT', '/', ''), 5.0))
            self.assertEqual(conn._buffer.content_length, b'0', 'Header Content-Length not set')

        srvr = server.CommandServer([RECEIVE, '', RECEIVE, ''], verbose=False)
        testLoop.run_until_complete(_run())
        srvr.stop()

    def test_putheader(self):
        conn = client.HTTPConnection('example.com')
        #conn.sock = FauxWriter()
        conn.putrequest('GET','/')
        conn.putheader('Content-length', 42)
        self.assertIn(b'Content-length: 42', conn._buffer)

    #@async_test
    def tst_ipv6host_header(self):
        # Default host header on IPv6 transaction should wrapped by [] if
        # its actual IPv6 address
        expected = b'GET /foo HTTP/1.1\r\nHost: [2001::]:81\r\n' \
                   b'Accept-Encoding: identity\r\n\r\n'

        @asyncio.coroutine
        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            conn = client.HTTPConnection('[2001::]:81')
            sock = FauxWriter(w)
            conn.writer = sock
            yield From (conn.request('GET', '/foo'))
            self.assertTrue(sock._data['data_out'].startswith(expected))

        _run_with_server(_run, [len(expected),''])

        expected1 = b'GET /foo HTTP/1.1\r\nHost: [2001:102A::]\r\n' \
                   b'Accept-Encoding: identity\r\n\r\n'

        @asyncio.coroutine
        def _run1(sock):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            conn = client.HTTPConnection('[2001:102A::]')
            sock = FauxWriter(w)
            conn.writer = sock
            yield From (conn.request('GET', '/foo'))
            self.assertTrue(sock._data['data_out'].startswith(expected1))

        _run_with_server(_run1, [len(expected1), ''])


class BasicTest(TestCase):

    def make_server(self, certfile):
        from test.ssl_servers import make_https_server
        return make_https_server(self, certfile=certfile)

    def test_status_lines(self):
        # Test HTTP status lines

        body = "HTTP/1.1 200 Ok\r\n\r\nText"

        @asyncio.coroutine
        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r)
            #yield From (resp.init())
            yield From (resp.begin())

            rr = yield From (resp.read(0))
            self.assertEqual(rr, b'')  # Issue #20007
            self.assertFalse(resp.isclosed())
            self.assertFalse(resp.closed)
            rr2 = yield From (resp.read())
            self.assertEqual(rr2, b"Text")
            self.assertTrue(resp.isclosed())
            self.assertFalse(resp.closed)
            resp.close()
            self.assertTrue(resp.closed)

        _run_with_server(_run, body)

        body1 = "HTTP/1.1 400.100 Not Ok\r\n\r\nText"

        @asyncio.coroutine
        def _run1(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r, w)
            resp = client.HTTPResponse(r)
            #yield From (resp.init())
            try:
                yield From (resp.begin())
            except client.BadStatusLine as e:
                self.assertTrue(True, 'BadStatusLine raised')
            else:
                self.assertTrue(False, 'BadStatusLine raised')

        _run_with_server(_run1, body1)


    def test_bad_status_repr(self):
        exc = client.BadStatusLine('')
        self.assertEqual(repr(exc), '''BadStatusLine("\'\'",)''')

    def test_partial_reads(self):

        body = "HTTP/1.1 200 Ok\r\nContent-Length: 4\r\n\r\nText"

        @asyncio.coroutine
        def _run(host, port):
            # if we have a length, the system knows when to close itself
            # same behaviour than when we read the whole thing with read()
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r)
            #yield From (resp.init())
            yield From (resp.begin())
            rr1 = yield From (resp.read(2))
            self.assertEqual(rr1, b'Te')
            self.assertFalse(resp.isclosed())
            rr2 = yield From (resp.read(2))
            self.assertEqual(rr2, b'xt')
            self.assertTrue(resp.isclosed())
            self.assertFalse(resp.closed)
            resp.close()
            self.assertTrue(resp.closed)

        _run_with_server(_run, body)


    def test_partial_readintos(self):
        # if we have a length, the system knows when to close itself
        # same behaviour than when we read the whole thing with read()
        body = "HTTP/1.1 200 Ok\r\nContent-Length: 4\r\n\r\nText"

        @asyncio.coroutine
        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r)
            #yield From (resp.init())
            yield From (resp.begin())
            b = bytearray(2)
            n = yield From (resp.readinto(b))
            self.assertEqual(n, 2)
            self.assertEqual(bytes(b), b'Te')
            self.assertFalse(resp.isclosed())
            n = yield From (resp.readinto(b))
            self.assertEqual(n, 2)
            self.assertEqual(bytes(b), b'xt')
            self.assertTrue(resp.isclosed())
            self.assertFalse(resp.closed)
            resp.close()
            self.assertTrue(resp.closed)

        _run_with_server(_run, body)

    #@async_test
    def test_partial_reads_no_content_length(self):

        body = "HTTP/1.1 200 Ok\r\n\r\nText"
        #sock = FakeSocket(body)

        @asyncio.coroutine
        def _run(host, port):
            # when no length is present, the socket should be gracefully closed when
            # all data was read
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r)
            #yield From (resp.init())
            yield From (resp.begin())
            rr1 = yield From (resp.read(2))
            self.assertEqual(rr1, b'Te')
            self.assertFalse(resp.isclosed())
            rr2 = yield From (resp.read(2))
            self.assertEqual(rr2, b'xt')
            rr3 = yield From (resp.read(1))
            self.assertEqual(rr3, b'')
            self.assertTrue(resp.isclosed())
            self.assertFalse(resp.closed)
            resp.close()
            self.assertTrue(resp.closed)

        _run_with_server(_run, body)

    def test_partial_readintos_no_content_length(self):
        # when no length is present, the socket should be gracefully closed when
        # all data was read
        body = "HTTP/1.1 200 Ok\r\n\r\nText"
        #sock = FakeSocket(body)

        @asyncio.coroutine
        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r)
            #yield From (resp.init())
            yield From (resp.begin())
            b = bytearray(2)
            n = yield From (resp.readinto(b))
            self.assertEqual(n, 2)
            self.assertEqual(bytes(b), b'Te')
            self.assertFalse(resp.isclosed())
            n = yield From (resp.readinto(b))
            self.assertEqual(n, 2)
            self.assertEqual(bytes(b), b'xt')
            n = yield From (resp.readinto(b))
            self.assertEqual(n, 0)
            self.assertTrue(resp.isclosed())

        _run_with_server(_run, body)

    def test_partial_reads_incomplete_body(self):
        # if the server shuts down the connection before the whole
        # content-length is delivered, the socket is gracefully closed
        body = "HTTP/1.1 200 Ok\r\nContent-Length: 10\r\n\r\nText"

        @asyncio.coroutine
        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r)
            #yield From (resp.init())
            yield From (resp.begin())
            rr1 = yield From (resp.read(2))
            self.assertEqual(rr1, b'Te')
            self.assertFalse(resp.isclosed())
            rr2 = yield From (resp.read(2))
            self.assertEqual(rr2, b'xt')
            rr3 = yield From (resp.read(1))
            self.assertEqual(rr3, b'')
            self.assertTrue(resp.isclosed())

        _run_with_server(_run, body)

    def test_partial_readintos_incomplete_body(self):
        # if the server shuts down the connection before the whole
        # content-length is delivered, the socket is gracefully closed
        body = "HTTP/1.1 200 Ok\r\nContent-Length: 10\r\n\r\nText"
        #sock = FakeSocket(body)

        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r)
            #yield From (resp.init())
            yield From (resp.begin())
            b = bytearray(2)
            n = yield From (resp.readinto(b))
            self.assertEqual(n, 2)
            self.assertEqual(bytes(b), b'Te')
            self.assertFalse(resp.isclosed())
            n = yield From (resp.readinto(b))
            self.assertEqual(n, 2)
            self.assertEqual(bytes(b), b'xt')
            n = yield From (resp.readinto(b))
            self.assertEqual(n, 0)
            self.assertTrue(resp.isclosed())
            self.assertFalse(resp.closed)
            resp.close()
            self.assertTrue(resp.closed)

        _run_with_server(_run, body)

    def test_host_port(self):
        # Check invalid host_port

        for hp in ("www.python.org:abc", "user:password@www.python.org"):
            self.assertRaises(client.InvalidURL, client.HTTPConnection, hp)

        for hp, h, p in (("[fe80::207:e9ff:fe9b]:8000",
                          "fe80::207:e9ff:fe9b", 8000),
                         ("www.python.org:80", "www.python.org", 80),
                         ("www.python.org:", "www.python.org", 80),
                         ("www.python.org", "www.python.org", 80),
                         ("[fe80::207:e9ff:fe9b]", "fe80::207:e9ff:fe9b", 80),
                         ("[fe80::207:e9ff:fe9b]:", "fe80::207:e9ff:fe9b", 80)):
            c = client.HTTPConnection(hp)
            self.assertEqual(h, c.host)
            self.assertEqual(p, c.port)

    def test_response_headers(self):
        # test response with multiple message headers with the same field name.
        text = ('HTTP/1.1 200 OK\r\n'
                'Set-Cookie: Customer="WILE_E_COYOTE"; '
                'Version="1"; Path="/acme"\r\n'
                'Set-Cookie: Part_Number="Rocket_Launcher_0001"; Version="1";'
                ' Path="/acme"\r\n'
                '\r\n'
                'No body\r\n')
        hdr = ('Customer="WILE_E_COYOTE"; Version="1"; Path="/acme"'
               ', '
               'Part_Number="Rocket_Launcher_0001"; Version="1"; Path="/acme"')

        #s = FakeSocket(text)
        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r, w)
            r = client.HTTPResponse(r)
            #yield From (r.init())
            yield From (r.begin())
            cookies = r.getheader("Set-Cookie")
            self.assertEqual(cookies, hdr)

        _run_with_server(_run, text)

    def test_read_head(self):
        # Test that the library doesn't attempt to read any data
        # from a HEAD request.  (Tickles SF bug #622042.)
        body = (
            'HTTP/1.1 200 OK\r\n'
            'Content-Length: 14432\r\n'
            '\r\n')

        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r, method="HEAD")
            #yield From (resp.init())
            yield From (resp.begin())
            rr = yield From (resp.read())
            if rr:
                self.fail("Did not expect response from HEAD request")

        _run_with_server(_run, body)

    def test_readinto_head(self):
        # Test that the library doesn't attempt to read any data
        # from a HEAD request.  (Tickles SF bug #622042.)
        body = (
            'HTTP/1.1 200 OK\r\n'
            'Content-Length: 14432\r\n'
            '\r\n')

        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r, method="HEAD")
            #yield From (resp.init())
            yield From (resp.begin())
            b = bytearray(5)
            n = yield From (resp.readinto(b))
            if n != 0:
                self.fail("Did not expect response from HEAD request")
            self.assertEqual(bytes(b), b'\x00'*5)

        _run_with_server(_run, body)

    def test_too_many_headers(self):
        headers = '\r\n'.join('Header%d: foo' % i
                              for i in range(client._MAXHEADERS + 1)) + '\r\n'
        text = ('HTTP/1.1 200 OK\r\n' + headers)

        @asyncio.coroutine
        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r, w)
            r = client.HTTPResponse(r)
            #yield From (r.init())
            try:
                yield From (r.begin())
            except client.HTTPException as e:
                self.assertTrue(re.compile("got more than \d+ headers").search(str(e)),'too many headers')
            else:
                self.assertFalse(True, r'')

        _run_with_server(_run, text)


    def test_send_file(self):
        expected = (b'GET /foo HTTP/1.1\r\nHost: 127.0.0.1:2222\r\n'
                    b'Accept-Encoding: identity\r\nContent-Length:')

        with open(__file__, 'rb') as body:
            tmp = body.read()
        with open(__file__, 'rb') as body:
            def _run(host, port):
                conn = client.HTTPConnection(host, port)
                #sock = FakeSocket(body)
                #sock = conn.sock
                yield From (conn.request('GET', '/foo', body))
                data = yield From (streamReader.read())
                self.assertTrue(data.startswith(expected), '%r != %r' %
                        (data[:len(expected)], expected))

            streamReader = asyncio.StreamReader()
            srvr, _ = _prep_server([len(tmp), expected], reader=streamReader)
            _run_with_server(_run, srvr=srvr)


    def test_send(self):
        expected = b'this is a test this is only a test'

        @asyncio.coroutine
        def _run(host, port):
            conn = client.HTTPConnection(host,port)
            #sock = FakeSocket(None)
            #conn.sock = sock
            yield From (conn.send(expected))
            rd = yield From (streamReader.readexactly(len(expected)))
            self.assertEqual(expected, rd)

            #yield From (conn.send(array.array('b', expected)))
            #rd = yield From (streamReader.readexactly(len(expected)))
            #self.assertEqual(expected, rd)

            yield From (conn.send(io.BytesIO(expected)))
            rd = yield From (streamReader.readexactly(len(expected)))
            self.assertEqual(expected, rd)

        streamReader = asyncio.StreamReader()
        srvr, _ = _prep_server([RECEIVE, '', RECEIVE, '', RECEIVE, ''], reader=streamReader)
        _run_with_server(_run, srvr=srvr)

    def test_send_updating_file(self):
        def data():
            yield 'data'
            yield None
            yield 'data_two'

        class UpdatingFile():
            mode = 'r'
            d = data()
            def read(self, blocksize=-1):
                return self.d.__next__()

        expected = b'data'

        def _run(host, port):
            conn = client.HTTPConnection(host, port)
            #sock = FakeSocket("")
            #conn.sock = sock
            yield From (conn.send(UpdatingFile()))
            resp = yield From (streamReader.read())
            self.assertEqual(resp, expected)

        streamReader = asyncio.StreamReader()
        srvr, _ = _prep_server([RECEIVE, '', RECEIVE, '', RECEIVE], reader=streamReader)
        _run_with_server(_run, srvr=srvr)

    def test_send_iter(self):
        expected = b'GET /foo HTTP/1.1\r\nHost: 127.0.0.1:2222\r\n' \
                   b'Accept-Encoding: identity\r\nContent-Length: 11\r\n' \
                   b'\r\nonetwothree'

        def body():
            yield b"one"
            yield b"two"
            yield b"three"

        def _run(host, port):
            conn = client.HTTPConnection(host, port)
            #sock = FakeSocket("")
            #conn.sock = sock
            yield From (conn.request('GET', '/foo', body(), {'Content-Length': '11'}))
            resp = yield From (streamReader.read())
            self.assertEqual(resp, expected)
            #print('iter done')
            srvr.stop()

        streamReader = asyncio.StreamReader()
        srvr, _ = _prep_server([len(expected)], reader=streamReader)
        _run_with_server(_run, srvr=srvr)

    #@async_test
    def test_send_type_error(self):

        @asyncio.coroutine
        def _run(host, port):
            # See: Issue #12676
            conn = client.HTTPConnection(host, port)
            #conn.sock = FakeSocket('')
            #with self.assertRaises(TypeError):
            try:
                yield From (conn.request('POST', 'test', conn))
            except TypeError as e:
                self.assertTrue(True, 'type error')
            else:
                self.assertFalse(True, 'type error')

        _run_with_server(_run, '')

    def test_chunked(self):

        expected = chunked_expected
        #sock = FakeSocket(chunked_start + last_chunk + chunked_end)

        @asyncio.coroutine
        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            ns = NotSocket(r,w)
            w.write(b' ')
            resp = client.HTTPResponse(ns, method="GET")
            #yield From (resp.init())
            yield From (resp.begin())
            r1 = yield From (resp.read())
            self.assertEqual(r1, expected)
            resp.close()

        _run_with_server(_run, chunked_start + last_chunk + chunked_end)

        @asyncio.coroutine
        def _run2(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            ns = NotSocket(r,w)
            resp = client.HTTPResponse(ns, method="GET")
            #yield From (resp.init())
            yield From (resp.begin())
            try:
                yield From (resp.read())
            except client.IncompleteRead as i:
                self.assertEqual(i.partial, expected)
                expected_message = 'IncompleteRead(%d bytes read)' % len(expected)
                self.assertEqual(repr(i), expected_message)
                self.assertEqual(str(i), expected_message)
            else:
                self.fail('IncompleteRead expected')
            finally:
                resp.close()
                #sock.close()

        _run_with_server(_run2, chunked_start + 'foo\r\n')

    def test_chunked1(self):

        expected = chunked_expected

        def _runO(n):

            @asyncio.coroutine
            def _run(host, port):

                r, w = yield From (asyncio.open_connection(host, port))
                w.write(b' ')
                r = NotSocket(r,w)
                resp = client.HTTPResponse(r, method="GET")
               # yield From (resp.init())
                yield From (resp.begin())
                r = []
                for _ in range(2):
                    r.append((yield From (resp.read(n))))
                r.append((yield From (resp.read())))
                self.assertEqual(b''.join(r), expected)
                resp.close()

            return _run

        # Various read sizes
        for n in range(1, 12):
            _run_with_server(_runO(n), chunked_start + last_chunk + chunked_end)

    def test_readinto_chunked(self):

        expected = chunked_expected
        nexpected = len(expected)
        b = bytearray(128)

        #sock = FakeSocket(chunked_start + last_chunk + chunked_end)

        @asyncio.coroutine
        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r, method="GET")
            #yield From (resp.init())
            yield From (resp.begin())
            n = yield From (resp.readinto(b))
            self.assertEqual(b[:nexpected], expected)
            self.assertEqual(n, nexpected)
            resp.close()

        _run_with_server(_run, chunked_start + last_chunk + chunked_end)

        @asyncio.coroutine
        def _run1(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r, method="GET")
            #yield From (resp.init())
            yield From (resp.begin())
            try:
                n = yield From (resp.readinto(b))
            except client.IncompleteRead as i:
                self.assertEqual(i.partial, expected)
                expected_message = 'IncompleteRead(%d bytes read)' % len(expected)
                self.assertEqual(repr(i), expected_message)
                self.assertEqual(str(i), expected_message)
            else:
                self.fail('IncompleteRead expected')
            finally:
                resp.close()

        _run_with_server(_run1, chunked_start + 'foo\r\n')

    def test_readinto_chunked1(self):

        expected = chunked_expected
        nexpected = len(expected)
        b = bytearray(128)

        def _runO(n):

            @asyncio.coroutine
            def _run(host, port):
                r, w = yield From (asyncio.open_connection(host, port))
                w.write(b' ')
                r = NotSocket(r,w)
                resp = client.HTTPResponse(r, method="GET")
                #yield From (resp.init())
                yield From (resp.begin())
                m = memoryview(b)
                i = yield From (resp.readinto(m[0:n]))
                i += (yield From (resp.readinto(m[i:n + i])))
                i += (yield From (resp.readinto(m[i:])))
                self.assertEqual(b[:nexpected], expected)
                self.assertEqual(i, nexpected)
                resp.close()

            return _run

        # Various read sizes
        for n in range(1, 12):
            _run_with_server(_runO(n), chunked_start + last_chunk + chunked_end)


    def test_chunked_head(self):
        chunked_start = (
            'HTTP/1.1 200 OK\r\n'
            'Transfer-Encoding: chunked\r\n\r\n'
            'a\r\n'
            'hello world\r\n'
            '1\r\n'
            'd\r\n'
        )
        # sock = FakeSocket(chunked_start + last_chunk + chunked_end)

        @asyncio.coroutine
        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r, method="HEAD")
            #yield From (resp.init())
            yield From (resp.begin())
            rr1 = yield From (resp.read())
            self.assertEqual(rr1, b'')
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.reason, 'OK')
            self.assertTrue(resp.isclosed())
            self.assertFalse(resp.closed)
            resp.close()
            self.assertTrue(resp.closed)

        _run_with_server(_run, chunked_start + last_chunk + chunked_end)

    def test_readinto_chunked_head(self):

        chunked_start_ = (
            'HTTP/1.1 200 OK\r\n'
            'Transfer-Encoding: chunked\r\n\r\n'
            'a\r\n'
            'hello world\r\n'
            '1\r\n'
            'd\r\n'
        )

        #sock = FakeSocket(chunked_start + last_chunk + chunked_end)

        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r, method="HEAD")
            #yield From (resp.init())
            yield From (resp.begin())
            b = bytearray(5)
            n = yield From (resp.readinto(b))
            self.assertEqual(n, 0)
            self.assertEqual(bytes(b), b'\x00'*5)
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.reason, 'OK')
            self.assertTrue(resp.isclosed())
            self.assertFalse(resp.closed)
            resp.close()
            self.assertTrue(resp.closed)

        _run_with_server(_run, chunked_start_+last_chunk+chunked_end)

    def tst_negative_content_length(self):
        #sock = FakeSocket(
        body = 'HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\nHello\r\n'

        @asyncio.coroutine
        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r, method="GET")
            #yield From (resp.init())
            yield From (resp.begin())
            rr1 = yield From (resp.read())
            self.assertEqual(rr1, b'Hello\r\n')
            self.assertTrue(resp.isclosed())

        _run_with_server(_run, body)

    def test_incomplete_read(self):
        body = 'HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nHello\r\n'

        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r, method="GET")
            #yield From (resp.init())
            yield From (resp.begin())
            try:
                yield From (resp.read())
            except client.IncompleteRead as i:
                self.assertEqual(i.partial, b'Hello\r\n')
                self.assertEqual(repr(i),
                                 "IncompleteRead(7 bytes read, 3 more expected)")
                self.assertEqual(str(i),
                                 "IncompleteRead(7 bytes read, 3 more expected)")
                self.assertTrue(resp.isclosed())
            else:
                self.fail('IncompleteRead expected')

        _run_with_server(_run, body)

    def tst_epipe(self):

        expected = (
            "HTTP/1.0 401 Authorization Required\r\n"
            "Content-type: text/html\r\n"
            "WWW-Authenticate: Basic realm=\"example\"\r\n")

        @asyncio.coroutine
        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            conn = client.HTTPConnection("example.com")
            sock = FauxWriter(sock=r)
            sock.breakOn(b'Content', OSError(errno.EPIPE, 'gotcha'))
            conn.soCk = sock

            try:
                yield From (conn.request("PUT", "/url", "body"*1000000))
            except OSError as e:
                self.assertTrue(True, 'OSError')
            else:
                self.fail('OSError')

        _run_with_server(_run, [RECEIVE, expected])

    def test_wwwauth(self):

        expected = (
            "HTTP/1.0 401 Authorization Required\r\n"
            "Content-type: text/html\r\n"
            "WWW-Authenticate: Basic realm=\"example\"\r\n")

        def _run1(host, port):
            """What is this test doing in an ePipe test? """

            conn = client.HTTPConnection(host, port)
            yield From (conn.request("PUT", "/url", "body"))

            resp = yield From (conn.getresponse())
            self.assertEqual(401, resp.status)
            self.assertEqual("Basic realm=\"example\"",
                             resp.getheader("www-authenticate"))

        _run_with_server(_run1, expected)


    # Test lines overflowing the max line size (_MAXLINE in http.aioclient)
    def test_overflowing_status_line(self):

        body = "HTTP/1.1 200 Ok" + "k" * 65536 + "\r\n"

        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r)
            #yield From (resp.init())
            try:
                yield From (resp.begin())
            except (client.LineTooLong, client.BadStatusLine) as e:
                self.assertTrue(True, 'overflow caught')
            else:
                self.assertTrue(False, 'overflow caught')

        _run_with_server(_run, body)

    def test_overflowing_header_line(self):
        body = (
            'HTTP/1.1 200 OK\r\n'
            'X-Foo: bar' + 'r' * 65536 + '\r\n\r\n'
        )

        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r)
            #yield From (resp.init())
            try:
                yield From (resp.begin())
            except client.LineTooLong as e:
                self.assertTrue(True, 'Raised LineTooLong')
            else:
                self.assertFalse(False, 'Raised LineTooLong')

        _run_with_server(_run, body)


    def test_overflowing_chunked_line(self):
        body = (
            'HTTP/1.1 200 OK\r\n'
            'Transfer-Encoding: chunked\r\n\r\n'
            + '0' * 65536 + 'a\r\n'
            'hello world\r\n'
            '0\r\n'
            '\r\n'
        )

        @asyncio.coroutine
        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r)
            #yield From (resp.init())
            yield From (resp.begin())
            try:
                yield From (resp.read())
            except client.LineTooLong as e:
                self.assertTrue(True, resp.read)

        _run_with_server(_run, body)


    def test_early_eof(self):
        # Test httpresponse with no \r\n termination,

        body = "HTTP/1.1 200 Ok"

        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            #sock = FakeSocket(body)
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r)
            #yield From (resp.init())
            yield From (resp.begin())
            rr1 = yield From (resp.read())
            self.assertEqual(rr1, b'')
            self.assertTrue(resp.isclosed())
            self.assertFalse(resp.closed)
            resp.close()
            self.assertTrue(resp.closed)

        _run_with_server(_run, body)

    def tst_delayed_ack_opt(self):
        # Test that Nagle/delayed_ack optimistaion works correctly.

        # For small payloads, it should coalesce the body with
        # headers, resulting in a single sendall() call
        @asyncio.coroutine
        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            conn = client.HTTPConnection('example.com')
            sock = FauxWriter(r)
            conn.writer = sock
            body = b'x' * (conn.mss - 1)
            yield From (conn.request('POST', '/', body))
            totCalls = sock._data['sendall_calls'] + sock._data['send_calls']
            self.assertEqual(totCalls, 1)

        _run_with_server(_run, '')

        @asyncio.coroutine
        def _run1(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            conn = client.HTTPConnection('example.com')
            r = NotSocket(r,w)
            sock = FauxWriter(r)
            conn.writer = sock
            body = b'x' * conn.mss * 2
            yield From (conn.request('POST', '/', body))
            totCalls = sock._data['sendall_calls'] + sock._data['send_calls']
            self.assertGreater(totCalls, 1)

        _run_with_server(_run1, '')

    def test_chunked_extension(self):
        extra = '3;foo=bar\r\n' + 'abc\r\n'
        expected = chunked_expected + b'abc'

        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r, method="GET")
            #yield From (resp.init())
            yield From (resp.begin())

            d = yield From (resp.read())
            self.assertEqual(d, expected)
            resp.close()

        _run_with_server(_run, chunked_start + extra + last_chunk_extended + chunked_end)

    def test_chunked_missing_end(self):
        """some servers may serve up a short chunked encoding stream"""
        expected = chunked_expected

        @asyncio.coroutine
        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r, method="GET")
            #yield From (resp.init())
            yield From (resp.begin())

            d = yield From (resp.read())
            self.assertEqual(d, expected)
            resp.close()

        _run_with_server(_run, chunked_start + last_chunk)

    def test_chunked_trailers(self):
        """See that trailers are read and ignored"""
        expected = chunked_expected

        @asyncio.coroutine
        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r, method="GET")
            #yield From (resp.init())
            yield From (resp.begin())

            d = yield From (resp.read())
            self.assertEqual(d, expected)
            # we should have reached the end of the file
            d1 = yield From (resp.read())
            self.assertEqual(d1, b"") #we read to the end
            resp.close()

        _run_with_server(_run, chunked_start + last_chunk + trailers + chunked_end)

    def test_chunked_sync(self):
        """Check that we don't read past the end of the chunked-encoding stream"""
        expected = chunked_expected
        extradata = "extradata"

        @asyncio.coroutine
        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r, method="GET")
            #yield From (resp.init())
            yield From (resp.begin())

            d = yield From (resp.read())
            self.assertEqual(d, expected)

            # the file should now have our extradata ready to be read
            d1 = yield From (resp.read())
            self.assertEqual(d1, b'') #we read to the end
            #self.assertEqual(sock.file.read(100),
            resp.close()

        _run_with_server(_run, chunked_start + last_chunk + trailers + chunked_end + extradata)

    def test_content_length_sync(self):
        """Check that we don't read past the end of the Content-Length stream"""
        extradata = "extradata"
        expected = b"Hello123\r\n"

        @asyncio.coroutine
        def _run(host, port):

            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r,w)
            resp = client.HTTPResponse(r, method="GET")
            #yield From (resp.init())
            yield From (resp.begin())

            d = yield From (resp.read())
            self.assertEqual(d, expected)

            # the file should now have our extradata ready to be read
            d1 = yield From (resp.read())
            self.assertEqual(d1, b'') #we read to the end
            resp.close()

        _run_with_server(_run, 'HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nHello123\r\n' + extradata)

# class ExtendedReadTest(TestCase):
#     """
#     Test peek(), read1(), readline()
#     """
#     lines = (
#         'HTTP/1.1 200 OK\r\n'
#         '\r\n'
#         'hello world!\n'
#         'and now \n'
#         'for something completely different\n'
#         'foo'
#         )
#     lines_expected = lines[lines.find('hello'):].encode("ascii")
#     lines_chunked = (
#         'HTTP/1.1 200 OK\r\n'
#         'Transfer-Encoding: chunked\r\n\r\n'
#         'a\r\n'
#         'hello worl\r\n'
#         '3\r\n'
#         'd!\n\r\n'
#         '9\r\n'
#         'and now \n\r\n'
#         '23\r\n'
#         'for something completely different\n\r\n'
#         '3\r\n'
#         'foo\r\n'
#         '0\r\n' # terminating chunk
#         '\r\n'  # end of trailers
#     )
#
#     def setUp(self):
#         sock = FakeSocket(self.lines)
#         resp = aioclient.HTTPResponse(sock, method="GET")
#         yield From (resp.init())
#         yield From (resp.begin())
#         resp.fp = io.BufferedReader(resp.fp)
#         self.resp = resp
#
#     def test_peek(self):
#         resp = self.resp
#         # patch up the buffered peek so that it returns not too much stuff
#         oldpeek = resp.fp.peek
#         def mypeek(n=-1):
#             p = oldpeek(n)
#             if n >= 0:
#                 return p[:n]
#             return p[:10]
#         resp.fp.peek = mypeek
#
#         all = []
#         while True:
#             # try a short peek
#             p = resp.peek(3)
#             if p:
#                 self.assertGreater(len(p), 0)
#                 # then unbounded peek
#                 p2 = resp.peek()
#                 self.assertGreaterEqual(len(p2), len(p))
#                 self.assertTrue(p2.startswith(p))
#                 next = resp.read(len(p2))
#                 self.assertEqual(next, p2)
#             else:
#                 next = resp.read()
#                 self.assertFalse(next)
#             all.append(next)
#             if not next:
#                 break
#         self.assertEqual(b"".join(all), self.lines_expected)
#
#     def test_readline(self):
#         resp = self.resp
#         self._verify_readline(self.resp.readline, self.lines_expected)
#
#     def _verify_readline(self, readline, expected):
#         all = []
#         while True:
#             # short readlines
#             line = readline(5)
#             if line and line != b"foo":
#                 if len(line) < 5:
#                     self.assertTrue(line.endswith(b"\n"))
#             all.append(line)
#             if not line:
#                 break
#         self.assertEqual(b"".join(all), expected)
#
#     def test_read1(self):
#         resp = self.resp
#         def r():
#             res = resp.read1(4)
#             self.assertLessEqual(len(res), 4)
#             return res
#         readliner = Readliner(r)
#         self._verify_readline(readliner.readline, self.lines_expected)
#
#     def test_read1_unbounded(self):
#         resp = self.resp
#         all = []
#         while True:
#             data = resp.read1()
#             if not data:
#                 break
#             all.append(data)
#         self.assertEqual(b"".join(all), self.lines_expected)
#
#     def test_read1_bounded(self):
#         resp = self.resp
#         all = []
#         while True:
#             data = resp.read1(10)
#             if not data:
#                 break
#             self.assertLessEqual(len(data), 10)
#             all.append(data)
#         self.assertEqual(b"".join(all), self.lines_expected)
#
#     def test_read1_0(self):
#         self.assertEqual(self.resp.read1(0), b"")
#
#     def test_peek_0(self):
#         p = self.resp.peek(0)
#         self.assertLessEqual(0, len(p))
#
# class ExtendedReadTestChunked(ExtendedReadTest):
#     """
#     Test peek(), read1(), readline() in chunked mode
#     """
#     lines = (
#         'HTTP/1.1 200 OK\r\n'
#         'Transfer-Encoding: chunked\r\n\r\n'
#         'a\r\n'
#         'hello worl\r\n'
#         '3\r\n'
#         'd!\n\r\n'
#         '9\r\n'
#         'and now \n\r\n'
#         '23\r\n'
#         'for something completely different\n\r\n'
#         '3\r\n'
#         'foo\r\n'
#         '0\r\n' # terminating chunk
#         '\r\n'  # end of trailers
#     )
#
#
class Readliner:
    """
    a simple readline class that uses an arbitrary read function and buffering
    """
    def __init__(self, readfunc):
        self.readfunc = readfunc
        self.remainder = b""

    def readline(self, limit):
        data = []
        datalen = 0
        read = self.remainder
        try:
            while True:
                idx = read.find(b'\n')
                if idx != -1:
                    break
                if datalen + len(read) >= limit:
                    idx = limit - datalen - 1
                # read more data
                data.append(read)
                read = self.readfunc()
                if not read:
                    idx = 0 #eof condition
                    break
            idx += 1
            data.append(read[:idx])
            self.remainder = read[idx:]
            return b"".join(data)
        except:
            self.remainder = b"".join(data)
            raise

class OfflineTest(TestCase):
    def test_responses(self):
        self.assertEqual(client.responses[client.NOT_FOUND], "Not Found")


class SourceAddressTest(TestCase):
    def setUp(self):
        self.serv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.port = support.bind_port(self.serv)
        self.source_port = support.find_unused_port()
        self.serv.listen(0)
        self.conn = None

    def tearDown(self):
        if self.conn:
            self.conn.close()
            self.conn = None
        self.serv.close()
        self.serv = None

    @async_test
    def tstHTTPConnectionSourceAddress(self):
        self.conn = client.HTTPConnection(HOST, self.port,
                source_address=('127.0.0.1', self.source_port))
        yield From (self.conn.connect())
        self.assertEqual(self.conn.writer.getsockname()[1], self.source_port)

    @unittest.skipIf(not hasattr(client, 'HTTPSConnection'),
                     'http.aioclient.HTTPSConnection not defined')
    def testHTTPSConnectionSourceAddress(self):
        self.conn = client.HTTPSConnection(HOST, self.port,
                source_address=('', self.source_port))
        # We don't test anything here other the constructor not barfing as
        # this code doesn't deal with setting up an active running SSL server
        # for an ssl_wrapped connect() to actually return from.

# sockets with asyncio are non-blocking, and timeouts are not relevant to test.

# class TimeoutTest(TestCase):
#     PORT = None
#
#     def _setUp(self):
#         self.serv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#         TimeoutTest.PORT = 2222 #support.bind_port(self.serv)
#         self.serv.bind(('127.0.0.1', 2222))
#         self.serv.listen(1)
#         time.sleep(1)
#
#     def _tearDown(self):
#         self.serv.close()
#         self.serv = None
#
#     def testTimeoutAttribute(self):
#         # This will prove that the timeout gets through HTTPConnection
#         # and into the socket.
#
#         def _run(host, port):
#             # default -- use global socket timeout
#             self.assertIsNone(socket.getdefaulttimeout())
#             socket.setdefaulttimeout(30)
#             try:
#                 httpConn = aioclient.HTTPConnection(host, port)
#                 yield From (httpConn.connect())
#             finally:
#                 socket.setdefaulttimeout(None)
#             self.assertEqual(httpConn.sock.gettimeout(), 30)
#             httpConn.close()
#
#             # no timeout -- do not use global socket default
#             self.assertIsNone(socket.getdefaulttimeout())
#             socket.setdefaulttimeout(30)
#             try:
#                 httpConn = aioclient.HTTPConnection(host, port, timeout=None)
#                 yield From (httpConn.connect())
#             finally:
#                 socket.setdefaulttimeout(None)
#             self.assertEqual(httpConn.sock.gettimeout(), None)
#             httpConn.close()
#
#             # a value
#             httpConn = aioclient.HTTPConnection(host, port, timeout=30)
#             yield From (httpConn.connect())
#             self.assertEqual(httpConn.sock.gettimeout(), 30)
#             httpConn.close()
#
#         _run_with_server(_run, '')


class HTTPSTest(TestCase):

    def setUp(self):
        if not hasattr(client, 'HTTPSConnection'):
            self.skipTest('ssl support required')

    def make_server(self, certfile):
        from test.ssl_servers import make_https_server
        return make_https_server(self, certfile=certfile)

    # def test_attributes(self):
    #     # simple test to check it's storing the timeout
    #     h = aioclient.HTTPSConnection(HOST, TimeoutTest.PORT, timeout=30)
    #     self.assertEqual(h.TIMEOUT, 30)

    def _check_svn_python_org(self, resp):
        # Just a simple check that everything went fine
        server_string = resp.getheader('server')
        self.assertIn('Apache', server_string)

    @async_test
    def test_networked(self):
        # Default settings: no cert verification is done
        support.requires('network')
        with support.transient_internet('svn.python.org'):
            h = client.HTTPSConnection('svn.python.org', 443)
            yield From (h.request('GET', '/'))
            resp = yield From (h.getresponse())
            self._check_svn_python_org(resp)

    @async_test
    def test_networked_good_cert(self):
        # We feed a CA cert that validates the server's cert
        import ssl
        support.requires('network')
        with support.transient_internet('svn.python.org'):
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1)
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(CACERT_svn_python_org)
            h = client.HTTPSConnection('svn.python.org', 443, context=context)
            yield From (h.request('GET', '/'))
            resp = yield From (h.getresponse())
            self._check_svn_python_org(resp)

    @async_test
    def test_networked_bad_cert(self):
        # We feed a "CA" cert that is unrelated to the server's cert
        import ssl
        support.requires('network')
        with support.transient_internet('svn.python.org'):
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1)
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(CERT_localhost)
            h = client.HTTPSConnection('svn.python.org', 443, context=context)
            with self.assertRaises(ssl.SSLError):
                yield From (h.request('GET', '/'))

    def test_local_good_hostname(self):
        # The (valid) cert validates the HTTP hostname
        import ssl

        def _run():
            server = self.make_server(CERT_localhost)
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1)
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(CERT_localhost)
            h = client.HTTPSConnection('localhost', server.port, context=context)
            yield From (h.request('GET', '/nonexistent'))
            resp = yield From (h.getresponse())
            self.assertEqual(resp.status, 404)
            del server

        coro = asyncio.coroutine(_run)
        future = coro()
        testLoop.run_until_complete(future)


    @async_test
    def test_local_bad_hostname(self):
        # The (valid) cert doesn't validate the HTTP hostname
        import ssl
        server = self.make_server(CERT_fakehostname)
        context = ssl.SSLContext(ssl.PROTOCOL_TLSv1)
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(CERT_fakehostname)
        h = client.HTTPSConnection('localhost', server.port, context=context)
        with self.assertRaises(ssl.CertificateError):
            yield From (h.request('GET', '/'))
        # Same with explicit check_hostname=True
        h = client.HTTPSConnection('localhost', server.port, context=context, check_hostname=True)
        with self.assertRaises(ssl.CertificateError):
            yield From (h.request('GET', '/'))
        # With check_hostname=False, the mismatching is ignored
        h = client.HTTPSConnection('localhost', server.port, context=context, check_hostname=False)
        yield From (h.request('GET', '/nonexistent'))
        resp = yield From (h.getresponse())
        self.assertEqual(resp.status, 404)
        del server

    @unittest.skipIf(not hasattr(client, 'HTTPSConnection'),
                     'http.aioclient.HTTPSConnection not available')
    def test_host_port(self):
        # Check invalid host_port

        for hp in ("www.python.org:abc", "user:password@www.python.org"):
            self.assertRaises(client.InvalidURL, client.HTTPSConnection, hp)

        for hp, h, p in (("[fe80::207:e9ff:fe9b]:8000",
                          "fe80::207:e9ff:fe9b", 8000),
                         ("www.python.org:443", "www.python.org", 443),
                         ("www.python.org:", "www.python.org", 443),
                         ("www.python.org", "www.python.org", 443),
                         ("[fe80::207:e9ff:fe9b]", "fe80::207:e9ff:fe9b", 443),
                         ("[fe80::207:e9ff:fe9b]:", "fe80::207:e9ff:fe9b",
                             443)):
            c = client.HTTPSConnection(hp)
            self.assertEqual(h, c.host)
            self.assertEqual(p, c.port)


class RequestBodyTest(TestCase):
    """Test cases where a request includes a message body."""

    def _setUpXXX(self):
        self.conn = client.HTTPConnection('example.com')
        #self.conn.sock = self.sock = FakeSocket("")
        #self.conn.sock = self.sock

    def get_headers_and_fp(self, streamReader):

        #while not srvr.received or b'\r\n' not in b''.join(srvr.received):
        #    pass
        f = streamReader
        #f = io.BytesIO()
        yield From (f.readline()  # read the request line)
        message = yield From (client.parse_headers(f))
        return message, f

    def test_manual_content_length(self):

        @asyncio.coroutine
        def _run(host, port):
            self.conn = client.HTTPConnection(host, port)
            # Set an incorrect content-length so that we can verify that
            # it will not be over-ridden by the library.
            yield From (self.conn.request("PUT", "/url", "body", {"Content-Length": "42"}))
            message, f = yield From (self.get_headers_and_fp(streamReader))
            self.assertEqual("42", message.get("content-length"))
            d = yield From (f.read())
            self.assertEqual(4, len(d))

        streamReader = asyncio.StreamReader()
        srvr, _ = _prep_server('', prime=False, reader=streamReader)
        _run_with_server(_run, srvr=srvr)


    def test_ascii_body(self):

        @asyncio.coroutine
        def _run(host, port):
            #r, w = yield From (asyncio.open_connection(host, port))
            #w.write(b' ')
            self.conn = client.HTTPConnection(host, port)
            yield From (self.conn.request("PUT", "/url", "body"))
            message, f = yield From (self.get_headers_and_fp(streamReader))
            self.assertEqual("text/plain", message.get_content_type())
            self.assertIsNone(message.get_charset())
            self.assertEqual("4", message.get("content-length"))
            d = yield From (f.read())
            self.assertEqual(b'body', d)

        streamReader = asyncio.StreamReader()
        srvr, _ = _prep_server('', prime=False, reader=streamReader)
        _run_with_server(_run, srvr=srvr)

    def test_latin1_body(self):

        @asyncio.coroutine
        def _run(host, port):

            self.conn = client.HTTPConnection(host, port)
            yield From (self.conn.request("PUT", "/url", "body\xc1"))
            message, f = yield From (self.get_headers_and_fp(streamReader))
            self.assertEqual("text/plain", message.get_content_type())
            self.assertIsNone(message.get_charset())
            self.assertEqual("5", message.get("content-length"))
            d = yield From (f.read())
            self.assertEqual(b'body\xc1', d)

        streamReader = asyncio.StreamReader()
        srvr, _ = _prep_server('', prime=False, reader=streamReader)
        _run_with_server(_run, srvr=srvr)

    def test_bytes_body(self):

        @asyncio.coroutine
        def _run(host, port):

            self.conn = client.HTTPConnection(host, port)
            yield From (self.conn.request("PUT", "/url", b"body\xc1"))
            message, f = yield From (self.get_headers_and_fp(streamReader))
            self.assertEqual("text/plain", message.get_content_type())
            self.assertIsNone(message.get_charset())
            self.assertEqual("5", message.get("content-length"))
            d = yield From (f.read())
            self.assertEqual(b'body\xc1', d)

        streamReader = asyncio.StreamReader()
        srvr, _ = _prep_server('', prime=False, reader=streamReader)
        _run_with_server(_run, srvr=srvr)

    def test_file_body(self):

        @asyncio.coroutine
        def _run(host, port):

            self.conn = client.HTTPConnection(host, port)
            self.addCleanup(support.unlink, support.TESTFN)
            with open(support.TESTFN, "w") as of:
                of.write("body")
            with open(support.TESTFN) as of:
                yield From (self.conn.request("PUT", "/url", of))
                message, f = yield From (self.get_headers_and_fp(streamReader))
                self.assertEqual("text/plain", message.get_content_type())
                self.assertIsNone(message.get_charset())
                self.assertEqual("4", message.get("content-length"))
                d = yield From (f.read())
                self.assertEqual(b'body', d)

        streamReader = asyncio.StreamReader()
        srvr, _ = _prep_server([93,''], prime=False, reader=streamReader)
        _run_with_server(_run, srvr=srvr)

    def test_binary_file_body(self):

        @asyncio.coroutine
        def _run(host, port):

            self.conn = client.HTTPConnection(host, port)
            self.addCleanup(support.unlink, support.TESTFN)
            with open(support.TESTFN, "wb") as of:
                of.write(b"body\xc1")
            with open(support.TESTFN, "rb") as of:
                yield From (self.conn.request("PUT", "/url", of))
                message, f = yield From (self.get_headers_and_fp(streamReader))
                self.assertEqual("text/plain", message.get_content_type())
                self.assertIsNone(message.get_charset())
                self.assertEqual("5", message.get("content-length"))
                d = yield From (f.read())
                self.assertEqual(b'body\xc1', d)

        streamReader = asyncio.StreamReader()
        srvr, _ = _prep_server([94, ''], prime=False, reader=streamReader)
        _run_with_server(_run, srvr=srvr)


class HTTPResponseTest(TestCase):

    body = "HTTP/1.1 200 Ok\r\nMy-Header: first-value\r\nMy-Header: \
            second-value\r\n\r\nText"

    @asyncio.coroutine
    def _setUp(self, sock):
        #sock = FakeSocket(body)
        self.resp = client.HTTPResponse(sock)
        #yield From (self.resp.init())
        yield From (self.resp.begin())

    def test_getting_header(self):

        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r, w)
            yield From (self._setUp(r))
            header = self.resp.getheader('My-Header')
            self.assertEqual(header, 'first-value, second-value')

            header = self.resp.getheader('My-Header', 'some default')
            self.assertEqual(header, 'first-value, second-value')

        _run_with_server(_run, self.body)

    def test_getting_nonexistent_header_with_string_default(self):

        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r, w)
            yield From (self._setUp(r))
            header = self.resp.getheader('No-Such-Header', 'default-value')
            self.assertEqual(header, 'default-value')

        _run_with_server(_run, self.body)

    def test_getting_nonexistent_header_with_iterable_default(self):

        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r, w)
            yield From (self._setUp(r))
            header = self.resp.getheader('No-Such-Header', ['default', 'values'])
            self.assertEqual(header, 'default, values')

            header = self.resp.getheader('No-Such-Header', ('default', 'values'))
            self.assertEqual(header, 'default, values')

        _run_with_server(_run, self.body)

    def test_getting_nonexistent_header_without_default(self):

        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r, w)
            yield From (self._setUp(r))
            header = self.resp.getheader('No-Such-Header')
            self.assertEqual(header, None)

        _run_with_server(_run, self.body)

    def test_getting_header_defaultint(self):

        def _run(host, port):
            r, w = yield From (asyncio.open_connection(host, port))
            w.write(b' ')
            r = NotSocket(r, w)
            yield From (self._setUp(r))
            header = self.resp.getheader('No-Such-Header',default=42)
            self.assertEqual(header, 42)

        _run_with_server(_run, self.body)

class TunnelTests(TestCase):

    # this test is not quite right. sometimes it works, and sometimes not
    def tst_connect(self):
        response_text = (
            'HTTP/1.0 200 OK\r\n\r\n', # Reply to CONNECT
            'HTTP/1.1 200 OK\r\n' # Reply to HEAD
            'Content-Length: 42\r\n\r\n'
        )

        def create_connection(address, timeout=None, source_address=None):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(address)
            return FauxSocket(sock=sock, host=address[0], port=address[1])

        @asyncio.coroutine
        def _run(host, port):

            conn = client.HTTPConnection(host, port)
            conn._create_connection = create_connection

            #print('create connect 1')
            # Once connected, we shouldn't be able to tunnel anymore
            yield From (conn.connect())
            self.assertRaises(RuntimeError, conn.set_tunnel, 'destination.com')

            # But if we close the connection, we're good
            #print('close 1')
            conn.close()
            conn.set_tunnel('destination.com')
            yield From (conn.request('HEAD', '/', ''))
            #print('head requested')

            self.assertEqual(conn.sock.host, '127.0.0.1')
            self.assertEqual(conn.sock.port, port)
            self.assertTrue(b'CONNECT destination.com' in conn.sock._data['data_out'])
            self.assertTrue(b'Host: destination.com' in conn.sock._data['data_out'])

            # This test should be removed when CONNECT gets the HTTP/1.1 blessing
            self.assertTrue(b'Host: 127.0.0.1' not in conn.sock._data['data_out'])

            #print('close 2')
            conn.close()
            yield From (conn.request('PUT', '/', ''))
            #print('PUT requested')
            self.assertEqual(conn.sock.host, '127.0.0.1')
            self.assertEqual(conn.sock.port, port)
            self.assertTrue(b'CONNECT destination.com' in conn.sock._data['data_out'])
            self.assertTrue(b'Host: destination.com' in conn.sock._data['data_out'])

        _run_with_server(_run, [RECEIVE, response_text[0], 39, response_text[1],
                                93, response_text[1], 39, response_text[1], 92])


def main(verbose=None):
    support.run_unittest(HeaderTests, OfflineTest, BasicTest, #TimeoutTest,
                         #HTTPSTest,
                         RequestBodyTest, SourceAddressTest,
                         HTTPResponseTest, #ExtendedReadTest,
                         #ExtendedReadTestChunked,
                         TunnelTests)


if __name__ == '__main__':
    main()
