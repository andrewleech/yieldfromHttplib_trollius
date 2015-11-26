"""HTTP/1.1 client library

<intro stuff goes here>
<other stuff, too>

HTTPConnection goes through a number of "states", which define when a client
may legally make another request or fetch the response for a particular
request. This diagram details these state transitions:

    (null)
      |
      | HTTPConnection()
      v
    Idle
      |
      | putrequest()
      v
    Request-started
      |
      | ( putheader() )*  endheaders()
      v
    Request-sent
      |
      | response = getresponse()
      v
    Unread-response   [Response-headers-read]
      |\____________________
      |                     |
      | response.read()     | putrequest()
      v                     v
    Idle                  Req-started-unread-response
                     ______/|
                   /        |
   response.read() |        | ( putheader() )*  endheaders()
                   v        v
       Request-started    Req-sent-unread-response
                            |
                            | response.read()
                            v
                          Request-sent

This diagram presents the following rules:
  -- a second request may not be started until {response-headers-read}
  -- a response [object] cannot be retrieved until {request-sent}
  -- there is no differentiation between an unread response body and a
     partially read response body

Note: this enforcement is applied by the HTTPConnection class. The
      HTTPResponse class does not enforce this state machine, which
      implies sophisticated clients may accelerate the request/response
      pipeline. Caution should be taken, though: accelerating the states
      beyond the above pattern may imply knowledge of the server's
      connection-close behavior for certain requests. For example, it
      is impossible to tell whether the server will close the connection
      UNTIL the response headers have been read; this means that further
      requests cannot be placed into the pipeline until it is known that
      the server will NOT be closing the connection.

Logical State                  __state            __response
-------------                  -------            ----------
Idle                           _CS_IDLE           None
Request-started                _CS_REQ_STARTED    None
Request-sent                   _CS_REQ_SENT       None
Unread-response                _CS_IDLE           <response_class>
Req-started-unread-response    _CS_REQ_STARTED    <response_class>
Req-sent-unread-response       _CS_REQ_SENT       <response_class>
"""
from __future__ import print_function
import trollius as asyncio
from trollius import From, Return
import email.parser
import email.message
import io
import os
import socket
import collections
import sys
from urllib.parse import urlsplit

__all__ = ["HTTPResponse", "HTTPConnection",
           "HTTPException", "NotConnected", "UnknownProtocol",
           "UnknownTransferEncoding", "UnimplementedFileMode",
           "IncompleteRead", "InvalidURL", "ImproperConnectionState",
           "CannotSendRequest", "CannotSendHeader", "ResponseNotReady",
           "BadStatusLine", "error", "responses"]

HTTP_PORT = 80
HTTPS_PORT = 443

_UNKNOWN = 'UNKNOWN'

# connection states
_CS_IDLE = 'Idle'
_CS_REQ_STARTED = 'Request-started'
_CS_REQ_SENT = 'Request-sent'

# status codes
# informational
CONTINUE = 100
SWITCHING_PROTOCOLS = 101
PROCESSING = 102

# successful
OK = 200
CREATED = 201
ACCEPTED = 202
NON_AUTHORITATIVE_INFORMATION = 203
NO_CONTENT = 204
RESET_CONTENT = 205
PARTIAL_CONTENT = 206
MULTI_STATUS = 207
IM_USED = 226

# redirection
MULTIPLE_CHOICES = 300
MOVED_PERMANENTLY = 301
FOUND = 302
SEE_OTHER = 303
NOT_MODIFIED = 304
USE_PROXY = 305
TEMPORARY_REDIRECT = 307

# client error
BAD_REQUEST = 400
UNAUTHORIZED = 401
PAYMENT_REQUIRED = 402
FORBIDDEN = 403
NOT_FOUND = 404
METHOD_NOT_ALLOWED = 405
NOT_ACCEPTABLE = 406
PROXY_AUTHENTICATION_REQUIRED = 407
REQUEST_TIMEOUT = 408
CONFLICT = 409
GONE = 410
LENGTH_REQUIRED = 411
PRECONDITION_FAILED = 412
REQUEST_ENTITY_TOO_LARGE = 413
REQUEST_URI_TOO_LONG = 414
UNSUPPORTED_MEDIA_TYPE = 415
REQUESTED_RANGE_NOT_SATISFIABLE = 416
EXPECTATION_FAILED = 417
UNPROCESSABLE_ENTITY = 422
LOCKED = 423
FAILED_DEPENDENCY = 424
UPGRADE_REQUIRED = 426
PRECONDITION_REQUIRED = 428
TOO_MANY_REQUESTS = 429
REQUEST_HEADER_FIELDS_TOO_LARGE = 431

# server error
INTERNAL_SERVER_ERROR = 500
NOT_IMPLEMENTED = 501
BAD_GATEWAY = 502
SERVICE_UNAVAILABLE = 503
GATEWAY_TIMEOUT = 504
HTTP_VERSION_NOT_SUPPORTED = 505
INSUFFICIENT_STORAGE = 507
NOT_EXTENDED = 510
NETWORK_AUTHENTICATION_REQUIRED = 511

# Mapping status codes to official W3C names
responses = {
    100: 'Continue',
    101: 'Switching Protocols',

    200: 'OK',
    201: 'Created',
    202: 'Accepted',
    203: 'Non-Authoritative Information',
    204: 'No Content',
    205: 'Reset Content',
    206: 'Partial Content',

    300: 'Multiple Choices',
    301: 'Moved Permanently',
    302: 'Found',
    303: 'See Other',
    304: 'Not Modified',
    305: 'Use Proxy',
    306: '(Unused)',
    307: 'Temporary Redirect',

    400: 'Bad Request',
    401: 'Unauthorized',
    402: 'Payment Required',
    403: 'Forbidden',
    404: 'Not Found',
    405: 'Method Not Allowed',
    406: 'Not Acceptable',
    407: 'Proxy Authentication Required',
    408: 'Request Timeout',
    409: 'Conflict',
    410: 'Gone',
    411: 'Length Required',
    412: 'Precondition Failed',
    413: 'Request Entity Too Large',
    414: 'Request-URI Too Long',
    415: 'Unsupported Media Type',
    416: 'Requested Range Not Satisfiable',
    417: 'Expectation Failed',
    428: 'Precondition Required',
    429: 'Too Many Requests',
    431: 'Request Header Fields Too Large',

    500: 'Internal Server Error',
    501: 'Not Implemented',
    502: 'Bad Gateway',
    503: 'Service Unavailable',
    504: 'Gateway Timeout',
    505: 'HTTP Version Not Supported',
    511: 'Network Authentication Required',
}

