# MIT License
# Copyright (c) 2020-2024 Pau Freixes

import asyncio
import logging
import re
from typing import Dict, List, Optional, Sequence, Tuple, Union

from ._address import MemcachedHostAddress, MemcachedUnixSocketPath
from ._cython import cyemcache
from .autobatching import AutoBatching
from .base import Client, ClusterEvents, ClusterManagment, Item, Pipeline
from .client_errors import CommandError, NotFoundCommandError, NotStoredStorageCommandError, StorageCommandError
from .cluster import Cluster
from .constants import regex_memcached_responses
from .default_values import (
    DEFAULT_AUTOBATCHING_ENABLED,
    DEFAULT_AUTOBATCHING_MAX_KEYS,
    DEFAULT_AUTODISCOVERY_POLL_INTERVAL,
    DEFAULT_AUTODISCOVERY_TIMEOUT,
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_MIN_CONNECTIONS,
    DEFAULT_PURGE_UNHEALTHY_NODES,
    DEFAULT_PURGE_UNUSED_CONNECTIONS_AFTER,
    DEFAULT_SSL,
    DEFAULT_SSL_VERIFY,
    DEFAULT_STARTUP_WAIT_AUTODISCOVERY,
    DEFAULT_TIMEOUT,
)
from .node import Node
from .protocol import DELETED, END, EXISTS, NOT_FOUND, NOT_STORED, OK, STORED, TOUCHED, VERSION
from .timeout import OpTimeout

logger = logging.getLogger(__name__)


MAX_ALLOWED_FLAG_VALUE = 2**16
MAX_ALLOWED_CAS_VALUE = 2**64


