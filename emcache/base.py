# MIT License
# Copyright (c) 2020-2024 Pau Freixes

from abc import ABCMeta, abstractmethod
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Sequence, Union

from ._address import MemcachedHostAddress, MemcachedUnixSocketPath
from .connection_pool import ConnectionPoolMetrics


@dataclass
class Item:
    value: bytes
    flags: Optional[int]
    cas: Optional[int]


class Client(metaclass=ABCMeta):
    @property
    @abstractmethod
    def closed(self) -> bool:
        """Returns True if the client is already closed and no longer
        available to be used."""

    @abstractmethod
    async def close(self) -> None:
        """Closes any active background task and close all TCP
        connections.

        It does not implement any graceful close at operation level,
        if there are active operations the outcome of these operations
        is not predictable.
        """

    @abstractmethod
    def cluster_managment(self) -> "ClusterManagment":
        """Returns the `ClusterManagment` instance class for managing
        the cluster related to that client.

        Same instance is returned at any call.
        """

    @abstractmethod
    async def get(self, key: bytes, return_flags=False) -> Optional[Item]:
        """Return the value associated with the key as an `Item` instance.

        If `return_flags` is set to True, the `Item.flags` attribute will be
        set with the value saved along the value will be returned, otherwise
        a None value will be set.

        If key is not found, a `None` value will be returned.

        If timeout is not disabled, an `asyncio.TimeoutError` will
        be returned in case of a timed out operation.
        """

    @abstractmethod
    async def gets(self, key: bytes, return_flags=False) -> Optional[Item]:
        """Return the value associated with the key and its cas value as
        an `Item` instance.

        If `return_flags` is set to True, the `Item.flags` attribute will be
        set with the value saved along the value will be returned, otherwise
        a None value will be set.

        If key is not found, a `None` value will be returned.

        If timeout is not disabled, an `asyncio.TimeoutError` will
        be returned in case of a timed out operation.
        """

    @abstractmethod
    async def get_many(self, keys: Sequence[bytes], return_flags=False) -> Dict[bytes, Item]:
        """Return the values associated with the keys.

        If a key is not found, the key won't be added to the result.

        More than one request could be sent concurrently to different nodes,
        where each request will be composed of one or many keys. Hashing
        algorithm will decide how keys will be grouped by.

        If any request fails due to a timeout - if it is configured - or any other
        error, all ongoing requests will be automatically canceled and the error will
        be raised back to the caller.
        """

    @abstractmethod
    async def gets_many(self, keys: Sequence[bytes], return_flags=False) -> Dict[bytes, Item]:
        """Return the values associated with the keys and their cas
        values.

        Take a look at the `get_many` command for parameters description.
        """

    @abstractmethod
    async def set(self, key: bytes, value: bytes, *, flags: int = 0, exptime: int = 0, noreply: bool = False) -> None:
        """Set a specific value for a given key.

        If command fails a `StorageCommandError` is raised, however
        when `noreply` option is used there is no ack from the Memcached
        server, not raising any command error.

        If timeout is not disabled, an `asyncio.TimeoutError` will
        be returned in case of a timed out operation.

        Other parameters are optional, use them in the following
        situations:

        - `flags` is an arbitrary 16-bit unsigned integer stored
        along the value that can be retrieved later with a retrieval
        command.
        - `exptime` is the expiration time expressed as an absolute
        timestamp. By default, it is set to 0 meaning that the there
        is no expiration time.
        - `noreply` when is set memcached will not return a response
        back telling how the opreation finished, avoiding a full round
        trip between the client and sever. By using this, the client
        won't have an explicit way for knowing if the storage command
        finished correctly. By default is disabled.
        """

    @abstractmethod
    async def add(self, key: bytes, value: bytes, *, flags: int = 0, exptime: int = 0, noreply: bool = False) -> None:
        """Set a specific value for a given key if and only if the key
        does not already exist.

        If the command fails because the key already exists a
        `NotStoredStorageCommandError` exception is raised, for other
        errors the generic `StorageCommandError` is used. However when
        `noreply` option is used there is no ack from the Memcached
        server, not raising any command error.

        Take a look at the `set` command for parameters description.
        """

    @abstractmethod
    async def replace(
        self, key: bytes, value: bytes, *, flags: int = 0, exptime: int = 0, noreply: bool = False
    ) -> None:
        """Set a specific value for a given key if and only if the key
        already exists.

        If the command fails because the key was not found a
        `NotStoredStorageCommandError` exception is raised, for other
        errors the generic `StorageCommandError` is used. However when
        `noreply` option is used there is no ack from the Memcached
        server, not raising any command error.

        Take a look at the `set` command for parameters description.
        """

    @abstractmethod
    async def append(self, key: bytes, value: bytes, *, noreply: bool = False) -> None:
        """Append a specific value for a given key to the current value
        if and only if the key already exists.

        If the command fails because the key was not found a
        `NotStoredStorageCommandError` exception is raised, for other
        errors the generic `StorageCommandError` is used. However when
        `noreply` option is used there is no ack from the Memcached
        server, not raising any command error.

        Take a look at the `set` command for parameters description.
        """

    @abstractmethod
    async def prepend(self, key: bytes, value: bytes, *, noreply: bool = False) -> None:
        """Prepend a specific value for a given key to the current value
        if and only if the key already exists.

        If the command fails because the key was not found a
        `NotStoredStorageCommandError` exception is raised, for other
        errors the generic `StorageCommandError` is used. However when
        `noreply` option is used there is no ack from the Memcached
        server, not raising any command error.

        Take a look at the `set` command for parameters description.
        use the documentation of that method.
        """

    @abstractmethod
    async def cas(
        self, key: bytes, value: bytes, cas: int, *, flags: int = 0, exptime: int = 0, noreply: bool = False
    ) -> None:
        """Update a specific value for a given key using a cas
        value, if cas value does not match with the server one
        command will fail.

        If command fails a `StorageCommandError` is raised, however
        when `noreply` option is used there is no ack from the Memcached
        server, not raising any command error.

        Take a look at the `set` command for parameters description.
        use the documentation of that method.
        """

    @abstractmethod
    async def increment(self, key: bytes, value: int, *, noreply: bool = False) -> Optional[int]:
        """Increment a specific integer stored with a key by a given `value`, the key
        must exist.

        If `noreply` is not used and the key exists the new value will be returned, otherwise
        a None is returned.

        If the command fails because the key was not found a
        `NotFoundCommandError` exception is raised.
        """

    @abstractmethod
    async def decrement(self, key: bytes, value: int, *, noreply: bool = False) -> Optional[int]:
        """Decrement a specific integer stored with a key by a given `value`, the key
        must exist.

        If `noreply` is not used and the key exists the new value will be returned, otherwise
        a None is returned.

        If the command fails because the key was not found a
        `NotFoundCommandError` exception is raised.
        """

    @abstractmethod
    async def touch(self, key: bytes, exptime: int, *, noreply: bool = False) -> None:
        """Set and override, if its the case, the exptime for an existing key.

        If the command fails because the key was not found a
        `NotFoundCommandError` exception is raised. Other errors
        raised by the memcached server which imply that the item was
        not touched raise a generic `CommandError` exception.
        """

    @abstractmethod
    async def delete(self, key: bytes, *, noreply: bool = False) -> None:
        """Delete an exixting key.

        If the command fails because the key was not found a
        `NotFoundCommandError` exception is raised. Other errors
        raised by the memcached server which imply that the item was
        not touched raise a generic `CommandError` exception.
        """

    @abstractmethod
    async def flush_all(
        self,
        memcached_host_address: Union[MemcachedHostAddress, MemcachedUnixSocketPath],
        delay: int = 0,
        *,
        noreply: bool = False
    ) -> None:
        """Flush all keys in a specific memcached host address.

        Flush can be deferred at memcached server side for a specific time by
        using the `delay` option, otherwise the flush will happen immediately.

        If the command fails a `CommandError` exception will be raised.
        """

    @abstractmethod
    async def version(
        self, memcached_host_address: Union[MemcachedHostAddress, MemcachedUnixSocketPath]
    ) -> Optional[str]:
        """Version is a command with no arguments:

        version\r\n

        In response, the server sends

        "VERSION <version>\r\n", where <version> is the version string for the
        server.
        """

    @abstractmethod
    async def gat(self, exptime: int, key: bytes, return_flags=False) -> Optional[Item]:
        """Gat command is used to fetch item and update the
        expiration time of an existing item.
        Get And Touch.

        gat <exptime> <key>\r\n
        """

    @abstractmethod
    async def gats(self, exptime: int, key: bytes, return_flags=False) -> Optional[Item]:
        """Gats command is used to fetch item and update the
        expiration time of an existing item.
        Get And Touch.

        An alternative gat command for using with CAS

        gats <exptime> <key>\r\n
        """

    @abstractmethod
    async def gat_many(self, exptime: int, keys: Sequence[bytes], return_flags=False) -> Optional[Item]:
        """Return the values associated with the keys.
        Gat command is used to fetch items and update the
        expiration time of an existing items.
        Some Get And Touch.

        gat <exptime> <key>*\r\n
        """

    @abstractmethod
    async def gats_many(self, exptime: int, keys: Sequence[bytes], return_flags=False) -> Optional[Item]:
        """Return the values associated with the keys.
        Gats command is used to fetch items and update the
        expiration time of an existing items.
        Some Get And Touch.

        An alternative gat command for using with CAS

        gats <exptime> <key>*\r\n
        """

    @abstractmethod
    async def cache_memlimit(
        self, memcached_host_address: MemcachedHostAddress, value: int, *, noreply: bool = False
    ) -> None:
        """Cache_memlimit is a command with a numeric argument. This allows runtime
        adjustments of the cache memory limit. The argument is in megabytes, not bytes.
        """

    @abstractmethod
    async def stats(self, memcached_host_address: MemcachedHostAddress, *args: str) -> Dict[str, str]:
        """The memcached command via "stats" which show needed statistics about server.
        Client send without arguments - `stats\r\n`, with arguments - `stats <args>\r\n`.
        Depending on the arguments, the server will return statistics to you until it finishes `END\r\n`.
        Please see a lot of detailed information in the documentation.
        """

    @abstractmethod
    async def verbosity(
        self,
        memcached_host_address: Union[MemcachedHostAddress, MemcachedUnixSocketPath],
        level: int,
        *,
        noreply: bool = False
    ) -> None:
        """Increase level of log verbosity for a memcached server.
        1 - print standard errors/warnings
        2 - also print client commands/responses
        3 - internal state transitions

        Send command "verbosity <level> [noreply]\r\n"
        Return always "OK\r\n" if skip noreply and correct command.
        """

    @abstractmethod
    def pipeline(self, memcached_host_address: Union[MemcachedHostAddress, MemcachedUnixSocketPath]) -> "Pipeline":
        """Return Pipeline object which support execute many commands to the Memcached server in one step."""


