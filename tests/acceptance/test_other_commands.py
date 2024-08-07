# MIT License
# Copyright (c) 2020-2024 Pau Freixes

import asyncio
import sys

import pytest

from emcache import NotFoundCommandError

pytestmark = pytest.mark.asyncio


class TestIncr:
    async def test_incr(self, client, key_generation):
        key = next(key_generation)

        # incr a value for a key that does not exist must fail
        with pytest.raises(NotFoundCommandError):
            await client.increment(key, 1)

        # set the new key and increment the value.
        await client.set(key, b"1")
        value = await client.increment(key, 1)

        assert value == 2

    @pytest.mark.skipif(sys.platform == "darwin", reason="https://github.com/memcached/memcached/issues/681")
    async def test_incr_noreply(self, client, key_generation):
        key = next(key_generation)

        # set the new key and increment the value using noreply
        await client.set(key, b"1")
        value = await client.increment(key, 1, noreply=True)

        # when noreply is used a None is returned
        assert value is None

        item = await client.get(key)

        assert item.value == b"2"


class TestDecr:
    async def test_decr(self, client, key_generation):
        key = next(key_generation)

        # decr a value for a key that does not exist must fail
        with pytest.raises(NotFoundCommandError):
            await client.decrement(key, 1)

        # set the new key and decrement the value.
        await client.set(key, b"2")
        value = await client.decrement(key, 1)

        assert value == 1

    @pytest.mark.skipif(sys.platform == "darwin", reason="https://github.com/memcached/memcached/issues/681")
    async def test_decr_noreply(self, client, key_generation):
        key = next(key_generation)

        # set the new key and decrement the value using noreply
        await client.set(key, b"2")
        value = await client.decrement(key, 1, noreply=True)

        # when noreply is used a None is always returned
        assert value is None

        item = await client.get(key)

        assert item.value == b"1"


class TestTouch:
    async def test_touch(self, client, key_generation):
        key_and_value = next(key_generation)

        # touch a key that does not exist must fail
        with pytest.raises(NotFoundCommandError):
            await client.touch(key_and_value, -1)

        # set the new key and make it expire using touch.
        await client.set(key_and_value, key_and_value)
        await client.touch(key_and_value, -1)

        item = await client.get(key_and_value)
        assert item is None

    @pytest.mark.skipif(sys.platform == "darwin", reason="https://github.com/memcached/memcached/issues/681")
    async def test_touch_noreply(self, client, key_generation):
        key_and_value = next(key_generation)

        # set the new key and make it expire using touch.
        await client.set(key_and_value, key_and_value)
        await client.touch(key_and_value, -1, noreply=True)

        item = await client.get(key_and_value)
        assert item is None


class TestDelete:
    async def test_delete(self, client, key_generation):
        key_and_value = next(key_generation)

        # delete a key that does not exist must fail
        with pytest.raises(NotFoundCommandError):
            await client.delete(key_and_value)

        # set the new key and delete it.
        await client.set(key_and_value, key_and_value)
        await client.delete(key_and_value)

        item = await client.get(key_and_value)
        assert item is None

    @pytest.mark.skipif(sys.platform == "darwin", reason="https://github.com/memcached/memcached/issues/681")
    async def test_delete_noreply(self, client, key_generation):
        key_and_value = next(key_generation)

        # set the new key and delete it.
        await client.set(key_and_value, key_and_value)
        await client.delete(key_and_value, noreply=True)

        item = await client.get(key_and_value)
        assert item is None


class TestFlushAll:
    @pytest.mark.skipif(sys.platform == "darwin", reason="https://github.com/memcached/memcached/issues/681")
    @pytest.mark.parametrize("noreply", [False, True])
    async def test_flush_all(self, client, key_generation, node_addresses, noreply):
        key_and_value = next(key_generation)

        # set a new key and value.
        await client.set(key_and_value, key_and_value)

        # flush all for all of the servers
        for node_address in node_addresses:
            await client.flush_all(node_address, noreply=noreply)

        # item should not be found.
        item = await client.get(key_and_value)
        assert item is None

    async def test_flush_all_with_delay(self, client, key_generation, node_addresses):
        key_and_value = next(key_generation)

        # set a new key and value.
        await client.set(key_and_value, key_and_value)

        # flush all for all of the servers
        for node_address in node_addresses:
            await client.flush_all(node_address, delay=2)

        # item should be found.
        item = await client.get(key_and_value)
        assert item is not None

        # wait for delay time
        await asyncio.sleep(2)

        # item should not be found.
        item = await client.get(key_and_value)
        assert item is None


class TestVersion:
    async def test_version(self, client, node_addresses):
        for node_address in node_addresses:
            assert isinstance(await client.version(node_address), str)


class TestCacheMemlimit:
    @pytest.mark.parametrize("noreply", [False, True])
    async def test_cache_memlimit(self, client, node_addresses, noreply):
        # set cache limit for selected of the servers
        for node_address in node_addresses:
            assert await client.cache_memlimit(node_address, 64, noreply=noreply) is None


class TestStats:
    async def test_stats(self, client, node_addresses):
        for node_address in node_addresses:
            default_stats = await client.stats(node_address)
            assert default_stats["version"]
            settings_stats = await client.stats(node_address, "settings")
            assert settings_stats["verbosity"]
            args_stats = await client.stats(node_address, "settings", "items")
            assert args_stats["verbosity"]


class TestVerbosity:
    @pytest.mark.parametrize("noreply", [False, True])
    async def test_verbosity(self, client, node_addresses, noreply):
        for node_address in node_addresses:
            assert await client.verbosity(node_address, 2, noreply=noreply) is None


class TestPipeline:
    async def test_pipeline(self, client, node_addresses):
        count_commands = 17
        for node_address in node_addresses:
            async with client.pipeline(node_address) as pipe:
                (
                    pipe.version()
                    .version()
                    .version()
                    .stats()
                    .stats()
                    .stats()
                    .stats()
                    .version()
                    .get(b"key")
                    .delete(b"key")
                    .get(b"key")
                    .set(b"key", b"value")
                    .get(b"key")
                    .delete(b"key")
                    .version()
                    .stats()
                    .stats()
                )
                result = await pipe.execute()
            assert count_commands == len(result)

    async def test_pipeline_all_commands(self, client, node_addresses):
        count_commands = 22
        for node_address in node_addresses:
            async with client.pipeline(node_address) as pipe:
                (
                    pipe.get(b"key")
                    .gets(b"key")
                    .get_many((b"key1", b"key2"))
                    .gets_many((b"key1", b"key2"))
                    .gat(0, b"key")
                    .gats(0, b"key")
                    .gat_many(0, (b"key1", b"key2"))
                    .gats_many(0, (b"key1", b"key2"))
                    .set(b"key", b"value")
                    .add(b"key", b"value")
                    .replace(b"key", b"value")
                    .append(b"key", b"value")
                    .prepend(b"key", b"value")
                    .cas(b"key", b"value", 0)
                    .increment(b"key", 1)
                    .decrement(b"key", 1)
                    .touch(b"key", 1)
                    .delete(b"key")
                    .flush_all()
                    .version()
                    .stats()
                    .verbosity(1)
                )
                result = await pipe.execute()

        assert count_commands == len(result)
