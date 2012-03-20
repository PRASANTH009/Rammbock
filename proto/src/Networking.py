import socket
import time
from binary_tools import to_hex

UDP_BUFFER_SIZE = 65536
TCP_BUFFER_SIZE = 1000000
TCP_MAX_QUEUED_CONNECTIONS = 5


class _WithConnections(object):

    _default_timeout = 10

    def _get_timeout(self, timeout):
        if timeout in (None, '') or str(timeout).lower() == 'none':
            return self._default_timeout
        elif str(timeout).lower() == 'blocking':
            return None
        return float(timeout)

    def _set_default_timeout(self, timeout):
        self._default_timeout = self._get_timeout(timeout)

    def get_own_address(self):
        return self._socket.getsockname()

    def get_peer_address(self, alias=None):
        if alias:
            raise AssertionError('Named connections not supported.')
        return self._socket.getpeername()


class _WithMessageStreams(object):

    def get_message(self, message_template, timeout=None, header_filter=None):
        return self._get_from_stream(message_template, self._message_stream, timeout=timeout, header_filter=header_filter)

    def _get_from_stream(self, message_template, stream, timeout, header_filter):
        return stream.get(message_template, timeout=timeout, header_filter=header_filter)

    def log_send(self, binary, ip, port):
        print '*DEBUG* Send %s to %s:%s over %s' % (to_hex(binary), ip, port, self._transport_layer_name)

    def empty(self):
        result = True
        try:
            while (result):
                result = self.receive(timeout=0.0)
        except (socket.timeout, socket.error):
            pass
        if self._message_stream:
            self._message_stream.empty()


class _Server(_WithConnections, _WithMessageStreams):

    def __init__(self, ip, port, timeout=None):
        self._ip = ip
        self._port = int(port)
        self._set_default_timeout(timeout)

    def _bind_socket(self):
        self._socket.bind((self._ip, self._port))
        self._is_connected = True

    def close(self):
        if self._is_connected:
            self._is_connected = False
            self._socket.close()
            self._message_stream = None

    def _get_message_stream(self, connection):
        if not self._protocol:
            return None
        return self._protocol.get_message_stream(BufferedStream(connection, self._default_timeout))


class UDPServer(_Server):

    _transport_layer_name = 'UDP'

    def __init__(self, ip, port, timeout=None, protocol=None):
        _Server.__init__(self, ip, port, timeout)
        self._protocol = protocol
        self._last_client = None
        self._init_socket()

    def _init_socket(self):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._bind_socket()
        self._message_stream = self._get_message_stream(self)

    def receive_from(self, timeout=None, alias=None):
        self._check_no_alias(alias)
        timeout = self._get_timeout(timeout)
        self._socket.settimeout(timeout)
        msg, (ip, host) = self._socket.recvfrom(UDP_BUFFER_SIZE)
        print "*DEBUG* Read %s" % to_hex(msg)
        self._last_client = (ip, host)
        return msg, ip, host

    def _check_no_alias(self, alias):
        if alias:
            raise Exception('Connection aliases are not supported on UDP Servers')

    def receive(self, timeout=None, alias=None):
        return self.receive_from(timeout, alias)[0]

    def send_to(self, msg, ip, port):
        self.log_send(msg, ip, port)
        self._socket.sendto(msg, (ip,int(port)))

    def send(self, msg, alias=None):
        if alias:
            raise Exception('UDP Server does not have connection aliases. Tried to use connection %s.' % alias)
        if not self._last_client:
            raise Exception('Server can not send to default client, because it has not received messages from clients.')
        self.send_to(msg, *self._last_client)

    def get_peer_address(self, alias=None):
        if alias:
            raise AssertionError('Named connections not supported.')
        return self._last_client


class TCPServer(_Server):

    _transport_layer_name = 'TCP'

    def __init__(self, ip, port, timeout=None, protocol=None):
        _Server.__init__(self, ip, port, timeout)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._bind_socket()
        self._socket.listen(TCP_MAX_QUEUED_CONNECTIONS)
        self._connections = _NamedCache('connection')
        self._protocol = protocol

    def receive(self, timeout=None, alias=None):
        return self.receive_from(timeout, alias)[0]

    def receive_from(self, timeout=None, alias=None):
        connection = self._connections.get(alias)
        return connection.receive_from(timeout=timeout)

    def accept_connection(self, alias=None):
        connection, client_address = self._socket.accept()
        self._connections.add(_Connection(connection, protocol=self._protocol), alias)
        return client_address

    def send(self, msg, alias=None):
        connection = self._connections.get(alias)
        connection.send(msg)

    def send_to(self, *args):
        raise Exception("TCP server cannot send to a specific address.")

    def close(self):
        if self._is_connected:
            self._is_connected = False
            for connection in self._connections:
                connection.close()
            self._socket.close()

    def close_connection(self, alias=None):
        raise Exception("Not yet implemented")

    def get_message(self, message_template, timeout=None, alias=None, header_filter=None):
        connection = self._connections.get(alias)
        return connection.get_message(message_template, timeout=timeout, header_filter=header_filter)

    def empty(self):
        for connection in self._connections:
            connection.empty()

    def get_peer_address(self, alias=None):
        connection = self._connections.get(alias)
        return connection.get_peer_address()


