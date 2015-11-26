#!/usr/bin/env python
#
#  python-unittest-skeleton helper which allows for creating TCP
#  servers that misbehave in certain ways, for testing code.
#
#===============
#  This is based on a skeleton test file, more information at:
#
#     https://github.com/linsomniac/python-unittest-skeleton

import sys
import threading
import socket
import time
PY3 = sys.version > '3'
from trollius import ConnectionAbortedError

class FauxWriter():
    """subclass of a true socket"""

    def __init__(self, writer):
        self._data = {'sendall_calls': 0,
                      'send_calls': 0,
                      'data_out': b'',
                      'cue': None,
                      'throw': None}
        self._writer = writer

    def write(self, data):
        self._data['sendall_calls'] += 1
        self._data['data_out'] += data
        self.testBreak()
        return self._writer.write(data)

    def testBreak(self):
        cue = self._data['cue']
        if cue is not None and cue in self._data['data_out']:
            raise self._data['throw']

    def breakOn(self, cue, exc):
        """registers cue, and when cue is seen in send data, exception is thrown"""
        self._data['cue'] = cue
        self._data['throw'] = exc


class TestTCPServer:
    '''A simple socket server so that specific error conditions can be tested.
    This must be subclassed and implment the "server()" method.

    The server() method would be implemented to do :py:func:`socket.send` and
    :py:func:`socket.recv` calls to communicate with the client process.
    '''

    GROUP = 'TestTCPServer'

    STOPPED = False

    def _perConn(self, count):
        if not self.STOPPED:
            try:
                connection, addr = self.s.accept()
            except OSError as e:
                return
            except socket.timeout as e:
                return
        if not self.STOPPED:
            self.server(self.s, connection, count)
            count += 1

    def __init__(self, host='127.0.0.1', port=2222):

        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        def _setup(self, evt):

            TestTCPServer.STOPPED = False

            self.s.settimeout(5)
            self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.s.bind((host, port))
            self.s.listen(1)
            self.port = self.s.getsockname()[1]

            count = 0
            evt.set()  # signals that server is started, main thread can continue
            while not self.STOPPED:
                self._perConn(count)
            self.s.close()

        while [t for t in threading.enumerate() if t.name == self.GROUP]:
            pass
        evt = threading.Event()
        thd = threading.Thread(name=self.GROUP, target=lambda: _setup(self, evt))
        thd.start()
        evt.wait() # wait for server to startup, before continuing
        time.sleep(0.1)

    def server(self, sock, conn, ct):
        raise NotImplementedError('implement .server method')

    def stop(self):
        TestTCPServer.STOPPED = True
        self.s.close()



RECEIVE = 0         # instruct the server to read data
BREAK = None


class GenericServer(TestTCPServer):

    def __init__(self, commands, host='127.0.0.1', port=2222):
        self.commands = commands
        TestTCPServer.__init__(self, host, port)

    def _recieveData(self, conn, minQuan=1):
        inp = b''
        d = True
        while d and len(inp) < minQuan:
            d = conn.recv(1024)
            inp = inp + d
        if not inp:
            return None
        return inp

    def _sendData(self, conn, data):
        if type(data) == type(b''):
            conn.send(data)
        else:
            if PY3:
                conn.send(bytes(data, 'ascii'))
            else:
                conn.send(bytes(data))

    def server(self, sock, conn, count):
        conn.settimeout(2.0)
        for command in self.commands:
            if self.STOPPED:
                break
            if type(command) == int:  # receive data
                try:
                    command = command if command else 1
                    d = self._recieveData(conn, abs(command))
                    self.withReceivedData(d)
                except ConnectionAbortedError as e:
                    return
                except socket.timeout:
                    self.STOPPED = True
                    break
            else:
                self._sendData(conn, command)

        self.atEOF(conn)
        conn.close()

    def withReceivedData(self, data):
        pass

    def atEOF(self, conn):
        pass


class CommandServer(GenericServer):
    '''A convenience class that allows you to specify a set of TCP
    interactions that will be performed, providing a generic "server()"
    method for FakeTCPServer().

    For example, if you want to test what happens when your code sends some
    data, receives a "STORED" response, sends some more data and then the
    connection is closed:

    >>> fake_server = CommandServer(
    >>>     [RECEIVE, "STORED\r\n", RECEIVE])
    >>> sc = memcached2.ServerConnection('memcached://127.0.0.1:{0}/'
    >>>         .format(fake_server.port))
    '''
    
    def __init__(self, commands, host='127.0.0.1', port=2222, verbose=True):
        GenericServer.__init__(self, commands, host, port)
        self.numBytesSent = 0
        self.received = []
        self.verbose = verbose
        if self.verbose:
            print('SERVER: initialized')

    def _recieveData(self, conn, minQuan=1):
        try:
            inp = GenericServer._recieveData(self, conn, minQuan)
        except socket.timeout:
            if self.verbose:
                print('SERVER: timedout')
            raise
        else:
            if self.verbose:
                if inp is None:
                    print('SERVER: socket recv null')
                else:
                    print('SERVER: received some %s bytes' % len(inp or ''))

        self.received.append(inp)
        return inp

    def _sendData(self, conn, data):
        GenericServer._sendData(self, conn, data)
        self.numBytesSent += len(data)
        if self.verbose:
            print('SERVER: sent %s bytes' % len(data))

    def atEOF(self, conn):
        GenericServer.atEOF(self, conn)
        if self.verbose:
            print('SERVER: closing socket connection %s after %s bytes' % (conn.getpeername()[1], self.numBytesSent))


class AsyncioCommandServer(CommandServer):
    """same as CommandServer, but feeds data into a StreamReader"""

    def __init__(self, commands, loop=None, reader=None, host='127.0.0.1', port=2222, verbose=True):
        CommandServer.__init__(self, commands, host, port, verbose)
        self.reader = reader
        self.loop = loop

    def withReceivedData(self, data):
        def _d(data):
            if self.verbose:
                print('SERVER: feed_data %s bytes' % len(data))
            self.reader.feed_data(data)
        if self.loop is not None:
            self.loop.call_soon_threadsafe(_d, data)

    def atEOF(self, conn):
        def _d():
            if self.verbose:
                print('SERVER: atEOF')
            self.reader.feed_eof()
        if self.loop is not None:
            self.loop.call_soon_threadsafe(_d)

class OneShotServer(CommandServer):

    def __init__(self, commands, host='127.0.0.1', port=2222, verbose=True):
        CommandServer.__init__(self, commands, host, port, verbose)

    def _perConn(self, count):
        if not self.STOPPED:
            try:
                connection, addr = self.s.accept()
            except OSError as e:
                return
            except socket.timeout as e:
                return
        count = 0
        if not self.STOPPED:
            self.server(self.s, connection, count)
            count += 1
        self.stop()