class _Client(Client):

    _cluster: Cluster
    _timeout: Optional[float]
    _loop: asyncio.AbstractEventLoop
    _closed: bool
    _autobatching_noflags_nocas: Optional[AutoBatching]
    _autobatching_flags_nocas: Optional[AutoBatching]
    _autobatching_noflags_cas: Optional[AutoBatching]
    _autobatching_flags_cas: Optional[AutoBatching]
    _autobatching: bool

    def __init__(
        self,
        node_addresses: Sequence[Union[MemcachedHostAddress, MemcachedUnixSocketPath]],
        timeout: Optional[float],
        max_connections: int,
        min_connections: Optional[int],
        purge_unused_connections_after: Optional[float],
        connection_timeout: Optional[float],
        cluster_events: Optional[ClusterEvents],
        purge_unealthy_nodes: bool,
        autobatching: bool,
        autobatching_max_keys: int,
        ssl: bool,
        ssl_verify: bool,
        ssl_extra_ca: Optional[str],
        username: Optional[str],
        password: Optional[str],
        autodiscovery: bool,
        autodiscovery_poll_interval: float,
        autodiscovery_timeout: float,
    ) -> None:

        if not node_addresses:
            raise ValueError("At least one memcached hosts needs to be provided")

        self._loop = asyncio.get_running_loop()
        self._cluster = Cluster(
            node_addresses,
            max_connections,
            min_connections,
            purge_unused_connections_after,
            connection_timeout,
            cluster_events,
            purge_unealthy_nodes,
            ssl,
            ssl_verify,
            ssl_extra_ca,
            username,
            password,
            autodiscovery,
            autodiscovery_poll_interval,
            autodiscovery_timeout,
            self._loop,
        )
        self._timeout = timeout
        self._closed = False

        if autobatching:
            # We generate 4 different autobatching instances, that would
            # be eligible depending on the parameters provided by the `get`
            # and the `gets`
            self._autobatching_noflags_nocas = AutoBatching(
                self,
                self._cluster,
                self._loop,
                return_flags=False,
                return_cas=False,
                timeout=self._timeout,
                max_keys=autobatching_max_keys,
            )
            self._autobatching_flags_nocas = AutoBatching(
                self,
                self._cluster,
                self._loop,
                return_flags=True,
                return_cas=False,
                timeout=self._timeout,
                max_keys=autobatching_max_keys,
            )
            self._autobatching_noflags_cas = AutoBatching(
                self,
                self._cluster,
                self._loop,
                return_flags=False,
                return_cas=True,
                timeout=self._timeout,
                max_keys=autobatching_max_keys,
            )
            self._autobatching_flags_cas = AutoBatching(
                self,
                self._cluster,
                self._loop,
                return_flags=True,
                return_cas=True,
                timeout=self._timeout,
                max_keys=autobatching_max_keys,
            )
            self._autobatching = True
        else:
            self._autobatching_noflags_nocas = None
            self._autobatching_flags_nocas = None
            self._autobatching_noflags_cas = None
            self._autobatching_flags_cas = None
            self._autobatching = False

    async def _storage_command(
        self, command: bytes, key: bytes, value: bytes, flags: int, exptime: int, noreply: bool, cas: int = None
    ) -> None:
        """Proxy function used for all storage commands `add`, `set`,
        `replace`, `append` and `prepend`.
        """
        if self._closed:
            raise RuntimeError("Emcache client is closed")

        if cas is not None and cas > MAX_ALLOWED_CAS_VALUE:
            raise ValueError(f"flags can not be higher than {MAX_ALLOWED_FLAG_VALUE}")

        if flags > MAX_ALLOWED_FLAG_VALUE:
            raise ValueError(f"flags can not be higher than {MAX_ALLOWED_FLAG_VALUE}")

        if cyemcache.is_key_valid(key) is False:
            raise ValueError("Key has invalid charcters")

        node = self._cluster.pick_node(key)
        async with OpTimeout(self._timeout, self._loop):
            async with node.connection() as connection:
                return await connection.storage_command(command, key, value, flags, exptime, noreply, cas)

    async def _incr_decr_command(self, command: bytes, key: bytes, value: int, noreply: bool) -> None:
        """Proxy function used for incr and decr."""
        if self._closed:
            raise RuntimeError("Emcache client is closed")

        if value < 0:
            raise ValueError("Incr or Decr by a positive value number expected")

        if cyemcache.is_key_valid(key) is False:
            raise ValueError("Key has invalid charcters")

        node = self._cluster.pick_node(key)
        async with OpTimeout(self._timeout, self._loop):
            async with node.connection() as connection:
                return await connection.incr_decr_command(command, key, value, noreply)

    async def _fetch_command(self, command: bytes, key: bytes) -> Optional[bytes]:
        """Proxy function used for all fetch commands `get`, `gets`"""
        if self._closed:
            raise RuntimeError("Emcache client is closed")

        if cyemcache.is_key_valid(key) is False:
            raise ValueError("Key has invalid charcters")

        node = self._cluster.pick_node(key)
        async with OpTimeout(self._timeout, self._loop):
            async with node.connection() as connection:
                return await connection.fetch_command(command, (key,))

    async def _fetch_many_command(
        self, command: bytes, keys: Sequence[bytes], return_flags=False
    ) -> Tuple[bytes, bytes, bytes]:
        """Proxy function used for all fetch many commands `get_many`, `gets_many`"""
        if self._closed:
            raise RuntimeError("Emcache client is closed")

        if not keys:
            return {}

        for key in keys:
            if cyemcache.is_key_valid(key) is False:
                raise ValueError("Key has invalid charcters")

        async def node_operation(node: Node, keys: List[bytes]):
            async with node.connection() as connection:
                return await connection.fetch_command(command, keys)

        tasks = [
            self._loop.create_task(node_operation(node, keys)) for node, keys in self._cluster.pick_nodes(keys).items()
        ]

        async with OpTimeout(self._timeout, self._loop):
            try:
                await asyncio.gather(*tasks)
            except Exception:
                # Any exception will invalidate any ongoing
                # task.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                raise

        return [task.result() for task in tasks]

    async def _get_and_touch_command(self, command: bytes, exptime: int, key: bytes) -> Optional[bytes]:
        """Proxy function used for all get_and_touch commands `gat`, `gats`"""
        if self._closed:
            raise RuntimeError("Emcache client is closed")

        if cyemcache.is_key_valid(key) is False:
            raise ValueError("Key has invalid charcters")

        node = self._cluster.pick_node(key)
        async with OpTimeout(self._timeout, self._loop):
            async with node.connection() as connection:
                return await connection.get_and_touch_command(command, exptime, (key,))

    async def _get_and_touch_many_command(
        self, command: bytes, exptime: int, keys: Sequence[bytes], return_flags=False
    ) -> Tuple[bytes, bytes, bytes]:
        """Proxy function used for all get_and_touch many commands `gat_many`, `gats_many`"""
        if self._closed:
            raise RuntimeError("Emcache client is closed")

        if not keys:
            return {}

        for key in keys:
            if cyemcache.is_key_valid(key) is False:
                raise ValueError("Key has invalid charcters")

        async def node_operation(node: Node, keys: List[bytes]):
            async with node.connection() as connection:
                return await connection.get_and_touch_command(command, exptime, keys)

        tasks = [
            self._loop.create_task(node_operation(node, keys)) for node, keys in self._cluster.pick_nodes(keys).items()
        ]

        async with OpTimeout(self._timeout, self._loop):
            try:
                await asyncio.gather(*tasks)
            except Exception:
                # Any exception will invalidate any ongoing
                # task.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                raise

        return [task.result() for task in tasks]

    @property
    def closed(self) -> bool:
        """Returns True if the client is already closed and no longer
        available to be used."""
        return self._closed

    async def close(self) -> None:
        """Close any active background task and close all TCP
        connections.

        It does not implement any graceful close at operation level,
        if there are active operations the outcome is not predictable.
        """
        if self._closed:
            return

        self._closed = True
        await self._cluster.close()

    def cluster_managment(self) -> ClusterManagment:
        """Returns the `ClusterManagment` instance class for managing
        the cluster related to that client.

        Same instance is returned at any call.
        """
        return self._cluster.cluster_managment

    async def get(self, key: bytes, return_flags=False) -> Optional[Item]:
        """Return the value associated with the key as an `Item` instance.

        If `return_flags` is set to True, the `Item.flags` attribute will be
        set with the value saved along the value will be returned, otherwise
        a None value will be set.

        If key is not found, a `None` value will be returned.

        If timeout is not disabled, an `asyncio.TimeoutError` will
        be returned in case of a timed out operation.
        """
        # route the execution to the Autobatching logic if its enabled
        if self._autobatching:
            if not return_flags:
                return await self._autobatching_noflags_nocas.execute(key)
            else:
                return await self._autobatching_flags_nocas.execute(key)

        keys, values, flags, _, client_error = await self._fetch_command(b"get", key)
        if client_error:
            raise CommandError(f"Command finished with error, response returned {client_error}")

        if key not in keys:
            return None

        if not return_flags:
            return Item(values[0], None, None)
        else:
            return Item(values[0], flags[0], None)

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
        # route the execution to the Autobatching logic if its enabled
        if self._autobatching:
            if not return_flags:
                return await self._autobatching_noflags_cas.execute(key)
            else:
                return await self._autobatching_flags_cas.execute(key)

        keys, values, flags, cas, client_error = await self._fetch_command(b"gets", key)
        if client_error:
            raise CommandError(f"Command finished with error, response returned {client_error}")

        if key not in keys:
            return None

        if not return_flags:
            return Item(values[0], None, cas[0])
        else:
            return Item(values[0], flags[0], cas[0])

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
        nodes_results = await self._fetch_many_command(b"get", keys, return_flags=return_flags)

        results = {}
        if not return_flags:
            for keys, values, flags, _, client_error in nodes_results:
                if client_error:
                    raise CommandError(f"Command finished with error, response returned {client_error}")
                for idx in range(len(keys)):
                    results[keys[idx]] = Item(values[idx], None, None)
        else:
            for keys, values, flags, _, client_error in nodes_results:
                if client_error:
                    raise CommandError(f"Command finished with error, response returned {client_error}")
                for idx in range(len(keys)):
                    results[keys[idx]] = Item(values[idx], flags[idx], None)

        return results

    async def gets_many(self, keys: Sequence[bytes], return_flags=False) -> Dict[bytes, Item]:
        """Return the values associated with the keys and their cas
        values.

        Take a look at the `get_many` command for parameters description.
        """
        nodes_results = await self._fetch_many_command(b"gets", keys, return_flags=return_flags)

        results = {}
        if not return_flags:
            for keys, values, flags, cas, client_error in nodes_results:
                if client_error:
                    raise CommandError(f"Command finished with error, response returned {client_error}")
                for idx in range(len(keys)):
                    results[keys[idx]] = Item(values[idx], None, cas[idx])
        else:
            for keys, values, flags, cas, client_error in nodes_results:
                if client_error:
                    raise CommandError(f"Command finished with error, response returned {client_error}")
                for idx in range(len(keys)):
                    results[keys[idx]] = Item(values[idx], flags[idx], cas[idx])

        return results

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
        result = await self._storage_command(b"set", key, value, flags, exptime, noreply)

        if not noreply and result != STORED:
            raise StorageCommandError(f"Command finished with error, response returned {result}")

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
        result = await self._storage_command(b"add", key, value, flags, exptime, noreply)

        if not noreply and result == NOT_STORED:
            raise NotStoredStorageCommandError()
        elif not noreply and result != STORED:
            raise StorageCommandError(f"Command finished with error, response returned {result}")

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
        result = await self._storage_command(b"replace", key, value, flags, exptime, noreply)

        if not noreply and result == NOT_STORED:
            raise NotStoredStorageCommandError()
        elif not noreply and result != STORED:
            raise StorageCommandError(f"Command finished with error, response returned {result}")

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
        # flags and exptime are not updated and are simply
        # ignored by Memcached.
        flags = 0
        exptime = 0

        result = await self._storage_command(b"append", key, value, flags, exptime, noreply)

        if not noreply and result == NOT_STORED:
            raise NotStoredStorageCommandError()
        elif not noreply and result != STORED:
            raise StorageCommandError(f"Command finished with error, response returned {result}")

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
        # flags and exptime are not updated and are simply
        # ignored by Memcached.
        flags = 0
        exptime = 0

        result = await self._storage_command(b"prepend", key, value, flags, exptime, noreply)

        if not noreply and result == NOT_STORED:
            raise NotStoredStorageCommandError()
        elif not noreply and result != STORED:
            raise StorageCommandError(f"Command finished with error, response returned {result}")

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
        result = await self._storage_command(b"cas", key, value, flags, exptime, noreply, cas=cas)

        if not noreply and result == EXISTS:
            raise NotStoredStorageCommandError()
        elif not noreply and result != STORED:
            raise StorageCommandError(f"Command finished with error, response returned {result}")

    async def increment(self, key: bytes, value: int, *, noreply: bool = False) -> Optional[int]:
        """Increment a specific integer stored with a key by a given `value`, the key
        must exist.

        If `noreply` is not used and the key exists the new value will be returned, otherwise
        a None is returned.

        If the command fails because the key was not found a
        `NotFoundCommandError` exception is raised.
        """
        result = await self._incr_decr_command(b"incr", key, value, noreply)

        if noreply:
            return

        if result == NOT_FOUND:
            raise NotFoundCommandError()

        return int(result)

    async def decrement(self, key: bytes, value: int, *, noreply: bool = False) -> Optional[int]:
        """Decrement a specific integer stored with a key by a given `value`, the key
        must exist.

        If `noreply` is not used and the key exists the new value will be returned, otherwise
        a None is returned.

        If the command fails because the key was not found a
        `NotFoundCommandError` exception is raised.
        """
        result = await self._incr_decr_command(b"decr", key, value, noreply)

        if noreply:
            return

        if result == NOT_FOUND:
            raise NotFoundCommandError()

        return int(result)

    async def touch(self, key: bytes, exptime: int, *, noreply: bool = False) -> None:
        """Set and override, if it's the case, the exptime for an existing key.

        If the command fails because the key was not found a
        `NotFoundCommandError` exception is raised. Other errors
        raised by the memcached server which imply that the item was
        not touched raise a generic `CommandError` exception.
        """
        if self._closed:
            raise RuntimeError("Emcache client is closed")

        if cyemcache.is_key_valid(key) is False:
            raise ValueError("Key has invalid charcters")

        node = self._cluster.pick_node(key)
        async with OpTimeout(self._timeout, self._loop):
            async with node.connection() as connection:
                result = await connection.touch_command(key, exptime, noreply)

        if noreply:
            return

        if result == NOT_FOUND:
            raise NotFoundCommandError()
        elif result != TOUCHED:
            raise CommandError(f"Command finished with error, response returned {result}")

        return

    async def delete(self, key: bytes, *, noreply: bool = False) -> None:
        """Delete an exixting key.

        If the command fails because the key was not found a
        `NotFoundCommandError` exception is raised. Other errors
        raised by the memcached server which imply that the item was
        not touched raise a generic `CommandError` exception.
        """
        if self._closed:
            raise RuntimeError("Emcache client is closed")

        if cyemcache.is_key_valid(key) is False:
            raise ValueError("Key has invalid charcters")

        node = self._cluster.pick_node(key)
        async with OpTimeout(self._timeout, self._loop):
            async with node.connection() as connection:
                result = await connection.delete_command(key, noreply)

        if noreply:
            return

        if result == NOT_FOUND:
            raise NotFoundCommandError()
        elif result != DELETED:
            raise CommandError(f"Command finished with error, response returned {result}")

        return

    async def flush_all(
        self,
        memcached_host_address: Union[MemcachedHostAddress, MemcachedUnixSocketPath],
        delay: int = 0,
        *,
        noreply: bool = False,
    ) -> None:
        """Flush all keys in a specific memcached host address.

        Flush can be deferred at memcached server side for a specific time by
        using the `delay` option, otherwise the flush will happen immediately.

        If the command fails a `CommandError` exception will be raised.
        """
        if self._closed:
            raise RuntimeError("Emcache client is closed")

        node = self._cluster.node(memcached_host_address)
        async with OpTimeout(self._timeout, self._loop):
            async with node.connection() as connection:
                result = await connection.flush_all_command(delay, noreply)

        if noreply:
            return

        if result != OK:
            raise CommandError(f"Command finished with error, response returned {result}")

        return

    async def version(
        self, memcached_host_address: Union[MemcachedHostAddress, MemcachedUnixSocketPath]
    ) -> Optional[str]:
        """Version is a command with no arguments:

        version\r\n

        In response, the server sends

        "VERSION <version>\r\n", where <version> is the version string for the
        server.
        """
        if self._closed:
            raise RuntimeError("Emcache client is closed")

        node = self._cluster.node(memcached_host_address)
        async with OpTimeout(self._timeout, self._loop):
            async with node.connection() as connection:
                result = await connection.version_command()

        if not result or not result.startswith(VERSION):
            raise CommandError(f"Command finished with error, response returned {result}")

        return result.removeprefix(b"VERSION ").decode()

    async def gat(self, exptime: int, key: bytes, return_flags=False) -> Optional[Item]:
        """Gat command is used to fetch item and update the
        expiration time of an existing item.
        Get And Touch.

        gat <exptime> <key>\r\n
        """
        keys, values, flags, _, client_error = await self._get_and_touch_command(b"gat", exptime, key)
        if client_error:
            raise CommandError(f"Command finished with error, response returned {client_error}")

        if key not in keys:
            return None

        if not return_flags:
            return Item(values[0], None, None)
        else:
            return Item(values[0], flags[0], None)

    async def gats(self, exptime: int, key: bytes, return_flags=False) -> Optional[Item]:
        """Gats command is used to fetch item and update the
        expiration time of an existing item.
        An alternative gat command for using with CAS

        gats <exptime> <key>\r\n
        """
        keys, values, flags, cas, client_error = await self._get_and_touch_command(b"gats", exptime, key)
        if client_error:
            raise CommandError(f"Command finished with error, response returned {client_error}")

        if key not in keys:
            return None

        if not return_flags:
            return Item(values[0], None, cas[0])
        else:
            return Item(values[0], flags[0], cas[0])

    async def gat_many(self, exptime: int, keys: Sequence[bytes], return_flags=False) -> Dict[bytes, Item]:
        """Return the values associated with the keys.
        Gat commands with many keys

        gat <exptime> <key>*\r\n
        """
        nodes_results = await self._get_and_touch_many_command(b"gat", exptime, keys, return_flags=return_flags)

        results = {}
        if not return_flags:
            for keys, values, flags, _, client_error in nodes_results:
                if client_error:
                    raise CommandError(f"Command finished with error, response returned {client_error}")
                for idx in range(len(keys)):
                    results[keys[idx]] = Item(values[idx], None, None)
        else:
            for keys, values, flags, _, client_error in nodes_results:
                if client_error:
                    raise CommandError(f"Command finished with error, response returned {client_error}")
                for idx in range(len(keys)):
                    results[keys[idx]] = Item(values[idx], flags[idx], None)

        return results

    async def gats_many(self, exptime: int, keys: Sequence[bytes], return_flags=False) -> Dict[bytes, Item]:
        """Return the values associated with the keys and their cas values.
        Gats commands with many keys

        gats <exptime> <key>*\r\n
        """
        nodes_results = await self._get_and_touch_many_command(b"gats", exptime, keys, return_flags=return_flags)

        results = {}
        if not return_flags:
            for keys, values, flags, cas, client_error in nodes_results:
                if client_error:
                    raise CommandError(f"Command finished with error, response returned {client_error}")
                for idx in range(len(keys)):
                    results[keys[idx]] = Item(values[idx], None, cas[idx])
        else:
            for keys, values, flags, cas, client_error in nodes_results:
                if client_error:
                    raise CommandError(f"Command finished with error, response returned {client_error}")
                for idx in range(len(keys)):
                    results[keys[idx]] = Item(values[idx], flags[idx], cas[idx])

        return results

    async def cache_memlimit(
        self, memcached_host_address: MemcachedHostAddress, value: int, *, noreply: bool = False
    ) -> None:
        """Cache_memlimit is a command with a numeric argument. This allows runtime
        adjustments of the cache memory limit. The argument is in megabytes, not bytes.
        """
        if self._closed:
            raise RuntimeError("Emcache client is closed")

        node = self._cluster.node(memcached_host_address)
        async with OpTimeout(self._timeout, self._loop):
            async with node.connection() as connection:
                result = await connection.cache_memlimit_command(value, noreply)

        if noreply:
            return

        if result != OK:
            raise CommandError(f"Command finished with error, response returned {result}")

        return

    async def stats(self, memcached_host_address: MemcachedHostAddress, *args: str) -> Dict[str, str]:
        """The memcached command via "stats" which show needed statistics about server.
        Client send without arguments - `stats\r\n`, with arguments - `stats <args>\r\n`.
        Depending on the arguments, the server will return statistics to you until it finishes `END\r\n`.
        Please see a lot of detailed information in the documentation.
        """
        if self._closed:
            raise RuntimeError("Emcache client is closed")

        node = self._cluster.node(memcached_host_address)
        async with OpTimeout(self._timeout, self._loop):
            async with node.connection() as connection:
                result = await connection.stats_command(*args)

        if not result or not result.endswith(END):
            raise CommandError(f"Command finished with error, response returned {result}")

        return dict(s.groups() for s in re.finditer(r"STAT (.+) (.+)\r\n", result.decode()))

    async def verbosity(
        self,
        memcached_host_address: Union[MemcachedHostAddress, MemcachedUnixSocketPath],
        level: int,
        *,
        noreply: bool = False,
    ) -> None:
        """Increase level of log verbosity for a memcached server.
        1 - print standard errors/warnings
        2 - also print client commands/responses
        3 - internal state transitions

        Send command "verbosity <level> [noreply]\r\n"
        Return always "OK\r\n" if skip noreply and correct command.
        """
        if self._closed:
            raise RuntimeError("Emcache client is closed")

        node = self._cluster.node(memcached_host_address)
        async with OpTimeout(self._timeout, self._loop):
            async with node.connection() as connection:
                result = await connection.verbosity_command(level, noreply)

        if noreply:
            return

        if result != OK:
            raise CommandError(f"Command finished with error, response returned {result}")

        return

    def pipeline(self, memcached_host_address: Union[MemcachedHostAddress, MemcachedUnixSocketPath]) -> "_Pipeline":
        return _Pipeline(memcached_host_address, self)


