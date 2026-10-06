"""Resolve source cache state and manage cancellable concurrent completeness checks."""

from __future__ import annotations

from collections import deque


def source_accessor(element):
    for attr in ("_Element__sources", "_sources", "sources"):
        try:
            obj = getattr(element, attr, None)
        except Exception:
            continue

        if obj is not None and hasattr(obj, "get_files"):
            return obj

    if hasattr(element, "get_files"):
        return element

    return None


def source_count(element, accessor):
    try:
        return len(accessor)
    except Exception:
        pass

    for obj in (accessor, element):
        if obj is None:
            continue

        for attr in ("_sources", "sources", "_Element__sources"):
            try:
                value = getattr(obj, attr, None)
            except Exception:
                continue

            if isinstance(value, (list, tuple, set)):
                return len(value)

    return None


def _call_get_files(accessor):
    getter = getattr(accessor, "get_files", None)
    if getter is None:
        return None

    return getter() if callable(getter) else getter


def load_source_directory(element):
    accessor = source_accessor(element)
    if accessor is None:
        return None, "nosources", None

    count = source_count(element, accessor)
    if count == 0:
        return None, "nosources", None

    # need_state=False avoids artifact state, so initialize source keys explicitly.
    update = getattr(accessor, "update_resolved_state", None)
    if callable(update):
        try:
            update()
            if not accessor.is_resolved():
                return (
                    None,
                    "unresolved",
                    "sources have no ref; configure refs or run bst source track",
                )
        except Exception as exc:
            return None, "uncached", str(exc)

    try:
        query = getattr(element, "_query_source_cache", None)
    except Exception as exc:
        return None, "uncached", str(exc)

    if callable(query):
        try:
            query()
        except Exception as exc:
            return None, "uncached", str(exc)

    try:
        cached_value = getattr(element, "_cached_sources", None)
    except Exception as exc:
        return None, "uncached", str(exc)

    if cached_value is not None:
        try:
            cached = cached_value() if callable(cached_value) else cached_value
        except Exception as exc:
            return None, "uncached", str(exc)

        if not cached:
            if count is None:
                try:
                    files = _call_get_files(accessor)
                    if files is not None:
                        return files, "ok", None
                except Exception:
                    pass

            return None, "uncached", None

    try:
        files = _call_get_files(accessor)
    except Exception as exc:
        return None, "uncached", str(exc)

    if files is None:
        return None, "uncached", "get_files() returned None"

    return files, "ok", None


def prefetch_source_cache_state(elements, ctx, workers):
    """
    Answer BuildStream's per-element source cache checks from concurrent ones.

    ElementSources.query_cache() verifies each tree with a casd FetchTree call
    that stats every file blob; done serially per element it dominates load on
    large projects. Issue the same local-only FetchTree once per unique tree,
    with up to `workers` RPCs in flight, and serve CASCache.contains_directory()
    from the results. RPC futures are awaited on this thread and all of them are
    cancelled if the wait is interrupted, so Ctrl-C never waits on casd. Only
    the definite answers (OK, NOT_FOUND) are recorded; anything else, or a
    remote cache, leaves BuildStream's own call in place.
    Returns the number of trees checked.
    """
    try:
        import grpc
        from buildstream._protos.build.buildgrid import local_cas_pb2

        cas = ctx.get_cascache()
        original = cas.contains_directory
        if getattr(cas, "_remote_cache", True):
            return 0
        local_cas = cas._casd.get_local_cas()
    except Exception:
        return 0

    digests = {}
    for element in elements:
        try:
            accessor = source_accessor(element)
            if accessor is None or not source_count(element, accessor):
                continue
            accessor.update_resolved_state()
            if not accessor.is_resolved():
                continue
            proto = accessor._elementsourcescache.load_proto(accessor)
        except Exception:
            continue
        if proto is not None:
            digests[(proto.files.hash, proto.files.size_bytes)] = proto.files

    if len(digests) < 2:
        return 0

    def start(digest):
        request = local_cas_pb2.FetchTreeRequest()
        request.root_digest.CopyFrom(digest)
        request.fetch_file_blobs = True  # same as BuildStream without a remote cache
        return local_cas.FetchTree.future(request)

    results = {}
    queue = deque(digests.items())
    in_flight = deque()
    try:
        while queue or in_flight:
            while queue and len(in_flight) < workers:
                key, digest = queue.popleft()
                in_flight.append((key, start(digest)))
            # Leave the awaited RPC in in_flight so an interrupt cancels it too.
            key, future = in_flight[0]
            try:
                future.result()
                results[key] = True
            except grpc.RpcError as exc:
                if exc.code() == grpc.StatusCode.NOT_FOUND:
                    results[key] = False
            in_flight.popleft()
    finally:
        for _, future in in_flight:
            future.cancel()

    def contains_directory(digest, *args, **kwargs):
        known = results.get((digest.hash, digest.size_bytes))
        if known is None or args or kwargs:
            return original(digest, *args, **kwargs)
        return known

    cas.contains_directory = contains_directory
    return len(digests)


CACHE_CHECK_WORKERS = 8