class Pipeline(metaclass=ABCMeta):
    @abstractmethod
    async def execute(self):
        """Accumulate commands and push on memcached server."""

    @abstractmethod
    def get(self, key: bytes) -> "Pipeline":
        """Push memcached `get` command in stack."""

    @abstractmethod
    def gets(self, key: bytes) -> "Pipeline":
        """Push memcached `gets` command in stack."""

    @abstractmethod
    def get_many(self, keys: Sequence[bytes]) -> "Pipeline":
        """Push memcached `get*` command in stack."""

    @abstractmethod
    def gets_many(self, keys: Sequence[bytes]) -> "Pipeline":
        """Push memcached `gets*` command in stack."""

    @abstractmethod
    async def set(
        self, key: bytes, value: bytes, *, flags: int = 0, exptime: int = 0, noreply: bool = False
    ) -> "Pipeline":
        """Push memcached `set` command in stack."""

    @abstractmethod
    def add(self, key: bytes, value: bytes, *, flags: int = 0, exptime: int = 0, noreply: bool = False) -> "Pipeline":
        """Push memcached `add` command in stack."""

    @abstractmethod
    def replace(
        self, key: bytes, value: bytes, *, flags: int = 0, exptime: int = 0, noreply: bool = False
    ) -> "Pipeline":
        """Push memcached `replace` command in stack."""

    @abstractmethod
    def append(self, key: bytes, value: bytes, *, noreply: bool = False) -> "Pipeline":
        """Push memcached `append` command in stack."""

    @abstractmethod
    def prepend(self, key: bytes, value: bytes, *, noreply: bool = False) -> "Pipeline":
        """Push memcached `prepend` command in stack."""

    @abstractmethod
    def cas(
        self, key: bytes, value: bytes, cas: int, *, flags: int = 0, exptime: int = 0, noreply: bool = False
    ) -> "Pipeline":
        """Push memcached `cas` command in stack."""

    @abstractmethod
    def increment(self, key: bytes, value: int, *, noreply: bool = False) -> "Pipeline":
        """Push memcached `increment` command in stack."""

    @abstractmethod
    def decrement(self, key: bytes, value: int, *, noreply: bool = False) -> "Pipeline":
        """Push memcached `decrement` command in stack."""

    @abstractmethod
    def touch(self, key: bytes, exptime: int, *, noreply: bool = False) -> "Pipeline":
        """Push memcached `touch` command in stack."""

    @abstractmethod
    def delete(self, key: bytes, *, noreply: bool = False) -> "Pipeline":
        """Push memcached `delete` command in stack."""

    @abstractmethod
    def flush_all(self, delay: int = 0, *, noreply: bool = False) -> "Pipeline":
        """Push memcached `flush_all` command in stack."""

    @abstractmethod
    def version(self) -> "Pipeline":
        """Push memcached `version` command in stack."""

    @abstractmethod
    def gat(self, exptime: int, key: bytes) -> "Pipeline":
        """Push memcached `gat` command in stack."""

    @abstractmethod
    def gats(self, exptime: int, key: bytes) -> "Pipeline":
        """Push memcached `gats` command in stack."""

    @abstractmethod
    def gat_many(self, exptime: int, keys: Sequence[bytes]) -> "Pipeline":
        """Push memcached `gat*` command in stack."""

    @abstractmethod
    def gats_many(self, exptime: int, keys: Sequence[bytes]) -> "Pipeline":
        """Push memcached `gats*` command in stack."""

    @abstractmethod
    def cache_memlimit(self, value: int, *, noreply: bool = False) -> "Pipeline":
        """Push memcached `cache_memlimit` command in stack."""

    @abstractmethod
    def stats(self, *args: str) -> "Pipeline":
        """Push memcached `stats` command in stack."""

    @abstractmethod
    def verbosity(self, level: int, *, noreply: bool = False) -> "Pipeline":
        """Push memcached `verbosity` command in stack."""