class _Pipeline(Pipeline):
    def __init__(
        self, memcached_host_address: Union[MemcachedHostAddress, MemcachedUnixSocketPath], client: _Client
    ) -> None:
        self.memcached_host_address = memcached_host_address
        self.client = client
        self.stack = []

    async def __aenter__(self: "_Pipeline") -> "_Pipeline":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stack = []

    def _retrieval_command(self, command: bytes, keys: Sequence[bytes]) -> bytes:
        """Proxy function used for all fetch many commands `get_many`, `gets_many`"""
        for key in keys:
            if cyemcache.is_key_valid(key) is False:
                raise ValueError("Key has invalid charcters")
        return b"%b %b\r\n" % (command, b" ".join(keys))

    def _storage_command(
        self, command: bytes, key: bytes, value: bytes, flags: int, exptime: int, noreply: bool, cas: int = None
    ) -> bytes:
        """Proxy function used for all storage commands `add`, `set`,
        `replace`, `append` and `prepend`.
        """
        if cas is not None and cas > MAX_ALLOWED_CAS_VALUE:
            raise ValueError(f"flags can not be higher than {MAX_ALLOWED_FLAG_VALUE}")

        if flags > MAX_ALLOWED_FLAG_VALUE:
            raise ValueError(f"flags can not be higher than {MAX_ALLOWED_FLAG_VALUE}")

        if cyemcache.is_key_valid(key) is False:
            raise ValueError("Key has invalid charcters")

        if cas is not None:
            extra = b" %a" % cas if not noreply else b" %a noreply" % cas
        else:
            extra = b"" if not noreply else b" noreply"

        return b"%b %b %a %a %a%b\r\n%b\r\n" % (command, key, flags, exptime, len(value), extra, value)

    def _incr_decr_command(self, command: bytes, key: bytes, value: int, noreply: bool) -> bytes:
        """Proxy function used for incr and decr."""
        if value < 0:
            raise ValueError("Incr or Decr by a positive value number expected")

        if cyemcache.is_key_valid(key) is False:
            raise ValueError("Key has invalid charcters")

        return b"%b %b %a%b\r\n" % (command, key, value, b" noreply" if noreply else b"")

    def _get_and_touch_command(self, command: bytes, exptime: int, keys: Sequence[bytes]) -> bytes:
        """Proxy function used for all get_and_touch many commands `gat_many`, `gats_many`"""
        for key in keys:
            if cyemcache.is_key_valid(key) is False:
                raise ValueError("Key has invalid charcters")
        return b"%b %a %b\r\n" % (command, exptime, b" ".join(keys))

    async def execute(self) -> list[str | dict]:
        """Accumulate commands and push on memcached server."""
        if self.client._closed:
            raise RuntimeError("Emcache client is closed")

        commands = b"".join(self.stack)
        node = self.client._cluster.node(self.memcached_host_address)
        async with OpTimeout(self.client._timeout, self.client._loop):
            async with node.connection() as connection:
                result = await connection._extract_pipeline_data(commands)

        response = []

        for i in regex_memcached_responses.finditer(result.decode()):
            groups = i.groupdict()
            if error_message := groups["error_message"]:
                response.append(error_message)
            elif client_error := groups["client_error"]:
                response.append(client_error)
            elif groups["error"]:
                response.append("ERROR")
            elif groups["stored"]:
                response.append("STORED")
            elif groups["not_stored"]:
                response.append("NOT_STORED")
            elif groups["exists"]:
                response.append("EXISTS")
            elif groups["not_found"]:
                response.append("NOT_FOUND")
            elif groups["end"]:
                response.append("END")
            elif groups["value"]:
                response.append(
                    Item(groups["data"], int(groups["flags"]), int(groups["cas"]) if groups["cas"] else None)
                )
            elif groups["deleted"]:
                response.append("DELETED")
            elif groups["touched"]:
                response.append("TOUCHED")
            elif version := groups["version"]:
                response.append(version.removeprefix("VERSION "))
            elif groups["ok"]:
                response.append("OK")
            elif stats := groups["stats"]:
                response.append(dict(s.groups() for s in re.finditer(r"STAT (.+) (.+)\r\n", stats)))
            elif incr_decr_value := groups["incr_decr_value"]:
                response.append(int(incr_decr_value))
            else:
                ...

        self.stack = []
        return response

    def get(self, key: bytes) -> "_Pipeline":
        """Push memcached `get` command in stack."""
        self.stack.append(self._retrieval_command(b"get", (key,)))
        return self

    def gets(self, key: bytes) -> "_Pipeline":
        """Push memcached `gets` command in stack."""
        self.stack.append(self._retrieval_command(b"gets", (key,)))
        return self

    def get_many(self, keys: Sequence[bytes]) -> "_Pipeline":
        """Push memcached `get*` command in stack."""
        self.stack.append(self._retrieval_command(b"get", keys))
        return self

    def gets_many(self, keys: Sequence[bytes]) -> "_Pipeline":
        """Push memcached `gets*` command in stack."""
        self.stack.append(self._retrieval_command(b"gets", keys))
        return self

    def set(self, key: bytes, value: bytes, *, flags: int = 0, exptime: int = 0, noreply: bool = False) -> "_Pipeline":
        """Push memcached `set` command in stack."""
        self.stack.append(self._storage_command(b"set", key, value, flags, exptime, noreply))
        return self

    def add(self, key: bytes, value: bytes, *, flags: int = 0, exptime: int = 0, noreply: bool = False) -> "_Pipeline":
        """Push memcached `add` command in stack."""
        self.stack.append(self._storage_command(b"add", key, value, flags, exptime, noreply))
        return self

    def replace(
        self, key: bytes, value: bytes, *, flags: int = 0, exptime: int = 0, noreply: bool = False
    ) -> "_Pipeline":
        """Push memcached `replace` command in stack."""
        self.stack.append(self._storage_command(b"replace", key, value, flags, exptime, noreply))
        return self

    def append(self, key: bytes, value: bytes, *, noreply: bool = False) -> "_Pipeline":
        """Push memcached `append` command in stack."""
        self.stack.append(self._storage_command(b"append", key, value, 0, 0, noreply))
        return self

    def prepend(self, key: bytes, value: bytes, *, noreply: bool = False) -> "_Pipeline":
        """Push memcached `prepend` command in stack."""
        self.stack.append(self._storage_command(b"prepend", key, value, 0, 0, noreply))
        return self

    def cas(
        self, key: bytes, value: bytes, cas: int, *, flags: int = 0, exptime: int = 0, noreply: bool = False
    ) -> "_Pipeline":
        """Push memcached `cas` command in stack."""
        self.stack.append(self._storage_command(b"cas", key, value, flags, exptime, noreply, cas))
        return self

    def increment(self, key: bytes, value: int, *, noreply: bool = False) -> "_Pipeline":
        """Push memcached `increment` command in stack."""
        self.stack.append(self._incr_decr_command(b"incr", key, value, noreply))
        return self

    def decrement(self, key: bytes, value: int, *, noreply: bool = False) -> "_Pipeline":
        """Push memcached `decrement` command in stack."""
        self.stack.append(self._incr_decr_command(b"decr", key, value, noreply))
        return self

    def touch(self, key: bytes, exptime: int, *, noreply: bool = False) -> "_Pipeline":
        """Push memcached `touch` command in stack."""
        self.stack.append(b"touch %b %a%b\r\n" % (key, exptime, b" noreply" if noreply else b""))
        return self

    def delete(self, key: bytes, *, noreply: bool = False) -> "_Pipeline":
        """Push memcached `delete` command in stack."""
        self.stack.append(b"delete %b%b\r\n" % (key, b" noreply" if noreply else b""))
        return self

    def flush_all(self, delay: int = 0, *, noreply: bool = False) -> "_Pipeline":
        """Push memcached `flush_all` command in stack."""
        self.stack.append(b"flush_all %a%b\r\n" % (delay, b" noreply" if noreply else b""))
        return self

    def version(self) -> "_Pipeline":
        """Push memcached `version` command in stack."""
        self.stack.append(b"version\r\n")
        return self

    def gat(self, exptime: int, key: bytes) -> "_Pipeline":
        """Push memcached `gat` command in stack."""
        self.stack.append(self._get_and_touch_command(b"gat", exptime, (key,)))
        return self

    def gats(self, exptime: int, key: bytes) -> "_Pipeline":
        """Push memcached `gats` command in stack."""
        self.stack.append(self._get_and_touch_command(b"gats", exptime, (key,)))
        return self

    def gat_many(self, exptime: int, keys: Sequence[bytes]) -> "_Pipeline":
        """Push memcached `gat*` command in stack."""
        self.stack.append(self._get_and_touch_command(b"gat", exptime, keys))
        return self

    def gats_many(self, exptime: int, keys: Sequence[bytes]) -> "_Pipeline":
        """Push memcached `gats*` command in stack."""
        self.stack.append(self._get_and_touch_command(b"gats", exptime, keys))
        return self

    def cache_memlimit(self, value: int, *, noreply: bool = False) -> "_Pipeline":
        """Push memcached `cache_memlimit` command in stack."""
        self.stack.append(b"cache_memlimit %a%b\r\n" % (value, b" noreply" if noreply else b""))
        return self

    def stats(self, *args: str) -> "_Pipeline":
        """Push memcached `stats` command in stack."""
        self.stack.append(b"stats %b\r\n" % " ".join(args).encode() if args else b"stats\r\n")
        return self

    def verbosity(self, level: int, *, noreply: bool = False) -> "_Pipeline":
        """Push memcached `verbosity` command in stack."""
        self.stack.append(b"verbosity %a%b\r\n" % (level, b" noreply" if noreply else b""))
        return self