# maximal amount of data to read at one time in _safe_read
MAXAMOUNT = 1048576

# maximal line length when calling readline().
_MAXLINE = 65536
_MAXHEADERS = 100


class NotSocket():
    
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

        self.write = self.writer.write
        self.read = self.reader.read
        self.readexactly = self.reader.readexactly
        self.readline = self.reader.readline

        self.transportRefCt = 1

    @asyncio.coroutine
    def writeAndDrain(self, data):
        self.writer.write(data)
        yield From (self.writer.drain())
        
    def close(self):
        self.transportRefCt -= 1
        if self.transportRefCt < 1:
            self.writer.transport.close()
            self.reader = self.writer = None
        
    def socket(self):
        return self.writer.transport.get_extra_info('socket')


class HTTPMessage(email.message.Message):
    # XXX The only usage of this method is in
    # http.server.CGIHTTPRequestHandler.  Maybe move the code there so
    # that it doesn't need to be part of the public API.  The API has
    # never been defined so this could cause backwards compatibility
    # issues.

    def getallmatchingheaders(self, name):
        """Find all header lines matching a given header name.

        Look through the list of headers and find all lines matching a given
        header name (and their continuation lines).  A list of the lines is
        returned, without interpretation.  If the header does not occur, an
        empty list is returned.  If the header occurs multiple times, all
        occurrences are returned.  Case is not important in the header name.

        """
        name = name.lower() + ':'
        n = len(name)
        lst = []
        hit = 0
        for line in self.keys():
            if line[:n].lower() == name:
                hit = 1
            elif not line[:1].isspace():
                hit = 0
            if hit:
                lst.append(line)
        return lst

@asyncio.coroutine
def parse_headers(fp, _class=HTTPMessage, timeout=5.0):
    """Parses only RFC2822 headers from a file pointer.

    email Parser wants to see strings rather than bytes.
    But a TextIOWrapper around self.rfile would buffer too many bytes
    from the stream, bytes which we later need to read as bytes.
    So we read the correct bytes here, as bytes, for email Parser
    to parse.

    """
    headers = []
    while True:
        try:
            try:
                line = yield From (asyncio.wait_for(fp.readline(), timeout))
                #line = fp._readline_with_timeout(timeout)
            except ValueError as e:
                if 'is too long' in e.args[0]:
                    raise LineTooLong("header line")
                else:
                    raise
            headers.append(line)
            if len(headers) > _MAXHEADERS:
                raise HTTPException("got more than %d headers" % _MAXHEADERS)
            if line in (b'\r\n', b'\n', b''):
                break
        except ValueError as e:
            if 'too long' in e.args[0]:
                raise LineTooLong('header line')
            else:
                raise
    hstring = b''.join(headers).decode('iso-8859-1')
    raise Return (email.parser.Parser(_class=_class).parsestr(hstring))


