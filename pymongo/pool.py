# Copyright 2011-2012 10gen, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you
# may not use this file except in compliance with the License.  You
# may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

import os
import socket
import sys
import time
import threading
import weakref

from pymongo import thread_util
from pymongo.errors import ConnectionFailure, ConfigurationError


have_ssl = True
try:
    import ssl
except ImportError:
    have_ssl = False


NO_REQUEST    = None
NO_SOCKET_YET = -1


if sys.platform.startswith('java'):
    from select import cpython_compatible_select as select
    from java.lang.management import ManagementFactory
    def _os_getpid():
        return ManagementFactory.getRuntimeMXBean().getName()
else:
    from select import select
    _os_getpid = os.getpid


def _closed(sock):
    """Return True if we know socket has been closed, False otherwise.
    """
    try:
        rd, _, _ = select([sock], [], [], 0)
    # Any exception here is equally bad (select.error, ValueError, etc.).
    except:
        return True
    return len(rd) > 0


class SocketInfo(object):
    """Store a socket with some metadata
    """
    def __init__(self, sock, pool_id):
        self.sock = sock
        self.authset = set()
        self.closed = False
        self.last_checkout = time.time()

        # The pool's pool_id changes with each reset() so we can close sockets
        # created before the last reset.
        self.pool_id = pool_id

    def close(self):
        self.closed = True
        # Avoid exceptions on interpreter shutdown.
        try:
            self.sock.close()
        except:
            pass

    def __eq__(self, other):
        # Need to check if other is NO_REQUEST or NO_SOCKET_YET, and then check
        # if its sock is the same as ours
        return hasattr(other, 'sock') and self.sock == other.sock

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash(self.sock)

    def __repr__(self):
        return "SocketInfo(%s)%s at %s" % (
            repr(self.sock),
            self.closed and " CLOSED" or "",
            id(self)
        )