async def create_client(
    node_addresses: Sequence[Union[MemcachedHostAddress, MemcachedUnixSocketPath]],
    *,
    timeout: Optional[float] = DEFAULT_TIMEOUT,
    max_connections: int = DEFAULT_MAX_CONNECTIONS,
    min_connections: Optional[int] = DEFAULT_MIN_CONNECTIONS,
    purge_unused_connections_after: Optional[float] = DEFAULT_PURGE_UNUSED_CONNECTIONS_AFTER,
    connection_timeout: Optional[float] = DEFAULT_CONNECTION_TIMEOUT,
    cluster_events: Optional[ClusterEvents] = None,
    purge_unhealthy_nodes: bool = DEFAULT_PURGE_UNHEALTHY_NODES,
    autobatching: bool = DEFAULT_AUTOBATCHING_ENABLED,
    autobatching_max_keys: int = DEFAULT_AUTOBATCHING_MAX_KEYS,
    ssl: bool = DEFAULT_SSL,
    ssl_verify: bool = DEFAULT_SSL_VERIFY,
    ssl_extra_ca: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    autodiscovery: bool = False,
    autodiscovery_poll_interval: float = DEFAULT_AUTODISCOVERY_POLL_INTERVAL,
    autodiscovery_timeout: float = DEFAULT_AUTODISCOVERY_TIMEOUT,
) -> Client:
    """Factory for creating a new `emcache.Client` instance.

    By default emcache client will be created with the following default values.

    An enabled timeout per operation configured to `DEFAULT_TIMEOUT`, for disabling it pass a `None`
    value to the `tiemout` keyword argument.

    A maximum number of TCP connections per Node to `DEFAULT_MAX_CONNECTIONS`.

    A minimum number of TCP connections per Node to `DEFAULT_MIN_CONNECTIONS`. The number of opened
    connections should fluctuate in normal circumstances between `min_connections` and `max_connections`.

    Purge unused TCP connections after being unused to `DEFAULT_PURGE_UNUSED_CONNECTIONS_AFTER` seconds, for
    disabling purging pass a `None` value to the `purge_unused_connections_after` keyword argument.

    Connection timeout for each new TCP connection, for disabling connection timeout pass a `None` value to the
    `connection_timeout` keyword argument.

    For receiving cluster events you must provide a valid `ClusterEvents` instance class.

    `purge_unealthy_nodes` can be used for avoid keep sending traffic to nodes that have been marked as
    unhealthy, by default this is disabled and for enabling it you should give a `True` value. This option
    should be enabled considering your specific use case, considering that nodes that are reported still
    healthy might receive more traffic and the hit/miss ratio might be affected. For more information you
    should take a look to the documentation.

    `autobatching` if enabled, not by default, provides a way for executing the get and gets operations
    using batches. Operations routed are piled up until the next loop iteracion, which one reached sends
    the batch - or multiple of them - using single commands. Performance for the `get` and `gets` operation
    can boost up x2/x3

    `autobatching_max_keys` when autobatching is used defines the maximum number of keys that would be send
    within the same batch, by default 32 keys.

    `ssl` if enabled an SSL connection to the Memcached hosts will be negotiated. By default False.

    `ssl_verify` if enabled certificate provided by Memcached servers will be verified. By default True.

    `ssl_extra_ca` By default None. You can provide an extra absolute file path where a new CA file
    can be loaded.

    `username` By default None. Used for authentication by SASL with username.
    Params username and password are used together.

    `password` By default None. Used for authentication by SASL with password.
    Params username and password are used together.

    `autodiscovery` if enabled the client will automatically call `config get cluster` and update node list.
    By default, False.

    `autodiscovery_poll_interval` when autodiscovery is enabled how frequently to check for node updates.
    By default, 60s.

    `autodiscovery_timeout` the timeout for the `config get cluster` command. By default, 5s.
    """
    # check SSL availability earlier, protocol which is the one that will use
    # it when connections are created in background won't need to deal with this
    # check.
    if ssl:
        try:
            import ssl as _  # noqa
        except ImportError:
            raise ValueError("SSL can not be enabled, no Python SSL module found")

    # check exists username and password or not exists, together, for SASL authentication
    if not ((username and password) or (not username and not password)):
        raise ValueError("For SASL authentication either username and password together")

    client = _Client(
        node_addresses,
        timeout,
        max_connections,
        min_connections,
        purge_unused_connections_after,
        connection_timeout,
        cluster_events,
        purge_unhealthy_nodes,
        autobatching,
        autobatching_max_keys,
        ssl,
        ssl_verify,
        ssl_extra_ca,
        username,
        password,
        autodiscovery,
        autodiscovery_poll_interval,
        autodiscovery_timeout,
    )

    if autodiscovery:
        await asyncio.wait_for(client._cluster._first_autodiscovery_done, DEFAULT_STARTUP_WAIT_AUTODISCOVERY)

    return client