class HTTPResponse(io.IOBase): #io.BufferedIOBase):

    # See RFC 2616 sec 19.6 and RFC 1945 sec 6 for details.

    # The bytes from the socket object are iso-8859-1 strings.
    # See RFC 2616 sec 2.2 which notes an exception for MIME-encoded
    # text following RFC 2047.  The basic status line parsing only
    # accepts iso-8859-1.

    def __init__(self, notsock, debuglevel=0, method=None, url=None):
        # If the response includes a content-length header, we need to
        # make sure that the client doesn't read more than the
        # specified number of bytes.  If it does, it will block until
        # the server times out and closes the connection.  This will
        # happen if a self.fp.read() is done (without a size) whether
        # self.fp is buffered or not.  So, no self.fp.read() by
        # clients unless they know what they are doing.
        self.fp = notsock
        notsock.transportRefCt += 1
        self.debuglevel = debuglevel
        self.TIMEOUT = 5.0
        self._method = method

        # The HTTPResponse object is returned via urllib.  The clients
        # of http and urllib expect different attributes for the
        # headers.  headers is used here and supports urllib.  msg is
        # provided as a backwards compatibility layer for http
        # clients.

        self.headers = self.msg = None

        # from the Status-Line of the response
        self.version = _UNKNOWN # HTTP-Version
        self.status = _UNKNOWN  # Status-Code
        self.reason = _UNKNOWN  # Reason-Phrase

        self.chunked = _UNKNOWN         # is "chunked" being used?
        self.chunk_left = _UNKNOWN      # bytes left to read in current chunk
        self.length = _UNKNOWN          # number of bytes left in response
        self.will_close = _UNKNOWN      # conn will close at end of response

    @asyncio.coroutine
    def init(self):
        yield None
        # assert self.fp is None
        # reader, writer = yield From (asyncio.open_connection(sock=self.soCk))
        # #writer.write_eof() # breaks SSL to close either channel
        # reader._limit = _MAXLINE
        # self.fp = reader
        #self.soCk = None

    @asyncio.coroutine
    def _read_status(self):
        try:
            line = yield From (asyncio.wait_for(self.fp.readline(), self.TIMEOUT))
        except ValueError as e:
            if 'is too long' in e.args[0]:
                raise LineTooLong('status line')
            else:
                raise
        line = str(line, "iso-8859-1")
        if self.debuglevel > 0:
            print("reply:", repr(line))
        if not line:
            # Presumably, the server closed the connection before
            # sending a valid response.
            raise BadStatusLine(line)
        try:
            version, status, reason = line.split(None, 2)
        except ValueError:
            try:
                version, status = line.split(None, 1)
                reason = ""
            except ValueError:
                # empty version will cause next test to fail.
                version = ""
        if not version.startswith("HTTP/"):
            self._close_conn()
            raise BadStatusLine(line)

        # The status code is a three-digit number
        try:
            status = int(status)
            if status < 100 or status > 999:
                raise BadStatusLine(line)
        except ValueError:
            raise BadStatusLine(line)
        raise Return (version, status, reason)

    @asyncio.coroutine
    def begin(self):
        if self.headers is not None:
            # we've already started reading the response
            return

        # read until we get a non-100 response
        while True:
            version, status, reason = yield From (self._read_status())
            if status != CONTINUE:
                break
            # skip the header from the 100 response
            while True:
                try:
                    skip = yield From (asyncio.wait_for(self.fp.readline(), self.TIMEOUT))
                except ValueError as e:
                    if 'is too long' in e.args[0]:
                        raise LineTooLong('header line')
                    else:
                        raise
                skip = skip.strip()
                if not skip:
                    break
                if self.debuglevel > 0:
                    print("header:", skip)

        self.code = self.status = status
        self.reason = reason.strip()
        if version in ("HTTP/1.0", "HTTP/0.9"):
            # Some servers might still return "0.9", treat it as 1.0 anyway
            self.version = 10
        elif version.startswith("HTTP/1."):
            self.version = 11   # use HTTP/1.1 code for HTTP/1.x where x>=1
        else:
            raise UnknownProtocol(version)

        self.headers = self.msg = yield From (parse_headers(self.fp))

        if self.debuglevel > 0:
            for hdr in self.headers:
                print("header:", hdr, end=" ")

        # are we using the chunked-style of transfer encoding?
        tr_enc = self.headers.get("transfer-encoding")
        if tr_enc and tr_enc.lower() == "chunked":
            self.chunked = True
            self.chunk_left = None
        else:
            self.chunked = False

        # will the connection close at the end of the response?
        self.will_close = self._check_close()

        # do we have a Content-Length?
        # NOTE: RFC 2616, S4.4, #3 says we ignore this if tr_enc is "chunked"
        self.length = None
        length = self.headers.get("content-length")

         # are we using the chunked-style of transfer encoding?
        tr_enc = self.headers.get("transfer-encoding")
        if length and not self.chunked:
            try:
                self.length = int(length)
            except ValueError:
                self.length = None
            else:
                if self.length < 0:  # ignore nonsensical negative lengths
                    self.length = None
        else:
            self.length = None

        # does the body have a fixed length? (of zero)
        if (status == NO_CONTENT or status == NOT_MODIFIED or
            100 <= status < 200 or      # 1xx codes
            self._method == "HEAD"):
            self.length = 0

        # if the connection remains open, and we aren't using chunked, and
        # a content-length was not provided, then assume that the connection
        # WILL close.
        if (not self.will_close and
            not self.chunked and
            self.length is None):
            self.will_close = True

    def _check_close(self):
        conn = self.headers.get("connection")
        if self.version == 11:
            # An HTTP/1.1 proxy is assumed to stay open unless
            # explicitly closed.
            conn = self.headers.get("connection")
            if conn and "close" in conn.lower():
                return True
            return False

        # Some HTTP/1.0 implementations have support for persistent
        # connections, using rules different than HTTP/1.1.

        # For older HTTP, Keep-Alive indicates persistent connection.
        if self.headers.get("keep-alive"):
            return False

        # At least Akamai returns a "Connection: Keep-Alive" header,
        # which was supposed to be sent by the client.
        if conn and "keep-alive" in conn.lower():
            return False

        # Proxy-Connection is a netscape hack.
        pconn = self.headers.get("proxy-connection")
        if pconn and "keep-alive" in pconn.lower():
            return False

        # otherwise, assume it will close
        return True

    def _close_conn(self):
        fp = self.fp
        self.fp = None
        fp.close()

    def close(self):
        super(HTTPResponse, self).close() # set "closed" flag
        if self.fp:
            self._close_conn()

    # These implementations are for the benefit of io.BufferedReader.

    # XXX This class should probably be revised to act more like
    # the "raw stream" that BufferedReader expects.

    def flush(self):
        pass

    def readable(self):
        return True

    # End of "raw stream" methods

    def isclosed(self):
        """True if the connection is closed."""
        # NOTE: it is possible that we will not ever call self.close(). This
        #       case occurs when will_close is TRUE, length is None, and we
        #       read up to the last byte, but NOT past it.
        #
        # IMPLIES: if will_close is FALSE, then self.close() will ALWAYS be
        #          called, meaning self.isclosed() is meaningful.
        return self.fp is None

    @asyncio.coroutine
    def read(self, amt=None):
        if self.fp is None:
            raise Return (b"")

        if self._method == "HEAD":
            self._close_conn()
            raise Return (b"")

        if amt is not None:
            # Amount is given, implement using readinto
            b = bytearray(amt)
            n = yield From (self.readinto(b))
            raise Return (memoryview(b)[:n].tobytes())
        else:
            # Amount is not given (unbounded read) so we must check self.length
            # and self.chunked

            if self.chunked:
                raise Return ((yield From (self._readall_chunked())))

            if self.length is None:
                #s = yield From (asyncio.wait_for(self.fp.read(), self.TIMEOUT))
                s = yield From (self._read_with_timeout(None, self.TIMEOUT))
            else:
                try:
                    s = yield From (self._safe_read(self.length))
                except IncompleteRead:
                    self._close_conn()
                    raise
                self.length = 0

            self._close_conn()        # we read everything
            raise Return (s)

    @asyncio.coroutine
    def readinto(self, b):
        if self.fp is None:
            raise Return (0)

        if self._method == "HEAD":
            self._close_conn()
            raise Return (0)

        if self.chunked:
            raise Return ((yield From (self._readinto_chunked(b))))

        if self.length is not None:
            if len(b) > self.length:
                # clip the read to the "end of response"
                b = memoryview(b)[0:self.length]

        # we do not use _safe_read() here because this may be a .will_close
        # connection, and the user is reading more bytes than will be provided
        # (for example, reading in 1k chunks)
        bLen = len(b)
        #n = yield From (self.fp.readinto(b))
        _b = yield From (self.fp.read(bLen))
        n = len(_b)
        b[0:n] = _b
        if not n and b:
            # Ideally, we would raise IncompleteRead if the content-length
            # wasn't satisfied, but it might break compatibility.
            self._close_conn()
        elif self.length is not None:
            self.length -= n
            if not self.length:
                self._close_conn()
        raise Return (n)

    @asyncio.coroutine
    def _read_next_chunk_size(self):
        # Read the next chunk size from the file
        try:
            #line = yield From (asyncio.wait_for(self.fp.readline(), self.TIMEOUT))
            line = yield From (self._readline_with_timeout(self.TIMEOUT))
        except ValueError as e:
            if 'ine is too long' in e.args[0]:
                raise LineTooLong('chunk size')
            else:
                raise
        i = line.find(b";")
        if i >= 0:
            line = line[:i] # strip chunk-extensions
        try:
            raise Return (int(line, 16))
        except ValueError:
            # close the connection as protocol synchronisation is
            # probably lost
            self._close_conn()
            raise

    @asyncio.coroutine
    def _read_and_discard_trailer(self):
        # read and discard trailer up to the CRLF terminator
        ### note: we shouldn't have any trailers!
        while True:
            try:
                #line = yield From (asyncio.wait_for(self.fp.readline(), self.TIMEOUT))
                line = yield From (self._readline_with_timeout(self.TIMEOUT))
            except ValueError as e:
                if 'is too long' in e.args[0]:
                    raise LineTooLong('trailer line')
                else:
                    raise
            if not line:
                # a vanishingly small number of sites EOF without
                # sending the trailer
                break
            if line in (b'\r\n', b'\n', b''):
                break

    @asyncio.coroutine
    def _get_chunk_left(self):
        # raise Return (self.chunk_left, reading a new chunk if necessary.)
        # chunk_left == 0: at the end of the current chunk, need to close it
        # chunk_left == None: No current chunk, should read next.
        # This function returns non-zero or None if the last chunk has
        # been read.
        chunk_left = self.chunk_left
        if not chunk_left: # Can be 0 or None
            if chunk_left is not None:
                # We are at the end of chunk. dicard chunk end
                yield From (self._safe_read(2))  # toss the CRLF at the end of the chunk
            try:
                chunk_left = yield From (self._read_next_chunk_size())
            except ValueError:
                raise IncompleteRead(b'')
            if chunk_left == 0:
                # last chunk: 1*("0") [ chunk-extension ] CRLF
                yield From (self._read_and_discard_trailer())
                # we read everything; close the "file"
                self._close_conn()
                chunk_left = None
            self.chunk_left = chunk_left
        raise Return (chunk_left)

    @asyncio.coroutine
    def _readall_chunked(self):
        assert self.chunked != _UNKNOWN
        value = []
        try:
            while True:
                chunk_left = yield From (self._get_chunk_left())
                if chunk_left is None:
                    break
                _v = yield From (self._safe_read(chunk_left))
                value.append(_v)
                self.chunk_left = 0
            raise Return (b''.join(value))
        except IncompleteRead:
            raise IncompleteRead(b''.join(value))

    @asyncio.coroutine
    def _readinto_chunked(self, b):
        assert self.chunked != _UNKNOWN
        total_bytes = 0
        mvb = memoryview(b)
        try:
            while True:
                chunk_left = yield From (self._get_chunk_left())
                if chunk_left is None:
                    raise Return (total_bytes)

                if len(mvb) <= chunk_left:
                    n = yield From (self._safe_readinto(mvb))
                    self.chunk_left = chunk_left - n
                    raise Return (total_bytes + n)

                temp_mvb = mvb[:chunk_left]
                n = yield From (self._safe_readinto(temp_mvb))
                mvb = mvb[n:]
                total_bytes += n
                self.chunk_left = 0

        except IncompleteRead:
            raise IncompleteRead(bytes(b[0:total_bytes]))

    @asyncio.coroutine
    def _read_with_timeout(self, amt, timeout=None):
        """in case connection does not see close, and timeout ends read."""
        timeout = timeout or self.TIMEOUT
        amtLim = min([r for r in [amt, MAXAMOUNT] if r])
        try:
            d = yield From (asyncio.wait_for(self.fp.read(amtLim), timeout))
        except asyncio.TimeoutError as e:
            ln = len(self.fp._buffer)
            d = yield From (asyncio.wait_for(self.fp.read(ln), timeout))
        raise Return (d)

    @asyncio.coroutine
    def _readline_with_timeout(self, timeout=None):
        """in case connection does not see close, and timeout ends read."""
        timeout = timeout or self.TIMEOUT
        try:
            d = yield From (asyncio.wait_for(self.fp.readline(), timeout))
        except asyncio.TimeoutError as e:
            ln = len(self.fp._buffer)
            #d = yield From (asyncio.wait_for(self.fp.read(ln), timeout))
            d = yield From (self._read_with_timeout(ln, timeout))
        raise Return (d)

    @asyncio.coroutine
    def _safe_read(self, amt):
        """Read the number of bytes requested, compensating for partial reads.

        Normally, we have a blocking socket, but a read() can be interrupted
        by a signal (resulting in a partial read).

        Note that we cannot distinguish between EOF and an interrupt when zero
        bytes have been read. IncompleteRead() will be raised in this
        situation.

        This function should be used when <amt> bytes "should" be present for
        reading. If the bytes are truly not available (due to EOF), then the
        IncompleteRead exception can be used to detect the problem.
        """
        s = []
        while amt > 0:
            chunk = yield From (self._read_with_timeout(amt))
            if not chunk:
                raise IncompleteRead(b''.join(s), amt)
            s.append(chunk)
            amt -= len(chunk)
        raise Return (b"".join(s))

    @asyncio.coroutine
    def _safe_readinto(self, b):
        """Same as _safe_read, but for reading into a buffer."""
        total_bytes = 0
        mvb = memoryview(b)
        while total_bytes < len(b):
            if MAXAMOUNT < len(mvb):
                temp_mvb = mvb[0:MAXAMOUNT]
            else:
                temp_mvb = mvb[:]
            bLen = len(temp_mvb)
            #_b = yield From (asyncio.wait_for(self.fp.read(bLen), self.TIMEOUT))
            _b = yield From (self._read_with_timeout(bLen))
            n = len(_b)
            temp_mvb[0:n] = _b