# Do *not* explicitly inherit from object or Jython won't call __del__
# http://bugs.jython.org/issue1057
class Pool:
    def __init__(self, pair, max_size, net_timeout, conn_timeout, use_ssl,
                 use_greenlets):
        """
        :Parameters:
          - `pair`: a (hostname, port) tuple
          - `max_size`: approximate number of idle connections to keep open
          - `net_timeout`: timeout in seconds for operations on open connection
          - `conn_timeout`: timeout in seconds for establishing connection
          - `use_ssl`: bool, if True use an encrypted connection
          - `use_greenlets`: bool, if True then start_request() assigns a
              socket to the current greenlet - otherwise it is assigned to the
              current thread
        """
        if use_greenlets and not thread_util.have_greenlet:
            raise ConfigurationError(
                "The greenlet module is not available. "
                "Install the greenlet package from PyPI."
            )

        self.sockets = set()
        self.lock = threading.Lock()

        # Keep track of resets, so we notice sockets created before the most
        # recent reset and close them.
        self.pool_id = 0
        self.pid = _os_getpid()
        self.pair = pair
        self.max_size = max_size
        self.net_timeout = net_timeout
        self.conn_timeout = conn_timeout
        self.use_ssl = use_ssl
        self._ident = thread_util.create_ident(use_greenlets)

        # Map self._ident.get() -> request socket
        self._tid_to_sock = {}

        # Count the number of calls to start_request() per thread or greenlet
        self._request_counter = thread_util.Counter(use_greenlets)

    def reset(self):
        # Ignore this race condition -- if many threads are resetting at once,
        # the pool_id will definitely change, which is all we care about.
        self.pool_id += 1
        self.pid = _os_getpid()

        sockets = None
        try:
            # Swapping variables is not atomic. We need to ensure no other
            # thread is modifying self.sockets, or replacing it, in this
            # critical section.
            self.lock.acquire()
            sockets, self.sockets = self.sockets, set()
        finally:
            self.lock.release()

        for sock_info in sockets: sock_info.close()

    def create_connection(self, pair):
        """Connect to *pair* and return the socket object.

        This is a modified version of create_connection from
        CPython >=2.6.
        """
        host, port = pair or self.pair

        # Check if dealing with a unix domain socket
        if host.endswith('.sock'):
            if not hasattr(socket, "AF_UNIX"):
                raise ConnectionFailure("UNIX-sockets are not supported "
                                        "on this system")
            sock = socket.socket(socket.AF_UNIX)
            try:
                sock.connect(host)
                return sock
            except socket.error, e:
                if sock is not None:
                    sock.close()
                raise e

        # Don't try IPv6 if we don't support it. Also skip it if host
        # is 'localhost' (::1 is fine). Avoids slow connect issues
        # like PYTHON-356.
        family = socket.AF_INET
        if socket.has_ipv6 and host != 'localhost':
            family = socket.AF_UNSPEC

        err = None
        for res in socket.getaddrinfo(host, port, family, socket.SOCK_STREAM):
            af, socktype, proto, dummy, sa = res
            sock = None
            try:
                sock = socket.socket(af, socktype, proto)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(self.conn_timeout or 20.0)
                sock.connect(sa)
                return sock
            except socket.error, e:
                err = e
                if sock is not None:
                    sock.close()

        if err is not None:
            raise err
        else:
            # This likely means we tried to connect to an IPv6 only
            # host with an OS/kernel or Python interpeter that doesn't
            # support IPv6. The test case is Jython2.5.1 which doesn't
            # support IPv6 at all.
            raise socket.error('getaddrinfo failed')

    def connect(self, pair):
        """Connect to Mongo and return a new (connected) socket. Note that the
           pool does not keep a reference to the socket -- you must call
           return_socket() when you're done with it.
        """
        sock = self.create_connection(pair)

        if self.use_ssl:
            try:
                sock = ssl.wrap_socket(sock)
            except ssl.SSLError:
                sock.close()
                raise ConnectionFailure("SSL handshake failed. MongoDB may "
                                        "not be configured with SSL support.")

        sock.settimeout(self.net_timeout)
        return SocketInfo(sock, self.pool_id)

    def get_socket(self, pair=None):
        """Get a socket from the pool.

        Returns a :class:`SocketInfo` object wrapping a connected
        :class:`socket.socket`, and a bool saying whether the socket was from
        the pool or freshly created.

        :Parameters:
          - `pair`: optional (hostname, port) tuple
        """
        # We use the pid here to avoid issues with fork / multiprocessing.
        # See test.test_connection:TestConnection.test_fork for an example of
        # what could go wrong otherwise
        if self.pid != _os_getpid():
            self.reset()

        # Have we opened a socket for this request?
        req_state = self._get_request_state()
        if req_state not in (NO_SOCKET_YET, NO_REQUEST):
            # There's a socket for this request, check it and return it
            checked_sock = self._check(req_state, pair)
            if checked_sock != req_state:
                self._set_request_state(checked_sock)

            checked_sock.last_checkout = time.time()
            return checked_sock

        # We're not in a request, just get any free socket or create one
        sock_info, from_pool = None, None
        try:
            try:
                # set.pop() isn't atomic in Jython less than 2.7, see
                # http://bugs.jython.org/issue1854
                self.lock.acquire()
                sock_info, from_pool = self.sockets.pop(), True
            finally:
                self.lock.release()
        except KeyError:
            sock_info, from_pool = self.connect(pair), False

        if from_pool:
            sock_info = self._check(sock_info, pair)

        if req_state == NO_SOCKET_YET:
            # start_request has been called but we haven't assigned a socket to
            # the request yet. Let's use this socket for this request until
            # end_request.
            self._set_request_state(sock_info)

        sock_info.last_checkout = time.time()
        return sock_info

    def start_request(self):
        if self._get_request_state() == NO_REQUEST:
            # Add a placeholder value so we know we're in a request, but we
            # have no socket assigned to the request yet.
            self._set_request_state(NO_SOCKET_YET)

        self._request_counter.inc()

    def in_request(self):
        return bool(self._request_counter.get())

    def end_request(self):
        tid = self._ident.get()

        # Check if start_request has ever been called in this thread / greenlet
        count = self._request_counter.get()
        if count:
            self._request_counter.dec()
            if count == 1:
                # End request
                sock_info = self._get_request_state()
                self._set_request_state(NO_REQUEST)
                if sock_info not in (NO_REQUEST, NO_SOCKET_YET):
                    self._return_socket(sock_info)

    def discard_socket(self, sock_info):
        """Close and discard the active socket.
        """
        if sock_info not in (NO_REQUEST, NO_SOCKET_YET):
            sock_info.close()

            if sock_info == self._get_request_state():
                # Discarding request socket; prepare to use a new request
                # socket on next get_socket().
                self._set_request_state(NO_SOCKET_YET)

    def maybe_return_socket(self, sock_info):
        """Return the socket to the pool unless it's the request socket.
        """
        if self.pid != _os_getpid():
            self.reset()
        elif sock_info not in (NO_REQUEST, NO_SOCKET_YET):
            if sock_info.closed:
                return

            if sock_info != self._get_request_state():
                self._return_socket(sock_info)

    def _return_socket(self, sock_info):
        """Return socket to the pool. If pool is full the socket is discarded.
        """
        try:
            self.lock.acquire()
            if len(self.sockets) < self.max_size:
                self.sockets.add(sock_info)
            else:
                sock_info.close()
        finally:
            self.lock.release()

    def _check(self, sock_info, pair):
        """This side-effecty function checks if this pool has been reset since
        the last time this socket was used, or if the socket has been closed by
        some external network error, and if so, attempts to create a new socket.
        If this connection attempt fails we reset the pool and reraise the
        error.

        Checking sockets lets us avoid seeing *some*
        :class:`~pymongo.errors.AutoReconnect` exceptions on server
        hiccups, etc. We only do this if it's been > 1 second since
        the last socket checkout, to keep performance reasonable - we
        can't avoid AutoReconnects completely anyway.
        """
        error = False

        if sock_info.closed:
            error = True

        elif self.pool_id != sock_info.pool_id:
            sock_info.close()
            error = True

        elif time.time() - sock_info.last_checkout > 1:
            if _closed(sock_info.sock):
                sock_info.close()
                error = True

        if not error:
            return sock_info
        else:
            try:
                return self.connect(pair)
            except socket.error:
                self.reset()
                raise

    def _set_request_state(self, sock_info):
        tid = self._ident.get()

        if sock_info == NO_REQUEST:
            # Ending a request
            self._ident.unwatch()
            self._tid_to_sock.pop(tid, None)
        else:
            self._tid_to_sock[tid] = sock_info

            if not self._ident.watching():
                # Closure over tid and poolref. Don't refer directly to self,
                # otherwise there's a cycle.

                # Do not access threadlocals in this function, or any
                # function it calls! In the case of the Pool subclass and
                # mod_wsgi 2.x, on_thread_died() is triggered when mod_wsgi
                # calls PyThreadState_Clear(), which deferences the
                # ThreadVigil and triggers the weakref callback. Accessing
                # thread locals in this function, while PyThreadState_Clear()
                # is in progress can cause leaks, see PYTHON-353.
                poolref = weakref.ref(self)
                def on_thread_died(ref):
                    try:
                        pool = poolref()
                        if pool:
                            # End the request
                            request_sock = pool._tid_to_sock.pop(tid, None)

                            # Was thread ever assigned a socket before it died?
                            if request_sock not in (NO_REQUEST, NO_SOCKET_YET):
                                pool._return_socket(request_sock)
                    except:
                        # Random exceptions on interpreter shutdown.
                        pass

                self._ident.watch(on_thread_died)

    def _get_request_state(self):
        tid = self._ident.get()
        return self._tid_to_sock.get(tid, NO_REQUEST)

    def __del__(self):
        # Avoid ResourceWarnings in Python 3
        for sock_info in self.sockets:
            sock_info.close()

        for request_sock in self._tid_to_sock.values():
            if request_sock not in (NO_REQUEST, NO_SOCKET_YET):
                request_sock.close()


class Request(object):
    """
    A context manager returned by Connection.start_request(), so you can do
    `with connection.start_request(): do_something()` in Python 2.5+.
    """
    def __init__(self, connection):
        self.connection = connection

    def end(self):
        self.connection.end_request()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end()
        # Returning False means, "Don't suppress exceptions if any were
        # thrown within the block"
        return False