class ClusterEvents(metaclass=ABCMeta):
    """ClusterEvents can be used for being notified about different
    events that happen at cluster level.

    Each kind of event is identified with its own function named
    `on_<event_name>` which might be called zero, one or many times.
    """

    @abstractmethod
    async def on_node_healthy(
        self, cluster_managment: "ClusterManagment", host: Union[MemcachedHostAddress, MemcachedUnixSocketPath]
    ) -> None:
        """Called when a node is marked as healthy.

        A node is marked as healthy when there is at least one TCP
        connection oppened to the host.
        """

    @abstractmethod
    async def on_node_unhealthy(
        self, cluster_managment: "ClusterManagment", host: Union[MemcachedHostAddress, MemcachedUnixSocketPath]
    ) -> None:
        """Called when a new node is marked as umhealthy.

        A node is marked as unhealthy when there is no TCP
        connection oppened to the host and the last attempts for
        oppening one have failed.

        Traffic might no be longer routed to that host depending
        on the cluster configuration, take a look to the
        `purge_unhealthy_nodes` parameter provided during the
        client creation.

        These event will be fired in any circumstance without depending on
        the value of the `purge_unhealthy_nodes` value.
        """


class ClusterManagment(metaclass=ABCMeta):
    """ClusterManagment provides you the public interface
    for managing the cluster.

    A `Client` instance provides you a way for having access
    to an instance of `ClusterManagment` related to the cluster
    used for that specific client, as the following example
    shows:

        >>> client = await emcache.create_client(...)
        >>> cluster_managment = client.cluster_managment()
        >>> print(cluster_managment.nodes())

    Take a look to the different methods for knowing what operations
    are currently supported.
    """

    @abstractmethod
    def nodes(self) -> Sequence[Union[MemcachedHostAddress, MemcachedUnixSocketPath]]:
        """Return the nodes that belong to the cluster."""

    @abstractmethod
    def healthy_nodes(self) -> Sequence[Union[MemcachedHostAddress, MemcachedUnixSocketPath]]:
        """Return the nodes that are considered healthy."""

    @abstractmethod
    def unhealthy_nodes(self) -> Sequence[Union[MemcachedHostAddress, MemcachedUnixSocketPath]]:
        """Return the nodes that are considered unhealthy."""

    @abstractmethod
    def connection_pool_metrics(
        self,
    ) -> Mapping[Union[MemcachedHostAddress, MemcachedUnixSocketPath], ConnectionPoolMetrics]:
        """Return the metrics for the connection pools."""