#            n = yield From (self.fp.readinto(temp_mvb))
            if not n:
                raise IncompleteRead(bytes(mvb[0:total_bytes]), len(b))
            mvb = mvb[n:]
            total_bytes += n
        raise Return (total_bytes)

    # @asyncio.coroutine
    # def read1(self, n=-1):
    #     """Read with at most one underlying system call.  If at least one
    #     byte is buffered, return that instead.
    #     """
    #     if self.fp is None or self._method == "HEAD":
    #         raise Return (b"")
    #     if self.chunked:
    #         raise Return (self._read1_chunked(n))
    #     try:
    #         result = yield From (self.fp.read1(n))
    #     except ValueError:
    #         if n >= 0:
    #             raise
    #         # some implementations, like BufferedReader, don't support -1
    #         # Read an arbitrarily selected largeish chunk.
    #         result = yield From (self.fp.read1(16*1024))
    #     if not result and n:
    #         self._close_conn()
    #     raise Return (result)

    # def peek(self, n=-1):
    #     # Having this enables IOBase.readline() to read more than one
    #     # byte at a time
    #     if self.fp is None or self._method == "HEAD":
    #         raise Return (b"")
    #     if self.chunked:
    #         raise Return (self._peek_chunked(n))
    #     raise Return (self.fp.peek(n))

    @asyncio.coroutine
    def _chunked_readline(self):
        assert self.chunked != _UNKNOWN
        s = []
        total_bytes = 0
        while True:
            chunk_left = yield From (self._get_chunk_left())
            if chunk_left is None:
                raise Return (b''.join(s))

            data = yield From (self._safe_read(1))
            s.append(data)
            total_bytes += len(data)
            if data == b'\n':
                raise Return (b''.join(s))
            if total_bytes > _MAXLINE:
                raise LineTooLong('readline')


    @asyncio.coroutine
    def readline(self):
        if self.fp is None or self._method == "HEAD":
            raise Return (b"")
        if self.chunked:
            # Fallback to IOBase readline which uses peek() and read()
            ret = yield From (self._chunked_readline())
        #result = yield From (asyncio.wait_for(self.fp.readline(), self.TIMEOUT))
        result = yield From (self._readline_with_timeout())
        if not result:# and limit:
            self._close_conn()
        raise Return (result)

    @asyncio.coroutine
    def readlines(self, ct):
        if self.fp is None or self._method == "HEAD":
            raise Return (b"")
        lines = []
        while len(lines) < ct:
            line = yield From (self.readline())
            lines.append(line)
        raise Return (lines)

    # @asyncio.coroutine
    # def _read1_chunked(self, n):
    #     # Strictly speaking, _get_chunk_left() may cause more than one read,
    #     # but that is ok, since that is to satisfy the chunked protocol.
    #     chunk_left = self._get_chunk_left()
    #     if chunk_left is None or n == 0:
    #         raise Return (b'')
    #     if not (0 <= n <= chunk_left):
    #         n = chunk_left # if n is negative or larger than chunk_left
    #     read = yield From (self.fp.read1(n))
    #     self.chunk_left -= len(read)
    #     if not read:
    #         raise IncompleteRead(b"")
    #     raise Return (read)

    # @asyncio.coroutine
    # def _peek_chunked(self, n):
    #     # Strictly speaking, _get_chunk_left() may cause more than one read,
    #     # but that is ok, since that is to satisfy the chunked protocol.
    #     try:
    #         chunk_left = self._get_chunk_left()
    #     except IncompleteRead:
    #         raise Return (b'' # peek doesn't worry about protocol)
    #     if chunk_left is None:
    #         raise Return (b'' # eof)
    #     # peek is allowed to raise Return (more than requested.  Just request the)
    #     # entire chunk, and truncate what we get.
    #     r = yield From (self.fp.peek(chunk_left)[:chunk_left])
    #     raise Return (r)

    def fileno(self):
        return None #self.fp.fileno()

    def getheader(self, name, default=None):
        if self.headers is None:
            raise ResponseNotReady()
        headers = self.headers.get_all(name) or default
        if isinstance(headers, str) or not hasattr(headers, '__iter__'):
            return headers
        else:
            return ', '.join(headers)

    def getheaders(self):
        """Return list of (header, value) tuples."""
        if self.headers is None:
            raise ResponseNotReady()
        return list(self.headers.items())

    # We override IOBase.__iter__ so that it doesn't check for closed-ness

    def __iter__(self):
        return self

    # For compatibility with old-style urllib responses.

    def info(self):
        return self.headers

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status

