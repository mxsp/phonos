import socket
import struct
import logging
from typing import Optional

logger = logging.getLogger(__name__)

def send_message(sock: socket.socket, message: bytes, timeout: int = 30) -> bool:
    """
    Send a message over a socket with length prefix and proper error handling.
    Returns True if successful, False otherwise.
    """
    try:
        # Set timeout for sending
        original_timeout = sock.gettimeout()
        sock.settimeout(timeout)
        
        # Send the length of the message first (4 bytes, big-endian)
        message_length = len(message)
        length_bytes = struct.pack('>I', message_length)
        
        # Send length prefix
        bytes_sent = 0
        while bytes_sent < 4:
            try:
                sent = sock.send(length_bytes[bytes_sent:])
                if sent == 0:
                    logger.error("Socket connection broken while sending length")
                    return False
                bytes_sent += sent
            except (ConnectionResetError, BrokenPipeError) as e:
                logger.error(f"Connection error while sending length: {e}")
                return False
        
        # Send the actual message
        bytes_sent = 0
        while bytes_sent < message_length:
            try:
                sent = sock.send(message[bytes_sent:])
                if sent == 0:
                    logger.error("Socket connection broken while sending message")
                    return False
                bytes_sent += sent
            except (ConnectionResetError, BrokenPipeError) as e:
                logger.error(f"Connection error while sending message: {e}")
                return False
        
        # Restore original timeout
        sock.settimeout(original_timeout)
        return True
        
    except socket.timeout:
        logger.error("Timeout while sending message")
        return False
    except Exception as e:
        logger.error(f"Unexpected error while sending message: {e}")
        return False
    finally:
        # Always restore original timeout
        try:
            sock.settimeout(original_timeout)
        except:
            pass


def receive_message(sock: socket.socket, timeout: int = 30) -> Optional[bytes]:
    """
    Receive a message from a socket with length prefix and proper error handling.
    Returns the message bytes if successful, None otherwise.
    """
    try:
        # Set timeout for receiving
        original_timeout = sock.gettimeout()
        sock.settimeout(timeout)
        
        # First, receive the length of the message (4 bytes)
        length_bytes = b''
        while len(length_bytes) < 4:
            try:
                chunk = sock.recv(4 - len(length_bytes))
                if not chunk:
                    logger.error("Socket connection broken while receiving length")
                    return None
                length_bytes += chunk
            except (ConnectionResetError, BrokenPipeError) as e:
                logger.error(f"Connection error while receiving length: {e}")
                return None
        
        # Unpack the message length
        message_length = struct.unpack('>I', length_bytes)[0]
        
        # Validate message length (prevent memory exhaustion attacks)
        MAX_MESSAGE_SIZE = 100 * 1024 * 1024  # 100MB max
        if message_length > MAX_MESSAGE_SIZE:
            logger.error(f"Message too large: {message_length} bytes (max: {MAX_MESSAGE_SIZE})")
            return None
        
        if message_length == 0:
            logger.warning("Received empty message")
            return b''
        
        # Now receive the actual message
        message = b''
        while len(message) < message_length:
            try:
                chunk_size = min(8192, message_length - len(message))  # Receive in chunks
                chunk = sock.recv(chunk_size)
                if not chunk:
                    logger.error("Socket connection broken while receiving message")
                    return None
                message += chunk
            except (ConnectionResetError, BrokenPipeError) as e:
                logger.error(f"Connection error while receiving message: {e}")
                return None
        
        # Restore original timeout
        sock.settimeout(original_timeout)
        return message
        
    except socket.timeout:
        logger.error("Timeout while receiving message")
        return None
    except Exception as e:
        logger.error(f"Unexpected error while receiving message: {e}")
        return None
    finally:
        # Always restore original timeout
        try:
            sock.settimeout(original_timeout)
        except:
            pass


def check_connection(sock: socket.socket) -> bool:
    """
    Check if a socket connection is still alive.
    Returns True if connection is alive, False otherwise.
    """
    try:
        # Try to peek at the socket without removing data
        data = sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        return True
    except socket.error as e:
        if e.errno == socket.EAGAIN or e.errno == socket.EWOULDBLOCK:
            # No data available, but connection is alive
            return True
        else:
            # Connection is broken
            return False
    except:
        return False


def safe_socket_close(sock: socket.socket):
    """Safely close a socket with proper error handling."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except:
        pass  # Socket might already be closed
    try:
        sock.close()
    except:
        pass


def create_server_socket(host: str, port: int, backlog: int = 10, reuse_addr: bool = True) -> Optional[socket.socket]:
    """
    Create a server socket with proper error handling and configuration.
    Returns the socket if successful, None otherwise.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        
        if reuse_addr:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        # Set socket options for better performance
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        
        sock.bind((host, port))
        sock.listen(backlog)
        
        logger.info(f"Server socket created and listening on {host}:{port}")
        return sock
        
    except Exception as e:
        logger.error(f"Failed to create server socket on {host}:{port}: {e}")
        return None


def create_client_socket(host: str, port: int, timeout: int = 30) -> Optional[socket.socket]:
    """
    Create a client socket with proper error handling and configuration.
    Returns the socket if successful, None otherwise.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        
        # Set socket options
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        
        # Set connection timeout
        sock.settimeout(timeout)
        
        sock.connect((host, port))
        
        # Remove timeout for normal operation
        sock.settimeout(None)
        
        logger.info(f"Client socket connected to {host}:{port}")
        return sock
        
    except Exception as e:
        logger.error(f"Failed to create client socket to {host}:{port}: {e}")
        try:
            sock.close()
        except:
            pass
        return None


def enable_keepalive(sock: socket.socket):
    """
    Enables TCP Keepalive on a socket for robust long-lived connections.
    """
    logger.info(f"Enabling TCP Keepalive on socket {sock.getpeername()}")
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
        elif hasattr(socket, "IPPROTO_TCP") and hasattr(socket, "TCP_KEEPALIVE"):
             sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 60)

        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)

    except (AttributeError, OSError) as e:
        logger.warning(f"Could not set all TCP Keepalive parameters (this is okay on some OS): {e}")
