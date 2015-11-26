yieldfromHttpLib
==============

Asyncio (trollius) conversion of http.client


The classes are named the same as in http.client.

class http.client.HTTPConnection(host, port=None, [timeout, ]source_address=None)

    conn = HTTPConnection('localhost', 8000)
    
    r = yield From (conn.request('GET', '/pagename'))
    resp = yield From (conn.getresponse())
    
    yield From (conn.connect())
    conn.putrequest(..)
    conn.putheader('X-Whatever', 'yesno')
    yield From (conn.endheaders('message body'))
    yield From (conn.send('more body'))
    
    resp = yield From (conn.getresponse())
    # returns an HTTPResponse object
    
    

class http.client.HTTPSConnection(host, port=None, [timeout, ]source_address=None, context=None)

    conn = HTTPSConnection('localhost', 8000, context=context)
    # same as above


class http.client.HTTPResponse(sock, debuglevel=0, method=None, url=None)

Generally, you wont need to call the constructor directly, but if you do, you need to call the .init() method with yield from.

    resp = HTTPResponse(sock=sock)
    yield From (resp.init())
    
Establishing the connection to the socket involves some input/output latency, so the yield From (is required, and having the constructor itself be a coroutine would be sketchy.)

    d = yield From (resp.read())
    # or
    b = bytearray(10)
    d = yield From (resp.readinto(b))


The fileno() method is a no-op.  The resp.fp attribute is an asyncio.StreamReader, with .read(), .readlines(), and .readexactly() methods, all coroutines.  The other attributes and methods work as per the regular HttpLib/http.client module.