@asyncio.coroutine
def create_connection(address, timeout=None, source_address=None, loop=None,
                      ssl=None, server_hostname=None):

    #def _tmp_protocol():
    #     raise Return (asyncio.Protocol())

    if loop is None:
        loop = asyncio.get_event_loop()
    host, port = address
    #(transport, protocol) = yield From (loop.create_connection(asyncio.StreamReader, host, port, ssl=ssl,
    #                                                          local_addr=source_address)

    reader, writer = yield From (asyncio.open_connection(host, port, ssl=ssl, limit=_MAXLINE,
                                                     local_addr=source_address))

    #sock = transport.get_extra_info('socket')
    #raise Return (sock)
    raise Return (NotSocket(reader, writer))


class HTTPConnection:

    _http_vsn = 11
    _http_vsn_str = 'HTTP/1.1'

    response_class = HTTPResponse
    default_port = HTTP_PORT
    auto_open = 1
    debuglevel = 0
    # TCP Maximum Segment Size (MSS) is determined by the TCP stack on
    # a per-connection basis.  There is no simple and efficient
    # platform independent mechanism for determining the MSS, so
    # instead a reasonable estimate is chosen.  The getsockopt()
    # interface using the TCP_MAXSEG parameter may be a suitable
    # approach on some operating systems. A value of 16KiB is chosen
    # as a reasonable estimate of the maximum MSS.
    mss = 16384

    loop = asyncio.get_event_loop()

    def __init__(self, host, port=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        if type(timeout) == type(object()):
            timeout = 30.0
        self.TIMEOUT = timeout
        self.source_address = source_address
        self.notSock = None
        self._buffer = []
        self.__response = None
        self.__state = _CS_IDLE
        self._method = None
        self._tunnel_host = None
        self._tunnel_port = None
        self._tunnel_headers = {}

        (self.host, self.port) = self._get_hostport(host, port)

        # This is stored as an instance variable to allow unit
        # tests to replace it with a suitable mockup
        self._create_connection = create_connection

    def set_tunnel(self, host, port=None, headers=None):
        """Set up host and port for HTTP CONNECT tunnelling.

        In a connection that uses HTTP CONNECT tunneling, the host passed to the
        constructor is used as a proxy server that relays all communication to
        the endpoint passed to `set_tunnel`. This done by sending an HTTP
        CONNECT request to the proxy server when the connection is established.

        This method must be called before the HTML connection has been
        established.

        The headers argument should be a mapping of extra HTTP headers to send
        with the CONNECT request.
        """

        if self.notSock:
            raise RuntimeError("Can't set up tunnel for established connection")

        self._tunnel_host = host
        self._tunnel_port = port
        if headers:
            self._tunnel_headers = headers
        else:
            self._tunnel_headers.clear()

    def _get_hostport(self, host, port):
        if port is None:
            i = host.rfind(':')
            j = host.rfind(']')         # ipv6 addresses have [...]
            if i > j:
                try:
                    port = int(host[i+1:])
                except ValueError:
                    if host[i+1:] == "": # http://foo.com:/ == http://foo.com/
                        port = self.default_port
                    else:
                        raise InvalidURL("nonnumeric port: '%s'" % host[i+1:])
                host = host[:i]
            else:
                port = self.default_port
            if host and host[0] == '[' and host[-1] == ']':
                host = host[1:-1]

        return (host, port)

    def set_debuglevel(self, level):
        self.debuglevel = level

    @asyncio.coroutine
    def _tunnel(self):
        (host, port) = self._get_hostport(self._tunnel_host,
                                          self._tunnel_port)
        connect_str = "CONNECT %s:%d HTTP/1.0\r\n" % (host, port)
        connect_bytes = connect_str.encode("ascii")
        yield From (self.send(connect_bytes))
        for header, value in self._tunnel_headers.items():
            header_str = "%s: %s\r\n" % (header, value)
            header_bytes = header_str.encode("latin-1")
            yield From (self.send(header_bytes))
        yield From (self.send(b'\r\n'))

        response = self.response_class(self.notSock, method=self._method)
        #yield From (response.init())
        (version, code, message) = yield From (response._read_status())

        if code != 200:
            self.close()
            raise OSError("Tunnel connection failed: %d %s" % (code, message.strip()))
        while True:
            try:
                line = yield From (asyncio.wait_for(response.fp.readline(), self.TIMEOUT))
            except ValueError as e:
                if 'is too long' in e.args[0]:
                    raise LineTooLong('header line')
                else:
                    raise
            if not line:
                # for sites which EOF without sending a trailer
                break
            if line in (b'\r\n', b'\n', b''):
                break

    @asyncio.coroutine
    def connect(self):
        """Connect to the host and port specified in __init__."""

        #t, p = yield From (self._create_connection((self.host, self.port), self.TIMEOUT, self.source_address))
        s = yield From (self._create_connection((self.host, self.port), self.TIMEOUT, self.source_address))

        self.notSock = s

        if self._tunnel_host:
            yield From (self._tunnel())

    def close(self):
        """Close the connection to the HTTP server."""

        if self.notSock:
            self.notSock.close()
            self.notSock = None
        if self.__response:
            self.__response.close()
            self.__response = None
        self.__state = _CS_IDLE

    @asyncio.coroutine
    def send(self, data):
        """Send `data' to the server.
        ``data`` can be a string object, a bytes object, an array object, a
        file-like object that supports a .read() method, or an iterable object.
        """

        if self.notSock is None:
            if self.auto_open:
                yield From (self.connect())
            else:
                raise NotConnected()

        if self.debuglevel > 0:
            print("send:", repr(data))
        blocksize = 8192
        if hasattr(data, "read") :
            if self.debuglevel > 0:
                print("sendIng a read()able")
            encode = False
            try:
                mode = data.mode
            except AttributeError:
                # io.BytesIO and other file-like objects don't have a `mode`
                # attribute.
                pass
            else:
                if "b" not in mode:
                    encode = True
                    if self.debuglevel > 0:
                        print("encoding file using iso-8859-1")
            while 1:
                datablock = data.read(blocksize)
                if not datablock:
                    break
                if encode:
                    datablock = datablock.encode("iso-8859-1")
                # yield From (self.loop.sock_sendall(self.soCk, datablock))
                yield From (self.notSock.writeAndDrain(datablock))
            return
        try:
            # yield From (self.loop.sock_sendall(self.soCk, data))
            yield From (self.notSock.writeAndDrain(data))
        except TypeError:
            if isinstance(data, collections.Iterable):
                 for d in data:
                     #yield From (self.loop.sock_sendall(self.soCk, d))
                     #d = chr(d).encode('ascii')
                     yield From (self.notSock.writeAndDrain(d))
            else:
                 raise TypeError("data should be a bytes-like object, got %r" % type(data))


    def _output(self, s):
        """Add a line of output to the current request buffer.

        Assumes that the line does *not* end with \\r\\n.
        """
        self._buffer.append(s)

    @asyncio.coroutine
    def _send_output(self, message_body=None):
        """Send the currently buffered request and clear the buffer.

        Appends an extra \\r\\n to the buffer.
        A message_body may be specified, to be appended to the request.
        """
        self._buffer.extend((b"", b""))
        msg = b"\r\n".join(self._buffer)
        del self._buffer[:]
        # If msg and message_body are sent in a single send() call,
        # it will avoid performance problems caused by the interaction
        # between delayed ack and the Nagle algorithm. However,
        # there is no performance gain if the message is larger
        # than MSS (and there is a memory penalty for the message
        # copy).
        if isinstance(message_body, bytes) and len(message_body) < self.mss:
            msg += message_body
            message_body = None
        yield From (self.send(msg))
        if message_body is not None:
            # message_body was not a string (i.e. it is a file), and
            # we must run the risk of Nagle.
            yield From (self.send(message_body))



    def putrequest(self, method, url, skip_host=0, skip_accept_encoding=0):
        """Send a request to the server.

        `method' specifies an HTTP request method, e.g. 'GET'.
        `url' specifies the object being requested, e.g. '/index.html'.
        `skip_host' if True does not add automatically a 'Host:' header
        `skip_accept_encoding' if True does not add automatically an
           'Accept-Encoding:' header
        """

        # if a prior response has been completed, then forget about it.
        if self.__response and self.__response.isclosed():
            self.__response = None


        # in certain cases, we cannot issue another request on this connection.
        # this occurs when:
        #   1) we are in the process of sending a request.   (_CS_REQ_STARTED)
        #   2) a response to a previous request has signalled that it is going
        #      to close the connection upon completion.
        #   3) the headers for the previous response have not been read, thus
        #      we cannot determine whether point (2) is true.   (_CS_REQ_SENT)
        #
        # if there is no prior response, then we can request at will.
        #
        # if point (2) is true, then we will have passed the socket to the
        # response (effectively meaning, "there is no prior response"), and
        # will open a new one when a new request is made.
        #
        # Note: if a prior response exists, then we *can* start a new request.
        #       We are not allowed to begin fetching the response to this new
        #       request, however, until that prior response is complete.
        #
        if self.__state == _CS_IDLE:
            self.__state = _CS_REQ_STARTED
        else:
            raise CannotSendRequest(self.__state)

        # Save the method we use, we need it later in the response phase
        self._method = method
        if not url:
            url = '/'
        request = '%s %s %s' % (method, url, self._http_vsn_str)

        # Non-ASCII characters should have been eliminated earlier
        self._output(request.encode('ascii'))

        if self._http_vsn == 11:
            # Issue some standard headers for better HTTP/1.1 compliance

            if not skip_host:
                # this header is issued *only* for HTTP/1.1
                # connections. more specifically, this means it is
                # only issued when the client uses the new
                # HTTPConnection() class. backwards-compat clients
                # will be using HTTP/1.0 and those clients may be
                # issuing this header themselves. we should NOT issue
                # it twice; some web servers (such as Apache) barf
                # when they see two Host: headers

                # If we need a non-standard port,include it in the
                # header.  If the request is going through a proxy,
                # but the host of the actual URL, not the host of the
                # proxy.

                netloc = ''
                if url.startswith('http'):
                    nil, netloc, nil, nil, nil = urlsplit(url)

                if netloc:
                    try:
                        netloc_enc = netloc.encode("ascii")
                    except UnicodeEncodeError:
                        netloc_enc = netloc.encode("idna")
                    self.putheader('Host', netloc_enc)
                else:
                    if self._tunnel_host:
                        host = self._tunnel_host
                        port = self._tunnel_port
                    else:
                        host = self.host
                        port = self.port

                    try:
                        host_enc = host.encode("ascii")
                    except UnicodeEncodeError:
                        host_enc = host.encode("idna")

                    # As per RFC 273, IPv6 address should be wrapped with []
                    # when used as Host header

                    if host.find(':') >= 0:
                        host_enc = b'[' + host_enc + b']'

                    if port == self.default_port:
                        self.putheader('Host', host_enc)
                    else:
                        host_enc = host_enc.decode("ascii")
                        self.putheader('Host', "%s:%s" % (host_enc, port))

            # note: we are assuming that clients will not attempt to set these
            #       headers since *this* library must deal with the
            #       consequences. this also means that when the supporting
            #       libraries are updated to recognize other forms, then this
            #       code should be changed (removed or updated).

            # we only want a Content-Encoding of "identity" since we don't
            # support encodings such as x-gzip or x-deflate.
            if not skip_accept_encoding:
                self.putheader('Accept-Encoding', 'identity')

            # we can accept "chunked" Transfer-Encodings, but no others
            # NOTE: no TE header implies *only* "chunked"
            #self.putheader('TE', 'chunked')

            # if TE is supplied in the header, then it must appear in a
            # Connection header.
            #self.putheader('Connection', 'TE')

        else:
            # For HTTP/1.0, the server will assume "not chunked"
            pass

    def putheader(self, header, *values):
        """Send a request header line to the server.

        For example: h.putheader('Accept', 'text/html')
        """
        if self.__state != _CS_REQ_STARTED:
            raise CannotSendHeader()

        if hasattr(header, 'encode'):
            header = header.encode('ascii')
        values = list(values)
        for i, one_value in enumerate(values):
            if hasattr(one_value, 'encode'):
                values[i] = one_value.encode('latin-1')
            elif isinstance(one_value, int):
                values[i] = str(one_value).encode('ascii')
        value = b'\r\n\t'.join(values)
        header = header + b': ' + value
        self._output(header)

    @asyncio.coroutine
    def endheaders(self, message_body=None):
        """Indicate that the last header line has been sent to the server.

        This method sends the request to the server.  The optional message_body
        argument can be used to pass a message body associated with the
        request.  The message body will be sent in the same packet as the
        message headers if it is a string, otherwise it is sent as a separate
        packet.
        """
        if self.__state == _CS_REQ_STARTED:
            self.__state = _CS_REQ_SENT
        else:
            raise CannotSendHeader()
        yield From (self._send_output(message_body))


    @asyncio.coroutine
    def request(self, method, url, body=None, headers={}):
        """Send a complete request to the server."""
        yield From (self._send_request(method, url, body, headers))

    def _set_content_length(self, body):
        # Set the content-length based on the body.
        thelen = None
        try:
            thelen = str(len(body))
        except TypeError as te:
            # If this is a file-like object, try to
            # fstat its file descriptor
            try:
                thelen = str(os.fstat(body.fileno()).st_size)
            except (AttributeError, OSError):
                # Don't send a length if this failed
                if self.debuglevel > 0: print("Cannot stat!!")

        if thelen is not None:
            self.putheader('Content-Length', thelen)

    @asyncio.coroutine
    def _send_request(self, method, url, body, headers):
        # Honor explicitly requested Host: and Accept-Encoding: headers.
        header_names = dict.fromkeys([k.lower() for k in headers])
        skips = {}
        if 'host' in header_names:
            skips['skip_host'] = 1
        if 'accept-encoding' in header_names:
            skips['skip_accept_encoding'] = 1

        self.putrequest(method, url, **skips)

        if body is not None and ('content-length' not in header_names):
            self._set_content_length(body)
        for hdr, value in headers.items():
            self.putheader(hdr, value)
        if isinstance(body, str):
            # RFC 2616 Section 3.7.1 says that text default has a
            # default charset of iso-8859-1.
            body = body.encode('iso-8859-1')
        yield From (self.endheaders(body))

    @asyncio.coroutine
    def getresponse(self):
        """Get the response from the server.

        If the HTTPConnection is in the correct state, returns an
        instance of HTTPResponse or of whatever object is returned by
        class the response_class variable.

        If a request has not been sent or if a previous response has
        not be handled, ResponseNotReady is raised.  If the HTTP
        response indicates that the connection should be closed, then
        it will be closed before the response is returned.  When the
        connection is closed, the underlying socket is closed.
        """

        # if a prior response has been completed, then forget about it.
        if self.__response and self.__response.isclosed():
            self.__response = None

        # if a prior response exists, then it must be completed (otherwise, we
        # cannot read this response's header to determine the connection-close
        # behavior)
        #
        # note: if a prior response existed, but was connection-close, then the
        # socket and response were made independent of this HTTPConnection
        # object since a new request requires that we open a whole new
        # connection
        #
        # this means the prior response had one of two states:
        #   1) will_close: this connection was reset and the prior socket and
        #                  response operate independently
        #   2) persistent: the response was retained and we await its
        #                  isclosed() status to become true.
        #
        if self.__state != _CS_REQ_SENT or self.__response:
            raise ResponseNotReady(self.__state)

        if self.debuglevel > 0:
            response = self.response_class(self.notSock, self.debuglevel,
                                           method=self._method)
        else:
            response = self.response_class(self.notSock, method=self._method)
        #yield From (response.init())

        yield From (response.begin())
        assert response.will_close != _UNKNOWN
        self.__state = _CS_IDLE

        if response.will_close:
        #     # this effectively passes the connection to the response
        #     #self.close()
            self.__response = None
        else:
            # remember this, so we can tell when it is complete
            self.__response = response

        self.notSock.close()
        self.notSock = None

        raise Return (response)

try:
    import ssl
except ImportError:
    pass
else:
    class HTTPSConnection(object, HTTPConnection):
        "This class allows communication via SSL."

        default_port = HTTPS_PORT

        # XXX Should key_ file and cert_ file be deprecated in favour of context?

        def __init__(self, host, port=None, key_file=None, cert_file=None,
                     timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                     source_address=None, context=None,
                     check_hostname=None):
            super(HTTPSConnection, self).__init__(host, port, timeout, source_address)
            self.key_file = key_file
            self.cert_file = cert_file
            if context is None:
                context = ssl._create_stdlib_context()
            will_verify = context.verify_mode != ssl.CERT_NONE
            if check_hostname is None:
                check_hostname = will_verify
            elif check_hostname and not will_verify:
                raise ValueError("check_hostname needs a SSL context with "
                                 "either CERT_OPTIONAL or CERT_REQUIRED")
            if key_file or cert_file:
                context.load_cert_chain(cert_file, key_file)
            self._context = context
            self._check_hostname = check_hostname

        @asyncio.coroutine
        def connect(self):
            "Connect to a host on a given (SSL) port."

            if self._tunnel_host:
                server_hostname = self._tunnel_host
            else:
                server_hostname = self.host

            # self.soCk = yield From (self._create_connection((self.host, self.port), self.TIMEOUT,
            #                                                self.source_address, ssl=self._context,
            #                                                server_hostname=server_hostname)
            ns = yield From (self._create_connection((self.host, self.port), self.TIMEOUT,
                                                      self.source_address, ssl=self._context,
                                                      server_hostname=server_hostname))

            self.notSock = ns

            if self._tunnel_host:
                yield From (self._tunnel())
                self.auto_open = 0

            #
            # self.soCk = self._context.wrap_socket(self.soCk, server_hostname=sni_hostname,
            #                                       do_handshake_on_connect=False)
            sock = self.notSock.socket()
            if not self._context.check_hostname and self._check_hostname:
                try:
                    ssl.match_hostname(sock.getpeercert(), server_hostname)
                except Exception as e:
                    self.close()
                    raise


    __all__.append("HTTPSConnection")

class HTTPException(Exception):
    # Subclasses that define an __init__ must call Exception.__init__
    # or define self.args.  Otherwise, str() will fail.
    pass

class NotConnected(HTTPException):
    pass

class InvalidURL(HTTPException):
    pass

class UnknownProtocol(HTTPException):
    def __init__(self, version):
        self.args = version,
        self.version = version

class UnknownTransferEncoding(HTTPException):
    pass

class UnimplementedFileMode(HTTPException):
    pass

class IncompleteRead(HTTPException):
    def __init__(self, partial, expected=None):
        self.args = partial,
        self.partial = partial
        self.expected = expected
    def __repr__(self):
        if self.expected is not None:
            e = ', %i more expected' % self.expected
        else:
            e = ''
        return '%s(%i bytes read%s)' % (self.__class__.__name__,
                                        len(self.partial), e)
    def __str__(self):
        return repr(self)

class ImproperConnectionState(HTTPException):
    pass

class CannotSendRequest(ImproperConnectionState):
    pass

class CannotSendHeader(ImproperConnectionState):
    pass

class ResponseNotReady(ImproperConnectionState):
    pass

class BadStatusLine(HTTPException):
    def __init__(self, line):
        if not line:
            line = repr(line)
        self.args = line,
        self.line = line

class LineTooLong(HTTPException):
    def __init__(self, line_type):
        HTTPException.__init__(self, "got more than %d bytes when reading %s"
                                     % (_MAXLINE, line_type))

# for backwards compatibility
error = HTTPException