class _Connection(_WithConnections, _WithMessageStreams):

    _transport_layer_name = 'TCP'

    # TODO: cleanup (lots of duplicated code, and default timeout should be inherited)
    def __init__(self, socket, protocol=None):
        self._socket = socket
        self._protocol = protocol
        self._message_stream = self._get_message_stream()

    def _get_message_stream(self):
        if not self._protocol:
            return None
        return self._protocol.get_message_stream(BufferedStream(self, self._default_timeout))

    def receive(self, timeout=None):
        return self.receive_from(timeout)[0]

    def receive_from(self, timeout=None):
        timeout = self._get_timeout(timeout)
        self._socket.settimeout(timeout)
        msg = self._socket.recv(TCP_BUFFER_SIZE)
        print "*DEBUG* Read %s" % to_hex(msg)
        ip, port = self._socket.getpeername()
        return msg, ip, port

    def send(self, msg):
        ip, port = self._socket.getpeername()
        self.log_send(msg, ip, port)
        self._socket.sendall(msg)

    def close(self):
        self._socket.close()


class _Client(_WithConnections, _WithMessageStreams):

    def __init__(self, timeout=None, protocol=None):
        self._is_connected = False
        self._init_socket()
        self._set_default_timeout(timeout)
        self._protocol = protocol
        self._message_stream = None

    def _get_message_stream(self):
        if not self._protocol:
            return None
        return self._protocol.get_message_stream(BufferedStream(self, self._default_timeout))

    def set_own_ip_and_port(self, ip=None, port=None):
        if ip and port:
            self._socket.bind((ip, int(port)))
        elif ip:
            self._socket.bind((ip, 0))
        elif port:
            self._socket.bind(("", int(port)))
        else:
            raise Exception("You must specify host or port")

    def connect_to(self, server_ip, server_port):
        if self._is_connected:
            raise Exception('Client already connected!')
        self._server_ip = server_ip
        self._socket.connect((server_ip, int(server_port)))
        self._message_stream = self._get_message_stream()
        self._is_connected = True
        return self

    def send(self, msg):
        ip, port = self._socket.getpeername()
        self.log_send(msg, ip, port)
        self._socket.sendall(msg)

    def receive(self, timeout=None):
        timeout = self._get_timeout(timeout)
        self._socket.settimeout(timeout)
        msg = self._socket.recv(self._size_limit)
        print "*DEBUG* Read %s" % to_hex(msg)
        return msg

    def close(self):
        if self._is_connected:
            self._is_connected = False
            self._socket.close()
            self._message_stream = None


class UDPClient(_Client):

    _transport_layer_name = 'UDP'
    _size_limit = UDP_BUFFER_SIZE

    def _init_socket(self):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


class TCPClient(_Client):

    _transport_layer_name = 'TCP'
    _size_limit = TCP_BUFFER_SIZE

    def _init_socket(self):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


class _NamedCache(object):

    def __init__(self, basename):
        self._basename=basename
        self._counter=0
        self._cache = {}
        self._current = None

    def add(self, value, name=None):
        if not name:
            name=self._next_name()
        self._cache[name] = value
        self._current = name

    def _next_name(self):
        self._counter+=1
        return self._basename+str(self._counter)

    def get(self, name=None):
        if not name:
            print '*DEBUG* Choosing %s by default' % self._current
            return self._cache[self._current]
        return self._cache[name]

    def __iter__(self):
        return self._cache.itervalues()


class BufferedStream(_WithConnections):

    def __init__(self, connection, default_timeout):
        self._connection = connection
        self._buffer = ''
        self._default_timeout = default_timeout

    def read(self, size, timeout=None):
        result = ''
        timeout = float(timeout if timeout else self._default_timeout)
        cutoff = time.time() + timeout
        while time.time() < cutoff:
            result += self._get(size-len(result))
            if len(result) == size:
                return result
            self._fill_buffer(timeout)
        raise AssertionError('Timeout %ds exceeded.' % timeout)

    def _get(self, size):
        if not self._buffer:
            return ''
        result = self._buffer[:size]
        self._buffer = self._buffer[size:]
        return result

    def _fill_buffer(self, timeout):
        self._buffer += self._connection.receive(timeout=timeout)

    def empty(self):
        self._buffer = ''