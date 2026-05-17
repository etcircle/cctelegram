"""Inbound aggregator — coalesce Telegram messages into one Claude turn (§2.8).

A single user intent often arrives as multiple Telegram updates: a media-group
of photos with one caption, a photo followed by descriptive text, a
caption-then-followup pair. Forwarding each update independently fragments
context across multiple Claude turns and (for media-groups) attaches the
caption to whichever photo arrived first, leaving the rest contextless.

This module buffers offers per route and flushes on a debounce window or on a
max-attachment cap. The flushed string follows the §2.8.2 shape: the user's
typed text once, then a single ``(attachments: …)`` block with all paths in
arrival order. The caption is never repeated per attachment.

Public surface:
  - ``aggregator_offer_text(route, text)``
  - ``aggregator_offer_voice(route, transcribed_text)``
  - ``aggregator_offer_photo(route, path, caption, media_group_id)``
  - ``aggregator_offer_document(route, path, caption, media_group_id)``
  - ``aggregator_replay_payload(route, text, attachments)`` — sync replay
  - ``aggregator_flush_route(route)`` — public force-flush, returns delivery ok
  - ``aggregator_clear_route(route)`` — teardown hook (cancels pending flush)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..config import config
from ..session import session_manager

logger = logging.getLogger(__name__)


Route = tuple[int, int, str]


@dataclass
class _PendingBundle:
    text_parts: list[str] = field(default_factory=list)
    attachment_paths: list[Path] = field(default_factory=list)
    flush_handle: asyncio.TimerHandle | None = None
    # Track the current media-group so a transition to a different group's
    # first attachment can force-flush the bundle in progress. Telegram
    # delivers media-group items within milliseconds, but two distinct
    # groups arriving inside the same debounce window must NOT merge — that
    # would attach group-2's caption to group-1's images.
    current_media_group_id: str | None = None
    # Caption dedup: Telegram repeats the same caption on every item of a
    # media-group when the user sets it on the album, so without dedup we
    # emit the caption N times.
    seen_captions: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class AggregatorReplayAttachment:
    """Attachment metadata for deterministic pending first-turn replay."""

    path: Path
    caption: str | None = None
    media_group_id: str | None = None


# Per-route pending bundle. Mutation guarded by ``_route_locks[route]`` so the
# flush callback and the offer paths can't race the same bundle's attachments
# / text-parts list.
_route_pending: dict[Route, _PendingBundle] = {}
_route_locks: dict[Route, asyncio.Lock] = {}

# Strong refs for fire-and-forget tasks. Without this, the GC can collect a
# task before it completes (cpython#91887) — most likely under load, exactly
# when boundary force-flushes fire.
_background_tasks: set[asyncio.Task[object]] = set()


def _spawn_background(coro: Coroutine[object, object, object]) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _get_lock(route: Route) -> asyncio.Lock:
    lock = _route_locks.get(route)
    if lock is None:
        lock = asyncio.Lock()
        _route_locks[route] = lock
    return lock


def _get_or_create_bundle(route: Route) -> _PendingBundle:
    bundle = _route_pending.get(route)
    if bundle is None:
        bundle = _PendingBundle()
        _route_pending[route] = bundle
    return bundle


def _cancel_handle(bundle: _PendingBundle) -> None:
    handle = bundle.flush_handle
    bundle.flush_handle = None
    if handle is not None:
        handle.cancel()


def _schedule_flush(route: Route, bundle: _PendingBundle) -> None:
    """(Re)schedule the debounced flush for this route's bundle.

    Only ever invoked from inside ``async with lock`` in the offer paths,
    so the loop is guaranteed running and ``get_running_loop`` is safe.
    """
    _cancel_handle(bundle)
    loop = asyncio.get_running_loop()
    delay = max(0.0, config.aggregator_debounce_seconds)

    def _fire() -> None:
        # Schedule the flush coroutine on the running loop; the TimerHandle
        # callback itself runs sync.
        _spawn_background(_flush(route))

    bundle.flush_handle = loop.call_later(delay, _fire)


def _format_bundle(bundle: _PendingBundle) -> str:
    """Render the §2.8.2 output shape for a bundle."""
    text_block = "\n\n".join(part for part in bundle.text_parts if part)
    if bundle.attachment_paths:
        path_lines = "\n".join(f"  - {path}" for path in bundle.attachment_paths)
        attached_block = f"(attachments:\n{path_lines})"
    else:
        attached_block = ""

    if text_block and attached_block:
        return f"{text_block}\n\n{attached_block}"
    if text_block:
        return text_block
    return attached_block


def _pop_bundle_locked(route: Route) -> _PendingBundle | None:
    """Pop and disarm the route's pending bundle.

    Caller is responsible for holding ``_get_lock(route)``. Split out from
    ``_flush`` so the public ``aggregator_flush_route`` can pop and send
    without re-entering the lock — ``asyncio.Lock`` is non-reentrant, and
    while the previous "lock → release → call _flush which re-locks" shape
    didn't formally deadlock, it left a window where another offer path
    could race in and rebuild the bundle between the cancel and the send.
    """
    bundle = _route_pending.pop(route, None)
    if bundle is not None:
        _cancel_handle(bundle)
    return bundle


async def _send_bundle(route: Route, bundle: _PendingBundle) -> bool:
    """Render and send a popped bundle. Caller must NOT hold the route lock."""
    text_to_send = _format_bundle(bundle)
    if not text_to_send:
        return True

    window_id = route[2]
    try:
        success, message = await session_manager.send_to_window(window_id, text_to_send)
        if not success:
            logger.warning(
                "aggregator flush send_to_window failed for route %s: %s",
                route,
                message,
            )
            return False
    except Exception as exc:
        logger.error(
            "aggregator flush raised for route %s: %s",
            route,
            exc,
        )
        return False

    # Closes the gap between "prompt accepted" and "first transcript event":
    # the V2 typing loop only refreshes RUNNING / RUNNING_TOOL routes, so
    # without this mark the indicator was dark during preliminary work.
    if config.busy_indicator_v2:
        from . import busy_indicator

        await busy_indicator.mark_inbound_sent(route)
    return True


async def _flush(route: Route) -> bool:
    """Send the buffered bundle to the bound tmux window and clear it."""
    async with _get_lock(route):
        bundle = _pop_bundle_locked(route)

    if bundle is None:
        return True

    return await _send_bundle(route, bundle)


async def aggregator_offer_text(route: Route, text: str) -> None:
    """Append a text part to the route's bundle and (re)schedule flush.

    The aggregator is intentionally independent of
    ``config.reply_context_enabled``. The kill switch only governs the
    quote→prompt rendering and the outbound ``reply_parameters`` anchor —
    bundling Telegram updates into one Claude turn is correct in both modes.
    """
    if not text:
        return
    lock = _get_lock(route)
    async with lock:
        bundle = _get_or_create_bundle(route)
        bundle.text_parts.append(text)
        _schedule_flush(route, bundle)


async def aggregator_offer_voice(route: Route, transcribed_text: str) -> None:
    """Voice transcripts ride the same path as text."""
    await aggregator_offer_text(route, transcribed_text)


async def _offer_attachment(
    route: Route,
    path: Path,
    caption: str | None,
    media_group_id: str | None,
) -> None:
    """Append an attachment (and any caption) to the route's bundle.

    When ``len(attachment_paths)`` reaches the configured cap the bundle is
    force-flushed immediately rather than waiting on the debounce — keeps an
    unbounded media dump from sitting in memory.
    """
    lock = _get_lock(route)
    flush_now = False
    async with lock:
        bundle = _get_or_create_bundle(route)

        # Boundary force-flush: a new media-group arriving inside the
        # debounce window must not merge with the previous group's items.
        # Pop and dispatch the in-progress bundle without awaiting (the
        # lock is non-reentrant and ``_send_bundle`` does network IO),
        # then start a fresh bundle under the same lock acquisition.
        if (
            media_group_id is not None
            and bundle.current_media_group_id is not None
            and media_group_id != bundle.current_media_group_id
            and (bundle.attachment_paths or bundle.text_parts)
        ):
            old_bundle = _pop_bundle_locked(route)
            if old_bundle is not None:
                _spawn_background(_send_bundle(route, old_bundle))
            bundle = _get_or_create_bundle(route)

        if caption and caption not in bundle.seen_captions:
            bundle.text_parts.append(caption)
            bundle.seen_captions.add(caption)
        bundle.attachment_paths.append(path)
        # Only update on a grouped attachment: a non-grouped item joining the
        # bundle must not erase the "last group" memory, or the next group's
        # boundary check would silently merge it with the previous album.
        if media_group_id is not None:
            bundle.current_media_group_id = media_group_id

        if len(bundle.attachment_paths) >= config.aggregator_max_attachments:
            flush_now = True
            _cancel_handle(bundle)
        else:
            _schedule_flush(route, bundle)

    if flush_now:
        await _flush(route)


async def aggregator_offer_photo(
    route: Route,
    path: Path,
    caption: str | None,
    media_group_id: str | None,
) -> None:
    await _offer_attachment(route, path, caption, media_group_id)


async def aggregator_offer_document(
    route: Route,
    path: Path,
    caption: str | None,
    media_group_id: str | None,
) -> None:
    await _offer_attachment(route, path, caption, media_group_id)


async def aggregator_offer_attachment(
    route: Route,
    path: Path,
    caption: str | None,
    media_group_id: str | None,
) -> None:
    """Kind-agnostic offer for the unbound-topic flush sites.

    Photo and document handlers stash a ``PendingAttachment`` without a
    ``kind`` field; the bind-flush replays them through this wrapper since
    both routes converge on the same internal coroutine.
    """
    await _offer_attachment(route, path, caption, media_group_id)


async def aggregator_replay_payload(
    route: Route,
    *,
    text: str | None,
    attachments: Sequence[AggregatorReplayAttachment],
) -> bool:
    """Synchronously send a pending first-turn payload and aggregate status.

    This is the safe replay path for unbound-topic payloads held while the user
    chooses a directory/window/session. It intentionally bypasses the offer API:
    offers can force-flush on media-group boundaries or attachment-count caps,
    and those intermediate sends are backgrounded/ignored in the normal live
    aggregation path. Pending replay must instead await every send it causes so
    the UI never reports "First message sent" after an earlier split failed.

    The bundle construction mirrors ``_offer_attachment`` as closely as
    practical: pending text is included once at the front, captions are deduped
    per bundle, media-group boundaries split bundles, and the max-attachment cap
    still prevents unbounded bundle growth. Every split is sent via
    ``_send_bundle`` sequentially and contributes to the returned boolean.
    """
    delivered = True
    bundle = _PendingBundle()
    max_attachments = max(1, config.aggregator_max_attachments)

    if text:
        bundle.text_parts.append(text)

    async def send_current_bundle() -> None:
        nonlocal bundle, delivered
        if not (bundle.text_parts or bundle.attachment_paths):
            return
        delivered = bool(await _send_bundle(route, bundle)) and delivered
        bundle = _PendingBundle()

    for attachment in attachments:
        media_group_id = attachment.media_group_id
        if (
            media_group_id is not None
            and bundle.current_media_group_id is not None
            and media_group_id != bundle.current_media_group_id
            and (bundle.attachment_paths or bundle.text_parts)
        ):
            await send_current_bundle()

        if attachment.caption and attachment.caption not in bundle.seen_captions:
            bundle.text_parts.append(attachment.caption)
            bundle.seen_captions.add(attachment.caption)
        bundle.attachment_paths.append(attachment.path)
        if media_group_id is not None:
            bundle.current_media_group_id = media_group_id

        if len(bundle.attachment_paths) >= max_attachments:
            await send_current_bundle()

    await send_current_bundle()
    return delivered


async def aggregator_flush_route(route: Route) -> bool:
    """Force-flush a route's bundle. Used by slash-command forwarders.

    Delegates straight to ``_flush`` so the pop and the cancel happen under
    a single lock acquisition — no reentrancy hazard, no race window where a
    concurrent offer can resurrect the bundle between cancel and send. Returns
    ``False`` when the forced send was attempted but delivery failed.
    """
    return await _flush(route)


def aggregator_clear_route(route: Route) -> None:
    """Drop a route's bundle without sending. Called by ``teardown_route``.

    Pending flush handle is cancelled in-place so a debounce that hadn't yet
    fired can't try to send into a torn-down window.
    """
    bundle = _route_pending.pop(route, None)
    if bundle is not None:
        _cancel_handle(bundle)
    _route_locks.pop(route, None)


def has_pending(route: Route) -> bool:
    """Test helper / introspection: is there a bundle waiting to flush?"""
    bundle = _route_pending.get(route)
    return bundle is not None and (
        bool(bundle.text_parts) or bool(bundle.attachment_paths)
    )
